from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.client_id_fixtures import ClientId as ClientIdBase
from orchestrator_test_helpers.unit.linear.data import CHILD


class ClientId1(ClientIdBase):
    def test_promotes_child_id_values(self):
        self.assertEqual(linear.client_id("DR-1", "pm", "2026-09-27T10:00:00.000Z"), CHILD)
        self.assertNotEqual(linear.client_id("DR-1", "pm", "2026-09-27T10:00:01.000Z"), CHILD)
        self.assertNotEqual(linear.client_id("DR-1", "engineer", "2026-09-27T10:00:00.000Z"), CHILD)

    def test_stable_and_shaped_as_uuid_v4(self):
        sid = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        a = linear.client_id(sid, "session")
        self.assertEqual(a, linear.client_id(sid, "session"))
        self.assertEqual(a, "6aa10f29-00e8-42bb-88f1-e74018389257")
        self.assertNotEqual(a, linear.client_id(sid, "start"))
        self.assertEqual(linear.client_id(sid, "start"), linear.client_id(f"{sid}/start"))
        for cid in (a, CHILD):
            self.assertRegex(
                cid, r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
            )
