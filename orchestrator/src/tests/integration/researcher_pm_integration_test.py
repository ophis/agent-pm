"""The researcher → pm segment as steps (segments.Segment): a Backlog research issue the router skips, the human's
move to Todo, a claimed agent run, write-back, the human's handoff and promote's pm issue; and its branches."""
import os
import sys
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [TESTS, os.path.dirname(TESTS)]
import hermetic  # noqa: E402,F401
import router  # noqa: E402
from flow_fixtures import HARNESS_EMAIL, HUMAN, LIGHT_RESEARCH, RESEARCH_DIR  # noqa: E402
from segments import RESEARCH, Segment  # noqa: E402

BLOCKER = "Pick a session store"
ANSWER = "Only harnesses that run agents in tmux."
EARLIER = "# Session registries (draft)\n\nTmux names each session."


class ResearcherToPm(Segment):
    def test_a1_research_to_a_pm_issue(self):
        """A1: researcher_to_pm (its claim step checks the claim event's task is null); the child is a pm start."""
        self.assert_pm_start(self.researcher_to_pm())

    def test_a2_light_research(self):
        """A2: researcher_to_pm of an issue labeled Light Research; its claim step checks the claim event's task."""
        self.researcher_to_pm(labels=[LIGHT_RESEARCH], task="light-research")

    def test_a3_blocked_until_its_blocker_is_done(self):
        """A3: an unassigned Todo issue blocks the research issue: router.py --issue refuses it, logging who blocks
        it, until the human moves the blocker to Done; then researcher_done."""
        with self.step("seed: backlog"):
            ident = self.backlog_issue("researcher", *RESEARCH)
            blocker = self.fake.issue(BLOCKER)["identifier"]
            self.fake.relate("blocks", blocker, ident)
            self.track(blocker)
        self.backlog_not_claimed(ident)
        self.human_move(ident, "todo", "human: to todo")
        with self.step("router: blocked"):
            self.assertEqual([(e["src"], e["kind"], e.get("issue"), e.get("by")) for e in self.refused(ident)],
                             [("router", "blocked", ident, [blocker])])
        self.human_move(blocker, "done", "human: blocker done")
        self.researcher_done(ident)

    def test_a4_an_answer_starts_a_new_session(self):
        """A4: a run asks; the human answers, back to Todo, and an earlier report is on the docs branch;
        researcher_done's claim is a new session whose input holds the answer and that report."""
        ident = self.researcher_ready()
        self.claim(ident, "researcher", self.script("researcher", "needs_input", ident))
        self.agent_run()
        self.human_say(ident, ANSWER, "todo", "human: answer")
        path = f"{RESEARCH_DIR}2026-10-01-{ident}-draft.md"
        self.publish(path, EARLIER)
        self.researcher_done(ident)
        first, second = self.runs()
        self.assertEqual((first["kind"], second["kind"]), ("start", "start"))
        self.assertNotEqual(first["sid"], second["sid"])
        prompt = self.prompt(ident, second["sid"])
        self.assertIn(ANSWER, self.section(prompt, "The user's comments"))
        self.assertIn(EARLIER, self.section(prompt, f"Earlier version: `{path}`"))

    def test_a5_a_failed_run_is_not_promoted(self):
        """A5: a failed run returns the issue to Todo; promote finds nothing to do."""
        ident = self.researcher_ready()
        self.claim(ident, "researcher", self.script("researcher", "failed", ident))
        self.agent_run()
        self.promote_nothing(ident)

    def test_a6_capped_after_four_failed_runs(self):
        """A6: router.CAP failed runs since the human's move to Todo; router.py --issue then skips the claim and moves
        the issue to In Review with the cap comment, the human subscribed."""
        ident = self.researcher_ready()
        self.wait_next_second(ident)
        for _ in range(router.CAP):
            self.claim(ident, "researcher", self.script("researcher", "failed", ident))
            self.agent_run()
        with self.step("router: cap"):
            attempts = self.attempts(ident)
            self.assertEqual([(e["src"], e["kind"], e.get("issue"), e.get("to"), e.get("reason"))
                              for e in self.refused(ident, attempts)],
                             [("router", "claim-skip", ident, "in_review", f"reached {router.CAP} attempts"),
                              ("router", "pick-none", None, None, "nothing claimable")], attempts)
            self.expect(ident, state="in_review", history=[("todo", "in_review", HARNESS_EMAIL)],
                        comments=[(HARNESS_EMAIL, "cap")], subscribers=[HUMAN])
        self.assertEqual(len(self.runs()), router.CAP)


if __name__ == "__main__":
    unittest.main()
