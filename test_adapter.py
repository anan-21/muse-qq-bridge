import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock
from http.server import ThreadingHTTPServer

import aiohttp
from aiohttp import web
from adapter import Adapter, APIError, State, StrictHttp, Client


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir(mode=0o700)
        self.runtime = {'allowed_qq': '99887766', 'onebot_token': 'dummy-onebot-key',
                        'report_secret': 'dummy-report-key'}
        self.creds = {'appid': '123456', 'secret': 'dummy-secret', 'is_sandbox': True}
        self.a = Adapter(self.root, self.runtime, self.creds)
        self.a.session = aiohttp.ClientSession(trust_env=False)
        self.a.state.bind('owner-openid')
        self.a.ready = True
        self.a.api = AsyncMock()
        self.a.api.post_c2c_message.return_value = {'id': 'official-receipt-1'}
        self.runners = []

    async def asyncTearDown(self):
        await self.a.session.close()
        for runner in self.runners:
            await runner.cleanup()
        self.a.state.db.close()
        self.temp.cleanup()

    async def server(self, app):
        runner = web.AppRunner(app, access_log=None)
        self.runners.append(runner)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        return 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])

    def incoming_context(self):
        row = self.a.state.receive('platform-message-1', 'owner-openid', 'hello')
        self.a.state.forwarded(row['id'])

    async def test_outbound_confirmed_receipt_and_exact_c2c_context(self):
        self.incoming_context()
        r = await self.a.send({'user_id': 99887766, 'message': 'answer'})
        self.assertEqual(r, {'status': 'ok', 'retcode': 0, 'data': {'message_id': 'official-receipt-1'}})
        self.a.api.post_c2c_message.assert_awaited_once_with(openid='owner-openid',
            msg_type=0, msg_id='platform-message-1', msg_seq=1, content='answer')

    async def test_repeat_send_does_not_send_twice_across_restart(self):
        self.incoming_context()
        data = {'user_id': 99887766, 'message': 'answer'}
        r = await self.a.send(data)
        self.a.state.db.close()
        self.a.state = State(self.root / 'state' / 'adapter.db')
        self.assertEqual(await self.a.send(data), r)
        self.a.api.post_c2c_message.assert_awaited_once()

    async def test_unknown_outcome_never_retried(self):
        self.incoming_context()
        self.a.api.post_c2c_message.side_effect = asyncio.TimeoutError()
        data = {'user_id': 99887766, 'message': 'answer'}
        r = await self.a.send(data)
        self.assertNotEqual(r['retcode'], 0)
        self.assertEqual(await self.a.send(data), r)
        self.a.api.post_c2c_message.assert_awaited_once()

    async def test_reserved_before_crash_is_not_reissued(self):
        self.incoming_context()
        self.a.state.reserve('platform-message-1', 'answer')
        r = await self.a.send({'user_id': 99887766, 'message': 'answer'})
        self.assertEqual(r['retcode'], 1504)
        self.a.api.post_c2c_message.assert_not_called()

    async def test_real_platform_error_code_and_secret_redaction(self):
        self.incoming_context()
        self.a.api.post_c2c_message.side_effect = APIError(403, 304023, 'rejected dummy-secret')
        r = await self.a.send({'user_id': 99887766, 'message': 'answer'})
        self.assertEqual(r['retcode'], 304023)
        self.assertEqual(r['data']['platform_http_status'], 403)
        self.assertNotIn('dummy-secret', json.dumps(r))

    async def test_business_failure_with_http_200_not_success(self):
        self.incoming_context()
        self.a.api.post_c2c_message.return_value = {'code': 12345, 'message': 'denied'}
        self.assertEqual((await self.a.send({'user_id': 99887766, 'message': 'x'}))['retcode'], 12345)

    async def test_no_receipt_is_not_success(self):
        self.incoming_context()
        self.a.api.post_c2c_message.return_value = {'ok': True}
        self.assertNotEqual((await self.a.send({'user_id': 99887766, 'message': 'x'}))['retcode'], 0)

    async def test_expired_context_prevents_send(self):
        self.incoming_context()
        self.a.state.db.execute('UPDATE incoming SET received=?', (time.time() - 301,))
        self.a.state.db.commit()
        self.assertEqual((await self.a.send({'user_id': 99887766, 'message': 'x'}))['retcode'], 1408)
        self.a.api.post_c2c_message.assert_not_called()

    async def test_unauthorized_recipient_and_sender_rejected(self):
        self.assertEqual((await self.a.send({'user_id': 123, 'message': 'x'}))['retcode'], 1403)
        self.assertFalse(await self.a.incoming('other-person', 'x', 'hello'))
        self.assertEqual(self.a.state.drops(), 1)
        self.assertFalse(self.a.state.pending())

    async def test_attachments_not_forwarded(self):
        self.assertFalse(await self.a.incoming('owner-openid', 'x', 'hello', True))
        self.assertEqual(self.a.state.drops(), 1)

    async def test_no_context_or_gateway_returns_failure(self):
        self.assertEqual((await self.a.send({'user_id': 99887766, 'message': 'x'}))['retcode'], 1503)
        self.incoming_context(); self.a.ready = False
        self.assertEqual((await self.a.send({'user_id': 99887766, 'message': 'x'}))['retcode'], 1503)

    async def test_owner_binding_is_explicit_expiring_and_persistent(self):
        self.a.state.db.execute("DELETE FROM meta WHERE key='owner'"); self.a.state.db.commit()
        self.assertFalse(await self.a.incoming('someone', 'id-1', 'hello'))
        phrase = 'unit binding challenge'
        p = self.root / 'config' / 'binding.json'
        p.write_text(json.dumps({'sha256': hashlib.sha256(phrase.encode()).hexdigest(), 'expires_at': time.time() + 20}))
        p.chmod(0o600)
        self.assertTrue(await self.a.incoming('verified-owner', 'id-2', phrase))
        self.assertEqual(self.a.state.owner(), 'verified-owner')
        self.assertFalse(await self.a.incoming('someone-else', 'id-3', phrase))
        self.assertFalse(self.a.state.pending())

    async def test_full_wire_adapter_to_actual_bridge_and_bridge_to_adapter(self):
        source = os.environ.get('BRIDGE_SOURCE', str(Path(__file__).with_name('mac_bridge.py')))
        spec = importlib.util.spec_from_file_location('isolated_test_bridge', source)
        bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
        bridge.TOKEN = 'dummy-bridge-key'; bridge.ALLOWED = '99887766'
        bridge.REPORT_SECRET = 'dummy-report-key'; bridge.ONEBOT_TOKEN = 'dummy-onebot-key'
        bridge.STORE = str(self.root / 'isolated-inbox.jsonl')
        bridge.ONEBOT = await self.server(self.a.app())
        server = ThreadingHTTPServer(('127.0.0.1', 0), bridge.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.a.bridge_url = 'http://127.0.0.1:' + str(server.server_port)
        try:
            self.assertTrue(await self.a.incoming('owner-openid', 'platform-id', 'simulated inbound'))
            self.assertTrue(await self.a.incoming('owner-openid', 'platform-id', 'simulated inbound'))
            async with self.a.session.get(self.a.bridge_url + '/health') as r:
                self.assertEqual((await r.json())['pending'], 1)
            async with self.a.session.post(self.a.bridge_url + '/send',
                    headers={'X-Bridge-Token': 'dummy-bridge-key'},
                    json={'user_id': '99887766', 'text': 'simulated reply'}) as r:
                body = await r.json()
                self.assertEqual(r.status, 200)
                self.assertTrue(body['ok'])
                self.assertEqual(body['message_id'], 'official-receipt-1')
            self.a.api.post_c2c_message.assert_awaited_once()
            async with self.a.session.get(self.a.bridge_url + '/health') as r:
                self.assertEqual((await r.json())['pending'], 1)  # send never acks
        finally:
            await asyncio.to_thread(server.shutdown); server.server_close(); thread.join()

    async def test_http_bearer_required(self):
        url = await self.server(self.a.app())
        async with self.a.session.post(url + '/send_private_msg', json={'user_id': 99887766, 'message': 'x'}) as r:
            self.assertEqual(r.status, 401)
        async with self.a.session.get(url + '/health') as r:
            self.assertFalse((await r.json())['dropped'])

    async def test_sandbox_and_loopback_are_mandatory(self):
        with self.assertRaises(ValueError):
            Adapter(self.root, self.runtime, {**self.creds, 'is_sandbox': False})
        with self.assertRaises(ValueError):
            Adapter(self.root, self.runtime, self.creds, bridge_url='https://example.com')

    async def test_official_sdk_event_dispatch_and_strict_transport(self):
        from botpy.message import C2CMessage
        client = Client(self.a)
        try:
            self.assertIsInstance(client.api._http, StrictHttp)
            self.assertTrue(client.http.is_sandbox)
            event = C2CMessage(client.api, 'evt-1', {
                'id': 'real-sdk-shape', 'author': {'user_openid': 'owner-openid'},
                'content': 'test from SDK event', 'attachments': []})
            self.a.forward = AsyncMock(return_value=True)
            await client.on_c2c_message_create(event)
            self.a.forward.assert_awaited_once()
            self.assertEqual(self.a.state.pending()[0]['id'], 'real-sdk-shape')
        finally:
            await client.close()

    async def test_strict_http_does_not_retry_connection_reset(self):
        class ResetSession:
            closed = False
            calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                raise ConnectionResetError('simulated reset after send')
        from botpy.http import Route
        http = StrictHttp(timeout=7, is_sandbox=True)
        http._session = ResetSession()
        http.check_session = AsyncMock()
        http._headers = {}
        with self.assertRaises(APIError):
            await http.request(Route('POST', '/v2/users/owner/messages'), json={'content': 'unit'})
        self.assertEqual(http._session.calls, 1)
        http._session = None


if __name__ == '__main__':
    unittest.main()
