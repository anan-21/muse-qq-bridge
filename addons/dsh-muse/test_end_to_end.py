import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

import broker
from muse_client import Client


@unittest.skipUnless(shutil.which("node"), "Node.js required for cross-runtime contract")
class EndToEndTests(unittest.TestCase):
    def test_node_router_python_broker_muse_reply_and_automatic_return(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []
            store = broker.Store(root / "broker", "12345678", lambda user, text: sent.append((user, text)) or "fixture-7")
            server = broker.make_server("127.0.0.1", 0, store, "d" * 40, "m" * 40)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            dsh_token = root / "dsh-token"
            muse_token = root / "muse-token"
            dsh_token.write_text("d" * 40)
            muse_token.write_text("m" * 40)
            dsh_token.chmod(0o600)
            muse_token.chmod(0o600)
            base = "http://127.0.0.1:" + str(server.server_port)
            config = {"enabled": True, "owner": "12345678", "baseUrl": base,
                      "tokenFile": str(dsh_token), "stateDir": str(root / "router"),
                      "consentExport": True, "normalTurnsToReturn": 2}
            module = (Path(__file__).parent / "client" / "router.mjs").resolve().as_uri()

            def route(text, number):
                code = f"""
import {{createRouter}} from {json.dumps(module)};
const router=createRouter({{config:{json.dumps(config)},snapshot:async()=>({{
 history:[{{role:'user',text:'[ordinary prior turn]'}},{{role:'assistant',text:'[ordinary prior reply]'}}],
 memories:[{{id:'m1',scope:'chat:private:12345678',text:'[authorized memory]',kind:'memory'}}]
}})}});
try {{console.log(JSON.stringify(await router.intercept({{
 chat:{{key:'private:12345678',id:'12345678',type:'private'}},sender:{{userId:'12345678'}},
 event:{{message_id:{number},user_id:12345678,message_type:'private',time:123}},text:{json.dumps(text)}
}})))}} finally {{await router.dispose()}}
"""
                result = subprocess.run(["node", "--input-type=module", "-e", code],
                                        capture_output=True, text=True, timeout=15, check=True)
                return json.loads(result.stdout)
            try:
                self.assertFalse(route("今天的天气", 1)["handled"])
                self.assertEqual(store.status()["pending"], 0)
                self.assertEqual(route("NSFW [synthetic topic label]", 2)["route"], "muse")
                self.assertEqual(route("帮我写代码", 3)["reason"], "return_deferred_pending")
                self.assertEqual(store.status()["pending"], 2)
                muse = Client({"owner": "12345678", "baseUrl": base,
                               "tokenFile": str(muse_token), "stateDir": str(root / "muse")})
                wake = muse.poll(now=1000)
                messages = wake["data"]["messages"]
                self.assertEqual(len(messages), 2)
                for row in messages:
                    snapshot = json.loads(Path(row["context_file"]).read_text())["snapshot"]
                    self.assertEqual(snapshot["memories"][0]["text"], "[authorized memory]")
                ids = [row["id"] for row in messages]
                muse.reply(ids, "[Muse synthetic reply]")
                self.assertEqual(len(sent), 1)
                self.assertEqual(store.status()["pending"], 0)
                returned = route("帮我写代码", 4)
                self.assertFalse(returned["handled"])
                self.assertEqual(returned["route"], "dsh")
                self.assertEqual(returned["reason"], "ordinary_topic_return")
                # Duplicate delivery across process restart never becomes a second job/send.
                self.assertTrue(route("NSFW [synthetic topic label]", 2)["handled"])
                self.assertEqual(store.status()["pending"], 0)
                self.assertEqual(len(sent), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                store.close()


if __name__ == "__main__":
    unittest.main()
