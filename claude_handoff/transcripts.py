"""Make sessions that went through another provider resumable on the Anthropic API again.

When resuming, Claude Code sends Anthropic ``diagnostics.previous_message_id``: the id of the last assistant message
that has a ``requestId`` (or, without one, whose id starts with ``msg_``). A message served by another provider has an
id like ``gen-...``; if a proxy passed that provider's request id back, Claude Code stored it as ``requestId`` and the
Anthropic API answers::

    400 diagnostics.previous_message_id: must be the `id` from a prior /v1/messages response

The claude-handoff proxy never passes request ids back, so its own sessions are fine. This module repairs sessions
recorded by other proxies or routers: it sets ``requestId`` to ``null`` on assistant messages whose request id is not
an Anthropic ``req_`` id and whose message id is not an Anthropic ``msg_`` id. Claude Code then skips them and picks
the last genuine Anthropic id.

The edit is done byte for byte, padded with spaces to the same length: no line moves, so a session that is writing to
the file at the same time is not disturbed. Each file is copied to a backup folder before it is changed.
"""

import json
import os
import re
import shutil
import sys
import time

MARK = b'"requestId":"'
# Anthropic request ids start with "req_": anything else came from another provider.
FOREIGN_REQUEST_ID = re.compile(rb'"requestId":"(?!req_)')
KEEP_BACKUP_RUNS = 20


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
    if not isinstance(request_id, str) or request_id.startswith("req_"):
        return None
    if isinstance(message_id, str) and message_id.startswith("msg_"):
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


def scan_file(path):
    """The patches one transcript needs, as ``[(file offset, bytes)]``."""
    patches = []
    with open(path, "rb") as file:
        offset = 0
        for line in file:
            patch = fix_line(line)
            if patch:
                patches.append((offset + patch[0], patch[1]))
            offset += len(line)
    return patches


def fix_file(path, dry_run=False, backup_to=None):
    """Patch one ``.jsonl`` transcript in place. Returns the number of messages fixed (or to fix, with ``dry_run``).

    ``backup_to``: a file path the original is copied to first; the file is left alone if that copy fails.
    """
    patches = scan_file(path)
    if not patches or dry_run:
        return len(patches)
    if backup_to:
        os.makedirs(os.path.dirname(backup_to), mode=0o700, exist_ok=True)
        shutil.copy2(path, backup_to)
    with open(path, "r+b") as file:
        for position, data in patches:
            file.seek(position)
            file.write(data)
    return len(patches)


def projects_dir():
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(config, "projects")


def _candidates(root, since):
    """Transcripts modified since ``since`` that contain a foreign request id. Unreadable ones count as failures."""
    found, failed = [], []
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
                        found.append(path)
            except OSError as error:
                failed.append((path, error))
    return found, failed


def _prune_backups(backup_dir):
    try:
        runs = sorted(name for name in os.listdir(backup_dir) if os.path.isdir(os.path.join(backup_dir, name)))
    except OSError:
        return
    for name in runs[:-KEEP_BACKUP_RUNS]:
        shutil.rmtree(os.path.join(backup_dir, name), ignore_errors=True)


def fix_all(root=None, stamp=None, quiet=False, dry_run=False, backup_dir=None):
    """Fix every transcript under ``root`` (default: Claude Code's projects folder). Returns the number of messages.

    ``stamp`` (a file path): only files modified since the previous successful run are read, which keeps this fast
    enough to run before every launch. A line still being written during a run is finished after that run started,
    so its file is read again next time. The stamp only moves when every file was handled.

    ``backup_dir``: each changed file is first copied to ``<backup_dir>/<run time>/<path under root>``; the last
    20 runs are kept.
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
    paths, failed = _candidates(root, since)
    run_dir = os.path.join(backup_dir, time.strftime("%Y%m%d-%H%M%S")) if backup_dir else None
    total = 0
    for path in paths:
        backup_to = os.path.join(run_dir, os.path.relpath(path, root)) if run_dir else None
        try:
            count = fix_file(path, dry_run=dry_run, backup_to=backup_to)
        except OSError as error:
            failed.append((path, error))
            continue
        if count and not quiet:
            verb = "would fix" if dry_run else "fixed"
            print(f"claude-handoff: {verb} {count} message(s) in {path}", file=sys.stderr)
        total += count
    for path, error in failed:
        print(f"claude-handoff: {path}: {error}", file=sys.stderr)
    if run_dir and total and not dry_run:
        _prune_backups(backup_dir)
    if stamp and not dry_run and not failed:
        os.makedirs(os.path.dirname(stamp), mode=0o700, exist_ok=True)
        os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT, 0o600))
        os.utime(stamp, (started, started))
    return total
