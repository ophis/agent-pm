"""Run startup argument transport and identity validation."""

import contextlib
import io
from dataclasses import replace

from agent_pm.linear_orchestrator.engine.types import Spec
from orchestrator_tests.case import Case


class RunSpec(Case):
    def spec(self, **overrides):
        return replace(
            Spec(
                uuid="00000000-0000-4000-8000-000000000001",
                issue="TASK-26",
                project="",
                assignee="engineer@example.com",
                sid="00000000-0000-4000-8000-000000000002",
                mode="new",
                runtime="claude",
                input="/tmp/run input.txt",
            ),
            **overrides,
        )

    def test_required_values_and_defaults_roundtrip(self):
        spec = self.spec()
        self.assertEqual(Spec.from_argv(spec.to_argv()), spec)
        self.assertNotIn("--target", spec.to_argv())
        self.assertNotIn("--tui", spec.to_argv())
        self.assertIsNone(Spec.from_argv(spec.to_argv()).task)

    def test_all_fields_roundtrip_without_shell_interpretation(self):
        spec = self.spec(
            target="Ophis/agent-pm",
            project="project with spaces",
            task="light-build",
            mode="resume",
            runtime="codex",
            input="--input-like = $(literal)\n骨架",
            tui=True,
            split="--split-like",
            split_from="%3",
            opener="open -a 'iTerm'",
            events="/tmp/event file.jsonl",
        )
        self.assertEqual(Spec.from_argv(spec.to_argv()), spec)
        self.assertIn("--tui", spec.to_argv())
        self.assertIn("--split-from=%3", spec.to_argv())

    def test_empty_optional_values_are_preserved(self):
        spec = self.spec(task="", split="", split_from="", opener="", events="")
        self.assertEqual(Spec.from_argv(spec.to_argv()), spec)

    def test_parser_keeps_required_arguments_and_mode_validation(self):
        for argv in ([], self.spec(mode="invalid").to_argv()):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    Spec.from_argv(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_identity_and_role_validation(self):
        spec = self.spec()
        self.assertIsNone(spec.identity_error())
        self.assertIsNone(spec.role_error("engineer", ("build",)))
        self.assertEqual(
            self.spec(issue="bad", sid="bad").identity_error(),
            "bad issue or session id: bad bad",
        )
        self.assertEqual(spec.role_error(None, ()), "'engineer@example.com' is not a role account")
        self.assertEqual(
            self.spec(task="unknown").role_error("engineer", ("build",)),
            "task 'unknown' is not one of engineer's tasks (build)",
        )
