"""The packaged default workflow: its Tasks, its lazy read and its checks."""

import tempfile
import tomllib
from importlib.resources import files
from pathlib import Path
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.flow import workflow
from agent_pm.linear_orchestrator.flow.types import Task
from orchestrator_tests.case import Case

HINT = "To change the target repo, edit the description's `Repo:` line."
GOOD = '[roles.pm]\nkind = "design"\nprefix = "PRD"\n'


class DefaultWorkflow(Case):
    def test_default_tasks(self):
        self.assertIs(config.TASKS, workflow.DEFAULT)
        self.assertEqual(
            dict(workflow.Workflow()),
            {
                "researcher": Task(
                    "research",
                    start="Research started:",
                    failed_new="todo",
                    hint=HINT,
                    next="pm",
                ),
                "pm": Task(
                    "design",
                    prefix="PRD",
                    start="PRD started:",
                    retitle=True,
                    approve=True,
                    next="engineer",
                    require_instructions=False,
                ),
                "engineer": Task(
                    "build",
                    prefix="ENG",
                    start="Build started:",
                    done="Build ready:",
                    question="Question:",
                    failed="Build failed:",
                    files=True,
                ),
            },
        )

    def test_packaged_config_has_no_flow_lines(self):
        with files("agent_pm.linear_orchestrator").joinpath("config.toml").open("rb") as f:
            roles = tomllib.load(f)["roles"]
        self.assertEqual(
            {r: sorted({"next", "require_instructions"} & set(p)) for r, p in roles.items()},
            dict.fromkeys(("researcher", "pm", "engineer"), []),
        )

    def test_reads_once_on_first_use(self):
        with mock.patch("tomllib.loads", wraps=tomllib.loads) as loads:
            flow = workflow.Workflow()
            self.assertEqual(loads.call_count, 0)
            self.assertEqual(flow["pm"].prefix, "PRD")
            self.assertEqual(sorted(flow), ["engineer", "pm", "researcher"])
            self.assertEqual(loads.call_count, 1)
            flow.load()
            self.assertEqual(loads.call_count, 2)


class BadWorkflow(Case):
    def read(self, text):
        """A fresh Workflow whose package directory holds workflow.toml = text."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if text is not None:
            Path(tmp.name, "workflow.toml").write_text(text, encoding="utf-8")
        with mock.patch("importlib.resources.files", lambda _: Path(tmp.name)):
            return dict(workflow.Workflow())

    def test_a_good_file(self):
        self.assertEqual(self.read(GOOD), {"pm": Task("design", prefix="PRD")})

    def test_bad_file(self):
        at = f"{workflow.REL}: "
        for text, message in (
            (None, "No such file or directory"),
            ("[roles.pm\n", "Expected ']'"),
            ("", "want only a [roles] table of [roles.<role>] tables"),
            ("extra = 1\n" + GOOD, "want only a [roles] table of [roles.<role>] tables"),
            ("roles = 1\n", "want only a [roles] table of [roles.<role>] tables"),
            ("[roles]\npm = 1\n", "roles.pm: want a table"),
            (GOOD + "bogus = 1\nzap = 2\n", "roles.pm: unknown keys: bogus, zap"),
            (
                '[roles.pm]\nprefix = "PRD"\n',
                "roles.pm.kind: want one of research, design, build: None",
            ),
            (
                GOOD + 'failed_new = "tdo"\n',
                "roles.pm.failed_new: want one of todo, in_review: 'tdo'",
            ),
            ('[roles.pm]\nkind = "x"\n', "roles.pm.kind: want one of research, design, build: 'x'"),
            (GOOD + 'retitle = "yes"\n', "roles.pm.retitle: want a bool: 'yes'"),
            (GOOD + "hint = true\n", "roles.pm.hint: want a str: True"),
            (GOOD + "next = 1\n", "roles.pm.next: want a str: 1"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as cm:
                    self.read(text)
                self.assertTrue(str(cm.exception).startswith(at), cm.exception)
                self.assertIn(message, str(cm.exception))
