"""Markdown code fences: FENCE matches an opening fence line, unfenced gives the lines outside fences, fenced the
blocks inside them."""
import re

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def unfenced(text: str) -> list[tuple[int, str]]:
    """(line index, line) of each line of `text` outside fences, fence lines excluded. A fence opens at 3+ backticks or
    tildes and closes at a line of only that char, as many times or more; one never closed runs to the end."""
    out, fence = [], None
    for i, line in enumerate(text.split("\n")):
        shut = line.strip()
        if fence:
            if len(shut) >= fence[1] and shut == fence[0] * len(shut):
                fence = None
        elif m := FENCE.match(line):
            fence = (m[1][0], len(m[1]))
        else:
            out.append((i, line))
    return out


def fenced(text: str) -> list[tuple[str, str]]:
    """(info string, body) of each fenced block of `text`, in order: fences open and close as in unfenced; the info
    string is the rest of the opening line, stripped; the body the lines between the fence lines (to the end when never
    closed), so a fence line inside a longer block is body."""
    out, fence, info, body = [], None, "", []
    for line in text.split("\n"):
        shut = line.strip()
        if fence:
            if len(shut) >= fence[1] and shut == fence[0] * len(shut):
                out.append((info, "\n".join(body)))
                fence = None
            else:
                body.append(line)
        elif m := FENCE.match(line):
            fence, info, body = (m[1][0], len(m[1])), line[m.end():].strip(), []
    if fence:
        out.append((info, "\n".join(body)))
    return out
