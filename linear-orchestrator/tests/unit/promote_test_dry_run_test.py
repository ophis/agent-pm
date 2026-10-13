from orchestrator_test_helpers.unit.promote.test_dry_run_fixtures import (
    TestDryRun as TestDryRunBase,
)
from orchestrator_test_helpers.unit.promote_support import STATES


class TestDryRun1(TestDryRunBase):
    def test_no_mutations(self):
        self.ready("DR-1")
        self.fake.add("DR-2")
        self.fake.moved("DR-2", 30, "Handoff", frm=STATES["In Review"])
        self.run_main("--dry-run")
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(
            self.out.splitlines(),
            [
                "promote DR-1 role=pm project=Deep Research",
                "handoff-bounce DR-2 to=in_review reason=no instructions",
            ],
        )
