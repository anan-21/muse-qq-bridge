#!/usr/bin/env python3
"""Private, sandbox-only botpy adapter. No QQ account login, model, or auto-replies."""
import asyncio
import datetime
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import sqlite3
import ssl
import time
import urllib.parse

import aiohttp
from aiohttp import web
import botpy
from botpy.api import BotAPI
from botpy.http import BotHttp, Route
from botpy.robot import Token

MAX_BODY = 65536


def audit(direction, code):
    # SDK debug output can include Authorization headers; use only this audit sink.
    print(json.dumps({'time': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'direction': direction, 'result_code': code}), flush=True)


def private_json(path):
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o077:
        raise ValueError('configuration must be a private regular file')
    return json.loads(p.read_text())


class APIError(Exception):
    def __init__(self, status=None, code=None, message='unconfirmed API result'):
        super().__init__('official API request failed')
        self.status, self.code, self.message = status, code, message


class SafeToken(Token):
    async def update_access_token(self):
        # Current Tencent OAuth endpoint; credentials stay in server memory.
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.post('https://api.bot.qq.com/app/getAppAccessToken',
                    json={'appId': self.app_id, 'clientSecret': self.secret},
                    timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False) as r:
                raw = await r.content.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise APIError(r.status, message='OAuth response too large')
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise APIError(r.status, message='OAuth response is not JSON') from None
                if not isinstance(data, dict) or not isinstance(data.get('access_token'), str) or not data['access_token']:
                    raise APIError(r.status, data.get('code') if isinstance(data, dict) else None,
                                   data.get('message', 'OAuth did not issue a token') if isinstance(data, dict) else 'OAuth failed')
                if r.status != 200:
                    raise APIError(r.status, message='OAuth HTTP error')
                expires = int(data['expires_in'])
                if not 60 <= expires <= 7200:
                    raise APIError(r.status, message='invalid OAuth token lifetime')
                self.access_token = data['access_token']
                self.expires_in = int(time.time()) + expires - 30


class StrictHttp(BotHttp):
    """Keep botpy API/gateway, enforce TLS verification and one HTTP attempt per send."""
    async def check_session(self):
        await self._token.check_token()
        self._headers = {'Authorization': self._token.get_string(),
                         'X-Union-Appid': self._token.app_id}
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl.create_default_context()),
                trust_env=False)

    async def request(self, route, retry_time=0, **kwargs):
        if not self.is_sandbox:
            raise APIError(message='sandbox is required')
        await self.check_session()
        route.is_sandbox = True
        if urllib.parse.urlsplit(route.url).hostname != Route.SANDBOX_DOMAIN:
            raise APIError(message='unexpected sandbox API host')
        try:
            async with self._session.request(route.method, route.url,
                    headers=self._headers, timeout=aiohttp.ClientTimeout(total=7),
                    allow_redirects=False, **kwargs) as r:
                raw = await r.content.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise APIError(r.status, message='API response too large')
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise APIError(r.status, message='API response is not JSON') from None
                code = body.get('code') if isinstance(body, dict) else None
                if r.status not in (200, 201, 202, 204) or code not in (None, 0):
                    message = body.get('message', 'official API rejected request') if isinstance(body, dict) else 'unexpected API response'
                    raise APIError(r.status, code, str(message))
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            raise APIError(message='network outcome unknown; no automatic retry') from None


