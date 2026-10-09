"""End-to-end launcher tests with a fake `claude` binary and a fake local model server."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_CLAUDE = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time, urllib.request
    out = os.environ["FAKE_CLAUDE_OUT"]
    record = {{"argv": sys.argv[1:], "env": {{k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))}}}}
    if os.environ.get("FAKE_CLAUDE_CALL"):
        body = json.dumps({{"model": "m", "messages": [{{"role": "user", "content": "hi"}}, {{"role": "system", "content": "note"}}]}}).encode()
        request = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", data=body,
                                         headers={{"Authorization": "Bearer " + os.environ["ANTHROPIC_AUTH_TOKEN"]}})
        with urllib.request.urlopen(request, timeout=10) as response:
            record["response"] = json.load(response)
    settings = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
    data = json.load(open(settings))
    data["model"] = os.environ["FAKE_CLAUDE_PICK"]  # what /model + Enter does
    json.dump(data, open(settings, "w"))
    json.dump(record, open(out, "w"))
    if os.environ.get("FAKE_CLAUDE_SLEEP"):
        open(out + ".ready", "w").close()
        time.sleep(30)
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
""")


class FakeServer:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _send(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("request-id", "local-1")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send({"data": [{"id": "qwen-local"}, {"id": "other-local"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"headers": dict(self.headers), "body": body})
                self._send({"id": "local-msg", "type": "message"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "config")
        os.makedirs(self.config)
        with open(os.path.join(self.config, "settings.json"), "w") as file:
            json.dump({"model": "opus", "theme": "dark"}, file)
        self.claude = os.path.join(self.tmp.name, "claude")
        with open(self.claude, "w") as file:
            file.write(FAKE_CLAUDE.format(python=sys.executable))
        os.chmod(self.claude, 0o755)
        self.out = os.path.join(self.tmp.name, "out.json")
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.tmp.name,
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "state"),
            "CLAUDE_BIN": self.claude,
            "CLAUDE_CONFIG_DIR": self.config,
            "FAKE_CLAUDE_OUT": self.out,
            "PYTHONPATH": ROOT,
        }

    def launch(self, *args, **env):
        return subprocess.Popen([sys.executable, "-m", "claude_handoff", *args], env={**self.env, **env}, cwd=ROOT,
                                start_new_session=True)

    def wait_ready(self):
        deadline = time.time() + 15
        while not os.path.exists(self.out + ".ready") and time.time() < deadline:
            time.sleep(0.05)

    def result(self):
        with open(self.out) as file:
            return json.load(file)

    def settings(self):
        with open(os.path.join(self.config, "settings.json")) as file:
            return json.load(file)

    def test_openrouter_arguments_environment_and_restore(self):
        process = self.launch("openrouter", "-m", "a/model", "--models", "b/model=Model B", "--resume", "abc",
                              OPENROUTER_API_KEY="sk-test", FAKE_CLAUDE_PICK="b/model", FAKE_CLAUDE_EXIT="3")
        self.assertEqual(process.wait(timeout=20), 3)
        record = self.result()
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "a/model")
        self.assertEqual(argv[-2:], ["--resume", "abc"])
        picker = json.loads(argv[argv.index("--settings") + 1])["modelPicker"]
        self.assertEqual(picker["options"], [{"model": "a/model", "label": "a/model"},
                                             {"model": "b/model", "label": "Model B"}])
        env = record["env"]
        self.assertTrue(env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:"))
        self.assertNotEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-test")  # the key stays in the proxy
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "a/model")
        # /model picked b/model during the session; the global default is back to what it was.
        self.assertEqual(self.settings(), {"model": "opus", "theme": "dark"})

    def test_local_lists_served_models_and_proxies(self):
        server = FakeServer()
        self.addCleanup(server.close)
        process = self.launch("local", "--url", server.url, "-p", "hello",
                              FAKE_CLAUDE_PICK="other-local", FAKE_CLAUDE_CALL="1")
        self.assertEqual(process.wait(timeout=20), 0)
        record = self.result()
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "qwen-local")
        picker = json.loads(argv[argv.index("--settings") + 1])["modelPicker"]
        self.assertEqual([o["model"] for o in picker["options"]], ["qwen-local", "other-local"])
        self.assertEqual(record["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES"], "effort,thinking")
        self.assertEqual(record["response"]["id"], "local-msg")
        sent = server.requests[0]["body"]
        self.assertEqual([m["role"] for m in sent["messages"]], ["user"])
        self.assertIs(sent["chat_template_kwargs"]["enable_thinking"], False)
        self.assertEqual(self.settings()["model"], "opus")

    def test_local_server_down(self):
        process = self.launch("local", "--url", "http://127.0.0.1:9", FAKE_CLAUDE_PICK="x")
        self.assertNotEqual(process.wait(timeout=20), 0)
        self.assertFalse(os.path.exists(self.out))

    def test_sigterm_reaches_claude_and_settings_are_restored(self):
        process = self.launch("openrouter", OPENROUTER_API_KEY="sk-test", FAKE_CLAUDE_PICK="deepseek/deepseek-v4.1-flash",
                              FAKE_CLAUDE_SLEEP="1")
        self.wait_ready()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertEqual(self.settings()["model"], "opus")

    def test_ctrl_c_goes_to_claude_not_the_launcher(self):
        process = self.launch("openrouter", OPENROUTER_API_KEY="sk-test", FAKE_CLAUDE_PICK="deepseek/deepseek-v4.1-flash",
                              FAKE_CLAUDE_SLEEP="1")
        self.wait_ready()
        os.killpg(process.pid, signal.SIGINT)  # what the terminal does on Ctrl+C: the whole foreground group
        # The fake claude dies of SIGINT; the launcher survives it, cleans up, and reports it like a shell.
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGINT)
        self.assertEqual(self.settings()["model"], "opus")


if __name__ == "__main__":
    unittest.main()
