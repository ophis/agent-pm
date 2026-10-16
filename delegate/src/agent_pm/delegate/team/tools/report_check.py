#!/usr/bin/env python3
"""Check research report display width without changing the report."""

import html
import re
import sys
import unicodedata

LINE_LIMIT = 80
CELL_LIMIT = 30
_TAG = r"</?[A-Za-z][^>]*>"
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_TYPE = re.compile(r"^[^:：]+[:：].*·\s*(?:web|local|mixed)(?:\s*·.*)?$")
_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")


def _plain(text):
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<?https?://[^\s<>]+>?", "", text)
    text = re.sub(_TAG, "", text)
    parts = re.split(r"(`+[^`]*`+|\\.)", text)
    for i, part in enumerate(parts):
        if part.startswith("`"):
            parts[i] = part.strip("`")
        elif part.startswith("\\"):
            parts[i] = part[1:]
        else:
            parts[i] = re.sub(r"(?<!\w)[*_~]+|[*_~]+(?!\w)", "", part)
    return " ".join(html.unescape("".join(parts)).split())


def _cells(line):
    cells, cell = [], []
    code = 0
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            cell.extend(line[i : i + 2])
            i += 2
            continue
        if char == "`":
            end = i + 1
            while end < len(line) and line[end] == "`":
                end += 1
            size = end - i
            code = 0 if code == size else code if code else size
            cell.append(line[i:end])
            i = end
            continue
        if char == "|" and not code:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        i += 1
    cells.append("".join(cell).strip())
    if line.startswith("|"):
        cells.pop(0)
    if line.endswith("|") and not line.endswith("\\|"):
        cells.pop()
    return cells


def _unquote(line):
    return re.sub(r"^\s*(?:>\s?)+", "", line).strip()


def _separator(line):
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def check(text):
    failures = []
    pending = []
    start, kind = 0, "paragraph"

    def measure(line, category, value, limit):
        value = _plain(value)
        width = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)
        if width > limit:
            failures.append(f"line {line}: {category} {width}>{limit}: {value[:60]}")

    def flush():
        if pending:
            measure(start, kind, " ".join(pending), LINE_LIMIT)
            pending.clear()

    lines = text.splitlines()
    fence = ""
    table = False
    after_title = False
    for number, raw in enumerate(lines, 1):
        line = _unquote(raw)
        marker = re.match(r"^(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
            continue
        if marker:
            flush()
            fence = marker[1]
            table = after_title = False
            continue
        if not line:
            flush()
            table = False
            continue
        if re.match(r"^#{1,6}\s", line):
            flush()
            after_title = bool(re.match(r"^#\s", line))
            table = False
            continue
        if after_title and _TYPE.fullmatch(line):
            flush()
            after_title = False
            continue
        after_title = False
        if (
            re.fullmatch(rf"(?:{_TAG}\s*)+", line)
            or re.fullmatch(r"\[![A-Z]+\]", line)
            or re.match(r"^\[[^\]]+\]:\s*\S", line)
            or re.fullmatch(r"(?:[-*_]\s*){3,}", line)
        ):
            flush()
            table = False
            continue
        next_line = _unquote(lines[number]) if number < len(lines) else ""
        if "|" in line and (table or _separator(next_line)):
            flush()
            table = True
            if not _separator(line):
                cells = _cells(line)
                for column, cell in enumerate(cells):
                    limit = LINE_LIMIT if column == len(cells) - 1 else CELL_LIMIT
                    measure(number, "cell", cell, limit)
            continue
        table = False
        item = _LIST.match(line)
        if item:
            flush()
            start, kind = number, "list"
            pending.append(line[item.end() :])
        else:
            if not pending:
                start, kind = number, "paragraph"
            pending.append(line)
    flush()
    return failures


def main(argv: list[str], *, out=sys.stdout) -> int:
    if len(argv) != 1:
        print("check failed: usage: report_check.py FILE", file=out)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as file:
            text = file.read()
    except (OSError, UnicodeError) as error:
        print(f"check failed: {error}", file=out)
        return 2
    failures = check(text)
    print("\n".join(failures) or "ok", file=out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
