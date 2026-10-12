def printable(text: str) -> str:
    """Without control characters but tabs, so agent output can't steer the terminal."""
    return "".join(c for c in text if c.isprintable() or c == "\t")
