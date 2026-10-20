"""A local issue from review to archive (spec AC12): bin/agent-pm-orchestrator router, promote and
prune as real processes with tracker = "local". A researcher's run reaches review; approved, it waits
out promote's undo window, then becomes pm's issue once; that issue done, prune cleans the run and
archives it flat, skipping it while a sub-issue is open.
"""

import os
import re
import unittest
from datetime import UTC, datetime, timedelta

from agent_pm.orchestrator.engine.config import BOARD_ENV
from agent_pm.orchestrator.trackers.local.local_tracker import LocalTracker
from orchestrator_tests.integration.flow_fixtures import BinFlow

from delegate.tests.replies import block

REPORT = "https://github.com/ophis/private_docs/blob/main/Research/queues.md"
OUTCOME = {"status": "done", "title": "Queue survey", "summary": "Compared.", "url": REPORT}
ROLES = ("researcher", "pm", "engineer")


def issue(ident, *, front="", history=""):
    return (
        f'---\nid: {ident}\ntitle: "Queue survey"\ncreated: 2026-10-20T09:00:00+00:00\n{front}---\n'
        f"### Description\nSurvey the queues.\n### History\n{history}"
    )


def minute(at):
    """A History time as the processes (TZ=UTC) write it."""
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


class ReviewToArchive(BinFlow):
    def setUp(self):
        super().setUp()
        self.board = os.path.join(self.root, "board")
        state = os.path.join(self.board, ".state")
        self.workdirs, self.logs = os.path.join(state, "work"), os.path.join(state, "logs")
        config = os.path.join(self.agent_pm, "config.local.toml")
        with open(config, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count('tracker = "linear"\n'), 1)
        with open(config, "w", encoding="utf-8") as f:
            f.write(text.replace('tracker = "linear"\n', 'tracker = "local"\n'))
        self.put("researcher/ready/LOC-1 Queue survey.md", issue("LOC-1", front="parent: LOC-9\n"))
        self.put("pm/backlog/LOC-3 Sub.md", issue("LOC-3", front="parent: LOC-10\n"))
        self.put(".next-id", "10\n")

    def bin_env(self):
        return {**super().bin_env(), BOARD_ENV: self.board}

    def at(self, rel):
        return os.path.join(self.board, rel)

    def put(self, rel, text):
        os.makedirs(os.path.dirname(self.at(rel)), exist_ok=True)
        with open(self.at(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(self.at(rel), encoding="utf-8") as f:
            return f.read()

    def move(self, rel, to, *, age=None):
        """A person's move of rel into to (a state dir), its mtime age ago when given."""
        os.makedirs(self.at(to), exist_ok=True)
        os.rename(self.at(rel), target := os.path.join(self.at(to), os.path.basename(rel)))
        if age is not None:
            when = (datetime.now(UTC) - age).timestamp()
            os.utime(target, (when, when))
        return os.path.relpath(target, self.board)

    def files(self):
        """Every board file but its state root's, by path."""
        found = {}
        for folder, dirs, names in os.walk(self.board):
            dirs[:] = [d for d in dirs if d != ".state"]
            for rel in (os.path.relpath(os.path.join(folder, n), self.board) for n in names):
                found[rel] = self.read(rel)
        return found

    def run_bin(self, *argv):
        res = self.linear(*argv)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""), self.said())
        return res

    def tracker(self):
        return LocalTracker(self.board, ROLES)

    def test_review_to_archive(self):
        with self.subTest("router: a researcher's run reaches review"):
            self.scene([block(OUTCOME)])
            self.run_bin("router", "--issue", "LOC-1")
            [row] = self.runs()
            self.wait_end(row["sid"], "LOC-1", role="researcher")
            self.assertTrue(os.path.exists(self.at("researcher/review/LOC-1 Queue survey.md")))
            self.assertTrue(os.path.isdir(os.path.join(self.workdirs, "LOC-1")))

        with self.subTest("approved, inside the undo window: promote waits"):
            source = self.move("researcher/review/LOC-1 Queue survey.md", "researcher/approved")
            text = self.read(source)  # a person's comment: `###` last in Comments
            self.put(
                source, text.replace("\n### History\n", "\n###\nWrite the PRD.\n\n### History\n")
            )
            before = self.files()
            self.run_bin("promote")
            self.assertEqual(self.files(), before)
            self.assertIn("promote handoff-wait LOC-1 reason=in Handoff under 10 min", self.said())

        with self.subTest("past the undo window: promote makes pm's issue once"):
            back = (datetime.now(UTC) - timedelta(minutes=11)).timestamp()
            os.utime(self.at(source), (back, back))
            self.run_bin("promote")
            [made] = os.listdir(self.at("pm/ready"))
            self.assertEqual(made, "LOC-10 PRD: Queue survey.md")
            text = self.read(f"pm/ready/{made}")
            self.assertIn("\nparent: LOC-9\n", text)
            self.assertIn("\nCreated by promote.\n", text)
            self.assertRegex(text, r"### History\n- \S+ \S+ ready self\n$")
            done = "researcher/done/LOC-1 Queue survey.md"
            self.assertIn("\nrelated: [LOC-10]\n", self.read(done))
            history = re.findall(r"- \S+ \S+ (\w+) (self|other)", self.read(done))
            self.assertEqual(history[-2:], [("approved", "other"), ("done", "self")])
            self.assertEqual(
                self.tracker().detail("LOC-1").comments[-1].body, "Promoted to LOC-10."
            )
            self.assertEqual(self.read(".next-id"), "11\n")
            before = self.files()
            self.run_bin("promote")
            self.assertEqual(self.files(), before)

        with self.subTest("done, a sub-issue open: prune cleans the run, archives nothing"):
            three = timedelta(hours=3)
            text = self.read(done)
            line = re.findall(r"- \S+ \S+ done self\n", text)[-1]
            self.put(done, text.replace(line, f"- {minute(datetime.now(UTC) - three)} done self\n"))
            moved = self.move(f"pm/ready/{made}", "pm/done", age=three)
            self.run_bin("prune")
            said = self.said()
            self.assertIn(f"prune prune-closed LOC-1 session={row['sid']}", said)
            self.assertIn("prune prune-removed LOC-1 what=agent dir", said)
            self.assertIn(
                "prune prune-skip LOC-10 reason=archive: sub-issue LOC-3 not finished", said
            )
            self.assertFalse(os.path.exists(self.at("archive")))

        with self.subTest("the sub-issue done: prune archives both flat"):
            self.move("pm/backlog/LOC-3 Sub.md", "pm/done", age=three)
            self.run_bin("prune")
            self.assertIn("prune prune-archived LOC-10", self.said())
            self.assertEqual(
                sorted(os.listdir(self.at("archive"))),
                ["LOC-10 PRD: Queue survey.md", "LOC-3 Sub.md"],
            )
            self.assertFalse(os.path.exists(self.at(moved)))
            self.assertIn(
                "Archived from pm/done/.", self.read("archive/LOC-10 PRD: Queue survey.md")
            )
            self.assertTrue(os.path.exists(self.at(done)))  # a researcher's report stays
            self.assertEqual(self.tracker().read("LOC-10").state.value, "done")


if __name__ == "__main__":
    unittest.main()
