"""agent-pm-linear issue: fixed issue subcommands and the rules they share. stdout holds one JSON
object of at most LIMIT UTF-8 bytes, a write's receipt at most RECEIPT; diagnostics go to stderr, as
bounded; exit OK, FAILED (Linear failed, or a write's result is unknown) or USAGE (usage,
configuration or an unresolved reference).
"""

import argparse
import contextlib
import copy
import functools
import json
import os
import subprocess
import sys
import tempfile

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.linear_orchestrator.engine import (
    config,
    issue_archive,
    issue_list,
    issue_lookup,
    issue_page,
    issue_read,
    issue_write,
)
from agent_pm.linear_orchestrator.engine.client import linear_gql, messages
from agent_pm.linear_orchestrator.engine.types import (
    ArchiveRefused,
    ConfigError,
    GraphQLErrors,
    IssueError,
    KeychainError,
    LinearError,
    Resource,
    Unavailable,
)

OK, FAILED, USAGE = 0, 1, 2
LIMIT, RECEIPT = 8192, 1024
PAGE, MAX_PAGE = 20, 50
PROG = "agent-pm-linear issue"
FULL = "--full --output FILE"
TEXTS = (
    "title",
    "description",
    "parent.title",
    "project.name",
    "milestone.name",
    "assignee.name",
    "assignee.email",
    "state.name",
)
# never shortened by fit_page
REFS = frozenset(
    (
        "id",
        "identifier",
        "url",
        "key",
        "type",
        "email",
        "team_id",
        "group_id",
        "project_id",
        "parent_id",
    )
)
_RULES = (
    "Prerequisites and effects:\n"
    "  Reads Linear as the configured harness_key account (Keychain); an issue\n"
    "  needs no recorded run. Help needs no credentials or services.\n"
    "  stdout: one JSON object of at most 8192 bytes; diagnostics go to stderr.\n"
    "  Exit 0: success; 1: Linear failed (GraphQL errors, HTTP, timeout);\n"
    "  2: usage, configuration, Keychain, or an issue or option value that\n"
    "  does not resolve.\n\n"
)
_HELP = (
    _RULES.removesuffix("\n")
    + "  move, comment and archive write as that account and print a receipt\n"
    "  instead; for them exit 1 also means the write's result is unknown, for\n"
    "  archive also a refusal (their --help).\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue read TASK-1\n"
    "  agent-pm-linear issue list --state 'In Progress' --assignee me\n"
    "  agent-pm-linear issue lookup states --team TASK\n"
    "  agent-pm-linear issue move TASK-1 --state Done\n"
    "  agent-pm-linear issue comment TASK-1 --body-file note.md\n"
    "  agent-pm-linear issue archive TASK-1 --dry-run\n\n"
    "Recovery and next help:\n"
    "  Command details: agent-pm-linear issue read --help,\n"
    "  agent-pm-linear issue list --help, agent-pm-linear issue lookup --help,\n"
    "  agent-pm-linear issue move --help, agent-pm-linear issue comment --help,\n"
    "  agent-pm-linear issue archive --help\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)
_PAGES = (
    f"Pages: --limit N items, 1-{MAX_PAGE} (default {PAGE}); --cursor TOKEN, a page's\n"
    "  next_cursor, gives the next page under the same options. stdout:\n"
    "  {items, has_next_page, next_cursor (null on the last page), truncated}.\n"
    "  Over 8192 bytes, items drop from the end (next_cursor then continues\n"
    "  after the last one kept); a lone item's longest text is shortened, never\n"
    "  an id, each named in truncated as items.<n>.<field>.\n"
    f"  {FULL}: every page, written as indented JSON {{items, truncated}}\n"
    "  to FILE; prints only {output: FILE's absolute path}; not with\n"
    "  --cursor or --limit.\n"
    "References: an id or an exact name; a team also takes its key. Without\n"
    "  --team, the configured team. No match, several, or one outside the team\n"
    "  exits 2 naming candidates and the lookup command listing them.\n\n"
)
_LIST_HELP = (
    _RULES + "Output items: id, identifier, title, url, state {id, name}, labels\n"
    "  [{id, name}], assignee {id, name}, project {id, name}, parent_id; empty:\n"
    "  null or []. Only the team's issues, every given filter holding, most\n"
    "  recently updated first; archived ones only with --include-archived.\n"
    "  --query TEXT matches title or description in any case; --state, --label,\n"
    "  --project in the team, --label not a group or retired; --assignee a\n"
    "  user's id, name, email or me (the harness_key account); --parent the\n"
    "  parent issue's identifier or id.\n"
    + _PAGES
    + "Examples (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue list --query 'linear issue'\n"
    "  agent-pm-linear issue list --team TASK --state Todo --label bug\n"
    "  agent-pm-linear issue list --assignee me --project 'Agent PM'\n"
    "  agent-pm-linear issue list --parent TASK-1 --include-archived --limit 50\n"
    "  agent-pm-linear issue list --cursor <next_cursor>\n"
    "  agent-pm-linear issue list --label bug --full --output bugs.json\n\n"
    "Recovery and next help:\n"
    "  Option values: agent-pm-linear issue lookup --help\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)
_LOOKUP_HELP = (
    _RULES + "Output items, every same-named entity its own:\n"
    "  teams       id, name, key (--query matches name or key)\n"
    "  states      id, name, type: the team's workflow states\n"
    "  labels      id, name, team_id (null: workspace), group_id: the team's and\n"
    "              the workspace's labels an issue can carry (no groups, none retired)\n"
    "  projects    id, name, team_ids: the team's projects\n"
    "  milestones  id, name, project_id: needs --project, within that project\n"
    "  users       id, name, email, active: --team's members, else the workspace's\n"
    "              users (--query matches name or email)\n"
    "  --query TEXT keeps names holding TEXT in any case.\n"
    + _PAGES
    + "Examples (configured Linear access):\n"
    "  agent-pm-linear issue lookup teams\n"
    "  agent-pm-linear issue lookup labels --team TASK --query bug\n"
    "  agent-pm-linear issue lookup milestones --project 'Agent PM' --limit 50\n"
    "  agent-pm-linear issue lookup users --cursor <next_cursor>\n"
    "  agent-pm-linear issue lookup states --full --output states.json\n\n"
    "Recovery and next help:\n"
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
    "  Over 8192 bytes, list items drop from the end, then the longest text\n"
    "  (title, description, names) is shortened, each named in truncated.\n"
    f"  {FULL}: read every page, keep every body, write the view as indented\n"
    "  JSON to FILE and print only {identifier, output: FILE's absolute path};\n"
    "  either option alone is a usage error. A failed read writes no file.\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue read TASK-1\n"
    "  agent-pm-linear issue read TASK-1 --full --output TASK-1.json\n\n"
    "Recovery and next help:\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)


_WRITE_RULES = (
    "Prerequisites and effects:\n"
    "  Writes Linear as the configured harness_key account (Keychain); an issue\n"
    "  needs no recorded run. Help needs no credentials or services.\n"
    "  stdout: one receipt of at most 1024 bytes: issue {id, identifier, url},\n"
    "  operation, result (changed or unchanged), actor {id, name}%s.\n"
    "  It never echoes what was written or Linear's response. Diagnostics go to\n"
    "  stderr.\n"
    "  Exit 0: changed or unchanged; 1: Linear failed (GraphQL errors, success\n"
    "  false, HTTP) or the result is unknown; 2: usage, configuration, Keychain,\n"
    "  or an issue or value that does not resolve: nothing was written.\n"
    "  A timeout or outage during the write leaves the result unknown: check with\n"
    "  agent-pm-linear issue read ID before running it again; nothing retries.\n\n"
)
_MOVE_HELP = (
    _WRITE_RULES
    % ""
    + "--state VALUE: the id or exact name (case-sensitive) of a workflow state of\n"
    "  the issue's own team, so a name other teams share still means this team's.\n"
    "  A state type such as completed is no name. No match, or a name the team\n"
    "  holds twice: exit 2 listing the team's states (name and id). Already in\n"
    "  that state: result unchanged, nothing written.\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue move TASK-1 --state Done\n"
    "  agent-pm-linear issue move TASK-1 --state 'In Progress'\n\n"
    "Recovery and next help:\n"
    "  The issue's current state: agent-pm-linear issue read TASK-1\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)

_COMMENT_HELP = (
    _WRITE_RULES
    % ", comment_id"
    + "--body-file FILE: the comment's Markdown body, UTF-8, sent verbatim as a\n"
    "  GraphQL variable: quotes, backticks, $ and newlines need no escaping.\n"
    "  - reads stdin. Blank, unreadable or non-UTF-8, or --body-file given twice:\n"
    "  exit 2 before anything is written. Each run posts a new top-level comment.\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue comment TASK-1 --body-file note.md\n"
    "  agent-pm-linear issue comment TASK-1 --body-file - < note.md\n\n"
    "Recovery and next help:\n"
    "  Whether a comment landed: agent-pm-linear issue read TASK-1\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)

_ARCHIVE_HELP = (
    _WRITE_RULES
    % ""
    + "Archives the issue only when it and every unarchived sub-issue, at any depth,\n"
    "  are finished: state type completed or canceled (Linear archives sub-issues\n"
    "  with their parent). It first reads every page of sub-issues at every level;\n"
    "  a failed read writes nothing. There is no override: an unfinished one exits\n"
    "  1 naming it. Already archived: result unchanged, nothing written. It closes\n"
    "  no run and deletes no file.\n"
    "--dry-run: write nothing and print, in at most 8192 bytes, {issue, operation,\n"
    "  dry_run, result (what archive would do: changed, unchanged or refused),\n"
    "  descendant_count, descendants (the sub-issues it would archive too: id,\n"
    "  identifier, state), unfinished_count, unfinished, truncated}; over 8192\n"
    "  bytes, list items drop from the end, each such list named in truncated.\n"
    "  Exit 0 whatever result says.\n\n"
    "Example (configured Linear access and TASK-1):\n"
    "  agent-pm-linear issue archive TASK-1 --dry-run\n"
    "  agent-pm-linear issue archive TASK-1\n\n"
    "Recovery and next help:\n"
    "  An unfinished issue: agent-pm-linear issue move TASK-1 --state Done\n"
    "  Configuration and command discovery: agent-pm-linear --help"
)


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} given twice")
        setattr(namespace, self.dest, values)


def encode(payload):
    # backslashreplace: a lone surrogate becomes its JSON escape
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8", "backslashreplace") + b"\n"


def fit(payload, lists, texts, follow):
    """payload encoded within LIMIT: drop the last item of its largest list in lists, then halve its
    longest text at a dotted path in texts, naming each cut in truncated, until it fits; continue is
    follow once anything is truncated, absent when follow is None.
    """
    payload = copy.deepcopy(payload)
    while True:
        if follow is not None:
            payload["continue"] = follow if payload["truncated"] else None
        data = encode(payload)
        if len(data) <= LIMIT:
            return data
        if full := [f for f in lists if payload[f]]:
            field = max(full, key=lambda f: len(encode(payload[f])))
            payload[field].pop()
            kept = len(payload[field])
            payload["truncated"] = [m for m in payload["truncated"] if not _gone(m, field, kept)]
            _mark(payload, field)
            continue
        found = {path: text for path in texts if (text := _text(payload, path))}
        if not found:
            raise OverflowError(f"result exceeds {LIMIT} bytes: read it with {FULL}")
        path = max(found, key=lambda p: len(encode(found[p])))
        *parents, leaf = path.split(".")
        node = functools.reduce(lambda d, k: d[k], parents, payload)
        node[leaf] = found[path][: len(found[path]) // 2]
        _mark(payload, path)


def _gone(marker, field, kept):
    # a marker inside an item fit dropped, such as comments.4.body once 4 comments remain
    parts = marker.split(".")
    return len(parts) > 2 and parts[0] == field and parts[1].isdigit() and int(parts[1]) >= kept


def _mark(payload, field):
    if field not in payload["truncated"]:
        payload["truncated"].append(field)


def _text(payload, path):
    node = payload
    for key in path.split("."):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else None


def fit_page(rows, cursor):
    """A page's rows [(item, cut fields, cursor), …] and the cursor after it (None: last page) as
    {items, has_next_page, next_cursor, truncated} within LIMIT: trailing items drop, next_cursor
    then following the last kept one; a lone item's longest text other than a reference halves;
    each cut is items.<n>.<field>.
    """
    items = [copy.deepcopy(item) for item, _, _ in rows]
    cut = [f"items.{n}.{field}" for n, (_, fields, _) in enumerate(rows) for field in fields]
    while True:
        page = {"items": items, "has_next_page": cursor is not None, "next_cursor": cursor}
        data = encode({**page, "truncated": cut})
        if len(data) <= LIMIT:
            return data
        if len(items) > 1:
            items.pop()
            cursor = rows[len(items) - 1][2]
            cut = [m for m in cut if int(m.split(".")[1]) < len(items)]
            continue
        found = dict(_texts(items[0], ()))
        if not found:
            raise OverflowError(f"page exceeds {LIMIT} bytes: write it with {FULL}")
        path = max(found, key=lambda p: len(encode(found[p])))
        node = functools.reduce(lambda node, key: node[key], path[:-1], items[0])
        node[path[-1]] = found[path][: len(found[path]) // 2]
        if (marker := ".".join(map(str, ("items", 0, *path)))) not in cut:
            cut.append(marker)


def _texts(node, path):
    # (path, text) of each non-empty string in node but a reference
    pairs = node.items() if isinstance(node, dict) else enumerate(node)
    for key, value in pairs:
        if isinstance(value, (dict, list)):
            yield from _texts(value, (*path, key))
        elif isinstance(value, str) and value and key not in REFS:
            yield (*path, key), value


def write_full(path, payload):
    """payload as normalized JSON in path, through a temp file renamed over it; its absolute path."""
    path = os.path.abspath(path)
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".issue-", suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8", errors="backslashreplace") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
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
        description="Read and write Linear issues and look up what their options name.",
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
    found = commands.add_parser(
        "list",
        help="search one team's issues by text, state, label, project, assignee or parent",
        description="List one team's issues that every given filter holds as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_LIST_HELP,
    )
    found.add_argument("--query", metavar="TEXT", help="title or description holds TEXT, any case")
    found.add_argument("--team", metavar="VALUE", help="team id, name or key (default: configured)")
    found.add_argument("--state", metavar="VALUE", help="workflow state id or name")
    found.add_argument("--label", metavar="VALUE", help="label id or name")
    found.add_argument("--project", metavar="VALUE", help="project id or name")
    found.add_argument("--assignee", metavar="VALUE", help="user id, name, email or me")
    found.add_argument("--parent", metavar="ID", help="parent issue identifier (TASK-1) or id")
    found.add_argument("--include-archived", action="store_true", help="archived issues too")
    _paged(found)
    lookup = commands.add_parser(
        "lookup",
        help="list teams, states, labels, projects, milestones or users with their ids",
        description="List one kind of Linear entity, with its id and fixed fields, as JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_LOOKUP_HELP,
    )
    lookup.add_argument(
        "resource",
        type=Resource,
        choices=list(Resource),
        metavar="RESOURCE",
        help=", ".join(Resource),
    )
    lookup.add_argument("--query", metavar="TEXT", help="keep names holding TEXT, any case")
    lookup.add_argument(
        "--team", metavar="VALUE", help="team id, name or key (default: configured)"
    )
    lookup.add_argument("--project", metavar="VALUE", help="milestones: the project's id or name")
    _paged(lookup)
    move = commands.add_parser(
        "move",
        help="set one issue's workflow state by name, within its team",
        description="Set one issue's workflow state by name or id, within the issue's team.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_MOVE_HELP,
    )
    move.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    move.add_argument(
        "--state", required=True, action=_Once, metavar="VALUE", help="state name or id"
    )
    comment = commands.add_parser(
        "comment",
        help="post a comment on one issue from a UTF-8 file or stdin",
        description="Post a new top-level comment on one issue, its body read verbatim.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_COMMENT_HELP,
    )
    comment.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    comment.add_argument(
        "--body-file", required=True, action=_Once, metavar="FILE", help="UTF-8 body; -: stdin"
    )
    archive = commands.add_parser(
        "archive",
        help="archive one issue once it and every sub-issue are finished",
        description="Archive one issue once it and every unarchived sub-issue are finished.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_ARCHIVE_HELP,
    )
    archive.add_argument("id", metavar="ID", help="issue identifier (TASK-1) or issue id")
    archive.add_argument(
        "--dry-run", action="store_true", help="list what it would archive; write nothing"
    )
    return parser


def _paged(parser):
    parser.add_argument(
        "--limit", type=_limit, metavar="N", help=f"items per page, 1-{MAX_PAGE} (default {PAGE})"
    )
    parser.add_argument("--cursor", metavar="TOKEN", help="a page's next_cursor: the next page")
    parser.add_argument("--full", action="store_true", help="every page; needs --output")
    parser.add_argument("--output", metavar="FILE", help="with --full: where the JSON goes")


def _limit(text):
    try:
        found = int(text)
    except ValueError:
        found = 0
    if not 1 <= found <= MAX_PAGE:
        raise argparse.ArgumentTypeError(f"want 1-{MAX_PAGE}, got {text!r}")
    return found


def _usage(args):
    """The usage error in args' page options, or None."""
    if "full" not in args:
        return None
    if args.full != (args.output is not None):
        if args.command == "read":
            return f"--full and --output go together: read {args.id} {FULL}"
        return f"--full and --output go together: {args.command} … {FULL}"
    if args.command != "read" and args.full and (args.cursor, args.limit) != (None, None):
        return "--full reads every page: drop --cursor and --limit"
    return None


def receipt(payload):
    """payload encoded within RECEIPT, its actor's name halved until it fits."""
    actor = payload["actor"]
    while len(data := encode(payload)) > RECEIPT and actor["name"]:
        actor["name"] = actor["name"][: len(actor["name"]) // 2]
    return data


def _run(gql, args, team_id, body):
    if args.command == "move":
        return receipt(issue_write.move(gql, args.id, args.state))
    if args.command == "comment":
        return receipt(issue_write.comment(gql, args.id, body))
    if args.command == "archive" and args.dry_run:
        return fit(issue_archive.dry_run(gql, args.id), ("descendants", "unfinished"), (), None)
    if args.command == "archive":
        return receipt(issue_archive.archive(gql, args.id))
    if args.command == "read":
        view = issue_read.view(gql, args.id, full=args.full)
        if args.full:
            return _written(args.output, view, {"identifier": view["identifier"]})
        return fit(
            issue_read.compact(view),
            ("comments", "labels", "blocks", "blocked_by", "children"),
            TEXTS,
            f"agent-pm-linear issue read {view['identifier']} {FULL}",
        )
    source = (issue_list if args.command == "list" else issue_lookup).source(gql, args, team_id)
    if not args.full:
        rows, more, end = issue_page.fetch(gql, source, args.limit or PAGE, args.cursor)
        return fit_page(rows, end if more else None)
    rows = issue_page.walk(gql, source)
    cut = [f"items.{n}.{field}" for n, (_, fields, _) in enumerate(rows) for field in fields]
    payload = {"items": [item for item, _, _ in rows], "truncated": cut}
    return _written(args.output, payload, {})


def _written(output, payload, summary):
    """summary and output's absolute path, once payload is written there."""
    try:
        path = write_full(output, payload)
    except OSError as error:
        raise IssueError(f"--output {output}: {error.strerror or error}") from None
    return encode({**summary, "output": path})


def main(argv, *, gql=linear_gql, config_path=None, out=None, err=None, stdin=None):
    args = _parser().parse_args(argv)
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr if err is None else err
    if usage := _usage(args):
        diagnose(err, usage)
        return USAGE
    body = None
    if args.command == "comment":
        try:
            body = issue_write.read_body(args.body_file, stdin)
        except IssueError as error:
            diagnose(err, error)
            return USAGE
    try:
        cfg = config.load(path=config_path)
    except (ConfigError, DelegateConfigError, ValueError, OSError) as error:
        diagnose(err, error)
        return USAGE
    gql = functools.partial(gql, service=cfg.harness_key)
    try:
        data = _run(gql, args, cfg.team, body)
    except IssueError as error:
        diagnose(err, error)
        return USAGE
    except (subprocess.SubprocessError, OSError, KeychainError) as error:
        diagnose(err, f"harness_key Keychain service {cfg.harness_key!r}: {type(error).__name__}")
        return USAGE
    except GraphQLErrors as error:
        diagnose(err, "linear api error: " + "; ".join(messages(error.errors)))
        return FAILED
    except (LinearError, Unavailable, OverflowError, ArchiveRefused) as error:
        diagnose(err, error)
        return FAILED
    out.write(data)
    return OK
