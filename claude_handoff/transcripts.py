"""Make sessions that went through another provider resumable on the Anthropic API again.

When resuming, Claude Code sends Anthropic ``diagnostics.previous_message_id``: the id of the last assistant message
that has a ``requestId`` (or, without one, whose id starts with ``msg_``). A message served by another provider has an
id like ``gen-...``; if a proxy passed that provider's request id back, Claude Code stored it as ``requestId`` and the
Anthropic API answers::

    400 diagnostics.previous_message_id: must be the `id` from a prior /v1/messages response

The claude-handoff proxy never passes request ids back, so its own sessions are fine. This module repairs sessions
recorded by other proxies or routers: it sets ``requestId`` to ``null`` on assistant messages whose id is not an
Anthropic ``msg_`` id. Claude Code then skips them and picks the last genuine Anthropic id.

The edit is done byte for byte, padded with spaces to the same length: no line moves, so a session that is writing to
the file at the same time is not disturbed.
"""

import json
import os
import re
import sys
import time

MARK = b'"requestId":"'
# Anthropic request ids start with "req_": anything else came from another provider.
FOREIGN_REQUEST_ID = re.compile(rb'"requestId":"(?!req_)')


def fix_line(line):
    """Return ``(offset, replacement)`` for one transcript line, or None if it needs no change."""
    if MARK not in line:
        return None
    try:
        entry = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None  # a line still being written, or damaged: leave it alone
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    request_id = entry.get("requestId")
    message = entry.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    if not isinstance(request_id, str) or (isinstance(message_id, str) and message_id.startswith("msg_")):
        return None
    old = b'"requestId":' + json.dumps(request_id, separators=(",", ":")).encode()
    if line.count(old) != 1:
        return None
    new = b'"requestId":null'
    new += b" " * (len(old) - len(new))
    entry["requestId"] = None
    if json.loads(line.replace(old, new)) != entry:
        return None
    return line.index(old), new


def fix_file(path):
    """Patch one ``.jsonl`` transcript in place. Returns the number of messages fixed."""
    patches = []
    with open(path, "r+b") as file:
        offset = 0
        for line in file:
            patch = fix_line(line)
            if patch:
                patches.append((offset + patch[0], patch[1]))
            offset += len(line)
        for position, data in patches:
            file.seek(position)
            file.write(data)
    return len(patches)


def projects_dir():
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config, "projects")


def _candidates(root, since):
    for folder, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(folder, name)
            try:
                if os.stat(path).st_mtime < since:
                    continue
                with open(path, "rb") as file:
                    if FOREIGN_REQUEST_ID.search(file.read()):
                        yield path
            except OSError:
                pass


def fix_all(root=None, stamp=None, quiet=False):
    """Fix every transcript under ``root`` (default: Claude Code's projects folder).

    With ``stamp`` (a file path), only files modified since the previous run are read, which keeps this fast enough to
    run before every launch. A line still being written during a run is finished after that run started, so its file
    is read again next time.
    """
    root = root or projects_dir()
    if not os.path.isdir(root):
        return 0
    started = time.time()
    since = 0
    if stamp:
        try:
            since = os.stat(stamp).st_mtime - 5  # margin for coarse file system timestamps
        except OSError:
            pass
    total = 0
    for path in _candidates(root, since):
        try:
            count = fix_file(path)
        except OSError as error:
            print(f"claude-handoff: {path}: {error}", file=sys.stderr)
            continue
        if count and not quiet:
            print(f"claude-handoff: fixed {count} message(s) in {path}", file=sys.stderr)
        total += count
    if stamp:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "a"):
            pass
        os.utime(stamp, (started, started))
    return total
