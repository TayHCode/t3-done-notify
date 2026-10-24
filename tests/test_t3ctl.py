import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response


CTL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "t3ctl")
loader = importlib.machinery.SourceFileLoader("t3ctl_under_test", CTL_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
t3ctl = importlib.util.module_from_spec(spec)
loader.exec_module(t3ctl)


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        return json.dumps(next(self.frames))


class RpcTests(unittest.TestCase):
    def test_request_frame_ping_pong_chunk_ack_and_interrupt(self):
        rpc = t3ctl.Rpc()
        rpc.ws = FakeWebSocket([
            {"_tag": "Ping"},
            {"_tag": "Chunk", "requestId": "1", "values": [{"snapshot": {"threads": []}}]},
        ])
        result = asyncio.run(rpc.call("orchestration.subscribeShell", {}))
        self.assertEqual(result, {"snapshot": {"threads": []}})
        self.assertEqual(rpc.ws.sent, [
            {"_tag": "Request", "id": "1", "tag": "orchestration.subscribeShell",
             "payload": {}, "headers": []},
            {"_tag": "Pong"},
            {"_tag": "Ack", "requestId": "1"},
            {"_tag": "Interrupt", "requestId": "1"},
        ])

    def test_exit_success(self):
        rpc = t3ctl.Rpc()
        rpc.ws = FakeWebSocket([{
            "_tag": "Exit", "requestId": "1",
            "exit": {"_tag": "Success", "value": {"messages": []}},
        }])
        result = asyncio.run(rpc.call("orchestration.getThreadProjection", {"threadId": "thread:test"}))
        self.assertEqual(result, {"messages": []})

    def test_exit_failure_does_not_expose_cause(self):
        rpc = t3ctl.Rpc()
        rpc.ws = FakeWebSocket([{
            "_tag": "Exit", "requestId": "1",
            "exit": {"_tag": "Failure", "cause": {"message": "synthetic private response"}},
        }])
        with self.assertRaises(t3ctl.AdapterError) as raised:
            asyncio.run(rpc.call("orchestration.getThreadProjection", {"threadId": "thread:test"}))
        self.assertEqual(str(raised.exception), "T3 request failed")
        self.assertNotIn("synthetic private response", str(raised.exception))


class ConfigTests(unittest.TestCase):
    def test_token_file_resolution(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/test-config"}, clear=True):
            self.assertEqual(t3ctl.token_file_path(), "/tmp/test-config/t3-done/t3-token")
        with mock.patch.dict(os.environ, {"T3_DONE_CONFIG_DIR": "/tmp/notifier-config"}, clear=True):
            self.assertEqual(t3ctl.token_file_path(), "/tmp/notifier-config/t3-token")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/test-config",
                                         "T3_TOKEN_FILE": "~/custom-token"}, clear=True):
            self.assertEqual(t3ctl.token_file_path(), os.path.expanduser("~/custom-token"))

    def test_default_token_file_fallback(self):
        def fake_expanduser(path):
            return path.replace("~", "/srv/test-home", 1)

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("os.path.expanduser", side_effect=fake_expanduser):
            self.assertEqual(t3ctl.token_file_path(), "/srv/test-home/.config/t3-done/t3-token")


