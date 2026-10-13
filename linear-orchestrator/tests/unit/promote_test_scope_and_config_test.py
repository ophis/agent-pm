from unittest import mock

from agent_pm.linear_orchestrator.engine import promote
from orchestrator_test_helpers.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_test_helpers.unit.board_ids import TEAM
from orchestrator_test_helpers.unit.promote.test_scope_and_config_fixtures import (
    TestScopeAndConfig as TestScopeAndConfigBase,
)
from orchestrator_test_helpers.unit.promote_support import (
    PM_NEXT,
    ROLE,
    STATES,
    FakeLinear,
    ago,
    instructions,
)


class TestScopeAndConfig1(TestScopeAndConfigBase):
    def test_role_without_next_untouched(self):
        src = self.ready(role="pm")
        self.run_main()
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(src["state"], "Handoff")

    def test_stage_prefixes(self):
        self.fake = FakeLinear()
        self.write_config(PM_NEXT)
        self.ready(role="pm", title="PRD: Title DR-1")
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertEqual(
            (child["projectId"], child["assigneeId"], child["title"]),
            ("p-dr", "u-engineer", "ENG: Title DR-1"),
        )
        self.assertEqual(
            instructions(child["description"]), f"## Instructions\nMe, {ago(45)}:\nbuild X"
        )

    def test_instructions_optional(self):
        self.write_config(PM_NEXT)
        src = self.fake.add("PD-1", project="Product Design", role="pm")
        self.fake.moved("PD-1", 60, "In Review")
        self.fake.moved("PD-1", 30, "Handoff", frm=STATES["In Review"])
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertEqual((child["projectId"], child["title"]), ("p-pd", "ENG: Title PD-1"))
        self.assertNotIn("## Instructions", child["description"])
        self.assertEqual(src["state"], "Done")

    def test_handoff_scans_role_accounts_in_projects(self):
        self.ready("DR-1", role=None)
        self.ready("DR-2", role="someone")
        self.ready("DR-3", project=None)
        self.run_main()
        self.assertEqual(self.fake.mutations, [])
        self.assertEqual(
            self.fake.handoff_vars, {"t": TEAM, "s": IDS_BY_KEY["handoff"], "a": mock.ANY}
        )
        self.assertEqual(sorted(self.fake.handoff_vars["a"]), sorted(f"u-{r}" for r in ROLE))
        self.assertIn("team: { id: { eq: $t } }", promote.Q_HANDOFF)
        self.assertIn("state: { id: { eq: $s } }", promote.Q_HANDOFF)
        self.assertIn("project: { null: false }", promote.Q_HANDOFF)
        self.assertIn("assignee: { id: { in: $a } }", promote.Q_HANDOFF)

    def test_unknown_flag(self):
        self.assertEqual(self.run_main("--nope"), 2)
