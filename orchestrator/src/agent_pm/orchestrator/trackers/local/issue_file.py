"""A local issue file's text read into an IssueFile: front matter, then the Description, Comments and
History sections (docs/board.md › Local board)."""

import json
import re
from datetime import datetime
from enum import StrEnum

from agent_pm.orchestrator.engine.flow.types import ISSUE_ID
from agent_pm.orchestrator.engine.types import TrackerFileError
from agent_pm.orchestrator.trackers.local.types import (
    Entry,
    FileLink,
    IssueFile,
    LocalState,
    Note,
    Source,
)


class _Key(StrEnum):
    ID = "id"
    TITLE = "title"
    TASK = "task"
    CLIENT = "client"
    PRIORITY = "priority"
    CREATED = "created"
    BLOCKED_BY = "blocked_by"
    RELATED = "related"
    PARENT = "parent"
    LINKS = "links"


class _Section(StrEnum):
    DESCRIPTION = "Description"
    COMMENTS = "Comments"
    HISTORY = "History"


_FRONT = re.compile(r"([a-z_]+):(.*)")
_LINK_TITLE = re.compile(r"  - title:(.*)")
_LINK_URL = re.compile(r"    url:(.*)")
_SECTION = re.compile(r"#{3} (Description|Comments|History)")
_TIME = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
_SOURCE = "(" + "|".join(Source) + ")"
_HEADER = re.compile(rf"#{{4}} {_SOURCE} · {_TIME}(?: <!-- id=\S+ key=(\S+) -->)?")
_BARE = "###"
_HISTORY = re.compile(rf"- {_TIME} (\w+) {_SOURCE}")


class _Bad(Exception):
    """A reason this file cannot be read."""


def parse(
    path: str, text: str, *, assignee: str | None, state: LocalState | None, mtime: datetime
) -> IssueFile:
    """assignee None: archived, its located state its last History entry's; naive times are local."""
    try:
        return _parse(path, text, assignee, state, mtime)
    except _Bad as bad:
        raise TrackerFileError(path, str(bad)) from None


def _parse(path, text, assignee, state, mtime):
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise _Bad("no front matter")
    if "---" not in lines[1:]:
        raise _Bad("front matter has no closing '---'")
    end = lines.index("---", 1)
    front = _front(lines[1:end])
    sections = _sections(lines[end + 1 :])
    history = tuple(
        sorted(map(_entry, _filled(sections.get(_Section.HISTORY, []))), key=lambda e: e.at)
    )
    if assignee is None:
        state = history[-1].state if history else None
    return IssueFile(
        path=path,
        assignee=assignee,
        state=state,
        mtime=mtime,
        id=_id(_Key.ID, _required(front, _Key.ID)),
        title=_required(front, _Key.TITLE),
        task=_scalar(front.get(_Key.TASK, "")),
        client=_scalar(front.get(_Key.CLIENT, "")),
        priority=_priority(front.get(_Key.PRIORITY, "")),
        created=_created(_required(front, _Key.CREATED)),
        blocked_by=_ids(_Key.BLOCKED_BY, front.get(_Key.BLOCKED_BY, "")),
        related=_ids(_Key.RELATED, front.get(_Key.RELATED, "")),
        parent=_optional_id(front.get(_Key.PARENT, "")),
        links=front.get(_Key.LINKS, ()),
        description="\n".join(sections.get(_Section.DESCRIPTION, [])).strip(),
        comments=_comments(sections.get(_Section.COMMENTS, []), mtime),
        history=history,
    )


def _front(lines):
    """Each key's value text; links as FileLinks."""
    front, i = {}, 0
    while i < len(lines):
        line, i = lines[i], i + 1
        if not line.strip():
            continue
        match = _FRONT.fullmatch(line)
        if not match:
            raise _Bad(f"bad front-matter line {line!r}")
        key, value = match[1], match[2].strip()
        if key not in set(_Key):
            raise _Bad(f"unknown key {key!r}")
        if key in front:
            raise _Bad(f"duplicate key {key!r}")
        front[key] = value
        if key == _Key.LINKS:
            front[key], i = _links(value, lines, i)
    return front


