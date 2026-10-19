"""agent-pm-linear issue create: plan resolves every reference before anything is written; create
makes the issue with only the given fields (the team's Backlog state unless --state), then each
blocking relation. Creating the issue is never retried; a relation that fails leaves the issue
made, and RelationsFailed names the commands that add only the failed ones.
"""

import shlex
import subprocess

from agent_pm.linear_orchestrator.engine import issue_refs, issue_write, ops
from agent_pm.linear_orchestrator.engine.client import messages
from agent_pm.linear_orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    RelationsFailed,
    Unavailable,
    WriteUnknown,
)

BACKLOG = "Backlog"
ERROR_CHARS = 200
RELATION = "agent-pm-linear issue relation add"
M_CREATE = (
    "mutation($in: IssueCreateInput!) { issueCreate(input: $in) "
    "{ success issue { id identifier url } } }"
)
M_RELATE = (
    "mutation($in: IssueRelationCreateInput!) { issueRelationCreate(input: $in) { success } }"
)
Q_VIEWER = "query { viewer { id name } }"
# option, and the receipt key naming the other issue, of each blocking direction
DIRECTIONS = (("--blocks", "blocks"), ("--blocked-by", "blocked_by"))


def plan(gql, args, team_id, body):
    """args resolved as (IssueCreateInput, [(option, {id, identifier}) of each relation], viewer);
    IssueError for any value that does not resolve, before anything is written.
    """
    team = issue_refs.team(gql, args.team)["id"] if args.team is not None else team_id
    fields = {"teamId": team, "title": args.title, "description": body}
    fields["stateId"] = _state(gql, team, args.state)["id"]
    if args.label:
        found = (issue_refs.label(gql, team, value)["id"] for value in dict.fromkeys(args.label))
        fields["labelIds"] = list(dict.fromkeys(found))
    project = None if args.project is None else issue_refs.project(gql, team, args.project)
    if project is not None:
        fields["projectId"] = project["id"]
    if args.milestone is not None:
        project_id = None if project is None else project["id"]
        fields["projectMilestoneId"] = issue_refs.milestone(gql, project_id, args.milestone)["id"]
    if args.parent is not None:
        fields["parentId"] = issue_refs.issue(gql, args.parent)["id"]
    if args.assignee is not None:
        fields["assigneeId"] = issue_refs.user(gql, args.assignee)["id"]
    relations = _relations(gql, {"--blocks": args.blocks, "--blocked-by": args.blocked_by})
    return fields, relations, gql(Q_VIEWER)["viewer"]


def _state(gql, team, value):
    if value is not None:
        return issue_refs.state(gql, team, value)
    try:
        return issue_refs.state(gql, team, BACKLOG)
    except IssueError as error:
        raise IssueError(f"without --state create sets {BACKLOG}, never another: {error}") from None


def _relations(gql, given):
    """[(option, issue), …] of each distinct issue given names; IssueError for one in both."""
    picked = {}
    for option, values in given.items():
        for value in dict.fromkeys(values):
            node = issue_refs.issue(gql, value, option=option)
            first, _ = picked.setdefault(node["id"], (option, node))
            if first != option:
                raise IssueError(
                    f"{node['identifier']}: given to both --blocks and --blocked-by; "
                    "an issue cannot both block the new one and be blocked by it"
                )
    return list(picked.values())


def create(gql, fields, relations, viewer):
    """Create the issue of fields, then each relation plan gave; its receipt, or RelationsFailed."""
    try:
        node = ops.call(gql, M_CREATE, "issueCreate", **{"in": fields})["issue"]
    except Unavailable as error:
        check = f"--team {fields['teamId']} --query={shlex.quote(fields['title'])}"
        raise WriteUnknown(
            f"create: result unknown ({error}): look for it with "
            f"agent-pm-linear issue list {check} before creating it again"
        ) from None
    done, failed = [], []
    for option, other in relations:
        blocker, blocked = (node, other) if option == "--blocks" else (other, node)
        relation = {dict(DIRECTIONS)[option]: other["identifier"]}
        variables = {"type": "blocks", "issueId": blocker["id"], "relatedIssueId": blocked["id"]}
        try:
            ops.call(gql, M_RELATE, "issueRelationCreate", **{"in": variables})
        except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as error:
            failed.append((option, {**relation, "error": _reason(error)}))
        else:
            done.append(relation)
    receipt = issue_write.receipt(node, "create", "changed", viewer)
    if failed:
        raise RelationsFailed(_report(receipt, done, failed))
    return receipt


def _reason(error):
    if isinstance(error, GraphQLErrors):
        text = "; ".join(messages(error.errors))
    elif isinstance(error, Unavailable):
        text = f"result unknown ({error})"
    else:
        text = str(error) or type(error).__name__
    return text[:ERROR_CHARS]


def _report(receipt, done, failed):
    ident = receipt["issue"]["identifier"]
    recover = [
        f"{RELATION} {ident} {option} {next(iter(item.values()))}" for option, item in failed
    ]
    return {
        **receipt,
        "result": "partial",
        "done": done,
        "failed": [item for _, item in failed],
        "recover": recover,
        "truncated": [],
    }
