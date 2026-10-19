"""agent-pm-linear issue: fixed issue subcommands and the rules they share. stdout holds one JSON
object of at most LIMIT UTF-8 bytes; diagnostics go to stderr, as bounded; exit OK, FAILED (Linear
failed) or USAGE (usage, configuration or an unresolved reference).
"""

import argparse
import functools
import json
import os
import subprocess
import sys

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.linear_orchestrator.engine import config, issue_read
from agent_pm.linear_orchestrator.engine.client import linear_gql
from agent_pm.linear_orchestrator.engine.types import (
    ConfigError,
    IssueError,
    LinearError,
    Unavailable,
)

OK, FAILED, USAGE = 0, 1, 2
LIMIT = 8192
PROG = "agent-pm-linear issue"
FULL = "--full --output FILE"
_RULES = (
    "Prerequisites and effects:\n"
    "  Reads Linear as the configured harness_key account (Keychain); an issue\n"
    "  needs no recorded run. Help needs no credentials or services.\n"
    "  stdout: one JSON object of at most 8192 bytes; diagnostics go to stderr.\n"
    "  Exit 0: success; 1: Linear failed (GraphQL errors, HTTP, timeout);\n"
    "  2: usage, configuration, Keychain, or an issue that does not resolve.\n\n"
)
_HELP = (
    _RULES + "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue read TASK-1\n\n"
    "Recovery and next help:\n"
    "  Command details: agent-pm-linear issue read --help\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)
_READ_HELP = (
    _RULES + "Output: id, identifier, title, url, state, labels, project, milestone,\n"
    "  assignee, description, comments (newest first: id, author, created_at, body,\n"
    "  parent_id, quoted_text, on_behalf_of), blocks (issues it blocks), blocked_by\n"
    "  (issues blocking it), parent, children; empty: null or [].\n"
    "  Defaults: 5 comments of at most 300 characters, a 600-character\n"
    "  description, 10 issues per relation column. truncated lists every field\n"
    f"  cut or with more pages; continue then gives the read with {FULL}.\n"
    f"  {FULL}: read every page, keep every body, write the view as indented\n"
    "  JSON to FILE and print only {identifier, output: FILE's absolute path};\n"
    "  either option alone is a usage error. A failed read writes no file.\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue read TASK-1\n"
    "  agent-pm-linear issue read TASK-1 --full --output TASK-1.json\n\n"
    "Recovery and next help:\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)


def encode(payload):
    # backslashreplace: a lone surrogate becomes its JSON escape
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "backslashreplace") + b"\n"


def fit(payload, lists, follow):
    """payload encoded within LIMIT: drop the last item of its largest list in lists, naming it in
    truncated, until it fits; continue is follow once anything is truncated.
    """
    payload = {**payload, "truncated": list(payload["truncated"])}
    for field in lists:
        payload[field] = list(payload[field])
    while True:
        payload["continue"] = follow if payload["truncated"] else None
        data = encode(payload)
        if len(data) <= LIMIT:
            return data
        full = [f for f in lists if payload[f]]
        if not full:
            raise OverflowError(f"result exceeds {LIMIT} bytes: read it with {FULL}")
        field = max(full, key=lambda f: len(encode(payload[f])))
        payload[field].pop()
        if field not in payload["truncated"]:
            payload["truncated"].append(field)


def write_full(path, payload):
    """payload as normalized JSON in path; its absolute path."""
    path = os.path.abspath(path)
    with open(path, "w", encoding="utf-8", errors="backslashreplace") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


def diagnose(err, message):
    data = f"{PROG}: {message}".encode("utf-8", "backslashreplace")
    if len(data) > LIMIT - 1:
        kept = data[: LIMIT // 2].decode("utf-8", "ignore").encode()
        data = kept + f" … ({len(data) - len(kept)} bytes omitted)".encode()
    print(data.decode(), file=err)


def _parser():
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Read Linear issues with fixed subcommands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser(
        "read",
        help="print one issue's fields, newest comments and relations",
        description="Print one issue's fields, newest comments and relations as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_READ_HELP,
    )
    read.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    read.add_argument("--full", action="store_true", help="every page and body; needs --output")
    read.add_argument("--output", metavar="FILE", help="with --full: where the JSON goes")
    return parser


def main(argv, *, gql=linear_gql, config_path=None, out=None, err=None):
    args = _parser().parse_args(argv)
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr if err is None else err
    if args.full != (args.output is not None):
        diagnose(err, f"--full and --output go together: read {args.id} {FULL}")
        return USAGE
    try:
        cfg = config.load(path=config_path)
    except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
        diagnose(err, error)
        return USAGE
    gql = functools.partial(gql, service=cfg.harness_key)
    try:
        view = issue_read.view(gql, args.id, full=args.full)
        if not args.full:
            data = fit(
                issue_read.compact(view),
                ("comments", "labels", "blocks", "blocked_by", "children"),
                f"agent-pm-linear issue read {view['identifier']} {FULL}",
            )
    except IssueError as error:
        diagnose(err, error)
        return USAGE
    except (subprocess.SubprocessError, OSError) as error:
        diagnose(err, f"harness_key Keychain service {cfg.harness_key!r}: {type(error).__name__}")
        return USAGE
    except (LinearError, Unavailable, OverflowError) as error:
        diagnose(err, error)
        return FAILED
    if args.full:
        try:
            path = write_full(args.output, view)
        except OSError as error:
            diagnose(err, f"--output {args.output}: {error.strerror or error}")
            return USAGE
        data = encode({"identifier": view["identifier"], "output": path})
    out.write(data)
    return OK
