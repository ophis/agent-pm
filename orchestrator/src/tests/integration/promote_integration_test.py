"""promote.py as a process against the fake Linear and the test's tmux server (flow_fixtures.Flow): a researcher's
Handoff issue becomes the pm's child issue, also after a failed move; prune after Done."""
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]
import hermetic  # noqa: E402,F401
import fake_linear  # noqa: E402
import promote  # noqa: E402
from flow_fixtures import ACCOUNTS, ENGINEER, HARNESS_EMAIL, HUMAN, PROJECT, TIMEOUT, Flow  # noqa: E402

SOURCE, TITLE = "TASK-12", "Session registries in agent harnesses"
INSTRUCTIONS = "Write the PRD for a session registry."
MUTATIONS = {op for op, text in fake_linear.texts().items() if text.startswith("mutation")}
WRITES = ["promote.M_CREATE", "promote.M_RELATE", "linear.M_STATE", "linear.M_COMMENT"]
DONE, OPEN = "TASK-20", "TASK-21"
TUI = f"engineer-{DONE}-0123abcd"
CLONE = ("src", "acme", "widgets")


class Tick(Flow):
    """promote.py runs and the checks of its every run."""

    def tick(self):
        """promote.py exits 0, printing nothing."""
        res = self.promote()
        self.assertEqual((res.returncode, res.stderr, res.stdout), (0, "", ""))

    def events(self):
        """orchestrator.jsonl's events without ts."""
        return [{k: v for k, v in e.items() if k != "ts"} for e in self.logged()]

    def assert_requests(self, writes, problems=()):
        """Every request the harness account's and answered ok but `problems` ((op, account, result) each); the
        mutations `writes`, oldest first."""
        self.assertEqual(self.problems(), list(problems))
        requests = self.requests()
        self.assertEqual({r.account for r in requests}, {HARNESS_EMAIL})
        self.assertEqual([r.op for r in requests if r.op in MUTATIONS], writes)


class Promote(Tick):
    """SOURCE, the researcher's in PROJECT, moved In Review → Handoff 20 min ago (past promote.MATURE, within GRACE),
    the human's instructions commented between."""

    def setUp(self):
        super().setUp()
        now, researcher, human = datetime.now(timezone.utc), self.ids[ACCOUNTS["researcher"]], self.ids[HUMAN]
        self.fake.issue(TITLE, identifier=SOURCE, state="in_progress", assignee=researcher, project=PROJECT)
        self.fake.move(SOURCE, "in_review", actor=researcher, at=now - timedelta(minutes=40))
        self.fake.comment(SOURCE, human, INSTRUCTIONS, at=now - timedelta(minutes=30))
        self.fake.move(SOURCE, "handoff", actor=human, at=now - timedelta(minutes=20))
        with self.fake.lock:
            source = self.fake.find(SOURCE)
            (said,), handoff = source["comments"], source["history"][-1]
            self.source, self.url, name = source["id"], source["url"], self.fake.users[human]["name"]
            self.instructions = f"## Instructions\n{name}, {said['createdAt']}:\n{INSTRUCTIONS}"
            self.child = promote.child_id(source["id"], "pm", handoff["createdAt"])

    def assert_promoted(self):
        """The child issue: the pm's, Todo, in PROJECT, titled and described from SOURCE, related to it; SOURCE Done
        by the harness, its one comment after the human's naming the child. Returns the child's identifier."""
        with self.fake.lock:
            child = self.fake.find(self.child)
            self.assertIsNotNone(child, f"no issue {self.child}")
            child, relations = dict(child), list(self.fake.relations)
        self.assertEqual((self.state(self.child), child["assignee"], child["project"], child["title"]),
                         ("todo", self.ids[ACCOUNTS["pm"]], PROJECT, f"PRD: {TITLE}"))
        self.assertEqual(child["description"].split("\n\n")[:2],
                         [f"Handoff from {SOURCE}: {self.url}", self.instructions])
        self.assertEqual(relations, [("related", self.source, self.child)])
        self.assertEqual(self.state(SOURCE), "done")
        self.assertEqual(self.history(SOURCE), [("in_progress", "in_review", ACCOUNTS["researcher"]),
                                                ("in_review", "handoff", HUMAN), ("handoff", "done", HARNESS_EMAIL)])
        self.assertEqual(self.comments(SOURCE), [(HUMAN, INSTRUCTIONS),
                                                 (HARNESS_EMAIL, f"Promoted to {child['identifier']}.")])
        return child["identifier"]

    def test_handoff_to_a_child_issue(self):
        self.tick()
        child = self.assert_promoted()
        self.assert_requests(WRITES)
        self.assertEqual(self.events(), [{"src": "promote", "kind": "promote", "issue": SOURCE, "to": child}])

    def test_a_failed_move_is_retried_without_a_second_child(self):
        self.fake.fail("linear.M_STATE", "error", account=HARNESS_EMAIL)
        failed = [("linear.M_STATE", HARNESS_EMAIL, "error")]
        self.tick()
        with self.fake.lock:
            self.assertIsNotNone(self.fake.find(self.child), f"no issue {self.child}")
            self.assertEqual(self.fake.relations, [("related", self.source, self.child)])
        self.assertEqual(self.state(SOURCE), "handoff")
        self.assertEqual(self.comments(SOURCE), [(HUMAN, INSTRUCTIONS)])
        [error] = self.events()
        self.assertEqual((error["src"], error["kind"], error["issue"]), ("promote", "handoff-error", SOURCE))
        self.assertIn("linear.M_STATE: injected error", error["error"])
        self.assert_requests(WRITES[:3], failed)

        self.tick()
        child = self.assert_promoted()
        self.assertEqual(self.events()[1:], [{"src": "promote", "kind": "promote", "issue": SOURCE, "to": child}])
        self.assert_requests(WRITES[:3] + WRITES[2:], failed)


