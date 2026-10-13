import subprocess
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import prune
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from agent_pm.utils import GUARD
from orchestrator_tests.unit import promote_support as tp
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import TEAM, team_node

NOW = tp.NOW

STATE_IDS = {
    "Done": IDS_BY_KEY["done"],
    "Canceled": IDS_BY_KEY["canceled"],
    "In Progress": IDS_BY_KEY["in_progress"],
}

ROLES = {
    "u-researcher": "researcher",
    "u-pm": "pm",
    "u-engineer": "engineer",
    "u-dummy-tester": "dummy-tester",
}

TEAM_OBJ = linear.Team(TEAM, "Team", dict(IDS_BY_KEY))

A, B = "engineer-TASK-7-0b6f2c1e", "dummy-tester-TASK-7-7c1d9e2f"


def gql_for(issues, owners=None, page=50, fail=(), refuse=()):
    owners = owners or {}

    def history(ident):
        return {
            "nodes": [
                {"createdAt": (NOW - timedelta(hours=h)).isoformat(), "toStateId": STATE_IDS[s]}
                for h, s in issues[ident][1]
            ]
        }

    def gql(query, **v):
        gql.calls.append(query)
        if query == linear.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == prune.Q_FINISHED:
            gql.finished.append(v)
            hits = [
                i
                for i, (state, _) in issues.items()
                if i not in gql.gone and owners.get(i) in v["a"] and STATE_IDS[state] in v["s"]
            ]
            start = int(v["c"] or 0)
            return {
                "issues": {
                    "nodes": [
                        {"id": f"id-{i}", "identifier": i, "history": history(i)}
                        for i in hits[start : start + page]
                    ],
                    "pageInfo": {
                        "hasNextPage": start + page < len(hits),
                        "endCursor": str(start + page),
                    },
                }
            }
        if query == prune.M_ARCHIVE:
            gql.archived.append(v["i"])
            ident = v["i"].removeprefix("id-")
            if ident in fail:
                raise LinearError.api("boom")
            if ident not in refuse:
                gql.gone.add(ident)
            return {"issueArchive": {"success": ident not in refuse}}
        return {
            "issue": {"state": {"id": STATE_IDS[issues[v["i"]][0]]}, "history": history(v["i"])}
        }

    gql.calls, gql.finished, gql.archived, gql.gone = [], [], [], set()
    return gql


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def git(*argv):
    return subprocess.run(
        ["git", *GUARD, *argv], check=True, capture_output=True, text=True
    ).stdout.strip()
