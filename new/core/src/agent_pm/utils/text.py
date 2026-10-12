def printable(text: str) -> str:
    """Without control characters but tabs."""
    return "".join(c for c in text if c.isprintable() or c == "\t")
