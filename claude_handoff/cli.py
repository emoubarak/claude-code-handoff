"""claude-handoff: run Claude Code on OpenRouter, a local model or Anthropic, and move sessions between them."""

import argparse
import os
import secrets
import sys

from . import __version__, launch, proxy, transcripts

USAGE = """\
usage: claude-handoff <command> [options] [claude arguments...]

Commands:
  openrouter       Claude Code on OpenRouter (needs OPENROUTER_API_KEY)
  local            Claude Code on a local Anthropic-compatible server (llama-server, llama-swap, vLLM, LiteLLM...)
  anthropic        Claude Code on the Anthropic API, after repairing transcripts recorded through other proxies
  fix-transcripts  Only repair the transcripts
  proxy            Run the proxy alone, for any client or a service manager

Options of openrouter / local come first; everything after them goes to `claude` unchanged:
  claude-handoff openrouter -m deepseek/deepseek-v4.1-flash --resume
  claude-handoff local --url http://127.0.0.1:8080 -p "explain this repo"

Run `claude-handoff <command> --help` for the options of one command.
"""

OPENROUTER_HELP = """\
usage: claude-handoff openrouter [options] [claude arguments...]

  -m, --model MODEL    model to start on            (env CLAUDE_HANDOFF_OPENROUTER_MODEL, default {default})
  --models LIST        extra models for /model, comma separated, `id` or `id=Label`
                                                    (env CLAUDE_HANDOFF_OPENROUTER_MODELS)
  --route LIST         pin a model to one OpenRouter provider, `model=provider,...`, no fallback
                                                    (env CLAUDE_HANDOFF_OPENROUTER_ROUTES)
  --dump FILE          append every rewritten request (full conversation!) to FILE, for debugging
"""

LOCAL_HELP = """\
usage: claude-handoff local [options] [claude arguments...]

  --url URL            server base URL               (env CLAUDE_HANDOFF_LOCAL_URL, default {default})
  -m, --model MODEL    model to start on             (env CLAUDE_HANDOFF_LOCAL_MODEL, default: the first one served)
  --models LIST        models for /model, `id` or `id=Label` (default: every model the server lists)
  --no-thinking-toggle do not translate thinking/effort into chat_template_kwargs.enable_thinking
  --capabilities LIST  capabilities announced to Claude Code (env CLAUDE_HANDOFF_LOCAL_CAPABILITIES,
                       default "effort,thinking"; "" to announce none)
  --dump FILE          append every rewritten request (full conversation!) to FILE, for debugging

  The server's API key, if it needs one, is read from CLAUDE_HANDOFF_LOCAL_API_KEY.
"""

OPENROUTER_URL = "https://openrouter.ai/api"
OPENROUTER_DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
LOCAL_DEFAULT_URL = "http://127.0.0.1:8080"


def split_options(args, with_value, flags=()):
    """Take the launcher's own options from the front of ``args``; the rest belongs to claude."""
    options, index = {}, 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        name, has_inline, inline = arg.partition("=") if arg.startswith("--") else (arg, False, None)
        if name in with_value:
            if has_inline:
                options[with_value[name]] = inline
                index += 1
            elif index + 1 < len(args):
                options[with_value[name]] = args[index + 1]
                index += 2
            else:
                sys.exit(f"claude-handoff: {name} needs a value")
            continue
        if arg in flags:
            options[flags[arg]] = True
            index += 1
            continue
        break
    return options, args[index:]


def merge_models(model, models):
    """Launch model first, then the others, without duplicates."""
    seen, merged = set(), []
    for model_id, label in [(model, model), *models]:
        if model_id not in seen:
            seen.add(model_id)
            merged.append((model_id, label))
        elif model_id == model and label != model_id:
            merged[0] = (model_id, label)
    return merged


def cmd_openrouter(args):
    if args[:1] in (["-h"], ["--help"]):
        print(OPENROUTER_HELP.format(default=OPENROUTER_DEFAULT_MODEL))
        return 0
    options, claude_args = split_options(
        args, {"-m": "model", "--model": "model", "--models": "models", "--route": "route", "--dump": "dump"}
    )
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("claude-handoff: OPENROUTER_API_KEY is not set")
    model = options.get("model") or os.environ.get("CLAUDE_HANDOFF_OPENROUTER_MODEL") or OPENROUTER_DEFAULT_MODEL
    models = merge_models(
        model, launch.parse_models(options.get("models") or os.environ.get("CLAUDE_HANDOFF_OPENROUTER_MODELS"))
    )
    config = proxy.ProxyConfig(
        upstream=OPENROUTER_URL,
        api_key=key,
        routes=launch.parse_routes(options.get("route") or os.environ.get("CLAUDE_HANDOFF_OPENROUTER_ROUTES")),
        log_path=os.path.join(proxy.state_dir(), "proxy.log"),
        dump_path=options.get("dump"),
    )
    return launch.run(config, model, models, claude_args)


