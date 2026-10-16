"""An issue's first-claim search: the team's issues, docs and merged PRs its title words or the ids
its description names match, minus its upstream chain; inputs.render lists them as Related work.
"""

import json
import re

from agent_pm.linear_orchestrator.engine import inputs, issues, ops
from agent_pm.linear_orchestrator.engine.types import (
    ISSUE_ID,
    Found,
    InputError,
    LinearError,
    Ref,
    Related,
)
from agent_pm.utils import SHORT, err_text

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "do",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "per",
        "should",
        "the",
        "then",
        "this",
        "that",
        "to",
        "up",
        "use",
        "via",
        "vs",
        "what",
        "when",
        "which",
        "why",
        "with",
        "without",
    }
)
MAX_WORDS, MAX_HOPS = 5, 5
_NODES = (
    "nodes { identifier title description state { id name type } "
    "attachments(first: 20) { nodes { url } } comments(first: 1, orderBy: createdAt) { nodes { body } } }"
)
_FOUND = f"searchIssues(term: $t, filter: $f, first: 10, includeArchived: true) {{ {_NODES} }}"
_NAMED = f"issues(filter: $n, first: 50, includeArchived: true) {{ {_NODES} }}"
Q_SEARCH = f"query($t: String!, $f: IssueFilter) {{ {_FOUND} }}"
Q_SEARCH_IDS = f"query($t: String!, $f: IssueFilter, $n: IssueFilter) {{ {_FOUND} {_NAMED} }}"
Q_IDS = f"query($n: IssueFilter) {{ {_NAMED} }}"
Q_UPSTREAM = "query($i: String!) { issue(id: $i) { identifier description } }"
GROUP_BY_STATE = {"done": 0, "in_review": 1, "in_progress": 2, "todo": 3, "canceled": 5}
GROUP_BY_TYPE = {"completed": 0, "started": 2, "unstarted": 3, "canceled": 5}
CANCELED = 5


def keywords(title) -> list[str]:
    # a role's title prefix (`ENG: `, `PRD: `) is on every issue of its kind
    text = re.sub(ISSUE_ID, " ", re.sub(r"^[A-Z][A-Z0-9]*: ", "", title))
    words = []
    for w in re.findall(r"\w+", text.lower()):
        if len(w) > 1 and not w.isdigit() and w not in STOPWORDS and w not in words:
            words.append(w)
    return words[:MAX_WORDS]


def _mentioned(issue):
    key = issue.identifier.rsplit("-", 1)[0] + "-"
    found = re.findall(ISSUE_ID, issue.description)
    return list(dict.fromkeys(i for i in found if i.startswith(key) and i != issue.identifier))


def _nodes(gql, team, words, ids):
    f = {"team": {"id": {"eq": team}}}
    n = {**f, "number": {"in": [int(i.rsplit("-", 1)[1]) for i in ids]}}
    if words and ids:
        data = gql(Q_SEARCH_IDS, t=" ".join(words), f=f, n=n)
    elif words:
        data = gql(Q_SEARCH, t=" ".join(words), f=f)
    else:
        data = gql(Q_IDS, n=n)
    return [
        *data.get("searchIssues", {"nodes": []})["nodes"],
        *data.get("issues", {"nodes": []})["nodes"],
    ]


def _chain(gql, issue):
    chain, src = [], issue.handoff and issue.handoff.source
    while src and src != issue.identifier and src not in chain:
        chain.append(src)
        if len(chain) == MAX_HOPS:
            break
        try:
            node = gql(Q_UPSTREAM, i=src)["issue"]
        except LinearError as e:
            if not ops.not_found(e):
                raise
            node = None
        h = node and issues.parse_handoff(node["description"] or "")
        src = h.source if h else None
    return chain


def _found(n, states):
    st, comments = n["state"], n["comments"]["nodes"]
    by_id = {states[k]: g for k, g in GROUP_BY_STATE.items()}
    group = by_id.get(st["id"], GROUP_BY_TYPE.get(st["type"], 4))
    return Found(
        n["identifier"],
        n["title"],
        st["name"],
        group,
        tuple(u for a in n["attachments"]["nodes"] if not inputs.CONTROL.search(u := a["url"])),
        n["description"] or "",
        comments[0]["body"] if group == CANCELED and comments else "",
    )


def _owner(text, names, excluded):
    """The candidate id text names, "" for none, None when it names an excluded issue."""
    tagged = re.findall(ISSUE_ID, text)
    if excluded.intersection(tagged) or inputs.CONTROL.search(text):
        return None
    return next((i for i in tagged if i in names), "")


def _doc_refs(run, docs, words, names, excluded):
    refs = []
    for d in dict.fromkeys(x.rstrip("/") for x in docs.dirs.values()):
        for name in inputs.listing(run, docs, d) or []:
            owner = _owner(name, names, excluded)
            if owner is not None and (owner or any(w in name.lower() for w in words)):
                path = f"{d}/{name}"
                refs.append(Ref(path, inputs.doc_url(docs, path), owner or None))
    return refs


def _pr_refs(run, target, words, names, excluded):
    res = run(
        [
            *("gh", "search", "prs", "--merged", "--repo", f"{target.owner}/{target.name}"),
            *("--limit", "5", "--json", "number,title,url", "--", " OR ".join(words)),
        ],
        SHORT,
    )
    if res.returncode != 0:
        raise InputError(f"gh search prs: {err_text(res)}")
    try:
        prs = [(int(p["number"]), str(p["title"]), str(p["url"])) for p in json.loads(res.stdout)]
    except (ValueError, KeyError, TypeError):
        raise InputError("gh search prs: unusable response") from None
    return [
        Ref(f"#{n} {title}", url, owner or None)
        for n, title, url in prs
        if (owner := _owner(title, names, excluded)) is not None and not inputs.CONTROL.search(url)
    ]


def search(gql, issue, cfg, *, target, run) -> Related:
    words, ids = keywords(issue.title), _mentioned(issue)
    if not words and not ids:
        return Related()
    nodes = _nodes(gql, cfg.team, words, ids)
    chain = _chain(gql, issue)
    excluded, found = {issue.identifier, *chain}, {}
    for n in nodes:
        ident, h = n["identifier"], issues.parse_handoff(n["description"] or "")
        if h and h.source in chain:
            excluded.add(ident)
        elif ident not in excluded and ident not in found:
            found[ident] = _found(n, cfg.states)
    refs = _doc_refs(run, cfg.docs, words, found, excluded)
    if target and words:
        refs += _pr_refs(run, target, words, found, excluded)
    return Related(tuple(found.values()), tuple(refs))
