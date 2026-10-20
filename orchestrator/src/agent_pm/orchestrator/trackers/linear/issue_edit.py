"""agent-pm-orchestrator issue edit: one issueUpdate naming only what changes among title, description,
task, client and priority; a task or client replaces the issue's labels of that group, every other
label staying. Every given value already so: unchanged, nothing written.
"""

from agent_pm.orchestrator.engine.flow.types import PRIORITY_NUMBERS
from agent_pm.orchestrator.trackers.linear import issue_label, issue_names, issue_read, issue_write

Q_READ = (
    "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url title description "
    f"priority team {{ id key }} {issue_label.labels_page()} }} }}"
)
M_EDIT = (
    "mutation($i: String!, $in: IssueUpdateInput!) { issueUpdate(id: $i, input: $in) { success } }"
)


def edit(gql, linear, args, body):
    """Change args.id's title, description (body), task, client and priority as given."""
    groups = {}
    if args.task is not None:
        groups["--task"] = (issue_names.task_label(linear, args.task), linear.task_label_group)
    if args.client is not None:
        groups["--client"] = (
            issue_names.client_label(linear, args.client),
            linear.agent_label_group,
        )
    data = issue_read.lookup(gql, Q_READ, args.id)
    node = data["issue"]
    change = {}
    if args.title is not None and args.title != node["title"]:
        change["title"] = args.title
    if body is not None and body != node["description"]:
        change["description"] = body
    priority = None if args.priority is None else PRIORITY_NUMBERS[args.priority]
    if priority is not None and priority != node["priority"]:
        change["priority"] = priority
    if groups:
        issue_names.in_team(linear, node, " and ".join(groups))
        change.update(_labels(issue_label.carried_labels(gql, args.id, node), groups.values()))
    if not change:
        return issue_write.receipt(node, "edit", "unchanged", data["viewer"])
    issue_write.write(gql, "edit", args.id, M_EDIT, "issueUpdate", i=node["id"], **{"in": change})
    return issue_write.receipt(node, "edit", "changed", data["viewer"])


def _labels(carried, targets):
    """addedLabelIds and removedLabelIds putting each target label in place of its group's others."""
    ids = {n["id"] for n in carried}
    added = [label for label, _ in targets if label not in ids]
    removed = [
        n["id"]
        for label, group in targets
        for n in carried
        if (n["parent"] or {}).get("id") == group and n["id"] != label
    ]
    return {
        key: found
        for key, found in (("addedLabelIds", added), ("removedLabelIds", removed))
        if found
    }
