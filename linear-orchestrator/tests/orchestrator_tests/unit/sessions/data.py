from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear, sessions

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

OTHER = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"

ISSUE, ISSUE_ID = "TASK-12", "00000000-0000-4000-8000-000000000012"

OTHER_ISSUE = "00000000-0000-4000-8000-000000000013"

CID = linear.client_id(SID, "session")

T1, T2, T3 = "2026-09-30T10:00:00+08:00", "2026-09-30T11:30:00+08:00", "2026-09-30T12:00:00+08:00"

WORKDIR, RUNTIME = "/w/TASK-12", "claude"

CMD = f"cd /w/TASK-12 && claude --resume {SID}"

NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")


def record(driver, started_at=T1):
    return sessions.base(
        sid=SID, workdir=WORKDIR, runtime=RUNTIME, started_at=started_at, driver=driver
    )


def text(line):
    return f"Run {SID} · {line}\n\n```\n{CMD}\n```"


def comment(id, body, issue=ISSUE_ID, *, mine=True):
    return {"id": id, "body": body, "mine": mine, "issue": issue}


def start(gql, rec, timeout=sessions.LIMIT):
    return sessions.post(ISSUE, rec, gql=gql, timeout=timeout)


def end(gql, rec, rc, ended_at=T2):
    return sessions.post(
        ISSUE, rec, gql=gql, end=sessions.SessionEnd(returncode=rc, ended_at=ended_at)
    )
