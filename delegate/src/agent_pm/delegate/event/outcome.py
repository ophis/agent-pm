"""An agent run's outcome: read parses an outcome block, reads its deliverable_file in and validates
it against SCHEMA and what SCHEMA can't say. Its text stays untrusted: a path is shown as a repr, at
most 200 characters.
"""

import json
import os
import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from agent_pm.delegate.event.types import STATUSES, InvalidOutcome, Outcome, OutcomeStatus
from agent_pm.utils import ConfinedFileError, ConfinedReason, read_confined

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outcome.schema.json")
SAVES_DELIVERABLE = (
    "local",
    "orchestrator",
)  # destinations whose deliverable comes back in the outcome
PR_PATH = re.compile(r"/[^/]+/[^/]+/(pull/\d+|compare/\S+|tree/\S+)")


def read(body: str, output: Mapping, workdir: str) -> Outcome:
    """`output`: the run's [output] table. Raises InvalidOutcome."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise InvalidOutcome.not_json(e) from None
    if not isinstance(data, dict):
        raise InvalidOutcome.not_object()
    if "deliverable_file" in data:
        if "deliverable" in data:
            raise InvalidOutcome.both_deliverables()
        data = {k: v for k, v in data.items() if k != "deliverable_file"} | {
            "deliverable": _read_file(_file(data["deliverable_file"], workdir), workdir)
        }
    return validate(data, output, workdir)


def validate(data: dict, output: Mapping, workdir: str) -> Outcome:
    """Raises InvalidOutcome."""
    if not isinstance(data, dict):
        raise InvalidOutcome.not_object()
    status, title, summary = data.get("status"), data.get("title"), data.get("summary")
    questions, files, deliverable = (
        data.get("questions") or [],
        data.get("files") or [],
        data.get("deliverable") or "",
    )
    if status not in STATUSES:
        raise InvalidOutcome.status(status)
    if not isinstance(title, str) or not title.strip() or not title.isprintable():
        raise InvalidOutcome.title()
    if not isinstance(summary, str) or not isinstance(deliverable, str):
        raise InvalidOutcome.text_fields()
    if not isinstance(questions, list) or not isinstance(files, list):
        raise InvalidOutcome.lists()
    if status == "needs_input" and not 1 <= len(questions) <= 4:
        raise InvalidOutcome.questions()
    if status == "done" and output["type"] in SAVES_DELIVERABLE and not deliverable.strip():
        raise InvalidOutcome.missing_deliverable(output["type"])
    outcome = Outcome(
        cast(OutcomeStatus, status),
        title.strip(),
        summary,
        [str(q) for q in questions] if status == "needs_input" else [],
        _url(data.get("url") or "", output),
        [_file(p, workdir) for p in files],
        deliverable,
    )
    _conform(data, _schema(), "outcome")
    return outcome


def _schema() -> dict:
    with open(SCHEMA) as f:
        return json.load(f)


def _conform(value, schema: Mapping, where: str) -> None:
    """Only the subset of JSON Schema the outcome schema uses."""
    validators = {"object": _object, "string": _string, "array": _array}
    if validate_kind := validators.get(schema.get("type")):
        validate_kind(value, schema, where)
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidOutcome.enum(where, value, schema["enum"])


def _object(value, schema, where):
    if not isinstance(value, dict):
        raise InvalidOutcome.not_object(where)
    props = schema.get("properties", {})
    if missing := [k for k in schema.get("required", ()) if k not in value]:
        raise InvalidOutcome.missing_field(where, missing[0])
    if schema.get("additionalProperties") is False and (extra := sorted(set(value) - set(props))):
        raise InvalidOutcome.unknown_field(extra[0])
    for key, item in value.items():
        if key in props:
            _conform(item, props[key], key)


def _string(value, schema, where):
    if not isinstance(value, str):
        raise InvalidOutcome.text(where)
    if len(value) < schema.get("minLength", 0):
        raise InvalidOutcome.too_short(where, schema["minLength"])
    if len(value) > schema.get("maxLength", len(value)):
        raise InvalidOutcome.too_long(where, schema["maxLength"])
    if "pattern" in schema and not re.search(schema["pattern"], value):
        raise InvalidOutcome.pattern(where, schema["pattern"])


def _array(value, schema, where):
    if not isinstance(value, list):
        raise InvalidOutcome.list(where)
    if len(value) > schema.get("maxItems", len(value)):
        raise InvalidOutcome.too_many_items(where, schema["maxItems"])
    for index, item in enumerate(value):
        _conform(item, schema.get("items", {}), f"{where}[{index}]")


def _url(url: str, out: Mapping) -> str:
    if not url or out["type"] in SAVES_DELIVERABLE:
        return ""
    parts = urlsplit(url) if isinstance(url, str) else None
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.netloc
        or not url.isprintable()
        or " " in url
        or posixpath.normpath(unquote(parts.path)) != unquote(parts.path)
    ):
        raise InvalidOutcome.plain_url()
    if out["type"] == "github":
        host, prefix = out.get("host", "github.com"), f"/{out['repo']}/blob/{out['branch']}/"
        if parts.netloc != host or not parts.path.startswith(prefix):
            raise InvalidOutcome.url_prefix(host, prefix)
    elif out["type"] == "pull-request" and not PR_PATH.fullmatch(parts.path):
        raise InvalidOutcome.pull_request_url()
    return url


def _file(path, workdir: str) -> str:
    base = Path(workdir).resolve()
    real = (base / path).resolve() if isinstance(path, str) and "\0" not in path else None
    # A hard link could bring an outside file (a key) into the workdir.
    if (
        real is None
        or real.suffix != ".md"
        or not real.is_relative_to(base)
        or not real.is_file()
        or real.stat().st_nlink != 1
    ):
        raise InvalidOutcome.file(path)
    return str(real)


def _read_file(real: str, workdir: str) -> str:
    """The file _file checked, rechecked through the open fd: a swap after the check is refused."""
    cap = 4 * _schema()["properties"]["deliverable"]["maxLength"]
    try:
        data = read_confined(real, workdir, limit=cap)
    except ConfinedFileError as error:
        errors = {
            ConfinedReason.REGULAR: InvalidOutcome.not_regular,
            ConfinedReason.SINGLE_LINK: InvalidOutcome.not_regular,
            ConfinedReason.SUFFIX: InvalidOutcome.file,
            ConfinedReason.OUTSIDE: InvalidOutcome.relocated,
            ConfinedReason.REPLACED: InvalidOutcome.replaced,
        }
        if error.reason is ConfinedReason.TOO_LARGE:
            raise InvalidOutcome.large_file(real, cap) from None
        raise errors[error.reason](real) from None
    except OSError as error:
        raise InvalidOutcome.unreadable(real, error) from None
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise InvalidOutcome.file_encoding(real) from None
