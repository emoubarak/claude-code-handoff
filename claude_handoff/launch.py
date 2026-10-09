"""Start Claude Code on a provider: proxy in a background thread, ``claude`` in the foreground."""

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.request

from . import proxy, transcripts

try:
    import fcntl
except ImportError:  # Windows: no lock, best effort
    fcntl = None

PICKER_TIERS = ("OPUS", "SONNET", "HAIKU", "FABLE")
# Credentials Claude Code must not inherit: the model can run `env` and read them into the conversation.
SECRET_ENV = ("OPENROUTER_API_KEY", "CLAUDE_HANDOFF_LOCAL_API_KEY", "CLAUDE_HANDOFF_PROXY_TOKEN", "ANTHROPIC_API_KEY")


def claude_binary():
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not path:
        sys.exit("claude-handoff: `claude` not found on PATH (install Claude Code, or set CLAUDE_BIN)")
    return path


def settings_path():
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config, "settings.json")


def read_default_model(path):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file).get("model")
    except (OSError, ValueError, AttributeError):
        return None


def write_json_in_place(path, data):
    """Replace a JSON file atomically, through symlinks (dotfile setups) and keeping its permissions."""
    target = os.path.realpath(path)
    mode = os.stat(target).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".settings.", suffix=".claude-handoff")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def restore_default_model(path, before, provider_models):
    """Undo a ``/model`` choice that Claude Code saved as the global default during a provider session.

    Picking a model in ``/model`` writes it to settings.json. Left there, the next plain ``claude`` would start on
    a model Anthropic does not know. Only the ``model`` key is touched, and only if it now names one of the
    provider models. Never raises: a failure here must not hide Claude Code's own exit.
    """
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as error:
        print(f"claude-handoff: could not read {path}: {error}", file=sys.stderr)
        return False
    try:
        current = data.get("model") if isinstance(data, dict) else None
        if current == before or current not in provider_models:
            return False
        if before is None:
            data.pop("model", None)
        else:
            data["model"] = before
        write_json_in_place(path, data)
        return True
    except (OSError, ValueError) as error:
        print(f"claude-handoff: could not restore the default model in {path}: {error}", file=sys.stderr)
        return False


class DefaultModelGuard:
    """Keeps the user's default model safe across provider sessions, including parallel and killed ones.

    A small record in the state folder holds the default from before the first provider session, the provider models
    seen, and the launcher processes still running. Every exit puts the default back if a provider model is sitting
    in settings.json. What a launcher killed with SIGKILL could not do is done by the next launch, or by
    ``claude-handoff anthropic``.
    """

    def __init__(self, settings, state=None):
        self.settings = settings
        state = state or proxy.state_dir()
        self.record = os.path.join(state, "default-model.json")
        self.lock_path = os.path.join(state, "default-model.lock")

    @contextlib.contextmanager
    def _locked(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self):
        try:
            with open(self.record, encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, ValueError):
            return None
        data["pids"] = [pid for pid in data.get("pids", []) if _alive(pid)]
        return data

    def _save(self, data):
        fd = os.open(self.record + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file)
        os.replace(self.record + ".tmp", self.record)

    def _restore(self, data):
        restore_default_model(self.settings, data.get("before"), set(data.get("models", [])))

    def _forget(self):
        with contextlib.suppress(OSError):
            os.unlink(self.record)

    def enter(self, models):
        with self._locked():
            data = self._load()
            if data is not None and not data["pids"]:
                # left by a launcher that was killed: repair, then start a fresh record
                self._restore(data)
                self._forget()
                data = None
            if data is None:
                data = {"before": read_default_model(self.settings), "models": [], "pids": []}
            data["models"] = sorted(set(data["models"]) | set(models))
            data["pids"].append(os.getpid())
            self._save(data)

    def exit(self):
        with self._locked():
            data = self._load()
            if data is None:
                return
            data["pids"] = [pid for pid in data["pids"] if pid != os.getpid()]
            # Other launchers pass --model explicitly, so restoring now does not disturb them.
            self._restore(data)
            if data["pids"]:
                self._save(data)
            else:
                self._forget()

    def recover(self):
        """Called by ``claude-handoff anthropic``: repair what a killed provider session left behind."""
        with self._locked():
            data = self._load()
            if data is None:
                return
            self._restore(data)
            if not data["pids"]:
                self._forget()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def picker_settings(models):
    """``--settings`` JSON for ``/model``: the provider's real model ids."""
    options = [{"model": model_id, "label": label} for model_id, label in models]
    return json.dumps({"modelPicker": {"replaceBuiltInOptions": True, "options": options}})


