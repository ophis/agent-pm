from orchestrator_test_helpers.unit.promote.test_identity_fixtures import (
    TestIdentity as TestIdentityBase,
)
from orchestrator_test_helpers.unit.promote_support import STATES
from orchestrator_test_helpers.unit.run_fixtures import HARNESS


class TestIdentity1(TestIdentityBase):
    def test_every_linear_call_goes_as_the_harness(self):
        self.ready("DR-1")
        self.fake.add("DR-2")
        self.fake.moved("DR-2", 30, "Handoff", frm=STATES["In Review"])
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(
            self.out.splitlines(),
            ["promote DR-1 to=C-1", "handoff-bounce DR-2 to=in_review reason=no instructions"],
        )
        self.assertEqual(set(self.fake.services), {HARNESS})