class Prune(Tick):
    def git_init(self, path):
        """A clone at path, by a real `git init` with env()."""
        res = subprocess.run(["git", "init", "--quiet", path], env=self.env(), stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=TIMEOUT)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_prune_after_done(self):
        engineer = self.ids[ENGINEER]
        done_id = self.fake.issue("ENG: Session registry", identifier=DONE, state="in_review", assignee=engineer,
                                  project=PROJECT)["id"]
        self.fake.move(DONE, "done", actor=self.ids[HUMAN], at=datetime.now(timezone.utc) - timedelta(hours=25))
        self.fake.issue("ENG: Session reaper", identifier=OPEN, state="in_progress", assignee=engineer, project=PROJECT)
        work = os.path.join(self.apm, "work")
        gone = [os.path.join(work, DONE, *p) for p in (CLONE, ("publish",), ("tmp",))]
        kept = os.path.join(work, OPEN, *CLONE)
        for path in (*gone[:2], kept):
            self.git_init(path)
        os.makedirs(gone[2])
        with open(os.path.join(gone[2], "x"), "w"):
            pass
        made = self.server.tmux("new-session", "-d", "-s", TUI, "cat", "-")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertTrue(self.live(TUI))

        self.tick()
        self.assertEqual([p for p in gone if os.path.lexists(p)], [])
        self.assertFalse(self.live(TUI))
        self.assertTrue(os.path.isdir(os.path.join(kept, ".git")))
        with self.fake.lock:
            self.assertEqual([self.fake.find(i)["archived"] for i in (DONE, OPEN)], [True, False])
        self.assertEqual(self.sent("prune.M_ARCHIVE"), [{"i": done_id}])
        self.assert_requests(["prune.M_ARCHIVE"])
        removed = (("src/acme/widgets", "clone"), ("publish", "clone"), ("tmp", "temp files"))
        self.assertEqual(self.events(), [
            {"src": "prune", "kind": "prune-closed", "issue": DONE, "session": TUI},
            *({"src": "prune", "kind": "prune-removed", "entry": f"{DONE}/{e}", "what": w} for e, w in removed),
            {"src": "prune", "kind": "prune-archived", "issue": DONE}])


if __name__ == "__main__":
    unittest.main()
