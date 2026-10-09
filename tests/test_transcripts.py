import contextlib
import io
import json
import os
import stat
import tempfile
import time
import unittest

from claude_handoff import transcripts


def line(entry):
    return (json.dumps(entry, separators=(",", ":")) + "\n").encode()


ANTHROPIC = {"type": "assistant", "requestId": "req_011abc", "message": {"id": "msg_01XYZ", "role": "assistant"}}
FOREIGN = {"type": "assistant", "requestId": "gen-1760000000-abcdef", "message": {"id": "gen-1760000000-abcdef"}}
USER = {"type": "user", "message": {"role": "user", "content": "hi"}}


def read(path):
    with open(path, "rb") as file:
        return file.read()


class FixTranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, entries):
        path = os.path.join(self.root, name)
        with open(path, "wb") as file:
            for entry in entries:
                file.write(line(entry))
        return path

    def test_foreign_request_id_is_nulled_in_place(self):
        path = self.write("s.jsonl", [USER, ANTHROPIC, USER, FOREIGN])
        before = read(path)
        self.assertEqual(transcripts.fix_file(path), 1)
        after = read(path)
        self.assertEqual(len(before), len(after))
        self.assertEqual(before.count(b"\n"), after.count(b"\n"))
        entries = [json.loads(l) for l in after.splitlines()]
        self.assertIsNone(entries[3]["requestId"])
        self.assertEqual(entries[1]["requestId"], "req_011abc")
        self.assertEqual(entries[3]["message"], FOREIGN["message"])

    def test_anthropic_messages_are_left_alone(self):
        path = self.write("s.jsonl", [USER, ANTHROPIC])
        before = read(path)
        self.assertEqual(transcripts.fix_file(path), 0)
        self.assertEqual(read(path), before)

    def test_partial_last_line_is_left_alone(self):
        path = self.write("s.jsonl", [USER])
        with open(path, "ab") as file:
            file.write(line(FOREIGN)[:40])
        before = read(path)
        self.assertEqual(transcripts.fix_file(path), 0)
        self.assertEqual(read(path), before)

    def test_fix_all_is_idempotent_and_uses_the_stamp(self):
        os.makedirs(os.path.join(self.root, "projects", "p"))
        path = os.path.join(self.root, "projects", "p", "s.jsonl")
        with open(path, "wb") as file:
            file.write(line(USER) + line(FOREIGN))
        stamp = os.path.join(self.root, "state", "stamp")
        projects = os.path.join(self.root, "projects")
        self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 1)
        self.assertEqual(stat.S_IMODE(os.stat(stamp).st_mode), 0o600)
        self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 0)
        # A file older than the stamp is not read again, even if it still has a foreign id.
        other = os.path.join(self.root, "projects", "p", "old.jsonl")
        with open(other, "wb") as file:
            file.write(line(FOREIGN))
        old = time.time() - 3600
        os.utime(other, (old, old))
        self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 0)
        self.assertEqual(transcripts.fix_all(projects, quiet=True), 1)

    def test_dry_run_changes_nothing(self):
        os.makedirs(os.path.join(self.root, "projects", "p"))
        path = os.path.join(self.root, "projects", "p", "s.jsonl")
        with open(path, "wb") as file:
            file.write(line(USER) + line(FOREIGN))
        before = read(path)
        stamp = os.path.join(self.root, "state", "stamp")
        self.assertEqual(transcripts.fix_all(os.path.join(self.root, "projects"), stamp=stamp, quiet=True,
                                             dry_run=True), 1)
        self.assertEqual(read(path), before)
        self.assertFalse(os.path.exists(stamp))

    def test_original_is_backed_up_before_the_change(self):
        projects = os.path.join(self.root, "projects")
        os.makedirs(os.path.join(projects, "p"))
        path = os.path.join(projects, "p", "s.jsonl")
        with open(path, "wb") as file:
            file.write(line(USER) + line(FOREIGN))
        before = read(path)
        backups = os.path.join(self.root, "backups")
        self.assertEqual(transcripts.fix_all(projects, quiet=True, backup_dir=backups), 1)
        runs = os.listdir(backups)
        self.assertEqual(len(runs), 1)
        self.assertEqual(read(os.path.join(backups, runs[0], "p", "s.jsonl")), before)
        self.assertNotEqual(read(path), before)

    def test_failed_backup_leaves_the_file_alone(self):
        path = self.write("s.jsonl", [USER, FOREIGN])
        before = read(path)
        blocker = os.path.join(self.root, "not-a-dir")
        with open(blocker, "w"):
            pass
        with self.assertRaises(OSError):
            transcripts.fix_file(path, backup_to=os.path.join(blocker, "s.jsonl"))
        self.assertEqual(read(path), before)

    def test_stamp_does_not_move_when_a_file_fails(self):
        projects = os.path.join(self.root, "projects")
        os.makedirs(projects)
        good = os.path.join(projects, "good.jsonl")
        bad = os.path.join(projects, "bad.jsonl")
        for path in (good, bad):
            with open(path, "wb") as file:
                file.write(line(FOREIGN))
        os.chmod(bad, 0o000)
        stamp = os.path.join(self.root, "state", "stamp")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 1)
        self.assertFalse(os.path.exists(stamp))
        os.chmod(bad, 0o600)
        self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 1)
        self.assertTrue(os.path.exists(stamp))

    def test_litellm_style_entries_are_left_alone(self):
        # LiteLLM stores no requestId and an id that starts with msg_: nothing for this rule to change.
        entry = {"type": "assistant", "requestId": None, "message": {"id": "msg_5b501c9d-0225-4eb3-a7e0-0cad99cf4135"}}
        path = self.write("s.jsonl", [USER, entry])
        self.assertEqual(transcripts.fix_file(path), 0)

    def test_missing_folder(self):
        self.assertEqual(transcripts.fix_all(os.path.join(self.root, "nope"), quiet=True), 0)


if __name__ == "__main__":
    unittest.main()
