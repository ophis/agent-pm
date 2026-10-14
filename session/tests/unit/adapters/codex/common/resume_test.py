"""The resume script replays the stored Codex launch and fails closed without exec."""

import contextlib
import dataclasses
import io
import json
import sys
from pathlib import Path
from unittest import mock

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.codex.common import SessionStore, resume
from agent_pm.session.engine.types import Access, ConfigError

from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base

SECRETS = {"CFG_TOKEN": "cfg-secret-1", "ACCESS_TOKEN": "access-secret-2", "SPEC_TOKEN": "spec-3"}

FLAGS = ("-s", "danger-full-access", "-c", 'approval_policy="on-request"')


class ResumeScriptTest(Base):
    def setUp(self):
        super().setUp()
        self.adapter = CodexAdapter({**self.adapter.config, "env": {"CFG_TOKEN": "cfg-secret-1"}})
        self.common = self.adapter.common
        self.spec = dataclasses.replace(
            self.spec,
            flags=FLAGS,
            env={"SPEC_TOKEN": "spec-3", "GIT_DIR": "/spec/git"},
            access=dataclasses.replace(self.spec.access, env={"ACCESS_TOKEN": "access-secret-2"}),
        )
        self.store = SessionStore().root

    def bound(self, native=NATIVE):
        self.prepare()
        return self.common.bind(str(self.work), self.spec.sid, native)

    def run_main(self, native=NATIVE, *, store=None):
        stderr = io.StringIO()
        with (
            mock.patch("os.execvpe") as execvpe,
            mock.patch("os.chdir") as chdir,
            contextlib.redirect_stderr(stderr),
        ):
            code = resume.main([store or self.store, str(self.work), self.spec.sid, native])
        return code, execvpe, chdir, stderr.getvalue()

    def test_main_execs_the_stored_resume_with_the_saved_env(self):
        self.bound()
        with mock.patch.dict("os.environ", {"CALLER": "kept"}):
            code, execvpe, chdir, stderr = self.run_main()
        self.assertEqual((code, stderr), (0, ""))
        chdir.assert_called_once_with(str(self.cwd))
        name, argv, env = execvpe.call_args.args
        self.assertEqual(name, "codex")
        self.assertEqual((argv[:2], argv[2:6], argv[-1]), (("codex", "resume"), FLAGS, NATIVE))
        self.assertEqual({key: env[key] for key in SECRETS}, SECRETS)
        self.assertEqual((env["CODEX_HOME"], env["CALLER"]), (str(self.codex_home), "kept"))

    def test_saved_env_wins_and_local_git_names_and_defaults_are_not_added(self):
        self.bound()
        with mock.patch.dict("os.environ", {"SPEC_TOKEN": "caller", "NO_COLOR": "caller"}):
            _, execvpe, _, _ = self.run_main()
        env = execvpe.call_args.args[2]
        self.assertEqual((env["SPEC_TOKEN"], env["NO_COLOR"]), ("spec-3", "caller"))
        self.assertNotIn("GIT_DIR", env)
        self.assertNotIn("CODEX_CI", env)

    def test_main_fails_closed(self):
        def malformed():
            self.bound()
            self.record().write_text("not json")

        cases = {
            "missing state": (lambda: None, NATIVE, "missing Codex recovery state for run ID"),
            "no native id": (self.prepare, NATIVE, "is bound to native ID None, not"),
            "other native id": (self.bound, CHILD, f"is bound to native ID '{NATIVE}', not"),
            "malformed state": (malformed, NATIVE, "malformed recovery state"),
        }
        for label, (arrange, native, message) in cases.items():
            with self.subTest(label):
                for path in Path(self.store).glob("*.json"):
                    path.unlink()
                arrange()
                code, execvpe, _, stderr = self.run_main(native)
                self.assertEqual(code, 1)
                execvpe.assert_not_called()
                self.assert_one_line(stderr, message)

    def test_a_relative_store_and_a_failed_exec_fail_closed(self):
        self.bound()
        code, execvpe, _, stderr = self.run_main(store="relative/store")
        self.assertEqual(code, 1)
        execvpe.assert_not_called()
        self.assert_one_line(stderr, "invalid Codex recovery store")
        stderr = io.StringIO()
        with (
            mock.patch("os.execvpe", side_effect=FileNotFoundError(2, "No such file", "codex")),
            mock.patch("os.chdir"),
            contextlib.redirect_stderr(stderr),
        ):
            code = resume.main([self.store, str(self.work), self.spec.sid, NATIVE])
        self.assertEqual(code, 1)
        self.assert_one_line(stderr.getvalue(), "No such file")

    def assert_one_line(self, stderr, message):
        self.assertTrue(stderr.startswith("codex resume: "), stderr)
        self.assertEqual(stderr.count("\n"), 1, stderr)
        self.assertIn(message, stderr)
        for value in SECRETS.values():
            self.assertNotIn(value, stderr)


class ResumeBootstrapTest(Base):
    def setUp(self):
        super().setUp()
        path = list(sys.path)
        self.addCleanup(sys.path.__setitem__, slice(None), path)

    def test_bootstrap_puts_the_roots_first_and_returns_the_locators(self):
        rest = resume._bootstrap([json.dumps(list(PACKAGE_ROOTS)), "/s", "/w", "sid", NATIVE])
        self.assertEqual(rest, ["/s", "/w", "sid", NATIVE])
        self.assertEqual(sys.path[: len(PACKAGE_ROOTS)], list(PACKAGE_ROOTS))
        self.assertEqual([sys.path.count(root) for root in PACKAGE_ROOTS], [1, 1])

    def test_bootstrap_rejects_bad_arguments_and_roots(self):
        roots = json.dumps(list(PACKAGE_ROOTS))
        file = self.root / "file"
        file.write_text("fixture")
        invalid = {
            "null": None,
            "object": {},
            "empty list": [],
            "bare path": str(self.root),
            "numeric element": [1],
            "relative path": ["relative"],
            "nul character": ["/invalid\0root"],
            "newline character": [str(self.root) + "\nx"],
            "missing directory": [str(self.root / "missing")],
            "regular file": [str(file)],
            "duplicate directory": [str(self.root), str(self.root)],
        }
        cases = {
            "no arguments": [],
            "four arguments": [roots, "/s", "/w", "sid"],
            "six arguments": [roots, "/s", "/w", "sid", NATIVE, "extra"],
            "not json": ["[", "/s", "/w", "sid", NATIVE],
            **{
                label: [json.dumps(value), "/s", "/w", "sid", NATIVE]
                for label, value in invalid.items()
            },
        }
        before = list(sys.path)
        for label, argv in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                resume._bootstrap(argv)
            self.assertEqual(sys.path, before)


class ResumeAccessTest(Base):
    def test_resume_launch_needs_a_native_id(self):
        state = self.prepare()
        with self.assertRaisesRegex(ConfigError, "missing Codex native ID for run ID 'caller-id'"):
            self.common.resume_launch(state)

    def test_resume_launch_ignores_adapter_config(self):
        self.spec = dataclasses.replace(self.spec, access=Access(cwd=str(self.cwd)))
        self.prepare()
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertEqual(
            CodexAdapter({}).common.resume_launch(state), self.common.resume_launch(state)
        )
