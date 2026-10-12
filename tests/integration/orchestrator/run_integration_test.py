"""run.py as a subprocess over the Delegate, headless: fake_claude as runtime `claude`, FakeLinear over HTTP, the local
files in the test's HOME. Done runs write back as writeback_test pins them, plus the session comment by its session id;
a run with no outcome; a usage limit (FR-59) and its resume; a resume after a lost ledger write (FR-54)."""

import os
import re
import shlex
import unittest
from datetime import UTC, datetime

from agent_pm.linear_orchestrator.linear import linear

from tests.integration.delegate.cli_integration_test import mark, option
from tests.integration.orchestrator.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    HUMAN,
    PROJECT,
    RUN,
    Flow,
    steps,
)
from tests.unit.delegate.runner_test import block
from tests.unit.orchestrator.board_ids import STATES
from tests.unit.orchestrator.run_fixtures import ID, SESSION, SID, UUID
from tests.unit.orchestrator.writeback_test import APPROVE_MAPPED, DOC, PR, PRD

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()
RESETS = "2026-10-11T10:00:00+00:00"  # T in the runs' TZ
LIMIT = {"kind": "limit", "resets_at": T}
PICK = "picked the task."
RUNNING = r"running · #1 · \S+"
DONE = r"done · #2 · \S+ → \S+ · exit 0"
LIMITED = r"limit · #2 · \S+ → \S+ · exit -9"  # the Driver kills a limit-hit session
KINDS = ["session", "input", "client_session", "progress", "result", "end"]
RESEARCH = {
    "status": "done",
    "title": "Queue options",
    "summary": "Three queues compared.\nRedis streams fit best.",
    "url": DOC,
}
READ = ("writeback.Q_ATTACHED", {"i": UUID})
SUBSCRIBE = ("linear.M_SUBSCRIBE", {"i": UUID, "e": HUMAN.lower()})
REREAD = ("linear.Q_ISSUE_STATE", {"i": UUID})
TO_REVIEW = ("linear.M_STATE", {"i": UUID, "s": STATES["in_review"]})


def cid(step):
    return linear.client_id(SID, step)


def post(step, body):
    return ("linear.M_COMMENT_ID", {"c": cid(step), "i": UUID, "b": body})


def find(step):
    return ("linear.Q_COMMENT", {"c": cid(step)})


def attach(url, title):
    return ("writeback.M_ATTACH", {"i": UUID, "u": url, "t": title})


class Run(Flow):
    def setUp(self):
        super().setUp()
        self.guard()
        self.rd = os.path.join(self.runs_dir, ID)

    def seed(self, role):
        self.role = role
        self.fake.issue(
            "Session registry",
            identifier=ID,
            id=UUID,
            state="in_progress",
            assignee=self.ids[ACCOUNTS[role]],
            project=PROJECT,
        )

    def run_py(self, steps, mode="new", **scenario):
        self.scene(steps, **scenario)
        res = self.script(
            *RUN,
            *("--uuid", UUID, "--issue", ID, "--project", PROJECT["id"], "--sid", SID),
            *("--assignee", ACCOUNTS[self.role], "--runtime", "claude", "--mode", mode),
            *(("--target", "ophis/agent-pm") if self.role == "engineer" else ()),
            "--input",
            "Do it.\n",
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def wrote(self, since=0):
        return [(r.op, r.account, r.variables, r.result) for r in self.requests()[since:]]

    def session(self, *lines, since=0):
        """The session comment's bodies written since, each `Run <SID> · <line>` and the resume command."""
        bodies = []
        for _, _, v, _ in self.wrote(since):
            if v.get("c") == SESSION and "b" in v and v["b"] not in bodies[-1:]:
                bodies.append(v["b"])
        command = re.escape(f"\n\n```\ncd {shlex.quote(self.rd)} && claude --resume {SID}\n```")
        self.assertEqual(len(bodies), len(lines), bodies)
        for body, line in zip(bodies, lines, strict=True):
            self.assertRegex(body, rf"\ARun {SID} · {line}{command}\Z")
        return bodies

    def framed(self, *calls, end=DONE, created=True, since=0):
        """A run's Linear calls: the session comment's first write (created: not found, then created by its id),
        `calls` as the role's account, the session comment's end and repair."""
        running, ended = self.session(RUNNING, end, since=since)
        update = ("sessions.M_UPDATE", HARNESS_EMAIL, {"c": SESSION, "b": running})
        create = ("linear.M_COMMENT_ID", HARNESS_EMAIL, {"c": SESSION, "i": ID, "b": running})
        return [
            *([(*update, "error"), (*create, "ok")] if created else [(*update, "ok")]),
            *((op, ACCOUNTS[self.role], v, "ok") for op, v in calls),
            ("sessions.M_UPDATE", HARNESS_EMAIL, {"c": SESSION, "b": ended}, "ok"),
            ("linear.Q_COMMENT", HARNESS_EMAIL, {"c": SESSION}, "ok"),
        ]

    def stored(self):
        return [(self.email(c["user"]), c["id"]) for c in self.issue(ID)["comments"]]


class Done(Run):
    def done(self, role, start, outcome, calls, ledger):
        self.seed(role)
        self.run_py([mark("start", PICK), block(outcome)])
        self.assertEqual(self.wrote(), self.framed(post("start", f"{start} {PICK}"), *calls))
        self.assertEqual(self.ledger(ID), {SID: ledger})
        self.assertEqual(self.said(), steps(*ledger))
        self.assertEqual([e["kind"] for e in self.records(ID)], KINDS)
        self.assertEqual(self.records(ID)[-2]["outcome"]["status"], "done")
        self.assertEqual([option(c, "--session-id") for c in self.calls()], [SID])
        account = ACCOUNTS[role]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (account, cid("start")), (account, cid("comment"))],
        )
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.issue(ID)["subscribers"], [HUMAN])
        self.assertEqual(
            self.issue(ID)["attachments"], [{"url": outcome["url"], "title": outcome["title"]}]
        )

    def test_a_researcher_run(self):
        self.done(
            "researcher",
            "Research started:",
            RESEARCH,
            [
                READ,
                SUBSCRIBE,
                post("comment", f"Three queues compared.\nRedis streams fit best.\n\n{DOC}"),
                attach(DOC, "Queue options"),
                REREAD,
                TO_REVIEW,
            ],
            ["start", "comment", f"attach:{DOC}", "move:in_review"],
        )

    def test_a_pm_run(self):
        self.done(
            "pm",
            "PRD started:",
            {
                "status": "done",
                "title": "Session Registry",
                "summary": "PRD for the registry.",
                "url": PRD,
            },
            [
                READ,
                ("writeback.M_TITLE", {"i": UUID, "t": "PRD: Session Registry"}),
                SUBSCRIBE,
                post("comment", f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}"),
                attach(PRD, "Session Registry"),
                REREAD,
                TO_REVIEW,
            ],
            ["start", "retitle", "comment", f"attach:{PRD}", "move:in_review"],
        )
        self.assertEqual(self.issue(ID)["title"], "PRD: Session Registry")

    def test_an_engineer_run(self):
        title = f"{ID}: Session registry"
        self.done(
            "engineer",
            "Build started:",
            {
                "status": "done",
                "title": title,
                "summary": "Adds the registry.\nVerify: python3 -m unittest",
                "url": PR,
            },
            [
                READ,
                SUBSCRIBE,
                post("comment", "Build ready:\nAdds the registry.\nVerify: python3 -m unittest"),
                attach(PR, title),
                REREAD,
                TO_REVIEW,
            ],
            ["start", "comment", f"attach:{PR}", "move:in_review"],
        )


