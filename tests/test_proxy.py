import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler

from claude_handoff import proxy
from claude_handoff.proxy import LocalServer


class FakeUpstream:
    """Records what the proxy sends and answers like an Anthropic-compatible server."""

    def __init__(self, status=200, stream=False):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                if status >= 400:
                    data = json.dumps({"error": {"message": "nope"}}).encode()
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


def post(port, path, payload, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(payload).encode()
    connection.request("POST", path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


class ProxyTest(unittest.TestCase):
    def run_proxy(self, upstream, **kwargs):
        server = proxy.start(proxy.ProxyConfig(upstream=upstream.url, **kwargs))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(upstream.close)
        return server.server_address[1]

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

    def test_request_id_headers_are_not_passed_back(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream)
        response, _ = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertIsNone(response.getheader("request-id"))
        self.assertIsNone(response.getheader("x-request-id"))

    def test_api_key_replaces_client_credentials(self):
        upstream = FakeUpstream()
        port = self.run_proxy(upstream, api_key="sk-upstream")
        post(port, "/v1/messages", {"model": "m", "messages": []},
             {"Authorization": "Bearer placeholder", "x-api-key": "placeholder"})
        headers = {k.lower(): v for k, v in upstream.requests[0]["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer sk-upstream")
        self.assertNotIn("x-api-key", headers)

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
        for request, (_, expected) in zip(upstream.requests, cases):
            self.assertEqual(request["body"]["chat_template_kwargs"]["enable_thinking"], expected)

    def test_streaming_passes_through(self):
        upstream = FakeUpstream(stream=True)
        port = self.run_proxy(upstream)
        response, data = post(port, "/v1/messages", {"model": "m", "stream": True, "messages": []})
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"event: a\ndata: {}\n\nevent: b\ndata: {}\n\n")

    def test_errors_are_forwarded_with_body(self):
        upstream = FakeUpstream(status=400)
        port = self.run_proxy(upstream)
        response, data = post(port, "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(data)["error"]["message"], "nope")
        self.assertIsNone(response.getheader("request-id"))

    def test_unreachable_upstream_gives_502(self):
        server = proxy.start(proxy.ProxyConfig(upstream="http://127.0.0.1:9", timeout=2))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        response, _ = post(server.server_address[1], "/v1/messages", {"model": "m", "messages": []})
        self.assertEqual(response.status, 502)

    def test_bad_upstream_url(self):
        with self.assertRaises(ValueError):
            proxy.ProxyConfig(upstream="openrouter.ai")


if __name__ == "__main__":
    unittest.main()
