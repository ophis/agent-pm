from agent_pm.linear_orchestrator.engine import promote
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.promote.test_idempotency_fixtures import (
    TestIdempotency as TestIdempotencyBase,
)
from orchestrator_test_helpers.unit.promote_support import CHILD, STATES, ago


class TestIdempotency1(TestIdempotencyBase):
    def test_existing_child_with_relation(self):
        src = self.ready()
        cid = promote.child_id("DR-1", "pm", ago(30))
        self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
        src["inverse"].append(cid)
        self.run_main()
        self.assertNotIn(promote.M_RELATE, [q for q, _ in self.fake.mutations])
        self.assertEqual(src["state"], "Done")

    def test_rehandoff_from_done_reuses_child(self):
        src = self.ready()
        self.run_main()
        self.fake.moved("DR-1", 15, "Handoff", frm=STATES["Done"])
        src["state"] = "Handoff"
        self.run_main()
        self.assertEqual(len(self.fake.children), 1)
        self.assertEqual(src["state"], "Done")

    def test_rehandoff_after_failure_bounce_reuses_child(self):
        src = self.ready(handoff=90)
        cid = promote.child_id("DR-1", "pm", ago(90))
        self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
        self.fake.moved("DR-1", 60, "In Review", frm=STATES["Handoff"])
        self.fake.moved("DR-1", 10, "Handoff", frm=STATES["In Review"])
        self.run_main()
        self.assertEqual(len(self.fake.children), 1)
        self.assertNotIn(promote.M_CREATE, [q for q, _ in self.fake.mutations])
        self.assertEqual(src["relations"], [cid])
        self.assertEqual(src["state"], "Done")

    def test_new_review_cycle_gives_new_child(self):
        src = self.ready()
        self.run_main()
        self.fake.moved("DR-1", 20, "In Review", frm=STATES["In Progress"])
        self.fake.said("DR-1", 15, body="again")
        self.fake.moved("DR-1", 10, "Handoff", frm=STATES["In Review"])
        src["state"] = "Handoff"
        self.run_main()
        self.assertEqual(len(self.fake.children), 2)

    def test_child_id(self):
        a = promote.child_id("DR-1", "pm", "2026-09-27T10:00:00.000Z")
        self.assertEqual(a, CHILD)
        self.assertEqual(a, linear.client_id("DR-1", "pm", "2026-09-27T10:00:00.000Z"))
        self.assertNotEqual(a, promote.child_id("DR-1", "pm", "2026-09-27T10:00:01.000Z"))
        self.assertNotEqual(a, promote.child_id("DR-1", "engineer", "2026-09-27T10:00:00.000Z"))
        self.assertEqual(a[14], "4")