class Undone(Run):
    def test_a_run_without_an_outcome_stays_in_progress(self):
        self.seed("engineer")
        self.run_py([mark("start", PICK), "Working."])
        self.assertEqual(self.wrote(), self.framed(post("start", f"Build started: {PICK}")))
        self.assertEqual(
            self.said(),
            [*steps("start"), f"run no-outcome {ID} error=the agent run returned no outcome"],
        )
        self.assertEqual((self.state(ID), self.ledger(ID)), ("in_progress", {SID: ["start"]}))

    def test_a_usage_limit_moves_to_in_review_once_per_sid(self):
        self.seed("researcher")
        self.run_py([mark("start", PICK), LIMIT], hang=True)
        self.assertEqual(
            self.wrote(),
            self.framed(
                post("start", f"Research started: {PICK}"),
                SUBSCRIBE,
                post("limit", f"Usage limit, resets at {RESETS}."),
                REREAD,
                TO_REVIEW,
                end=LIMITED,
            ),
        )
        limit = f"writeback step {ID} sid={SID} step=limit"
        self.assertEqual(self.said(), [*steps("start", end=-9), limit])
        self.assertEqual(self.records(ID)[-3]["kind"], "limit")
        note = {"kind": "limit", "issue": ID, "sid": SID, "runtime": "claude", "resets_at": RESETS}
        self.assertEqual(self.runs(), [note])
        self.assertEqual(self.state(ID), "in_review")

        self.fake.move(ID, "in_progress", actor=self.ids[HARNESS_EMAIL])
        n = len(self.requests())
        self.run_py([], mode="resume", resume=[LIMIT], hang=True)
        self.assertEqual(
            self.wrote(n),
            self.framed(SUBSCRIBE, REREAD, TO_REVIEW, end=LIMITED, created=False, since=n),
        )
        self.assertEqual(self.runs(), [note, note])
        self.assertEqual([s for _, s in self.stored()].count(cid("limit")), 1)
        researcher = ACCOUNTS["researcher"]
        self.assertEqual(
            self.history(ID),
            [
                ("in_progress", "in_review", researcher),
                ("in_review", "in_progress", HARNESS_EMAIL),
                ("in_progress", "in_review", researcher),
            ],
        )

    def test_a_resume_after_a_lost_ledger_write_posts_no_second_comment(self):
        self.seed("researcher")
        script = [mark("start", PICK), block(RESEARCH)]
        self.run_py(script)
        ledger = self.ledger(ID)
        os.remove(os.path.join(self.rd, "writeback.json"))
        n, said = len(self.requests()), len(self.said())
        self.run_py(script, mode="resume", resume=script)
        self.assertEqual(
            self.wrote(n),
            self.framed(
                find("start"), READ, SUBSCRIBE, find("comment"), REREAD, created=False, since=n
            ),
        )
        self.assertEqual(
            self.said()[said:],
            steps(*ledger[SID], mode="resume", found=("start", "comment")),
        )
        self.assertEqual(self.ledger(ID), ledger)
        self.assertEqual([option(c, "--resume") for c in self.calls()], [None, SID])
        account = ACCOUNTS["researcher"]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (account, cid("start")), (account, cid("comment"))],
        )


if __name__ == "__main__":
    unittest.main()
