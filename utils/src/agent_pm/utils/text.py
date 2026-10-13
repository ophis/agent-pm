def printable(text: str) -> str:
    """Without control characters but tabs."""
    return "".join(c for c in text if c.isprintable() or c == "\t")


def sid_error(sid) -> str | None:
    if isinstance(sid, str) and sid and sid.isprintable() and "/" not in sid:
        return None
    return f'sid must be a non-empty printable string without "/", got {sid!r:.80}'
