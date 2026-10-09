import contextlib
import http.client
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from claude_handoff import proxy
from claude_handoff.proxy import LocalServer

TOKEN = "test-token-123"


class FakeUpstream:
    """Records what the proxy sends and answers like an Anthropic-compatible server."""

    def __init__(self, status=200, stream=False):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _record(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                       "body": json.loads(body) if body else None})

            def do_OPTIONS(self):
                self._record()
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                self._record()
                data = json.dumps({"data": [{"id": "m"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self._record()
                if status >= 400:
                    data = json.dumps({"error": {"message": "nope"},
                                       "metadata": {"raw": "messages.3.role: invalid"}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("request-id", "gen-123")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("request-id", "gen-123")
                self.send_header("x-request-id", "gen-123")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Set-Cookie", "session=abc")
                if stream:
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for event in (b"event: a\ndata: {}\n\n", b"event: b\ndata: {}\n\n"):
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    data = json.dumps({"id": "gen-123", "type": "message"}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.server = LocalServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class GarbageUpstream:
    """Answers anything with bytes that are not HTTP."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.socket.accept()
            except OSError:
                return
            with client:
                client.recv(65536)
                client.sendall(b"this is not http\r\n\r\n")

    def close(self):
        self.socket.close()


def request(port, method, path, payload=None, headers=None, token=TOKEN):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(payload).encode() if payload is not None else None
    sent = {"Content-Type": "application/json"}
    if token:
        sent["Authorization"] = f"Bearer {token}"
    sent.update(headers or {})
    connection.request(method, path, body=body, headers=sent)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


def post(port, path, payload, headers=None, token=TOKEN):
    return request(port, "POST", path, payload, headers, token)


class ProxyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = os.path.join(self.tmp.name, "proxy.log")

    def run_proxy(self, upstream, **kwargs):
        kwargs.setdefault("client_token", TOKEN)
        kwargs.setdefault("log_path", self.log)
        server = proxy.start(proxy.ProxyConfig(upstream=upstream.url, **kwargs))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(upstream.close)
        return server.server_address[1]

    def read_log(self):
        try:
            with open(self.log, encoding="utf-8") as file:
                return file.read()
        except FileNotFoundError:
            return ""


class ForwardingTest(ProxyTestCase):
    def test_messages_are_normalized_and_prefixed(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        response, data = post(port, "/v1/messages?beta=true", {
            "model": "m", "diagnostics": {"previous_message_id": "msg_1"},
            "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": "note"}],
        })
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["id"], "gen-123")
        sent = upstream.requests[0]
        self.assertEqual(sent["path"], "/api/v1/messages?beta=true")
        self.assertNotIn("diagnostics", sent["body"])
        self.assertEqual([m["role"] for m in sent["body"]["messages"]], ["user"])

    def test_count_tokens_and_models_are_served(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        response, _ = post(port, "/v1/messages/count_tokens", {"model": "m", "messages": [], "diagnostics": {}})
        self.assertEqual(response.status, 200)
        self.assertNotIn("diagnostics", upstream.requests[0]["body"])
        response, data = request(port, "GET", "/v1/models")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["data"][0]["id"], "m")

    def test_request_id_headers_are_not_passed_back(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        response, _ = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertIsNone(response.getheader("request-id"))
        self.assertIsNone(response.getheader("x-request-id"))

    def test_api_key_replaces_client_credentials(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, api_key="sk-upstream")
        post(port, "/v1/messages", {"model": "m", "messages": []}, {"x-api-key": "placeholder"})
        headers = {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer sk-upstream")
        self.assertNotIn("x-api-key", headers)

    def test_proxy_token_is_never_sent_upstream(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)  # no upstream key: a local server without auth
        post(port, "/v1/messages", {"model": "m", "messages": []})
        headers = {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn(TOKEN, json.dumps(upstream.requests[0]["headers"]))

    def test_routes_pin_openrouter_provider(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, routes={"a/model": "someprovider"})
        post(port, "/v1/messages", {"model": "a/model", "messages": []})
        post(port, "/v1/messages", {"model": "b/model", "messages": []})
        self.assertEqual(upstream.requests[0]["body"]["provider"], {"only": ["someprovider"], "allow_fallbacks": False})
        self.assertNotIn("provider", upstream.requests[1]["body"])

    def test_thinking_toggle(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, thinking_toggle=True)
        cases = [
            ({}, False),
            ({"thinking": {"type": "disabled"}}, False),
            ({"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}, False),
            ({"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}, True),
            ({"thinking": {"type": "enabled", "budget_tokens": 1024}}, True),
        ]
        for extra, _ in cases:
            post(port, "/v1/messages", {"model": "m", "messages": [], **extra})
        for sent, (_, expected) in zip(upstream.requests, cases):
            self.assertEqual(sent["body"]["chat_template_kwargs"]["enable_thinking"], expected)

    def test_streaming_passes_through(self):
        upstream = FakeUpstream(stream=True)
        port = self.run_proxy(upstream)
        response, data = post(port, "/v1/messages", {"model": "m", "stream": True, "messages": []})
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"event: a\ndata: {}\n\nevent: b\ndata: {}\n\n")

    def test_errors_are_forwarded_and_logged_with_metadata(self):
        upstream = FakeUpstream(status=400)
        port = self.run_proxy(upstream)
        response, data = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(data)["error"]["message"], "nope")
        self.assertIsNone(response.getheader("request-id"))
        log = self.read_log()
        self.assertIn("nope", log)
        self.assertIn("messages.3.role: invalid", log)

    def test_unreachable_upstream_gives_502(self):
        server = proxy.start(proxy.ProxyConfig(upstream="http://127.0.0.1:9", timeout=2, client_token=TOKEN,
                                               log_path=self.log))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        response, _ = post(server.server_address[1], "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 502)
        self.assertIn("upstream error", self.read_log())

    def test_non_http_upstream_gives_502_and_nothing_on_stderr(self):
        upstream = GarbageUpstream()
        port = self.run_proxy(upstream)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            response, _ = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 502)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("BadStatusLine", self.read_log())

    def test_handler_crash_goes_to_the_log_not_stderr(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        stderr = io.StringIO()
        with mock.patch.object(proxy, "rewrite", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises((http.client.HTTPException, ConnectionError)):
                post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("RuntimeError: boom", self.read_log())

    def test_bad_upstream_url(self):
        with self.assertRaises(ValueError):
            proxy.ProxyConfig(upstream="openrouter.ai")


class SecurityTest(ProxyTestCase):
    def test_missing_or_wrong_token_is_refused(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, api_key="sk-upstream")
        for token, headers in ((None, None), ("wrong", None), (None, {"x-api-key": "wrong"}),
                               (None, {"Authorization": TOKEN})):  # not a Bearer value
            response, _ = post(port, "/v1/messages", {"model": "m", "messages": []}, headers, token=token)
            self.assertEqual(response.status, 401)
        self.assertEqual(upstream.requests, [])

    def test_token_accepted_as_bearer_or_x_api_key(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        response, _ = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 200)
        response, _ = post(port, "/v1/messages", {"model": "m", "messages": []}, {"x-api-key": TOKEN}, token=None)
        self.assertEqual(response.status, 200)

    def test_browser_requests_are_refused(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, api_key="sk-upstream")
        response, _ = post(port, "/v1/messages", {"model": "m", "messages": []}, {"Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)
        response, _ = request(port, "OPTIONS", "/v1/messages", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}, token=None)
        self.assertNotIn(response.status, (200, 204))
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(upstream.requests, [])
        self.assertIn("evil.example", self.read_log())

    def test_dns_rebinding_host_is_refused(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, api_key="sk-upstream")
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1:1"):
            response, _ = post(port, "/v1/messages", {"model": "m", "messages": []}, {"Host": host})
            self.assertEqual(response.status, 403, host)
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            response, _ = post(port, "/v1/messages", {"model": "m", "messages": []}, {"Host": host})
            self.assertEqual(response.status, 200, host)
        self.assertEqual(len(upstream.requests), 2)

    def test_only_messages_count_tokens_and_models(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, api_key="sk-upstream")
        for method, path in (("OPTIONS", "/v1/messages"), ("PUT", "/v1/messages"), ("DELETE", "/v1/messages"),
                             ("GET", "/v1/messages"), ("POST", "/v1/complete"), ("POST", "/api/v1/credits"),
                             ("GET", "/v1/messages/batches"), ("POST", "/v1/models")):
            response, _ = request(port, method, path, {"model": "m", "messages": []})
            self.assertIn(response.status, (404, 501), f"{method} {path}")
        self.assertEqual(upstream.requests, [])

    def test_cors_and_cookies_are_not_passed_back(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        response, _ = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 200)
        names = {name.lower() for name, _ in response.getheaders()}
        self.assertFalse([name for name in names if name.startswith("access-control-")])
        self.assertNotIn("set-cookie", names)

    def test_log_and_dump_are_private(self):
        dump = os.path.join(self.tmp.name, "dump.jsonl")
        upstream = FakeUpstream(status=400)
        port = self.run_proxy(upstream, dump_path=dump)
        post(port, "/v1/messages", {"model": "m", "messages": []})
        for path in (self.log, dump):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_start_only_listens_on_loopback(self):
        with self.assertRaises(ValueError):
            proxy.start(proxy.ProxyConfig(upstream="http://127.0.0.1:9"), host="0.0.0.0")

    def test_serve_refuses_remote_without_flag(self):
        with self.assertRaises(ValueError):
            proxy.serve(proxy.ProxyConfig(upstream="http://127.0.0.1:9", client_token=TOKEN), host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
