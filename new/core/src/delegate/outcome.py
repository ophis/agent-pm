"""An agent run's outcome (FR-19, FR-20): read parses an outcome block's body (JSON), swaps its deliverable_file for
that file's text and validates the result (validate: the Composer's outcome schema, SCHEMA, read on each call, and what
it can't say); save writes a file atomically. Ported from the old core's driver (validate, _conform, _url, _file, save)
with identical rules and messages. An outcome's text stays untrusted: an agent's path is shown as its repr, at most 200
characters."""
import json
import os
import posixpath
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import unquote, urlsplit

from composer import SCHEMA, RunConfig

Status = Literal["done", "needs_input", "failed"]
STATUSES = get_args(Status)
SAVES_DELIVERABLE = ("local", "orchestrator")   # destinations whose deliverable comes back in the outcome
PR_PATH = re.compile(r"/[^/]+/[^/]+/(pull/\d+|compare/\S+|tree/\S+)")


class InvalidOutcome(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    status: Status
    title: str
    summary: str
    questions: list[str] = field(default_factory=list)
    url: str = ""
    files: list[str] = field(default_factory=list)
    deliverable: str = ""


def read(body: str, run: RunConfig, workdir: str) -> Outcome:
    """The outcome an outcome block's `body` holds, its deliverable_file (a path, as validate's files) read in as its
    deliverable; raises InvalidOutcome."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise InvalidOutcome(f"not JSON: {e}") from None
    if not isinstance(data, dict):
        raise InvalidOutcome("not an object")
    if "deliverable_file" in data:
        if "deliverable" in data:
            raise InvalidOutcome("give deliverable or deliverable_file, not both")
        data = {k: v for k, v in data.items() if k != "deliverable_file"} | {
            "deliverable": _read_file(_file(data["deliverable_file"], workdir), workdir)}
    return validate(data, run, workdir)


def validate(data: dict, run: RunConfig, workdir: str) -> Outcome:
    """The outcome an agent run returned, once it holds; raises InvalidOutcome. Checks the schema (SCHEMA) and what it
    can't. Its text stays untrusted."""
    if not isinstance(data, dict):
        raise InvalidOutcome("not an object")
    status, title, summary = data.get("status"), data.get("title"), data.get("summary")
    questions, files, deliverable = data.get("questions") or [], data.get("files") or [], data.get("deliverable") or ""
    if status not in STATUSES:
        raise InvalidOutcome(f"status {status!r} is not one of {', '.join(STATUSES)}")
    if not isinstance(title, str) or not title.strip() or not title.isprintable():
        raise InvalidOutcome("title must be one line of printable text")
    if not isinstance(summary, str) or not isinstance(deliverable, str):
        raise InvalidOutcome("summary and deliverable must be text")
    if not isinstance(questions, list) or not isinstance(files, list):
        raise InvalidOutcome("questions and files must be lists")
    if status == "needs_input" and not 1 <= len(questions) <= 4:
        raise InvalidOutcome("needs_input must carry 1–4 questions")
    if status == "done" and run.output["type"] in SAVES_DELIVERABLE and not deliverable.strip():
        raise InvalidOutcome(f"done with a {run.output['type']} destination must carry the deliverable")
    outcome = Outcome(status, title.strip(), summary, [str(q) for q in questions] if status == "needs_input" else [],
                      _url(data.get("url") or "", run), [_file(p, workdir) for p in files], deliverable)
    _conform(data, _schema(), "outcome")
    return outcome


def _schema() -> dict:
    with open(SCHEMA) as f:
        return json.load(f)


def _conform(value, schema: Mapping, where: str) -> None:
    """`value` against the subset of JSON Schema the outcome schema uses."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise InvalidOutcome(f"{where} must be an object")
        props = schema.get("properties", {})
        if missing := [k for k in schema.get("required", ()) if k not in value]:
            raise InvalidOutcome(f"{where} lacks {missing[0]!r}")
        if schema.get("additionalProperties") is False and (extra := sorted(set(value) - set(props))):
            raise InvalidOutcome(f"unknown field {extra[0][:100]!r}")
        for k, v in value.items():
            if k in props:
                _conform(v, props[k], k)
    elif kind == "string":
        if not isinstance(value, str):
            raise InvalidOutcome(f"{where} must be text")
        if len(value) < schema.get("minLength", 0):
            raise InvalidOutcome(f"{where} is shorter than {schema['minLength']} characters")
        if len(value) > schema.get("maxLength", len(value)):
            raise InvalidOutcome(f"{where} is longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise InvalidOutcome(f"{where} must match {schema['pattern']}")
    elif kind == "array":
        if not isinstance(value, list):
            raise InvalidOutcome(f"{where} must be a list")
        if len(value) > schema.get("maxItems", len(value)):
            raise InvalidOutcome(f"{where} has more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            _conform(v, schema.get("items", {}), f"{where}[{i}]")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidOutcome(f"{where} {value!r} is not one of {', '.join(map(str, schema['enum']))}")


def _url(url: str, run: RunConfig) -> str:
    """The url, once it is a plain https link the agent run's destination can produce."""
    out = run.output
    if not url or out["type"] in SAVES_DELIVERABLE:
        return ""
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme != "https" or not parts.netloc or not url.isprintable() or " " in url
            or posixpath.normpath(unquote(parts.path)) != unquote(parts.path)):
        raise InvalidOutcome("url must be a plain https link")
    if out["type"] == "github":
        host, prefix = out.get("host", "github.com"), f"/{out['repo']}/blob/{out['branch']}/"
        if parts.netloc != host or not parts.path.startswith(prefix):
            raise InvalidOutcome(f"url must start with https://{host}{prefix}")
    elif out["type"] == "pull-request" and not PR_PATH.fullmatch(parts.path):
        raise InvalidOutcome("url must be a pull request, compare or tree link")
    return url


