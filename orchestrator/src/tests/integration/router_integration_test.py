"""router.py --issue as a process, then tmux → run.py → drive → the fake claude → write-back, against the fake
Linear, gh and git (flow_fixtures.Flow). Issue TASK-7 in Todo, assigned to the engineer account, in the mapped
project."""
import os
import signal
import sys
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]
import hermetic  # noqa: E402,F401
from flow_fixtures import ENGINEER, HARNESS_EMAIL, HUMAN, PROJECT, TIMEOUT, Flow, live_tmux  # noqa: E402

ID, TITLE, BRANCH = "TASK-7", "ENG: Session registry", "TASK-7-session-registry"
PR = "https://github.com/acme/widgets/pull/1"
STARTED, READY = "Building the session registry.", "Added the session registry."
START = {"kind": "progress", "name": "start", "text": STARTED}
DONE = {"kind": "outcome", "outcome": {"status": "done", "title": "Session registry", "summary": READY, "url": PR}}
LEDGER = ["start", "comment", f"attach:{PR}", "move:in_review"]
# The harness reads the board, claims and keeps the session comment; the engineer account writes back.
HARNESS_OPS = {"linear.Q_TEAM", "linear.Q_TASK_GROUP", "linear.Q_USER", "router.issues", "router.issues+relations",
               "router.Q_RECHECK", "linear.M_STATE", "issues.Q_ISSUE", "sessions.Q_FIND", "linear.M_COMMENT",
               "sessions.M_UPDATE"}
ENGINEER_OPS = {"linear.M_COMMENT", "writeback.Q_ATTACHED", "linear.M_SUBSCRIBE", "writeback.M_ATTACH",
                "linear.Q_ISSUE_STATE", "linear.M_STATE"}


class Router(Flow):
    def setUp(self):
        super().setUp()
        self.fake.issue(TITLE, identifier=ID, assignee=self.ids[ENGINEER], project=PROJECT,
                        description="Add a session registry.")

    def claim(self, mode="start"):
        """router.py --issue ID exits 0; the sid of runs.jsonl's last line, which is a `mode` line."""
        res = self.router("--issue", ID)
        self.assertEqual(res.returncode, 0, res.stderr)
        last = self.runs()[-1]
        self.assertEqual((last["kind"], last["issue"], last["role"]), (mode, ID, "engineer"))
        return last["sid"]

    def assert_built(self, sid):
        """TASK-7 In Review, claimed by the harness and moved by the engineer; one session comment for sid, done; the
        start and ready comments; the PR attached; the human subscribed; every write-back step ledgered."""
        self.assertEqual(self.state(ID), "in_review")
        self.assertEqual(self.history(ID), [("todo", "in_progress", HARNESS_EMAIL),
                                            ("in_progress", "in_review", ENGINEER)])
        (author, session), *rest = self.comments(ID)
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertRegex(session, rf"^Run {sid} · done · .* · exit 0\n")
        self.assertEqual(rest, [(ENGINEER, f"Build started: {STARTED}"), (ENGINEER, f"Build ready: {READY}")])
        self.assertEqual(self.fake.find(ID)["attachments"], [{"url": PR, "title": "Session registry"}])
        self.assertEqual(self.fake.find(ID)["subscribers"], [HUMAN])
        self.assertEqual(self.ledger(ID), {sid: LEDGER})
        [running] = [v["b"] for v in self.sent("linear.M_COMMENT", HARNESS_EMAIL)]
        self.assertTrue(running.startswith(f"Run {sid} · running · "), running)

    def test_claim_to_in_review(self):
        self.scene(steps=[START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assert_built(sid)
        self.assertEqual(self.runs(), [{"kind": "start", "issue": ID, "sid": sid, "role": "engineer"}])
        prompt = self.transcript(ID, sid)[0]
        self.assertEqual(prompt["type"], "user")
        lines = prompt["message"]["content"].splitlines()
        self.assertIn("Repo: acme/widgets", lines)
        self.assertIn(f"Branch: {BRANCH}", lines)
        self.assertEqual(self.problems(), [])
        self.assertEqual({(r.op, r.account) for r in self.requests()},
                         {(op, HARNESS_EMAIL) for op in HARNESS_OPS} | {(op, ENGINEER) for op in ENGINEER_OPS})
        self.assertTrue(self.sent("sessions.M_UPDATE", HARNESS_EMAIL)[-1]["b"].startswith(f"Run {sid} · done · "))

    def test_a_crash_mid_run_resumes_the_session(self):
        self.scene(steps=[START], hang=True)
        sid = self.claim()
        live_tmux.wait(lambda: "start" in self.ledger(ID).get(sid, []), TIMEOUT, "write-back's start step")
        driver = self.driver("engineer", ID)
        pane = self.server.tmux("list-panes", "-t", f"={driver}", "-F", "#{pane_pid}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        os.kill(int(pane.stdout), signal.SIGKILL)
        try:
            os.kill(self.calls()[0]["pid"], signal.SIGKILL)
        except ProcessLookupError:   # orphaned, it ended by itself
            pass
        live_tmux.wait(lambda: not self.live(driver), TIMEOUT, f"{driver} to end")
        self.assertEqual(self.state(ID), "in_progress")
        [(author, session)] = [c for c in self.comments(ID) if c[1].startswith(f"Run {sid} · ")]
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertTrue(session.startswith(f"Run {sid} · running · "), session)
        self.assertEqual(self.ledger(ID), {sid: ["start"]})
        self.assertEqual([e for e in self.logged() if e["kind"] == "end"], [])

        self.scene(steps=[START, DONE])
        self.assertEqual(self.claim("resume"), sid)
        self.wait_end(sid, ID)
        self.assertEqual([(r["kind"], r["sid"]) for r in self.runs()], [("start", sid), ("resume", sid)])
        first, second = (c["argv"] for c in self.calls())
        self.assertEqual(first[first.index("--session-id") + 1], sid)
        self.assertEqual(second[second.index("--resume") + 1], sid)
        self.assertNotIn("--session-id", second)
        self.assert_built(sid)
        started = [v for v in self.sent("linear.M_COMMENT", ENGINEER) if v["b"].startswith("Build started")]
        self.assertEqual(len(started), 1)
        self.assertEqual(self.problems(), [])

    def test_a_linear_failure_mid_write_back_resumes_the_session(self):
        self.fake.fail("linear.M_STATE", "503", account=ENGINEER)
        self.scene(steps=[START, DONE])
        sid = self.claim()
        self.wait_end(sid, ID)
        self.assertEqual(self.state(ID), "in_progress")
        self.assertEqual(self.ledger(ID), {sid: LEDGER[:3]})
        self.assertEqual(self.comments(ID)[-1], (ENGINEER, f"Build ready: {READY}"))
        [error] = [e for e in self.logged() if e["kind"] == "step-error"]
        self.assertEqual((error["src"], error["issue"], error["sid"], error["step"]),
                         ("writeback", ID, sid, "move:in_review"))
        self.assertIn("503", error["error"])

        self.assertEqual(self.claim("resume"), sid)
        self.wait_end(sid, ID)
        self.assertEqual([(r["kind"], r["sid"]) for r in self.runs()], [("start", sid), ("resume", sid)])
        self.assert_built(sid)
        ready = [v for v in self.sent("linear.M_COMMENT", ENGINEER) if v["b"].startswith("Build ready")]
        self.assertEqual(len(ready), 1)
        self.assertEqual(len(self.sent("writeback.M_ATTACH", ENGINEER)), 1)
        self.assertEqual(self.problems(), [("linear.M_STATE", ENGINEER, "503")])


if __name__ == "__main__":
    unittest.main()
