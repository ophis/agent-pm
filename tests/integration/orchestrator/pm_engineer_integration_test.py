from tests import hermetic  # noqa: F401
from tests.integration.orchestrator.flow_fixtures import HARNESS_EMAIL, MANAGER, ROUTER, fake_linear
from tests.integration.orchestrator.segments import BUILD, REPORT, Segment

import os
import sys
import unittest
from urllib.parse import unquote

import issues
import promote

REWORK = "Add a section on stale sessions."
PRD = "# PRD: Session registry\n\nThe registry maps each tmux session to its run."


class PmToEngineer(Segment):
    def test_b1_prd_to_an_engineer_issue(self):
        child = self.pm_to_engineer()
        [run] = self.runs()
        ident = run["issue"]
        [prd] = self.attachments(ident)
        with self.fake.lock:
            titles = [self.fake.find(i)["title"] for i in (ident, child)]
        self.assertEqual(titles, [f"PRD: {prd['title']}", f"ENG: {prd['title']}"])
        self.assertIn(REPORT, self.section(self.prompt(ident, run["sid"]), "Reports"))

    def test_b2_rework_starts_a_new_session(self):
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
        ident = self.pm_ready()
        self.claim(ident, "pm", self.script("pm", "failed", ident))
        self.agent_run()
        self.promote_nothing(ident)

    def test_b4_a_linear_outage_relating_the_child_is_retried(self):
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
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.script("pm", "none", ident))
        self.agent_run()
        self.resume(ident, "pm", sid, self.script("pm", "done", ident))
        self.agent_run()
        self.handoff(ident, BUILD)
        self.promote(ident, "pm", "engineer")
        self.assertEqual([e["issue"] for e in self.logged() if (e["src"], e["kind"]) == ("run", "no-outcome")],
                         [ident])

    def test_b6_an_attended_run_that_gives_up_is_resumed_headless(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.script("pm", "give_up", ident), tui=True)
        self.agent_run()
        self.resume(ident, "pm", sid, self.script("pm", "done", ident))
        self.agent_run()
        self.handoff(ident, BUILD)
        self.promote(ident, "pm", "engineer")

    def test_b7_a_lost_transcript_starts_a_new_session(self):
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

    def test_b8_a_managed_attended_run_is_recovered_from_the_roster(self):
        ident = self.pm_ready()
        sid = self.claim(ident, "pm", self.script("pm", "give_up", ident), tui=True, managed=True)
        driver, tui = self.driver("pm", ident), self.pending.tui
        events = os.path.join(self.apm, "managers", MANAGER, "events")
        entry = {"kind": "pipeline", "sid": sid, "cwd": os.path.realpath(self.workdir(ident)), "note": ident,
                 "tui": tui, "pane": None, "split": None, "state": "working",
                 "resume": [sys.executable, ROUTER, "--issue", ident, "--tui", "--events", events,
                            "--manager", MANAGER]}
        self.assertEqual(self.roster(), {driver: {**entry, "opener": None, "split_from": MANAGER}})
        self.agent_run()
        self.kill_tui(ident)
        self.resume(ident, "pm", sid, self.script("pm", "done", ident), entry=driver)
        self.assertEqual(self.roster(), {driver: {**entry, "opener": MANAGER, "split_from": None}})
        self.agent_run()


if __name__ == "__main__":
    unittest.main()
