"""A small local HTTP proxy between Claude Code and an Anthropic-compatible upstream.

For every ``/v1/messages`` request it:

- runs ``compat.normalize`` (so a session started on Anthropic can continue here);
- optionally pins OpenRouter to one provider per model (``routes``);
- optionally turns Claude Code's thinking/effort settings into ``chat_template_kwargs.enable_thinking``, the switch
  that llama.cpp's ``llama-server`` and vLLM understand (``thinking_toggle``);
- optionally replaces the client's credentials with the upstream API key, so Claude Code never holds it.

On the way back it drops the ``request-id`` headers. Claude Code then records no ``requestId`` for these messages, and
the same session can later be resumed on the Anthropic API without
``400 diagnostics.previous_message_id: must be the `id` from a prior /v1/messages response``.

Everything else (other paths, other methods, streaming responses) is passed through untouched.
"""

import http.client
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .compat import normalize

HOP_BY_HOP_REQUEST = {"host", "connection", "content-length", "transfer-encoding", "accept-encoding", "keep-alive"}
HOP_BY_HOP_RESPONSE = {"connection", "transfer-encoding", "content-length", "keep-alive"}
REQUEST_ID_HEADERS = {"request-id", "x-request-id"}
CREDENTIAL_HEADERS = {"authorization", "x-api-key"}


@dataclass
class ProxyConfig:
    upstream: str
    api_key: str | None = None
    routes: dict = field(default_factory=dict)
    thinking_toggle: bool = False
    timeout: float = 3600.0
    log_path: str | None = None
    dump_path: str | None = None

    def __post_init__(self):
        parts = urlsplit(self.upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"upstream must be an http(s) URL, got {self.upstream!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.prefix = parts.path.rstrip("/")


def reasoning_enabled(payload):
    """Claude Code's thinking settings reduced to the on/off switch most local chat templates expose.

    thinking absent or ``{"type": "disabled"}``  -> off   (Alt+T, or a model without thinking)
    ``output_config.effort == "low"``           -> off   (fast, direct answers)
    thinking ``enabled`` / ``adaptive``         -> on    (medium, high, xhigh, max)
    """
    thinking = payload.get("thinking")
    if not (isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive")):
        return False
    output_config = payload.get("output_config")
    return not (isinstance(output_config, dict) and output_config.get("effort") == "low")


def rewrite(payload, config, endpoint):
    """Apply the proxy's rewrites to a decoded request body (in place) and return it."""
    normalize(payload)
    if endpoint == "messages":
        provider = config.routes.get(payload.get("model"))
        if provider:
            payload["provider"] = {"only": [provider], "allow_fallbacks": False}
        if config.thinking_toggle:
            kwargs = dict(payload.get("chat_template_kwargs") or {})
            kwargs["enable_thinking"] = reasoning_enabled(payload)
            payload["chat_template_kwargs"] = kwargs
    return payload


def _endpoint(path):
    path = path.split("?", 1)[0].rstrip("/")
    if path.endswith("/messages"):
        return "messages"
    if path.endswith("/messages/count_tokens"):
        return "count_tokens"
    return None


class _Log:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, text):
        if not self.path:
            return
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n"
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as file:
                    file.write(line)
            except OSError:
                pass


def make_handler(config):
    log = _Log(config.log_path)
    dump = _Log(config.dump_path)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            self._forward()

        def do_POST(self):
            self._forward()

        def do_PUT(self):
            self._forward()

        def do_DELETE(self):
            self._forward()

        def do_OPTIONS(self):
            self._forward()

        def _forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            endpoint = _endpoint(self.path) if self.command == "POST" else None
            if endpoint:
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(400, "Messages request body must be JSON")
                    return
                if config.dump_path:
                    before = json.loads(body)
                rewrite(payload, config, endpoint)
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
                if config.dump_path:
                    dump.write(json.dumps({"path": self.path, "in": before, "out": payload}, ensure_ascii=False))

            headers = {
                name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP_REQUEST
            }
            if config.api_key:
                headers = {name: value for name, value in headers.items() if name.lower() not in CREDENTIAL_HEADERS}
                headers["Authorization"] = f"Bearer {config.api_key}"
            headers["Content-Length"] = str(len(body))
            headers["Accept-Encoding"] = "identity"

            connection_class = http.client.HTTPSConnection if config.scheme == "https" else http.client.HTTPConnection
            connection = connection_class(config.host, config.port, timeout=config.timeout)
            started = False
            try:
                connection.request(self.command, config.prefix + self.path, body=body, headers=headers)
                response = connection.getresponse()
                error_body = response.read() if response.status >= 400 else b""
                if error_body:
                    log.write(f"{self.command} {self.path} -> HTTP {response.status} {response.reason}: "
                              + _error_detail(error_body))
                self.send_response(response.status, response.reason)
                for name, value in response.getheaders():
                    lower = name.lower()
                    if lower not in HOP_BY_HOP_RESPONSE and lower not in REQUEST_ID_HEADERS:
                        self.send_header(name, value)
                if response.getheader("Content-Length") is not None and not error_body:
                    self.send_header("Content-Length", response.getheader("Content-Length"))
                elif error_body:
                    self.send_header("Content-Length", str(len(error_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                started = True

                if error_body:
                    self.wfile.write(error_body)
                else:
                    # read1 returns as soon as data arrives, so server-sent events are not held back.
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError as error:
                log.write(f"{self.command} {self.path} -> connection error: {error}")
                if not started:
                    try:
                        self.send_error(502, f"Could not reach {config.upstream}: {error}")
                    except OSError:
                        pass
            finally:
                connection.close()

    return Handler


def _error_detail(body):
    try:
        data = json.loads(body)
        error = data.get("error", data)
        detail = error.get("message", error) if isinstance(error, dict) else error
        request_id = data.get("request_id")
        return str(detail)[:500] + (f" (request {request_id})" if request_id else "")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return body.decode("utf-8", errors="replace")[:500]


def start(config, host="127.0.0.1", port=0):
    """Start the proxy in a background thread. Returns the server; its port is ``server.server_address[1]``."""
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="claude-handoff-proxy", daemon=True).start()
    return server


def serve(config, host="127.0.0.1", port=8787):
    """Run the proxy in the foreground (``claude-handoff proxy``)."""
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.daemon_threads = True
    print(f"claude-handoff proxy: http://{host}:{server.server_address[1]} -> {config.upstream}", file=sys.stderr)
    print(f"  export ANTHROPIC_BASE_URL=http://{host}:{server.server_address[1]}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    path = os.path.join(base, "claude-code-handoff")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path