def _links(value, lines, i):
    """The `  - title:` / `    url:` pairs from lines[i], and the index after them."""
    if value:
        raise _Bad(f"bad links value {value!r}")
    links = []
    while i < len(lines) and lines[i].startswith(" "):
        title = _LINK_TITLE.fullmatch(lines[i])
        if not title:
            raise _Bad(f"bad links line {lines[i]!r}")
        line = lines[i + 1] if i + 1 < len(lines) else ""
        url = _LINK_URL.fullmatch(line)
        if not url or not _scalar(url[1].strip()):
            raise _Bad(f"bad links line {line or lines[i]!r}")
        links.append(FileLink(_scalar(title[1].strip()) or "", _scalar(url[1].strip())))
        i += 2
    return tuple(links), i


def _scalar(value):
    """Empty → None; a JSON string; else the text."""
    if not value:
        return None
    if not value.startswith('"'):
        return value
    try:
        loaded = json.loads(value)
    except ValueError:
        loaded = None
    if not isinstance(loaded, str):
        raise _Bad(f"bad string {value!r}")
    return loaded


def _required(front, key):
    value = _scalar(front.get(key, ""))
    if not value:
        raise _Bad(f"missing {key}")
    return value


def _id(key, value):
    if not re.fullmatch(ISSUE_ID, value):
        raise _Bad(f"bad {key} {value!r}")
    return value


def _optional_id(value):
    value = _scalar(value)
    return value and _id(_Key.PARENT, value)


def _ids(key, value):
    if not value:
        return ()
    if not (value.startswith("[") and value.endswith("]")):
        raise _Bad(f"bad list {key}: {value!r}")
    items = [item.strip() for item in value[1:-1].split(",")]
    if items == [""]:
        return ()
    return tuple(_id(f"{key} id", item) for item in items)


def _priority(value):
    value = _scalar(value)
    if value is None:
        return 0
    if not re.fullmatch(r"[0-4]", value):
        raise _Bad(f"bad priority {value!r}")
    return int(value)


def _created(value):
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        created = None
    if created is None or created.tzinfo is None:
        raise _Bad(f"bad created {value!r}: want ISO 8601 with an offset or Z")
    return created


def _sections(lines):
    """Each section's lines; only blank lines may come before the first."""
    sections, current = {}, None
    for line in lines:
        match = _SECTION.fullmatch(line)
        if not match:
            if current is None and line.strip():
                raise _Bad(f"text before the first section: {line!r}")
            if current is not None:
                sections[current].append(line)
            continue
        section = _Section(match[1])
        if section in sections:
            raise _Bad(f"duplicate section {section.value!r}")
        later = [s for s in sections if list(_Section).index(s) > list(_Section).index(section)]
        if later:
            raise _Bad(f"section {section.value!r} after {later[0].value!r}")
        sections[section], current = [], section
    return sections


def _local(text):
    """`YYYY-MM-DD HH:MM` in the reading process's zone."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone()


def _comments(lines, mtime):
    notes = []
    for line in lines:
        if line == _BARE:
            notes.append([mtime, Source.OTHER, [], None])
        elif line.startswith("####") or (not notes and line.strip()):
            header = _HEADER.fullmatch(line)
            try:
                at = header and _local(header[2])
            except ValueError:
                at = None
            if not at:
                raise _Bad(f"bad comment header {line!r}")
            notes.append([at, Source(header[1]), [], header[3]])
        elif notes:
            notes[-1][2].append(line)
    return tuple(Note(at, source, "\n".join(body).strip(), key) for at, source, body, key in notes)


def _filled(lines):
    return [line for line in lines if line.strip()]


def _entry(line):
    match = _HISTORY.fullmatch(line)
    try:
        if match:
            return Entry(_local(match[1]), LocalState(match[2]), Source(match[3]))
    except ValueError:
        pass
    raise _Bad(f"bad History line {line!r}")