class State:
    def __init__(self, path):
        p = Path(path)
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p))
        os.chmod(p, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS incoming(
            id TEXT PRIMARY KEY, openid TEXT NOT NULL, bridge_id TEXT NOT NULL,
            received REAL NOT NULL, text TEXT NOT NULL, forwarded INTEGER DEFAULT 0,
            seq INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS outgoing(
            key TEXT PRIMARY KEY, state TEXT NOT NULL, reply TEXT NOT NULL);
        ''')
        self.db.commit()

    def owner(self):
        r = self.db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()
        return r[0] if r else None

    def bind(self, openid):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('owner',?)", (openid,))
        return self.owner() == openid

    def count_drop(self):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES('dropped','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")

    def drops(self):
        r = self.db.execute("SELECT value FROM meta WHERE key='dropped'").fetchone()
        return int(r[0]) if r else 0

    def receive(self, msgid, openid, content):
        bridge_id = 'qqc:' + hashlib.sha256(msgid.encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO incoming(id,openid,bridge_id,received,text) VALUES(?,?,?,?,?)',
                            (msgid, openid, bridge_id, time.time(), content))
        return self.db.execute('SELECT * FROM incoming WHERE id=?', (msgid,)).fetchone()

    def forwarded(self, msgid):
        with self.db:
            self.db.execute("UPDATE incoming SET forwarded=1,text='' WHERE id=?", (msgid,))

    def pending(self):
        return self.db.execute('SELECT * FROM incoming WHERE forwarded=0 ORDER BY received').fetchall()

    def latest(self):
        return self.db.execute('SELECT * FROM incoming WHERE forwarded=1 ORDER BY received DESC,rowid DESC LIMIT 1').fetchone()

    def reserve(self, msgid, content):
        key = hashlib.sha256((msgid + '\0' + content).encode()).hexdigest()
        previous = self.db.execute('SELECT reply FROM outgoing WHERE key=?', (key,)).fetchone()
        if previous:
            return key, None, json.loads(previous[0])
        unknown = {'status': 'failed', 'retcode': 1504, 'message': 'previous send outcome unknown; no automatic retry'}
        with self.db:
            self.db.execute('UPDATE incoming SET seq=seq+1 WHERE id=?', (msgid,))
            seq = self.db.execute('SELECT seq FROM incoming WHERE id=?', (msgid,)).fetchone()[0]
            self.db.execute('INSERT INTO outgoing VALUES(?,?,?)', (key, 'reserved', json.dumps(unknown)))
        return key, seq, None

    def finish(self, key, reply):
        with self.db:
            self.db.execute('UPDATE outgoing SET state=?,reply=? WHERE key=?',
                            ('confirmed' if reply['retcode'] == 0 else 'unconfirmed', json.dumps(reply), key))


class Adapter:
    def __init__(self, root, runtime, credentials, state=None, bridge_url='http://127.0.0.1:8765'):
        self.root, self.runtime, self.credentials = Path(root), runtime, credentials
        if credentials.get('is_sandbox') is not True:
            raise ValueError('sandbox must remain enabled')
        if (not isinstance(runtime.get('allowed_qq'), str) or
            not runtime['allowed_qq'].isascii() or not runtime['allowed_qq'].isdecimal() or
            int(runtime['allowed_qq']) <= 0 or runtime['allowed_qq'] == credentials.get('appid')):
            raise ValueError('owner compatibility ID must be positive, numeric and distinct from AppID')
        if any(not isinstance(runtime.get(k), str) or not runtime[k] for k in ('onebot_token', 'report_secret')):
            raise ValueError('internal authentication keys must not be empty')
        self.state = state or State(self.root / 'state' / 'adapter.db')
        url = urllib.parse.urlsplit(bridge_url)
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.path or url.query or url.fragment:
            raise ValueError('bridge must use loopback HTTP')
        self.bridge_url = bridge_url
        self.ready = False
        self.api = None
        self.send_lock, self.event_lock = asyncio.Lock(), asyncio.Lock()
        self.session = None

    def scrub(self, message):
        s = str(message)
        for secret in (self.credentials.get('secret'), self.runtime.get('onebot_token'), self.runtime.get('report_secret')):
            if secret:
                s = s.replace(secret, '[redacted]')
        return s[:500]

    async def incoming(self, openid, msgid, content, attachments=False):
        async with self.event_lock:
            if (not isinstance(openid, str) or not 1 <= len(openid) <= 128 or
                not isinstance(msgid, str) or not 1 <= len(msgid) <= 512 or
                not isinstance(content, str) or not content.strip() or attachments):
                self.state.count_drop(); audit('inbound', 1400); return False
            owner = self.state.owner()
            if owner is None:
                # Only a short-lived, operator-issued challenge can establish the owner.
                cfg = private_json(self.root / 'config' / 'binding.json') if (self.root / 'config' / 'binding.json').exists() else {}
                digest = hashlib.sha256(content.strip().encode()).hexdigest()
                if (time.time() < cfg.get('expires_at', 0) and cfg.get('sha256') and
                    hmac.compare_digest(digest, cfg['sha256'])):
                    self.state.bind(openid)
                    audit('binding', 0)
                    return True
                self.state.count_drop(); audit('inbound', 1403); return False
            if not hmac.compare_digest(owner, openid):
                self.state.count_drop(); audit('inbound', 1403); return False
            row = self.state.receive(msgid, openid, content)
            if row['forwarded']:
                return True
            return await self.forward(row)

    async def forward(self, row):
        event = {'post_type': 'message', 'message_type': 'private', 'sub_type': 'friend',
                 'self_id': self.credentials['appid'], 'user_id': int(self.runtime['allowed_qq']),
                 'message_id': row['bridge_id'], 'raw_message': row['text'],
                 'message': [{'type': 'text', 'data': {'text': row['text']}}]}
        raw = json.dumps(event, ensure_ascii=False).encode()
        signature = 'sha1=' + hmac.new(self.runtime['report_secret'].encode(), raw, hashlib.sha1).hexdigest()
        try:
            async with self.session.post(self.bridge_url + '/onebot/event', data=raw,
                    headers={'Content-Type': 'application/json', 'X-Signature': signature},
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as r:
                body = await r.json()
                if r.status != 200 or not isinstance(body, dict) or body.get('ok') is not True:
                    audit('inbound', 1502); return False
            self.state.forwarded(row['id']); audit('inbound', 0); return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            audit('inbound', 1504); return False

    async def send(self, data):
        if str(data.get('user_id')) != self.runtime['allowed_qq']:
            return {'status': 'failed', 'retcode': 1403, 'message': 'recipient not whitelisted'}
        text = data.get('message')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return {'status': 'failed', 'retcode': 1400, 'message': 'text must contain 1-2000 characters'}
        async with self.send_lock:
            row = self.state.latest()
            if not self.ready or self.api is None or row is None or row['openid'] != self.state.owner():
                return {'status': 'failed', 'retcode': 1503, 'message': 'gateway or bound reply context unavailable'}
            # botpy 1.2.1 documents five-minute passive reply contexts; be conservative.
            if time.time() - row['received'] > 290:
                return {'status': 'failed', 'retcode': 1408, 'message': 'passive reply context expired; new incoming message required'}
            key, seq, previous = self.state.reserve(row['id'], text)
            if previous is not None:
                return previous
            try:
                result = await self.api.post_c2c_message(openid=row['openid'], msg_type=0,
                    msg_id=row['id'], msg_seq=seq, content=text)
                code = result.get('code') if isinstance(result, dict) else None
                receipt = result.get('id') if isinstance(result, dict) else None
                if code not in (None, 0):
                    raise APIError(200, code, result.get('message', 'API rejected request'))
                if not isinstance(receipt, str) or not receipt or len(receipt) > 128:
                    raise APIError(message='API did not confirm a message receipt')
                reply = {'status': 'ok', 'retcode': 0, 'data': {'message_id': receipt}}
            except APIError as error:
                reply = {'status': 'failed', 'retcode': error.code if type(error.code) is int and error.code != 0 else 1504,
                         'message': self.scrub(error.message), 'data': {'platform_http_status': error.status}}
            except Exception:
                reply = {'status': 'failed', 'retcode': 1504, 'message': 'send outcome unknown; no automatic retry'}
            self.state.finish(key, reply); audit('outbound', reply['retcode']); return reply

    def app(self):
        application = web.Application(client_max_size=MAX_BODY)
        async def health(_):
            return web.json_response({'ok': True, 'sandbox': True, 'gateway_ready': self.ready,
                'owner_bound': bool(self.state.owner()), 'dropped': self.state.drops()})
        async def send(request):
            expected = 'Bearer ' + self.runtime['onebot_token']
            if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), expected.encode()):
                return web.json_response({'status': 'failed', 'retcode': 1401}, status=401)
            try:
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return web.json_response({'status': 'failed', 'retcode': 1400}, status=400)
            return web.json_response(await self.send(data))
        application.router.add_get('/health', health)
        application.router.add_post('/send_private_msg', send)
        return application

    async def retry_incoming(self):
        while True:
            await asyncio.sleep(5)
            async with self.event_lock:
                for row in self.state.pending():
                    if not await self.forward(row):
                        break


class Client(botpy.Client):
    def __init__(self, adapter):
        super().__init__(intents=botpy.Intents(public_messages=True), is_sandbox=True,
                         timeout=7, bot_log=False, ext_handlers=False)
        self.adapter = adapter
        self.http = StrictHttp(timeout=7, is_sandbox=True)
        self.api = BotAPI(http=self.http)
        adapter.api = self.api

    async def start(self, appid, secret, ret_coro=False):
        token = SafeToken(appid, secret)
        self.ret_coro = ret_coro
        await self._bot_login(token)
        return await self._bot_init(token)

    async def on_ready(self):
        self.adapter.ready = True; audit('gateway_ready', 0)

    async def on_resumed(self):
        self.adapter.ready = True; audit('gateway_resumed', 0)

    async def bot_connect(self, session):
        try:
            await super().bot_connect(session)
        finally:
            self.adapter.ready = False; audit('gateway_disconnected', 1503)

    async def on_error(self, *args, **kwargs):
        audit('gateway_event', 1504)

    async def on_c2c_message_create(self, message):
        await self.adapter.incoming(message.author.user_openid, message.id,
                                    message.content, bool(message.attachments))


async def run(root):
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    root = Path(root)
    (root / 'state').mkdir(mode=0o700, exist_ok=True)
    lock = open(root / 'state' / 'adapter.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    credentials = private_json(root / 'config' / 'credentials.json')
    runtime = private_json(root / 'config' / 'runtime.json')
    if not credentials.get('appid') or not credentials.get('secret'):
        raise ValueError('credentials incomplete')
    adapter = Adapter(root, runtime, credentials)
    async with aiohttp.ClientSession(trust_env=False) as session:
        adapter.session = session
        runner = web.AppRunner(adapter.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 3001).start()
        client = Client(adapter)
        retry = asyncio.create_task(adapter.retry_incoming())
        try:
            await client.start(appid=credentials['appid'], secret=credentials['secret'])
        except APIError as error:
            # Diagnostic file is local-only, sanitized, and does not contain tokens.
            (root / 'state' / 'startup-error.json').write_text(json.dumps({
                'platform_http_status': error.status, 'code': error.code,
                'message': adapter.scrub(error.message)}, ensure_ascii=False))
            raise
        finally:
            retry.cancel()
            await client.close()
            await runner.cleanup()


if __name__ == '__main__':
    import sys
    try:
        asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / 'data'))
    except KeyboardInterrupt:
        pass
    except Exception:
        audit('lifecycle_failed', 1503)
        raise SystemExit(1)
