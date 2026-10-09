import copy
import unittest

from claude_handoff.compat import normalize


def reminder(text):
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


class NormalizeTest(unittest.TestCase):
    def test_plain_request_is_unchanged(self):
        payload = {
            "model": "some/model",
            "system": [{"type": "text", "text": "You are a coding agent."}],
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
                {"role": "user", "content": [{"type": "text", "text": "list the files"}]},
            ],
        }
        expected = copy.deepcopy(payload)
        self.assertEqual(normalize(payload), expected)

    def test_anthropic_only_fields_are_dropped(self):
        payload = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "diagnostics": {"previous_message_id": "msg_01ABC"},
            "thread": {"id": "thr_1"},
        }
        normalize(payload)
        self.assertNotIn("diagnostics", payload)
        self.assertNotIn("thread", payload)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])

    def test_system_after_user_joins_that_user_message(self):
        payload = {"messages": [
            {"role": "user", "content": "fix the bug"},
            {"role": "system", "content": [
                {"type": "text", "text": "The Bash tool is now available."},
                {"type": "tool_addition", "tool": {"name": "Bash"}},
            ]},
        ]}
        normalize(payload)
        self.assertEqual(payload["messages"], [
            {"role": "user", "content": [
                {"type": "text", "text": "fix the bug"},
                reminder("The Bash tool is now available."),
            ]},
        ])

    def test_system_after_assistant_goes_after_tool_results_of_next_user(self):
        tool_result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
        payload = {"messages": [
            {"role": "user", "content": "run the tests"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]},
            {"role": "system", "content": "Tool list changed."},
            {"role": "user", "content": [tool_result]},
        ]}
        normalize(payload)
        self.assertEqual(len(payload["messages"]), 3)
        self.assertEqual(payload["messages"][2], {"role": "user", "content": [
            tool_result, reminder("Tool list changed."),
        ]})

    def test_system_first_waits_for_first_user(self):
        payload = {"messages": [
            {"role": "system", "content": "Context."},
            {"role": "user", "content": "hello"},
        ]}
        normalize(payload)
        self.assertEqual(payload["messages"], [{"role": "user", "content": [
            {"type": "text", "text": "hello"}, reminder("Context."),
        ]}])

    def test_trailing_system_after_assistant_becomes_user_message(self):
        payload = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "Session resumed."},
        ]}
        normalize(payload)
        self.assertEqual(payload["messages"][-1], {"role": "user", "content": [reminder("Session resumed.")]})
        self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant", "user"])

    def test_system_with_only_tool_blocks_is_dropped(self):
        payload = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "tool_removal", "tool": {"name": "WebFetch"}}]},
            {"role": "assistant", "content": "hello"},
        ]}
        normalize(payload)
        self.assertEqual(payload["messages"], [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])

    def test_no_system_role_survives(self):
        payload = {"messages": [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "system", "content": "c"},
            {"role": "assistant", "content": "d"},
            {"role": "system", "content": [{"type": "text", "text": "e"}, {"type": "text", "text": "  "}]},
            {"role": "user", "content": "f"},
            {"role": "system", "content": "g"},
        ]}
        normalize(payload)
        roles = [m["role"] for m in payload["messages"]]
        self.assertNotIn("system", roles)
        self.assertEqual(roles, ["user", "assistant", "user"])
        texts = [b["text"] for m in payload["messages"] if m["role"] == "user" for b in m["content"]]
        self.assertEqual(texts, ["b", reminder("a")["text"], reminder("c")["text"],
                                 "f", reminder("e")["text"], reminder("g")["text"]])

    def test_input_messages_are_not_mutated(self):
        user = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        payload = {"messages": [user, {"role": "system", "content": "note"}]}
        normalize(payload)
        self.assertEqual(user, {"role": "user", "content": [{"type": "text", "text": "hi"}]})

    def test_non_dict_payload_passes_through(self):
        self.assertEqual(normalize([1, 2]), [1, 2])
        self.assertIsNone(normalize(None))


if __name__ == "__main__":
    unittest.main()
