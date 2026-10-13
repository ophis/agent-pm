from agent_pm.linear_orchestrator.engine import promote
from orchestrator_test_helpers.unit.promote.test_child_title_fixtures import (
    TestChildTitle as TestChildTitleBase,
)


class TestChildTitle1(TestChildTitleBase):
    def test_child_title(self):
        for prefix, src_prefix, src, want in (
            ("TDD", "PRD", "PRD: Session Registry", "TDD: Session Registry"),
            ("TDD", "PRD", "PRD: PRD: X", "TDD: PRD: X"),
            ("TDD", "PRD", "PRD:\nX", "TDD: X"),
            ("TDD", "PRD", "Fix: X", "TDD: Fix: X"),
            ("TDD", "PRD", "PRD:X", "TDD: PRD:X"),
            ("TDD", "PRD", "prd: X", "TDD: prd: X"),
            ("TDD", "PRD", "PRDs: X", "TDD: PRDs: X"),
            ("TDD", "PRD", "X PRD: Y", "TDD: X PRD: Y"),
            ("TDD", "PRD", "PRD: ", "TDD: PRD:"),
            ("PRD", None, "PRD: X", "PRD: PRD: X"),
            ("PRD", "", "PRD: X", "PRD: PRD: X"),
        ):
            with self.subTest(src_prefix=src_prefix, src=src):
                self.assertEqual(promote.child_title(prefix, src_prefix, src), want)
