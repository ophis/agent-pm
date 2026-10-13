from agent_pm.linear_orchestrator.engine import config
from orchestrator_tests.case import Case
from orchestrator_tests.unit.config_data import ID, SID, A, B


class TuiNames(Case):
    def test_prefix_of_a_role_and_issue(self):
        for role_name in ("engineer", "dummy-tester", "pm2"):
            with self.subTest(role=role_name):
                self.assertEqual(config.tui_prefix(role_name, ID), f"{role_name}-{ID}-")
        self.assertEqual(config.session("pm", ID), f"agent-pm-pm-{ID}")

    def test_a_prefix_outside_the_contract(self):
        for role_name, ident in (
            ("Engineer", ID),
            ("eng_x", ID),
            ("", ID),
            ("2pm", ID),
            ("engineer", "task-12"),
            ("engineer", "TASK-12 "),
        ):
            with (
                self.subTest(role=role_name, ident=ident),
                self.assertRaisesRegex(ValueError, "TUI session prefix"),
            ):
                config.tui_prefix(role_name, ident)
        with self.assertRaises(ValueError) as cm:
            config.tui_prefix("Engineer", ID)
        self.assertEqual(
            str(cm.exception),
            r"TUI session prefix 'Engineer-TASK-12': want ([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)",
        )

    def test_the_name_round_trips(self):
        self.assertEqual(config.TUI.pattern, r"([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)-[0-9a-f]{8}")
        self.assertEqual(config.tui_name("engineer", ID, SID), A)
        self.assertEqual(
            config.tui_issue(config.tui_name("dummy-tester", "T-1", SID)), ("dummy-tester", "T-1")
        )

    def test_found_by_name_only(self):
        found = {
            A: ("engineer", ID),
            B: ("dummy-tester", ID),
            "pm-TASK-12-cccccccc": ("pm", ID),
            "engineer-TASK-1-aaaaaaaa": ("engineer", "TASK-1"),
            "pm-T-1-aaaaaaaa": ("pm", "T-1"),
        }
        others = [
            "Engineer-TASK-12-bbbbbbbb",
            "agent-pm-engineer-TASK-12",
            "agent-pm-engineer-TASK-12345678",
            "engineer-0b6f2c1e",
            "engineer-TASK-12-0B6F2C1E",
            "engineer-TASK-12-0b6f2c1",
            "engineer-task-12-0b6f2c1e",
            "mine",
            "pm-T-1-aaaaaaaa-drive",
            "agent-pm-pm-T-1",
        ]
        for name, want in [*found.items(), *((o, None) for o in others)]:
            with self.subTest(name):
                self.assertEqual(config.tui_issue(name), want)

