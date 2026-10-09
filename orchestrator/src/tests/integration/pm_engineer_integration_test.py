"""The pm → engineer segment as steps (segments.Segment): a Backlog pm issue handed off from a done research issue,
the router's skip, the human's move to Todo, a claimed agent run, write-back, the human's handoff and promote's engineer
issue; and its branches."""
import os
import sys
import unittest
from urllib.parse import unquote

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]
import hermetic  # noqa: E402,F401
import issues  # noqa: E402
from segments import BUILD, REPORT, Segment  # noqa: E402

REWORK = "Add a section on stale sessions."
PRD = "# PRD: Session registry\n\nThe registry maps each tmux session to its run."


class PmToEngineer(Segment):
    def test_b1_prd_to_an_engineer_issue(self):
        """B1: pm_to_engineer (its run step checks the retitle, its promote step the child's title); the run's input
        holds the report the pm issue's Source links; titles `PRD: <outcome title>`, then `ENG: <outcome title>`."""
        child = self.pm_to_engineer()
        [run] = self.runs()
        ident = run["issue"]
        [prd] = self.attachments(ident)
        with self.fake.lock:
            titles = [self.fake.find(i)["title"] for i in (ident, child)]
        self.assertEqual(titles, [f"PRD: {prd['title']}", f"ENG: {prd['title']}"])
        self.assertIn(REPORT, self.section(self.prompt(ident, run["sid"]), "Reports"))

    def test_b2_rework_starts_a_new_session(self):
        """B2: a PRD done; the human asks for rework, back to Todo, and the PRD is on the docs branch at the url the run
        reported; pm_done's claim is a new session whose input holds the rework comment and that PRD, its report the
        same url; the child's Instructions hold only the handoff comment."""
        ident = self.pm_ready()
        first = self.script("pm", "done", ident)
        self.claim(ident, "pm", first)
        self.agent_run()
        self.human_say(ident, REWORK, "todo", "human: rework")
        url = first.report["url"]
        path = unquote(url.removeprefix(self.doc_url("")))
        self.publish(path, PRD)
        child = self.pm_done(ident, url)
        one, two = self.runs()
        self.assertEqual((one["kind"], two["kind"]), ("start", "start"))
        self.assertNotEqual(one["sid"], two["sid"])
        prompt = self.prompt(ident, two["sid"])
        self.assertIn(REWORK, self.section(prompt, "The user's later words"))
        self.assertIn(PRD, self.section(prompt, f"Earlier version: `{path}`"))
        with self.fake.lock:
            description = self.fake.find(child)["description"]
        self.assertEqual(issues.parse_handoff(description).instructions.partition(":\n")[2], BUILD)

    def test_b3_a_failed_run_is_not_promoted(self):
        """B3: a failed run moves the issue to In Review, the human subscribed; promote finds nothing to do."""
        ident = self.pm_ready()
        self.claim(ident, "pm", self.script("pm", "failed", ident))
        self.agent_run()
        self.promote_nothing(ident)


if __name__ == "__main__":
    unittest.main()
