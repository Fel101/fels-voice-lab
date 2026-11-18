"""The loopback MCP connection requires its token and keeps per-client voice settings."""

import http.client
import json
from pathlib import Path
import select
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tests import test_openai_route
from tests.test_slate_bundle import sample_bundle
from tools import mcp_http, mcp_server


class McpHttpTests(test_openai_route.RouteCase):
    def test_http_mcp_requires_token_and_keeps_separate_voice_defaults(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            lab = mcp_server.Lab(url=self.service.base, can_start=False)
            token = "local-test-token-" + "x" * 34
            host = mcp_http.Host(0, lab, root, root, token)
            cls = threading.Thread(target=host.serve_forever, daemon=True)
            cls.start()
            endpoint = f"http://127.0.0.1:{host.server_port}/mcp"

            def rpc(method, params, ident=None, auth=token, origin=None):
                headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
                if ident:
                    headers["Mcp-Session-Id"] = ident
                if auth:
                    headers["Authorization"] = "Bearer " + auth
                if origin:
                    headers["Origin"] = origin
                doc = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(endpoint, data=json.dumps(doc).encode(), headers=headers), timeout=15
                    ) as reply:
                        return reply.status, reply.headers, json.loads(reply.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.headers, None

            try:
                self.assertEqual(rpc("initialize", {}, auth="")[0], 401)
                self.assertEqual(rpc("initialize", {}, origin="https://example.com")[0], 403)
                sessions = [rpc("initialize", {"protocolVersion": "2025-11-25"})[1]["Mcp-Session-Id"] for _ in range(2)]
                for ident, voice in zip(sessions, ["af_heart", "ef_dora"]):
                    _, _, result = rpc("tools/call", {"name": "set_voice", "arguments": {"voice": voice}}, ident)
                    self.assertEqual(result["result"]["structuredContent"]["voice"], voice)
                for ident, voice in zip(sessions, ["af_heart", "ef_dora"]):
                    _, _, result = rpc("tools/call", {"name": "speak", "arguments": {"text": "Hi", "play": False}}, ident)
                    self.assertEqual(result["result"]["structuredContent"]["voice"], voice, result)
                _, _, result = rpc("tools/call", {"name": "transcribe", "arguments": {"file": "../outside.wav"}}, sessions[0])
                self.assertTrue(result["result"]["isError"], result)
                self.assertIn("outside the selected workspace", result["result"]["content"][0]["text"])
            finally:
                for ident in list(host.sessions):
                    host.end(ident)
                host.shutdown()
                host.server_close()
                cls.join(timeout=3)

    def test_mcp_slate_handoff_requires_permission_and_leaves_candidate_unapproved(self):
        self.service.add_voice(character="Handoff")
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "review.zip").write_bytes(sample_bundle())
            mcp = mcp_server
            lab = mcp.Lab(url=self.service.base, can_start=False)
            tools = mcp.Tools(lab, mcp.Player(), workspace=root, clips=root)
            call = mcp.Call(1, "preview_voice_candidate")
            _, preview = tools.preview_voice_candidate(call, {"file": "review.zip"})
            engine = "qwen3-tts-1.7b-base"
            self.assertIn(engine, preview["engines"])
            args = {
                "file": "review.zip",
                "character": "Handoff",
                "engine_id": engine,
                "sidecar_sha256": preview["clip"]["sidecar_sha256"],
                "permission": False,
            }
            with self.assertRaisesRegex(mcp.ToolError, "confirm its permitted use"):
                tools.import_voice_candidate(call, args)
            with self.assertRaisesRegex(mcp.LabRefused, "changed after preview"):
                tools.import_voice_candidate(call, {**args, "permission": True, "sidecar_sha256": "a" * 64})
            _, added = tools.import_voice_candidate(call, {**args, "permission": True})
            identity = self.service.cast.get_identity(added["identity_id"])
            self.assertEqual(identity["status"], "candidate")
            self.assertIsNone(identity["approved_reference_id"])


class TokenFileTests(unittest.TestCase):
    def test_a_token_file_remains_the_same_across_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "token"
            first = mcp_http._connection_token(path)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(mcp_http._connection_token(path), first)


class RefusedBodyTests(unittest.TestCase):
    def test_mcp_refuses_requests_without_resetting_the_connection(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            token = "local-test-token-" + "x" * 34
            with mcp_http.Host(0, None, root, root, token) as host:
                thread = threading.Thread(target=host.serve_forever, daemon=True)
                thread.start()
                client = http.client.HTTPConnection("127.0.0.1", host.server_port, timeout=5)
                body = json.dumps({"text": "a" * 60_000}).encode()
                try:
                    for _ in range(30):
                        client.request("POST", "/mcp", body, {"Content-Type": "application/json"})
                        with client.getresponse() as reply:
                            self.assertEqual(reply.status, 401)
                            self.assertIn("Bearer", reply.headers["WWW-Authenticate"])
                            self.assertEqual(reply.read(), b"")

                    # A client may still be transmitting when the server decides to refuse it.
                    client.putrequest("POST", "/mcp")
                    client.putheader("Content-Type", "application/json")
                    client.putheader("Content-Length", str(len(body)))
                    client.endheaders()
                    self.assertFalse(select.select([client.sock], [], [], 0.25)[0], "reply arrived before the body")
                    client.send(body)
                    with client.getresponse() as reply:
                        self.assertEqual(reply.status, 401)
                        self.assertEqual(reply.read(), b"")

                    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
                    oversized = b"a" * (mcp_http.MAX_BODY + 4096)
                    for _ in range(20):
                        client.request("POST", "/mcp", oversized, headers)
                        with client.getresponse() as reply:
                            self.assertEqual(reply.status, 413)
                            self.assertEqual(reply.headers["Connection"], "close")
                            self.assertEqual(reply.read(), b"")
                finally:
                    client.close()
                    host.shutdown()
                    thread.join(timeout=3)
