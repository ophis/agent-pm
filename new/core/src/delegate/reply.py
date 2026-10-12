"""A reply's progress mark lines outside code fences (marks) and its outcome block (outcome_block)."""

import re

from composer import OUTCOME, PROGRESS
from utils.markdown import fenced, unfenced

MARK = re.compile(rf"^\s*(?:[-*] )?\[{PROGRESS}:(?P<name>[^\]\s]*)\](?P<text>.*)$")
NAME = re.compile(r"[\w-]+")


def marks(text: str) -> list[tuple[str, str]]:
    """(name, text) of each mark line, whitespace collapsed; one with a bad name or no text is skipped."""
    out = []
    for _, line in unfenced(text):
        if (
            (m := MARK.match(line))
            and NAME.fullmatch(m["name"])
            and (said := " ".join(m["text"].split()))
        ):
            out.append((m["name"], said))
    return out


def outcome_block(text: str) -> str | None:
    """The body of the last block fenced as OUTCOME, else None."""
    return next((body for info, body in reversed(fenced(text)) if info == OUTCOME), None)
