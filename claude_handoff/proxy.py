"""A small local HTTP proxy between Claude Code and an Anthropic-compatible upstream.

For every ``/v1/messages`` request it:

- runs ``compat.normalize`` (so a session started on Anthropic can continue here);
- optionally pins OpenRouter to one provider per model (``routes``);
- optionally turns Claude Code's thinking/effort settings into ``chat_template_kwargs.enable_thinking``, the switch
  that llama.cpp's ``llama-server`` and vLLM understand (``thinking_toggle``);
- replaces the client's credentials with the upstream API key, so Claude Code never holds it.

On the way back it drops the ``request-id`` headers. Claude Code then records no ``requestId`` for these messages, and
the same session can later be resumed on the Anthropic API without
``400 diagnostics.previous_message_id: must be the `id` from a prior /v1/messages response``.

The proxy spends the upstream key on behalf of whoever reaches it, so it only answers:

- clients presenting its token (``client_token``, a random value per launch), as ``Authorization: Bearer`` or
  ``x-api-key``;
- requests whose ``Host`` is the loopback address it listens on and that carry no ``Origin`` header, which shuts out
  web pages (cross-site requests and DNS rebinding);
- ``POST /v1/messages``, ``POST /v1/messages/count_tokens`` and ``GET /v1/models``. Everything else is refused.

CORS headers from the upstream are dropped, so a browser never gets permission to read a response.
"""

import contextlib
import hmac
import http.client
import json
import os
import socketserver
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .compat import normalize

HOP_BY_HOP_REQUEST = {"host", "connection", "content-length", "transfer-encoding", "accept-encoding", "keep-alive"}
HOP_BY_HOP_RESPONSE = {"connection", "transfer-encoding", "content-length", "keep-alive"}
REQUEST_ID_HEADERS = {"request-id", "x-request-id"}
CREDENTIAL_HEADERS = {"authorization", "x-api-key"}
BROWSER_HEADERS = {"origin", "referer", "cookie"}
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


@dataclass
class ProxyConfig:
    upstream: str
    api_key: str | None = None
    client_token: str | None = None
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


def route(command, path):
    """The endpoint a request is for, or None if the proxy does not serve it."""
    path = path.split("?", 1)[0]
    if command == "POST" and path == "/v1/messages":
        return "messages"
    if command == "POST" and path == "/v1/messages/count_tokens":
        return "count_tokens"
    if command == "GET" and (path == "/v1/models" or path.startswith("/v1/models/")):
        return "models"
    return None


def token_matches(headers, token):
    candidates = [headers.get("x-api-key") or ""]
    authorization = headers.get("Authorization") or ""
    if authorization[:7].lower() == "bearer ":
        candidates.append(authorization[7:].strip())
    expected = token.encode()
    # compare every candidate, so timing does not reveal which header was checked
    return any([hmac.compare_digest(candidate.encode(), expected) for candidate in candidates])


