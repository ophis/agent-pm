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
from urllib.parse import unquote, urlsplit

from agent_pm.delegate.event.types import (
    STATUSES,
    Autopilot,
    AutopilotStatus,
    InvalidOutcome,
    Outcome,
    OutcomeStatus,
)
from agent_pm.utils import ConfinedFileError, ConfinedReason, read_confined

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outcome.schema.json")
SAVES_DELIVERABLE = (
    "local",
    "orchestrator",
)  # destinations whose deliverable comes back in the outcome
DEFAULT_HOST = "github.com"
PULL = re.compile(r"/[^/]+/[^/]+/pull/\d+")
PR_PATH = re.compile(rf"{PULL.pattern}|/[^/]+/[^/]+/(compare|tree)/\S+")


def read(body: str, output: Mapping, workdir: str) -> tuple[Outcome, Autopilot | None]:
    """`output`: the run's [output] table; the Autopilot is the outcome's `autopilot`, None when it
    has none. Raises InvalidOutcome.
    """
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
            "deliverable": _read_file(_file(data["deliverable_file"], workdir), workdir)
        }
    return validate(data, output, workdir), _autopilot(data.get("autopilot"))


def _autopilot(found) -> Autopilot | None:
    """`found`: a validated outcome's `autopilot`."""
    return None if found is None else Autopilot(AutopilotStatus(found["status"]), found["head"])


def validate(data: dict, output: Mapping, workdir: str) -> Outcome:
    """Raises InvalidOutcome."""
    if not isinstance(data, dict):
        raise InvalidOutcome("not an object")
    status, title, summary = data.get("status"), data.get("title"), data.get("summary")
    questions, files, deliverable = (
        data.get("questions") or [],
        data.get("files") or [],
        data.get("deliverable") or "",
    )
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
    if status == "done" and output["type"] in SAVES_DELIVERABLE and not deliverable.strip():
        raise InvalidOutcome(f"done with a {output['type']} destination must carry the deliverable")
    outcome = Outcome(
        OutcomeStatus(status),
        title.strip(),
        summary,
        [str(q) for q in questions] if status == "needs_input" else [],
        _url(data.get("url") or "", output),
        [_file(p, workdir) for p in files],
        deliverable,
    )
    _conform(data, _schema(), "outcome")
    return outcome


def summary_max() -> int:
    return _schema()["properties"]["summary"]["maxLength"]


def _schema() -> dict:
    with open(SCHEMA) as f:
        return json.load(f)


def _conform(value, schema: Mapping, where: str) -> None:
    """Only the subset of JSON Schema the outcome schema uses."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise InvalidOutcome(f"{where} must be an object")
        props = schema.get("properties", {})
        if missing := [k for k in schema.get("required", ()) if k not in value]:
            raise InvalidOutcome(f"{where} lacks {missing[0]!r}")
        if schema.get("additionalProperties") is False and (
            extra := sorted(set(value) - set(props))
        ):
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
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise InvalidOutcome(f"{where} must match {schema['pattern']}")
    elif kind == "array":
        if not isinstance(value, list):
            raise InvalidOutcome(f"{where} must be a list")
        if len(value) > schema.get("maxItems", len(value)):
            raise InvalidOutcome(f"{where} has more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            _conform(v, schema.get("items", {}), f"{where}[{i}]")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidOutcome(
            f"{where} {value!r} is not one of {', '.join(map(str, schema['enum']))}"
        )


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
        raise InvalidOutcome("url must be a plain https link")
    if out["type"] == "github":
        host, prefix = out.get("host", DEFAULT_HOST), f"/{out['repo']}/blob/{out['branch']}/"
        if parts.netloc != host or not parts.path.startswith(prefix):
            raise InvalidOutcome(f"url must start with https://{host}{prefix}")
    elif out["type"] == "pull-request" and not PR_PATH.fullmatch(parts.path):
        raise InvalidOutcome("url must be a pull request, compare or tree link")
    return url


def _file(path, workdir: str) -> str:
    base = Path(workdir).resolve()
    real = (base / path).resolve() if isinstance(path, str) and "\0" not in path else None
    if real is None or real.suffix != ".md" or not real.is_relative_to(base) or not real.is_file():
        raise InvalidOutcome(f"file {str(path)[:200]!r} is not a .md file under the workdir")
    return str(real)


def _read_file(real: str, workdir: str) -> str:
    """The file _file checked, its type and place rechecked through the open fd."""
    cap = 4 * _schema()["properties"]["deliverable"]["maxLength"]
    try:
        data = read_confined(real, workdir, limit=cap)
    except ConfinedFileError as error:
        path = str(real)[:200]
        messages = {
            ConfinedReason.REGULAR: f"deliverable_file {path!r}: not a regular file",
            ConfinedReason.SUFFIX: f"file {path!r} is not a .md file under the workdir",
            ConfinedReason.OUTSIDE: f"deliverable_file {path!r}: no longer under the workdir",
            ConfinedReason.TOO_LARGE: f"deliverable_file {path!r}: too large (over {cap} bytes)",
        }
        raise InvalidOutcome(messages[error.reason]) from None
    except OSError as error:
        reason = error.strerror or "unreadable"
        raise InvalidOutcome(f"deliverable_file {str(real)[:200]!r}: {reason}") from None
    try:
        return data.decode()
    except UnicodeDecodeError:
        raise InvalidOutcome(f"deliverable_file {str(real)[:200]!r}: not UTF-8 text") from None
