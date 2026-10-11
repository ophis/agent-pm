"""What the Delegate reads in a reply's text (FR-18): its progress mark lines outside code fences (marks) and its
outcome block (outcome_block), fences as utils/markdown.py finds them; composer names both (PROGRESS, OUTCOME)."""
import re

from composer import OUTCOME, PROGRESS
from utils.markdown import fenced, unfenced

MARK = re.compile(rf"^\s*(?:[-*] )?\[{PROGRESS}:(?P<name>[^\]\s]*)\](?P<text>.*)$")   # a mark line
NAME = re.compile(r"[\w-]+")   # a mark's name, whole


def marks(text: str) -> list[tuple[str, str]]:
    """(name, text) of each mark line of `text` outside fences, in order, the text's whitespace runs joined by one space
    and stripped; a line whose name fails NAME or whose text is then empty is skipped."""
    out = []
    for _, line in unfenced(text):
        if (m := MARK.match(line)) and NAME.fullmatch(m["name"]) and (said := " ".join(m["text"].split())):
            out.append((m["name"], said))
    return out


def outcome_block(text: str) -> str | None:
    """The body of the last fenced block of `text` whose info string, stripped, is OUTCOME; None when there is none."""
    return next((body for info, body in reversed(fenced(text)) if info == OUTCOME), None)
