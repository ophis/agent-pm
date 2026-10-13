import os
import re

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.case import REPO
from orchestrator_test_helpers.unit.board_ids import HEADER, role
from orchestrator_test_helpers.unit.config.data import LABEL1, LABEL2, PIPELINE, ROLES, fields
from orchestrator_test_helpers.unit.config.runnable_fixtures import Runnable as RunnableBase


class Runnable1(RunnableBase):
    def test_roles_in_pipeline_order_tasks_from_the_index(self):
        roles = self.runs()
        self.assertEqual(fields(roles), ROLES)
        self.assertEqual(list(roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            {r: x.tasks for r, x in roles.items()},
            {r: tuple(delegate.task_index(r)) for r in roles},
        )
        roles = self.runs(HEADER + role("engineer") + role("researcher", "max_runs = 3"))
        self.assertEqual(
            [(r, x.max_runs) for r, x in roles.items()], [("engineer", 1), ("researcher", 3)]
        )
        text = PIPELINE + f'[task_labels]\nlight-research = "{LABEL1}"\nbuild = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])

    def test_tasks_by_role(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        self.assertEqual(
            {r: (x.kind, x.prefix, x.files) for r, x in config.TASKS.items()},
            {
                "researcher": ("research", "", False),
                "pm": ("design", "PRD", False),
                "engineer": ("build", "ENG", True),
            },
        )

    def test_rejected(self):
        cases = [
            (PIPELINE + role("ghost"), "role 'ghost' is not in delegate/config.toml"),
            (PIPELINE + role("dummy-tester"), "role 'dummy-tester' has no entry in config.TASKS"),
            (
                HEADER + role("researcher", 'next = "pm"'),
                "next of 'researcher' names undefined role 'pm'",
            ),
            (
                HEADER + role("researcher") + role("pm", 'next = "researcher"'),
                "next of 'pm' is role 'researcher', whose config.TASKS entry has no prefix",
            ),
            (
                PIPELINE.replace("linear-api-key-pm", "linear-api-key-researcher"),
                "[roles.pm] key 'linear-api-key-researcher' is also [roles.researcher]'s",
            ),
            (
                PIPELINE.replace("linear-api-key-engineer", "linear-api-key"),
                "[roles.engineer] key 'linear-api-key' is harness_key",
            ),
            *[
                (
                    text + f'[task_labels]\n{task} = "{LABEL1}"\n',
                    f"task_labels.{task} is not a task of a role in linear-orchestrator/config.toml",
                )
                for task, text in (
                    ("echo", PIPELINE),
                    ("ghost", PIPELINE),
                    ("product-design", HEADER + role("researcher")),
                )
            ],
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails("linear-orchestrator/config.toml: " + message, text)

    def test_role_for(self):
        roles = self.runs()
        for email in ("researcher@agents.test", "RESEARCHER@Agents.Test"):
            self.assertEqual(config.role_for(roles, email), "researcher")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(config.role_for(roles, email))

    def test_clones_match_core(self):
        self.assertEqual(config.CLONES, ("src", "publish"))
        with open(delegate.CONFIG) as f:
            dirs = re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read())
        self.assertTrue(dirs)
        self.assertEqual(set(dirs), {config.CLONES[0]})
        with open(
            os.path.join(
                REPO,
                "delegate",
                "src",
                "agent_pm",
                "delegate",
                "team",
                "output",
                "destinations",
                "github.md",
            )
        ) as f:
            self.assertIn(f"`<Workdir>/{config.CLONES[1]}`", f.read())
        self.assertEqual(
            delegate.CONFIG,
            os.path.join(config.ROOT, "delegate", "config.toml"),
        )
