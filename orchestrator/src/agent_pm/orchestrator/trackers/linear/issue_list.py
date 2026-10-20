"""agent-pm-orchestrator issue list: the issue_page source of the configured team's issues that every
given filter holds, most recently updated first; archived ones only with --include-archived.
"""

from agent_pm.orchestrator.trackers.linear import issue_names, issue_page, issue_refs

LABELS = 50
FIELDS = (
    "id identifier title url state { id name } assignee { id name } project { id name } "
    f"parent {{ id }} labels(first: {LABELS}) {{ nodes {{ id name }} pageInfo {{ hasNextPage }} }}"
)
_SELECTION = issue_page.connection(
    "issues", "filter: $f, orderBy: updatedAt, includeArchived: $a", FIELDS
)
Q_LIST = f"query($f: IssueFilter, $a: Boolean, $first: Int!, $after: String) {{ {_SELECTION} }}"


def source(gql, args, linear):
    """The source of the configured team's issues that args' filters select."""
    team = linear.team
    match: list[dict] = [{"team": {"id": {"eq": team}}}]
    if args.query:
        text = {"containsIgnoreCase": args.query}
        match.append({"or": [{"title": text}, {"description": text}]})
    if args.state is not None:
        match.append({"state": _id(issue_names.state_id(linear, args.state))})
    if args.role is not None:
        who = issue_names.assignee(gql, linear, args.role)
        match.append({"assignee": {"null": True} if who is None else _id(who)})
    if args.task is not None:
        match.append({"labels": {"some": _id(issue_names.task_label(linear, args.task))}})
    if args.client is not None:
        match.append({"labels": {"some": _id(issue_names.client_label(linear, args.client))}})
    if args.priority is not None:
        match.append({"priority": {"eq": issue_names.PRIORITIES[args.priority]}})
    if args.label is not None:
        match.append({"labels": {"some": _id(issue_refs.label(gql, team, args.label)["id"])}})
    if args.project is not None:
        match.append({"project": _id(issue_refs.project(gql, team, args.project)["id"])})
    if args.parent is not None:
        match.append({"parent": _id(issue_refs.issue(gql, args.parent)["id"])})
    variables = {"f": issue_refs.every(*match), "a": args.include_archived}
    return Q_LIST, "issues", variables, _item


def _id(found):
    return {"id": {"eq": found}}


def _item(node):
    labels, parent = node["labels"], node["parent"]
    item = {key: node[key] for key in ("id", "identifier", "title", "url", "state")}
    item.update(labels=labels["nodes"], assignee=node["assignee"], project=node["project"])
    item["parent_id"] = parent["id"] if parent else None
    return item, ["labels"] if labels["pageInfo"]["hasNextPage"] else []