def cmd_local(args):
    if args[:1] in (["-h"], ["--help"]):
        print(LOCAL_HELP.format(default=LOCAL_DEFAULT_URL))
        return 0
    options, claude_args = split_options(
        args,
        {"--url": "url", "-m": "model", "--model": "model", "--models": "models", "--capabilities": "capabilities",
         "--dump": "dump"},
        {"--no-thinking-toggle": "no_thinking"},
    )
    url = (options.get("url") or os.environ.get("CLAUDE_HANDOFF_LOCAL_URL") or LOCAL_DEFAULT_URL).rstrip("/")
    key = os.environ.get("CLAUDE_HANDOFF_LOCAL_API_KEY")
    try:
        served = launch.fetch_models(url, key)
    except Exception as error:  # noqa: BLE001 - any failure means the same thing to the user
        sys.exit(f"claude-handoff: no answer from {url}/v1/models ({error}). Is the server running?")
    model = options.get("model") or os.environ.get("CLAUDE_HANDOFF_LOCAL_MODEL") or (served[0] if served else None)
    if not model:
        sys.exit(f"claude-handoff: {url} serves no model; pass one with -m")
    listed = launch.parse_models(options.get("models")) or [(model_id, model_id) for model_id in served]
    models = merge_models(model, listed)
    capabilities = options.get("capabilities")
    if capabilities is None:
        capabilities = os.environ.get("CLAUDE_HANDOFF_LOCAL_CAPABILITIES", "effort,thinking")
    config = proxy.ProxyConfig(
        upstream=url,
        api_key=key,
        thinking_toggle=not options.get("no_thinking"),
        log_path=os.path.join(proxy.state_dir(), "proxy.log"),
        dump_path=options.get("dump"),
    )
    return launch.run(config, model, models, claude_args, capabilities=capabilities or None)


def cmd_anthropic(args):
    launch.run_anthropic(args)


def cmd_fix_transcripts(args):
    parser = argparse.ArgumentParser(prog="claude-handoff fix-transcripts",
                                     description="Repair transcripts so the sessions resume on the Anthropic API.")
    parser.add_argument("folder", nargs="?", help="default: Claude Code's projects folder")
    parser.add_argument("--dry-run", action="store_true", help="only list what would change")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not copy changed files to the state folder first")
    options = parser.parse_args(args)
    backup_dir = None if options.no_backup else os.path.join(proxy.state_dir(), "backups")
    total = transcripts.fix_all(options.folder, dry_run=options.dry_run, backup_dir=backup_dir)
    verb = "would be fixed" if options.dry_run else "fixed"
    print(f"claude-handoff: {total} message(s) {verb}", file=sys.stderr)
    if total and backup_dir and not options.dry_run:
        print(f"claude-handoff: originals saved under {backup_dir}", file=sys.stderr)
    return 0


def cmd_proxy(args):
    parser = argparse.ArgumentParser(
        prog="claude-handoff proxy",
        description="Run the proxy in the foreground. Clients must send its token "
                    "(ANTHROPIC_AUTH_TOKEN for Claude Code).")
    parser.add_argument("--upstream", required=True, help="Anthropic-compatible base URL, e.g. " + OPENROUTER_URL)
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1; anything else needs --allow-remote")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow listening beyond loopback (the token is then the only protection)")
    parser.add_argument("--api-key-env", metavar="NAME", help="environment variable holding the upstream key")
    parser.add_argument("--route", help="model=provider,... (OpenRouter provider pinning)")
    parser.add_argument("--thinking-toggle", action="store_true",
                        help="translate thinking/effort into chat_template_kwargs.enable_thinking")
    parser.add_argument("--log", help="upstream errors and refused requests (default: the state folder's proxy.log)")
    parser.add_argument("--dump", help="append every rewritten request (full conversation!) to this file")
    options = parser.parse_args(args)
    key = os.environ.get(options.api_key_env) if options.api_key_env else None
    if options.api_key_env and not key:
        sys.exit(f"claude-handoff: {options.api_key_env} is not set")
    token = os.environ.get("CLAUDE_HANDOFF_PROXY_TOKEN") or secrets.token_urlsafe(32)
    config = proxy.ProxyConfig(
        upstream=options.upstream,
        api_key=key,
        client_token=token,
        routes=launch.parse_routes(options.route),
        thinking_toggle=options.thinking_toggle,
        log_path=options.log or os.path.join(proxy.state_dir(), "proxy.log"),
        dump_path=options.dump,
    )
    try:
        proxy.serve(config, options.host, options.port, allow_remote=options.allow_remote)
    except ValueError as error:
        sys.exit(f"claude-handoff: {error}")
    return 0


COMMANDS = {
    "openrouter": cmd_openrouter,
    "local": cmd_local,
    "anthropic": cmd_anthropic,
    "fix-transcripts": cmd_fix_transcripts,
    "proxy": cmd_proxy,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        print(f"claude-handoff {__version__}")
        return 0
    command = COMMANDS.get(argv[0])
    if not command:
        print(f"claude-handoff: unknown command {argv[0]!r}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    return command(argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