class HttpTests(unittest.TestCase):
    def run_server(self, handler_type):
        server = HTTPServer(("127.0.0.1", 0), handler_type)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        return server, worker

    def with_token_and_url(self, token_path, server):
        return mock.patch.dict(os.environ, {
            "T3_URL": f"http://127.0.0.1:{server.server_port}",
            "T3_TOKEN_FILE": token_path,
        })

    def test_ticket_auth_uses_existing_token_file(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["authorization"] = self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ticket":"synthetic-ticket"}')

            def log_message(self, *_):
                pass

        server, worker = self.run_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as temp:
                token_path = os.path.join(temp, "token")
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write("unit-test-token\n")
                with self.with_token_and_url(token_path, server):
                    self.assertEqual(t3ctl.websocket_ticket(), "synthetic-ticket")
                self.assertEqual(seen["path"], "/api/auth/websocket-ticket")
                self.assertEqual(seen["authorization"], "Bearer unit-test-token")
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_auth_failure_suppresses_body_url_and_token(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"unit-test-token private-response-body")

            def log_message(self, *_):
                pass

        server, worker = self.run_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as temp:
                token_path = os.path.join(temp, "token")
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write("unit-test-token")
                with self.with_token_and_url(token_path, server):
                    with self.assertRaises(t3ctl.AdapterError) as raised:
                        t3ctl.websocket_ticket()
                error_text = str(raised.exception)
                self.assertEqual(error_text, "T3 authentication failed")
                self.assertNotIn("unit-test-token", error_text)
                self.assertNotIn("private-response-body", error_text)
                self.assertNotIn("127.0.0.1", error_text)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_redirect_is_refused_and_not_followed(self):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirect-target")
                self.end_headers()

            def log_message(self, *_):
                pass

        server, worker = self.run_server(Handler)
        try:
            with tempfile.TemporaryDirectory() as temp:
                token_path = os.path.join(temp, "token")
                with open(token_path, "w", encoding="utf-8") as token_file:
                    token_file.write("unit-test-token")
                with self.with_token_and_url(token_path, server):
                    with self.assertRaises(t3ctl.AdapterError) as raised:
                        t3ctl.websocket_ticket()
                self.assertEqual(str(raised.exception), "T3 ticket redirect refused")
                self.assertEqual(hits, ["/api/auth/websocket-ticket"])
                self.assertNotIn("127.0.0.1", str(raised.exception))
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_local_websocket_integration_and_handshake_redirect_refusal(self):
        async def run_snapshot():
            seen = {"paths": [], "rpc": None}

            async def process_request(connection, request):
                seen["paths"].append(request.path)
                return None

            async def handler(websocket):
                request = json.loads(await websocket.recv())
                seen["rpc"] = request
                await websocket.send(json.dumps({"_tag": "Ping"}))
                await websocket.send(json.dumps({"_tag": "Chunk", "requestId": request["id"],
                                                  "values": [{"snapshot": {"threads": [{"id": "thread:local"}]}}]}))
                for _ in range(3):
                    await websocket.recv()

            async with serve(handler, "127.0.0.1", 0, process_request=process_request, family=socket.AF_INET) as server:
                port = server.sockets[0].getsockname()[1]
                with mock.patch.dict(os.environ, {"T3_URL": f"http://127.0.0.1:{port}"}), \
                        mock.patch.object(t3ctl, "websocket_ticket", return_value="synthetic-ticket"):
                    result = await t3ctl.shell_snapshot()
            return result, seen

        snapshot, seen = asyncio.run(run_snapshot())
        self.assertEqual(snapshot["threads"], [{"id": "thread:local"}])
        self.assertEqual(len(seen["paths"]), 1)
        query = dict(item.split("=", 1) for item in seen["paths"][0].split("?", 1)[1].split("&"))
        self.assertEqual(query["wsTicket"], "synthetic-ticket")
        self.assertEqual(query["orchestrationProtocol"], "2")
        self.assertEqual(seen["rpc"]["tag"], "orchestration.subscribeShell")

        async def run_redirect():
            seen_paths = []

            async def process_request(connection, request):
                seen_paths.append(request.path)
                if request.path.startswith("/ws?"):
                    return Response(302, "Found", Headers([("Location", "/redirect-target"),
                                                            ("Content-Length", "0")]), b"")
                return Response(404, "Not Found", Headers([("Content-Length", "0")]), b"")

            async def handler(websocket):
                raise AssertionError("redirect response must prevent websocket upgrade")

            async with serve(handler, "127.0.0.1", 0, process_request=process_request, family=socket.AF_INET) as server:
                port = server.sockets[0].getsockname()[1]
                with mock.patch.dict(os.environ, {"T3_URL": f"http://127.0.0.1:{port}"}), \
                        mock.patch.object(t3ctl, "websocket_ticket", return_value="synthetic-ticket"):
                    try:
                        await t3ctl.shell_snapshot()
                    except Exception as exc:
                        return type(exc).__name__, seen_paths
                    self.fail("websocket handshake redirect unexpectedly succeeded")

        failure_type, seen_paths = asyncio.run(run_redirect())
        self.assertEqual(failure_type, "InvalidStatus")
        self.assertEqual(len(seen_paths), 1)
        self.assertTrue(seen_paths[0].startswith("/ws?"))


class CommandTests(unittest.TestCase):
    def test_lanes_includes_all_undeleted_rows_without_arbitrary_limit(self):
        rows = [{"id": f"thread-{index}", "archivedAt": "2026-01-01"} for index in range(100_005)]
        rows.extend([{"id": "deleted", "deletedAt": "2026-01-01"},
                     {"id": "active"}])
        async def fake_snapshot():
            return {"threads": rows}

        with mock.patch.object(t3ctl, "shell_snapshot", fake_snapshot):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(t3ctl.main(["lanes", "--json", "--include-archived"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(len(result), 100_006)
        self.assertEqual(result[-1]["id"], "active")
        self.assertNotIn("deleted", {row["id"] for row in result})

    def test_lanes_excludes_archived_by_default(self):
        async def fake_snapshot():
            return {"threads": [{"id": "old", "archivedAt": "2026-01-01"}, {"id": "new"}]}

        with mock.patch.object(t3ctl, "shell_snapshot", fake_snapshot):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(t3ctl.main(["lanes", "--json"]), 0)
        self.assertEqual([row["id"] for row in json.loads(output.getvalue())], ["new"])

    def test_thread_uses_exact_id_for_projection(self):
        seen = []

        async def fake_projection(thread_id):
            seen.append(thread_id)
            return {"threadId": thread_id, "messages": []}

        with mock.patch.object(t3ctl, "thread_projection", fake_projection):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(t3ctl.main(["thread", "thread:exact-id", "--json"]), 0)
        self.assertEqual(seen, ["thread:exact-id"])
        self.assertEqual(json.loads(output.getvalue())["threadId"], "thread:exact-id")

    def test_cli_suppresses_untrusted_exception_text(self):
        async def failed_snapshot():
            raise RuntimeError("https://example.invalid/?token=unit-test-token private-body")

        with mock.patch.object(t3ctl, "shell_snapshot", failed_snapshot):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(t3ctl.main(["lanes", "--json"]), 1)
        self.assertEqual(output.getvalue(), "t3ctl: request failed (RuntimeError)\n")
        self.assertNotIn("example.invalid", output.getvalue())
        self.assertNotIn("unit-test-token", output.getvalue())
        self.assertNotIn("private-body", output.getvalue())


if __name__ == "__main__":
    unittest.main()
