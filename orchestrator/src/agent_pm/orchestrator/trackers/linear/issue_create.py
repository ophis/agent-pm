"""agent-pm-orchestrator issue create: plan resolves every reference before anything is written; create
makes the issue in the configured team with only the given fields (backlog unless --state), then
each blocking relation. Creating the issue is never retried; a relation that fails leaves the issue
made, and RelationsFailed names the commands that add only the failed ones.
"""

import shlex
import subprocess

from agent_pm.orchestrator.engine.flow.types import (
    BOTH_DIRECTIONS,
    PRIORITY_NUMBERS,
    IssueState,
)
from agent_pm.orchestrator.engine.types import (
    GraphQLErrors,
    IssueError,
    LinearError,
    RelationsFailed,
    Unavailable,
    WriteUnknown,
)
from agent_pm.orchestrator.trackers.linear import issue_names, issue_refs, issue_write, ops
from agent_pm.orchestrator.trackers.linear.client import messages

ERROR_CHARS = 200
RELATION = "agent-pm-orchestrator issue relation add"
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


def plan(gql, args, linear, body):
    """args resolved as (IssueCreateInput, [(option, {id, identifier}) of each relation], viewer);
    IssueError for any value that does not resolve, before anything is written; abstract names
    first.
    """
    team = linear.team
    fields = {"teamId": team, "title": args.title, "description": body}
    fields["stateId"] = issue_names.state_id(linear, args.state or IssueState.BACKLOG)
    labels = [] if args.task is None else [issue_names.task_label(linear, args.task)]
    if args.client is not None:
        labels.append(issue_names.client_label(linear, args.client))
    who = None if args.role is None else issue_names.assignee(gql, linear, args.role)
    labels += (issue_refs.label(gql, team, value)["id"] for value in dict.fromkeys(args.label))
    if labels:
        fields["labelIds"] = list(dict.fromkeys(labels))
    project = None if args.project is None else issue_refs.project(gql, team, args.project)
    if project is not None:
        fields["projectId"] = project["id"]
    if args.milestone is not None:
        project_id = None if project is None else project["id"]
        fields["projectMilestoneId"] = issue_refs.milestone(gql, project_id, args.milestone)["id"]
    if args.parent is not None:
        fields["parentId"] = issue_refs.issue(gql, args.parent)["id"]
    if who is not None:
        fields["assigneeId"] = who
    if args.priority is not None:
        fields["priority"] = PRIORITY_NUMBERS[args.priority]
    relations = _relations(gql, {"--blocks": args.blocks, "--blocked-by": args.blocked_by})
    return fields, relations, gql(Q_VIEWER)["viewer"]


def _relations(gql, given):
    """[(option, issue), …] of each distinct issue given names; IssueError for one in both."""
    picked = {}
    for option, values in given.items():
        for value in dict.fromkeys(values):
            node = issue_refs.issue(gql, value, option=option)
            first, _ = picked.setdefault(node["id"], (option, node))
            if first != option:
                raise IssueError(BOTH_DIRECTIONS.format(identifier=node["identifier"]))
    return list(picked.values())


def create(gql, fields, relations, viewer):
    """Create the issue of fields, then each relation plan gave; its receipt, or RelationsFailed."""
    try:
        node = ops.call(gql, M_CREATE, "issueCreate", **{"in": fields})["issue"]
    except Unavailable as error:
        raise WriteUnknown(
            f"create: result unknown ({error}): look for it with agent-pm-orchestrator issue "
            f"list --query={shlex.quote(fields['title'])} before creating it again"
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
    }