def _file(path, workdir: str) -> str:
    """A regular .md file under the workdir with one link, symlinks resolved; a relative path is the workdir's."""
    base = Path(workdir).resolve()
    real = (base / path).resolve() if isinstance(path, str) and "\0" not in path else None
    # A hard link is the same file under another name: it could carry, e.g., a key into the workdir.
    if (real is None or real.suffix != ".md" or not real.is_relative_to(base) or not real.is_file()
            or real.stat().st_nlink != 1):
        raise InvalidOutcome(f"file {str(path)[:200]!r} is not a single-link .md file under the workdir")
    return str(real)


def _read_file(real: str, workdir: str) -> str:
    """The UTF-8 text of the file _file checked at `real`, read without a check-then-use race: opened without following
    a symlink at `real` or blocking on a FIFO, then checked again through the open file (a regular file with one link,
    `real` still under the workdir and still that file); at most 4 bytes per character of the schema's deliverable
    maxLength."""
    def invalid(reason: str) -> InvalidOutcome:
        return InvalidOutcome(f"deliverable_file {str(real)[:200]!r}: {reason}")

    cap = 4 * _schema()["properties"]["deliverable"]["maxLength"]
    try:
        with os.fdopen(os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            opened = os.fstat(f.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise invalid("not a single-link regular file")
            if not Path(os.path.realpath(real)).is_relative_to(os.path.realpath(workdir)):
                raise invalid("no longer under the workdir")
            now = os.stat(real)
            if (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
                raise invalid("replaced while it was opened")
            data = f.read(cap + 1)
    except OSError as e:
        raise invalid(e.strerror or "unreadable") from None
    if len(data) > cap:
        raise invalid(f"too large (over {cap} bytes)")
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise invalid("not UTF-8 text") from None


# The agent run controls its workdir, where the Delegate writes too: writes replace a symlink planted at the path, never
# follow it.
def save(path: str | Path, text: str) -> None:
    """Writes `text` to `path` atomically, through a temp file renamed over it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".tmp-", delete=False) as f:
        try:
            f.write(text)
            f.close()
            os.replace(f.name, path)
        except BaseException:
            os.unlink(f.name)
            raise
