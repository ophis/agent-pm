import hashlib
from datetime import UTC, datetime

from agent_pm.delegate import Outcome, Progress
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import issues, linear, writeback
from orchestrator_test_helpers.unit import board_ids

ID, UUID = "ENG-7", "11111111-2222-4333-8444-555555555555"

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

OTHER_SID = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"

SRC_UUID = "99999999-8888-4777-8666-555555555555"

OTHER_ISSUE = "77777777-6666-4555-8444-333333333333"

HUMANS = ("ann@example.com", "bob@example.com")

STATES = board_ids.STATES

PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

REPOS = {PROJECT: "ophis/agent-pm"}

TARGET = ("ophis", "agent-pm")

TREE = "https://github.com/ophis/agent-pm/tree/ENG-7-session-registry"

SPEC_TEXT = "# Spec\n\nBuild the registry.\n"

PLAN_TEXT = "# Plan\n\nRESUME: phase=2\n\n- [ ] task 1\n"

FOOTER = "Answer in a comment, then move this issue back to Todo."

APPROVE = "**🔴 To approve, move this issue to Handoff with a comment `Repo: <owner>/<name>` naming the target repo.**"

NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")

START = Progress("start", "x")

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()

NAMES = {
    linear.M_SUBSCRIBE: "subscribe",
    linear.M_COMMENT: "comment",
    linear.M_COMMENT_ID: "comment",
    linear.Q_COMMENT: "find",
    linear.M_STATE: "state",
    writeback.M_TITLE: "title",
    writeback.M_ATTACH: "attach",
    writeback.M_UNARCHIVE: "unarchive",
    writeback.Q_ATTACHED: "read",
    linear.Q_ISSUE_STATE: "reread",
    writeback.Q_ID: "id",
}

FIELDS = {
    "subscribe": "issueSubscribe",
    "comment": "commentCreate",
    "state": "issueUpdate",
    "title": "issueUpdate",
    "attach": "attachmentLinkURL",
    "unarchive": "issueUnarchive",
}


def mine(body, issue=UUID):
    return {"body": body, "issue": issue, "mine": True}


READ = ("read", {"i": UUID})


def reread(i=UUID):
    return ("reread", {"i": i})


def sub(e, i=UUID):
    return ("subscribe", {"i": i, "e": e})


def cid(step, sid=SID):
    return linear.client_id(sid, step)


def post(step, b, sid=SID):
    return ("comment", {"c": cid(step, sid), "i": UUID, "b": b})


def find(step):
    return ("find", {"c": cid(step)})


def comment(b, i=UUID):
    return ("comment", {"i": i, "b": b})


def move(s, i=UUID):
    return ("state", {"i": i, "s": STATES[s]})


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def spec_comment(name="ENG-7-spec.md", text=SPEC_TEXT):
    return post(f"file:{name}:{sha(text)}", f"**Spec** `{name}`\n\n---\n\n{text}")


def plan_comment(name="ENG-7-plan.md", text=PLAN_TEXT):
    return post(f"file:{name}:{sha(text)}", f"**Plan** `{name}`\n\n---\n\n{text}")


def expect(
    *,
    body,
    state,
    files=(),
    title=None,
    subscribe=True,
    attach=None,
    attach_title=None,
    resume=False,
):
    def posted(call):
        return [("find", {"c": call[1]["c"]}), call] if resume else [call]

    calls = [READ, *(c for f in files for c in posted(f))]
    if title:
        calls.append(("title", {"i": UUID, "t": title}))
    if subscribe:
        calls += [sub(e) for e in HUMANS]
    calls += posted(post("comment", body))
    if attach:
        calls.append(("attach", {"i": UUID, "u": attach, "t": attach_title}))
    return calls + [reread(), move(state)]


def outcome(status, title="Queue options", summary="", questions=(), url="", files=()):
    return Outcome(status, title, summary, list(questions), url, list(files))


HANDOFF = (
    "Handoff from PRD-3: https://linear.app/t/issue/PRD-3\n\n## Source\n- PRD: x\n\n"
    "## Instructions\nBuild it.\n\n## Comments\n* Ann:\n  > Repo: ophis/other"
)


def issue(description):
    return issues.Issue(
        UUID,
        ID,
        f"https://linear.app/t/issue/{ID}",
        "ENG: Session registry",
        description,
        "2026-09-01T00:00:00.000Z",
        None,
        (),
        (),
        (),
    )
