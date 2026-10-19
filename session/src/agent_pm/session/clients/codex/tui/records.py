"""The one reader of a Codex session file's message records."""

TEXT = {"user": "input_text", "assistant": "output_text"}  # role → its text blocks' type


def message(record: dict) -> tuple[str, str] | None:
    """A user or assistant `response_item` message's role and its text blocks joined; else None."""
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if payload.get("type") != "message" or role not in TEXT:
        return None
    content = payload.get("content")
    text = "\n".join(
        block["text"]
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict)
        and block.get("type") == TEXT[role]
        and isinstance(block.get("text"), str)
    )
    return role, text
