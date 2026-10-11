"""The Composer's paths, its error and the text helpers its other modules share."""
import os
import re
from collections.abc import Mapping

from utils.markdown import unfenced

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # <scripts>'s value
CONFIG = os.path.join(os.path.dirname(SRC), "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"
TEAM = os.path.join(os.path.dirname(SRC), "team")   # the built-in team root
OUTPUT = "output"
SCHEMA = os.path.join(TEAM, OUTPUT, "outcome.schema.json")
PLACEHOLDER = re.compile(r"\{\{(\w+)(?:\|([^{}]*))?\}\}")   # {{name}} or {{name|default}}
METHOD = re.compile(r"<methods>/([\w-]+)\.md")   # a method file named in a text
HEADING = re.compile(r"^(#{1,6}) (.+)$")
BOLD_ITEM = re.compile(r"^\s*(?:(?:[-*]|\d+\.) )?\*\*(.+?)\*\*")
ANCHOR_LINE = re.compile(r'<a id="([^"]+)"></a>')
# Section references (the prompt's reference check).
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
FILE_REF = re.compile(r"`<(tasks|methods)>/(<task>|[\w-]+)\.md`")
PAREN = re.compile(r"\(([^()]*)\)")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)


class ConfigError(Exception):
    pass


def fill(text: str, values: Mapping, where: str) -> str:
    def value(m):
        if m.group(1) in values:
            return str(values[m.group(1)])
        return m.group(2) if m.group(2) is not None else m.group(0)
    out = PLACEHOLDER.sub(value, text)
    if m := PLACEHOLDER.search(out):
        raise ConfigError(f"unfilled placeholder {m.group(0)} in {where}")
    return out


def anchor(heading: str) -> str:
    """GitHub's heading anchor."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def outline(text: str) -> list[tuple[int, tuple[str, ...], str, bool]]:
    """(line index, path, anchor, is_heading) of each heading and bold item of `text` outside fences, in order. A
    heading's path is its ancestors' titles then its own, its anchor the `<a id>` line right before it, else
    `anchor(title)`; a bold item (`**name**` opening its line after a list marker, `name` less a trailing `:` or `.`)
    has its section's path plus its name and its section's anchor."""
    out, stack, section, last = [], [], "", (-2, "")
    for i, line in unfenced(text):
        if m := HEADING.match(line):
            level, title = len(m[1]), m[2].strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            explicit = ANCHOR_LINE.fullmatch(last[1]) if last[0] == i - 1 else None
            section = explicit[1] if explicit else anchor(title)
            out.append((i, tuple(t for _, t in stack), section, True))
        elif m := BOLD_ITEM.match(line):
            out.append((i, tuple(t for _, t in stack) + (re.sub(r"[:.]$", "", m[1]),), section, False))
        last = (i, line)
    return out


def _text(path: str) -> str:
    """File `path`'s text, stripped, plus a newline; ConfigError naming it when it can't be read."""
    try:
        with open(path) as f:
            return f.read().strip() + "\n"
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None


def _read(root: str, rel: str) -> str:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise ConfigError(f"missing file {rel}")
    with open(path) as f:
        return f.read().strip() + "\n"


def _without(text: str, placeholder: str) -> str:
    """`text` less each line holding `placeholder`."""
    return "".join(line for line in text.splitlines(True) if placeholder not in line)


def _title(text: str, rel: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    if not first.startswith("# "):
        raise ConfigError(f"{rel}: must start with a '# ' heading")
    return first[2:].strip()


def _fence(text: str, least: int = 3) -> str:
    ticks = max([len(r) + 1 for r in re.findall(r"`+", text)] + [least])
    return "`" * ticks


def _code(value: str) -> str:
    """`value` as Markdown inline code."""
    f, pad = _fence(value, 1), " " if "`" in value else ""
    return f"{f}{pad}{value}{pad}{f}"
