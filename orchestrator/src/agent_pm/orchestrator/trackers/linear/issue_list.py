"""agent-pm-linear issue list: the issue_page source of one team's issues that every given filter
holds, most recently updated first; archived ones only with --include-archived.
"""

from agent_pm.orchestrator.trackers.linear import issue_page, issue_refs

LABELS = 50
FIELDS = (
    "id identifier title url state { id name } assignee { id name } project { id name } "
    f"parent {{ id }} labels(first: {LABELS}) {{ nodes {{ id name }} pageInfo {{ hasNextPage }} }}"
)
_SELECTION = issue_page.connection(
    "issues", "filter: $f, orderBy: updatedAt, includeArchived: $a", FIELDS
)
Q_LIST = f"query($f: IssueFilter, $a: Boolean, $first: Int!, $after: String) {{ {_SELECTION} }}"


def source(gql, args, team_id):
    """The source of the issues of args.team (resolved) else team_id that args' filters select."""
    team = issue_refs.team(gql, args.team)["id"] if args.team is not None else team_id
    match: list[dict] = [{"team": {"id": {"eq": team}}}]
    if args.query:
        text = {"containsIgnoreCase": args.query}
        match.append({"or": [{"title": text}, {"description": text}]})
    if args.state is not None:
        match.append({"state": _id(issue_refs.state(gql, team, args.state))})
    if args.label is not None:
        match.append({"labels": {"some": _id(issue_refs.label(gql, team, args.label))}})
    if args.project is not None:
        match.append({"project": _id(issue_refs.project(gql, team, args.project))})
    if args.assignee is not None:
        match.append({"assignee": _id(issue_refs.user(gql, args.assignee))})
    if args.parent is not None:
        match.append({"parent": _id(issue_refs.issue(gql, args.parent))})
    variables = {"f": issue_refs.every(*match), "a": args.include_archived}
    return Q_LIST, "issues", variables, _item


def _id(node):
    return {"id": {"eq": node["id"]}}


def _item(node):
    labels, parent = node["labels"], node["parent"]
    item = {key: node[key] for key in ("id", "identifier", "title", "url", "state")}
    item.update(labels=labels["nodes"], assignee=node["assignee"], project=node["project"])
    item["parent_id"] = parent["id"] if parent else None
    return item, ["labels"] if labels["pageInfo"]["hasNextPage"] else []
