"""Make Claude Code's Messages requests acceptable to providers other than Anthropic.

A session started on the Anthropic API and resumed through another provider replays parts of the request
that only Anthropic understands. OpenRouter, for one, rejects them with
``400 Invalid Anthropic Messages API request``:

- ``role: "system"`` messages in the middle of the conversation, carrying ``tool_addition`` /
  ``tool_removal`` blocks next to plain text;
- top-level ``diagnostics`` and ``thread`` fields, which point at Anthropic message ids.

``normalize`` drops the Anthropic-only fields and folds the text of each system message into a user message as a
``<system-reminder>`` block, which is how Claude Code itself delivers that text when it does not use system
messages. Nothing the model should read is lost.
"""

ANTHROPIC_ONLY_FIELDS = ("diagnostics", "thread")


def _text_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip()
    ]


def _as_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content) if isinstance(content, list) else []


def _reminder(text):
    return {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}


def normalize(payload):
    """Rewrite a ``/v1/messages`` request body in place and return it.

    Requests that contain nothing Anthropic-specific come back unchanged, so this is safe to run on every request.
    """
    if not isinstance(payload, dict):
        return payload
    for field in ANTHROPIC_ONLY_FIELDS:
        payload.pop(field, None)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not any(_is_system(m) for m in messages):
        return payload

    result = []
    pending = []  # text from a system message that follows an assistant turn: goes into the next user message
    for message in messages:
        if not _is_system(message):
            if pending and isinstance(message, dict) and message.get("role") == "user":
                # Appended, not prepended: tool_result blocks must stay first in a user message.
                message = {**message, "content": _as_blocks(message.get("content")) + pending}
                pending = []
            result.append(message)
            continue
        reminders = [_reminder(block["text"]) for block in _text_blocks(message.get("content"))]
        if not reminders:
            continue
        last = result[-1] if result else None
        if isinstance(last, dict) and last.get("role") == "user":
            # The system message directly follows a user message: keep its text at the same point in the conversation.
            result[-1] = {**last, "content": _as_blocks(last.get("content")) + reminders}
        else:
            pending.extend(reminders)
    if pending:
        result.append({"role": "user", "content": pending})
    payload["messages"] = result
    return payload


def _is_system(message):
    return isinstance(message, dict) and message.get("role") == "system"
