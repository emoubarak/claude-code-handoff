import json
import os
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
        self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 0)
        # A file older than the stamp is not read again, even if it still has a foreign id.
        other = os.path.join(self.root, "projects", "p", "old.jsonl")
        with open(other, "wb") as file:
            file.write(line(FOREIGN))
        old = time.time() - 3600
        os.utime(other, (old, old))
        self.assertEqual(transcripts.fix_all(projects, stamp=stamp, quiet=True), 0)
        self.assertEqual(transcripts.fix_all(projects, quiet=True), 1)

    def test_missing_folder(self):
        self.assertEqual(transcripts.fix_all(os.path.join(self.root, "nope"), quiet=True), 0)


if __name__ == "__main__":
    unittest.main()
