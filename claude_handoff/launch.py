"""Start Claude Code on a provider: proxy in a background thread, ``claude`` in the foreground."""

import json
import os
import shutil
import signal
import subprocess
import sys
import urllib.request

from . import proxy, transcripts

PICKER_TIERS = ("OPUS", "SONNET", "HAIKU", "FABLE")


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


def restore_default_model(path, before, provider_models):
    """Undo a ``/model`` choice that Claude Code saved as the global default during a provider session.

    Picking a model in ``/model`` writes it to settings.json. Left there, the next plain ``claude`` would start on
    a model Anthropic does not know. Only the ``model`` key is touched, and only if it now names one of this
    provider's models.
    """
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return False
    current = data.get("model") if isinstance(data, dict) else None
    if current == before or current not in provider_models:
        return False
    if before is None:
        data.pop("model", None)
    else:
        data["model"] = before
    tmp = path + ".claude-handoff.tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        file.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return True


def picker_settings(models):
    """``--settings`` JSON for ``/model``: the provider's real model ids, nothing remapped."""
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


def run(config, model, models, claude_args, capabilities=None):
    """Run Claude Code through the proxy; returns its exit code."""
    claude = claude_binary()
    settings = settings_path()
    before = read_default_model(settings)
    server = proxy.start(config)
    port = server.server_address[1]

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    # The proxy holds the real key when it has one; Claude Code only needs a non-empty token.
    env["ANTHROPIC_AUTH_TOKEN"] = "claude-handoff"
    env.pop("ANTHROPIC_API_KEY", None)
    env["ANTHROPIC_MODEL"] = model
    # Background work and subagents ask for "haiku", "sonnet"...: send them to the launch model, which this
    # provider serves, instead of an Anthropic model it does not know.
    for tier in PICKER_TIERS:
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = model
    if capabilities:
        for tier in ("OPUS", "SONNET"):
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL_SUPPORTED_CAPABILITIES"] = capabilities
    env["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"

    command = [claude, "--settings", picker_settings(models), "--model", model, *claude_args]
    child = subprocess.Popen(command, env=env)

    # Ctrl+C belongs to Claude Code (same terminal, same process group); the launcher just waits for it.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    try:
        code = child.wait()
    finally:
        restore_default_model(settings, before, {model_id for model_id, _ in models})
        server.shutdown()
        server.server_close()
    return 128 - code if code < 0 else code  # killed by a signal: exit like a shell would (SIGTERM -> 143)


def run_anthropic(claude_args):
    """Plain ``claude`` on the Anthropic API, after repairing transcripts recorded through other proxies."""
    transcripts.fix_all(stamp=os.path.join(proxy.state_dir(), "transcripts.stamp"))
    claude = claude_binary()
    os.execv(claude, [claude, *claude_args])
