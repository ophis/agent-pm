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
import promote  # noqa: E402
from flow_fixtures import HARNESS_EMAIL, fake_linear  # noqa: E402
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

    def test_b4_a_linear_outage_relating_the_child_is_retried(self):
        """B4: promote.py's relate answers 503: it exits 1 having made the child, its key promote.child_id of the first
        Handoff move after the last move to In Review; no relation, the source still in Handoff, no prune (the failed
        relate is the last request). The next promote relates that child, makes no other and moves the source to
        Done."""
        ident = self.pm_ready()
        self.claim(ident, "pm", self.script("pm", "done", ident))
        self.agent_run()
        self.handoff(ident, BUILD)
        with self.step("promote: linear 503"):
            self.fake.fail("promote.M_RELATE", "503", account=HARNESS_EMAIL)
            events = len(self.logged())
            res = self.promote_now()
            self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", ""))
            self.assertEqual([{k: v for k, v in e.items() if k != "ts"} for e in self.logged()[events:]],
                             [{"src": "promote", "kind": "linear-error", "op": "issueRelationCreate", "status": 503}])
            last = self.requests()[-1]
            self.assertEqual((last.op, last.result), ("promote.M_RELATE", "503"))
            with self.fake.lock:
                src = self.fake.find(ident)
                moves = [(fake_linear.NAMES[h["toStateId"]], h["createdAt"]) for h in src["history"]]
                cutoff = max(at for to, at in moves if to == "in_review")
                first = min(at for to, at in moves if to == "handoff" and at > cutoff)
                key = promote.child_id(src["id"], "engineer", first)
                made, relations = self.fake.find(key), list(self.fake.relations)
            self.assertIsNotNone(made, f"no issue {key}")
            self.assertEqual(relations, [])
            self.assertEqual([v["in"]["id"] for v in self.sent("promote.M_CREATE")], [key])
            child = made["identifier"]
            self.track(child, state="todo")
            self.expect_problem(("promote.M_RELATE", HARNESS_EMAIL, "503"))
        self.assertEqual(self.promote(ident, "pm", "engineer"), child)
        self.assertEqual([v["in"]["id"] for v in self.sent("promote.M_CREATE")], [key])

    def test_b5_a_run_without_an_outcome_is_resumed(self):
        """B5: a run ends without an outcome (run.py's one no-outcome event), the issue still In Progress; router.py
        --issue resumes its session, which ends in review (no second start comment); handoff, promote."""
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.script("pm", "none", ident))
        self.agent_run()
        self.resume(ident, "pm", sid, self.script("pm", "done", ident))
        self.agent_run()
        self.handoff(ident, BUILD)
        self.promote(ident, "pm", "engineer")
        self.assertEqual([e["issue"] for e in self.logged() if (e["src"], e["kind"]) == ("run", "no-outcome")],
                         [ident])

    def test_b7_a_lost_transcript_starts_a_new_session(self):
        """B7: a run ends without an outcome and its transcript is lost: router.py --issue cannot resume it, comments
        router.INTERRUPTED and moves the issue back to Todo (a recover event); pm_done's claim is a new session."""
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.script("pm", "none", ident))
        self.agent_run()
        with self.step("drop transcript"):
            os.remove(self.transcript_path(ident, sid))
        with self.step("router: interrupted"):
            self.assertEqual([(e["src"], e["kind"], e.get("issue"), e.get("to"), e.get("reason"), e.get("sid"))
                              for e in self.refused(ident)],
                             [("router", "recover", ident, "todo", "no transcript", sid)])
            self.expect(ident, state="todo", history=[("in_progress", "todo", HARNESS_EMAIL)],
                        comments=[(HARNESS_EMAIL, "interrupted")])
        self.pm_done(ident)


if __name__ == "__main__":
    unittest.main()