class Log:
    """Append-only text file, created private (0600). A None path makes it a no-op."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, text):
        if not self.path:
            return
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + text.rstrip() + "\n"
        with self.lock:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with contextlib.suppress(OSError):
                    os.fchmod(fd, 0o600)  # also for a file created by an older version
                with os.fdopen(fd, "a", encoding="utf-8") as file:
                    file.write(line)
            except OSError:
                pass


def make_handler(config):
    log = Log(config.log_path)
    dump = Log(config.dump_path)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            self._forward()

        def do_POST(self):
            self._forward()

        def _refuse(self, status, reason):
            log.write(f"refused {self.command} {self.path} -> {status} {reason}"
                      + (f" (Origin: {self.headers.get('Origin')})" if self.headers.get("Origin") else ""))
            self.send_error(status, reason)

        def _check(self):
            """The refusal (status, reason) for this request, or None if it may go upstream."""
            allowed_hosts = self.server.allowed_hosts
            if allowed_hosts is not None and (self.headers.get("Host") or "").lower() not in allowed_hosts:
                return 403, "Host not allowed"
            if self.headers.get("Origin") is not None:
                return 403, "Browser requests are not allowed"
            if config.client_token and not token_matches(self.headers, config.client_token):
                return 401, "Missing or wrong proxy token"
            if route(self.command, self.path) is None:
                return 404, "Not served by this proxy"
            return None

        def _forward(self):
            refusal = self._check()
            if refusal:
                self._refuse(*refusal)
                return
            endpoint = route(self.command, self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._refuse(400, "Bad Content-Length")
                return
            body = self.rfile.read(length) if length else b""

            if endpoint in ("messages", "count_tokens"):
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

            lowered = HOP_BY_HOP_REQUEST | CREDENTIAL_HEADERS | BROWSER_HEADERS
            headers = {name: value for name, value in self.headers.items() if name.lower() not in lowered}
            if config.api_key:
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
                              + error_detail(error_body))
                self.send_response(response.status, response.reason)
                for name, value in response.getheaders():
                    if keep_response_header(name):
                        self.send_header(name, value)
                if error_body:
                    self.send_header("Content-Length", str(len(error_body)))
                elif response.getheader("Content-Length") is not None:
                    self.send_header("Content-Length", response.getheader("Content-Length"))
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
            except (OSError, http.client.HTTPException) as error:
                log.write(f"{self.command} {self.path} -> upstream error: {type(error).__name__}: {error}")
                if not started:
                    try:
                        self.send_error(502, f"Upstream error: {type(error).__name__}")
                    except OSError:
                        pass
                else:
                    self.close_connection = True
            finally:
                connection.close()

    return Handler


def keep_response_header(name):
    lower = name.lower()
    return not (
        lower in HOP_BY_HOP_RESPONSE
        or lower in REQUEST_ID_HEADERS
        or lower == "set-cookie"
        or lower.startswith("access-control-")
    )


def error_detail(body):
    """A one-line summary of an upstream error body, keeping OpenRouter's metadata (it names the offending field)."""
    try:
        data = json.loads(body)
        error = data.get("error", data)
        detail = error.get("message", error) if isinstance(error, dict) else error
        text = str(detail)[:500]
        if data.get("request_id"):
            text += f" (request {data['request_id']})"
        metadata = data.get("metadata") or (error.get("metadata") if isinstance(error, dict) else None)
        if metadata:
            text += " metadata: " + json.dumps(metadata, ensure_ascii=False)[:2000]
        return text
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return body.decode("utf-8", errors="replace")[:1000]


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


class LocalServer(ThreadingHTTPServer):
    """The proxy's HTTP server.

    - no reverse DNS lookup at bind time (seconds on some macOS setups);
    - handler crashes go to the log file, never to the terminal Claude Code is drawing in;
    - ``allowed_hosts``: accepted ``Host`` header values, or None to accept any (non-loopback ``--allow-remote``).
    """

    daemon_threads = True

    def __init__(self, address, handler, log_path=None, check_host=True):
        self.log = Log(log_path)
        self.allowed_hosts = None
        super().__init__(address, handler)
        if check_host:
            port = self.server_address[1]
            self.allowed_hosts = {f"{name}:{port}" for name in LOOPBACK_NAMES}
            if port == 80:
                self.allowed_hosts |= set(LOOPBACK_NAMES)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        self.log.write("internal error:\n" + traceback.format_exc())


def start(config, host="127.0.0.1", port=0):
    """Start the proxy on loopback in a background thread. Returns the server; its port is ``server_address[1]``."""
    if not is_loopback(host):
        raise ValueError("start() only listens on loopback")
    server = LocalServer((host, port), make_handler(config), log_path=config.log_path)
    threading.Thread(target=server.serve_forever, name="claude-handoff-proxy", daemon=True).start()
    return server


def serve(config, host="127.0.0.1", port=8787, allow_remote=False):
    """Run the proxy in the foreground (``claude-handoff proxy``)."""
    remote = not is_loopback(host)
    if remote and not allow_remote:
        raise ValueError(f"refusing to listen on {host}: anyone who can reach it with the token spends your key "
                         "(pass --allow-remote if that is what you want)")
    server = LocalServer((host, port), make_handler(config), log_path=config.log_path, check_host=not remote)
    address = f"http://{host}:{server.server_address[1]}"
    print(f"claude-handoff proxy: {address} -> {config.upstream}", file=sys.stderr)
    if remote:
        print("  warning: listening beyond loopback; the token is the only protection", file=sys.stderr)
    print(f"  export ANTHROPIC_BASE_URL={address}", file=sys.stderr)
    print(f"  export ANTHROPIC_AUTH_TOKEN={config.client_token}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def state_dir():
    """``$XDG_STATE_HOME/claude-code-handoff`` (default ``~/.local/state/claude-code-handoff``), private."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    path = os.path.join(base, "claude-code-handoff")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path