def parse_models(spec):
    """``"a/b=Nice name,c/d"`` -> ``[("a/b", "Nice name"), ("c/d", "c/d")]``."""
    models = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        model_id, _, label = item.partition("=")
        models.append((model_id.strip(), label.strip() or model_id.strip()))
    return models


def parse_routes(spec):
    """``"model-a=provider1,model-b=provider2"`` -> dict."""
    return {model: provider for model, provider in parse_models(spec) if provider != model}


def fetch_models(base_url, api_key=None, timeout=5):
    """Model ids served by an OpenAI-compatible ``/v1/models`` endpoint. Raises on failure."""
    request = urllib.request.Request(base_url.rstrip("/") + "/v1/models")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    return [item["id"] for item in data.get("data", []) if isinstance(item, dict) and item.get("id")]


def claude_environment(base_url, token, model, capabilities=None, secrets_to_drop=()):
    """The environment Claude Code runs with: pointed at the proxy, without any upstream credential."""
    env = dict(os.environ)
    for name in SECRET_ENV:
        env.pop(name, None)
    drop = {value for value in secrets_to_drop if value}
    for name in [name for name, value in env.items() if value in drop]:
        del env[name]  # the same key exported under another name
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_MODEL"] = model
    # Background work and subagents ask for "haiku", "sonnet"...: send them to the launch model, which this
    # provider serves, instead of an Anthropic model it does not know.
    for tier in PICKER_TIERS:
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
    if capabilities:
        for tier in ("OPUS", "SONNET"):
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL_SUPPORTED_CAPABILITIES"] = capabilities
    env["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
    return env


def run(config, model, models, claude_args, capabilities=None):
    """Run Claude Code through the proxy; returns its exit code."""
    claude = claude_binary()
    guard = DefaultModelGuard(settings_path())
    model_ids = [model_id for model_id, _ in models]
    guard.enter(model_ids)

    # A fresh token per launch: only this Claude Code process can use the proxy, and so the key behind it.
    config.client_token = secrets.token_urlsafe(32)
    server = proxy.start(config)
    port = server.server_address[1]
    env = claude_environment(f"http://127.0.0.1:{port}", config.client_token, model, capabilities,
                             secrets_to_drop=(config.api_key,))

    command = [claude, "--settings", picker_settings(models), "--model", model, *claude_args]
    try:
        child = subprocess.Popen(command, env=env)
    except BaseException:
        guard.exit()
        server.shutdown()
        server.server_close()
        raise

    # Ctrl+C belongs to Claude Code (same terminal, same process group); the launcher just waits for it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def forward(signum, _frame):
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    for name in ("SIGTERM", "SIGHUP"):  # SIGHUP: the terminal was closed
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), forward)
    try:
        code = child.wait()
    finally:
        guard.exit()
        server.shutdown()
        server.server_close()
    return 128 - code if code < 0 else code  # killed by a signal: exit like a shell would (SIGTERM -> 143)


def run_anthropic(claude_args):
    """Plain ``claude`` on the Anthropic API, after repairing what provider sessions may have left behind."""
    DefaultModelGuard(settings_path()).recover()
    transcripts.fix_all(stamp=os.path.join(proxy.state_dir(), "transcripts.stamp"),
                        backup_dir=os.path.join(proxy.state_dir(), "backups"))
    claude = claude_binary()
    os.execv(claude, [claude, *claude_args])
