"""headless launch integration test behavior."""

import dataclasses
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.session import Driver, StartOptions
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine.types import (
    ConfigError,
    Exited,
)

from session.tests.integration.adapters.codex import fake_codex
from session.tests.integration.adapters.codex.headless_fixtures import (
    HeadlessSessions,
)


class HeadlessLaunchIntegrationTest(HeadlessSessions):
    def test_fixture_cleanup_removes_owned_home_outside_writable_roots(self):
        self.assertNotEqual(
            os.path.commonpath((self.home, tempfile.gettempdir())), tempfile.gettempdir()
        )
        self.assertNotEqual(os.path.commonpath((self.home, self.work)), str(self.work))
        root = self.root
        self.doCleanups()
        self.assertFalse(root.exists())

    def test_fresh_launch_has_exact_flags_prompt_cwd_and_stdin_eof(self):
        prompt = "\n# Input\n\nHello.\n"
        self.drain(
            self.driver.start(
                "codex", "headless", prompt, dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        (entry,) = self.logged()
        self.assertEqual(entry["argv"], self.expected_args(prompt))
        self.assertEqual(entry["prompt"], self.expected_args(prompt)[-1])
        self.assertTrue(entry["stdin_eof"])
        self.assertEqual(
            (entry["cwd"], entry["env"]["PWD"], entry["env"]["CODEX_HOME"]),
            (str(self.cwd), str(self.cwd), str(self.codex_home)),
        )

    def test_workspace_write_exact_flags_and_saved_policy_through_driver(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=(
                "-s",
                "workspace-write",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
            ),
        )
        self.driver = Driver(**self.config)
        self.drain(
            self.driver.start(
                "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        self.spec = dataclasses.replace(self.spec, flags=("-s", "danger-full-access"))
        self.driver = Driver(**self.config)
        self.drain(
            self.driver.start(
                "codex",
                "headless",
                "again",
                dataclasses.replace(self.spec, sid=self.sid),
                options=StartOptions(resume=self.sid),
            )
        )
        first, second = self.logged()
        self.assertEqual(first["argv"], self.expected_args("one", sandbox="workspace-write"))
        self.assertEqual(
            second["argv"], self.expected_args("again", resume=True, sandbox="workspace-write")
        )
        self.assertEqual(second["snapshot"]["snapshot"]["flags"][1], "workspace-write")

    def test_fresh_native_prompt_boundary_preserves_subcommands_and_options(self):
        for prompt in (
            "review",
            "resume",
            "fork",
            "--dangerously-bypass-approvals-and-sandbox",
            "--last",
            "--",
        ):
            with self.subTest(prompt=prompt):
                try:
                    self.direct_launch(prompt)
                finally:
                    self.record().unlink()

    def test_resumed_native_prompt_boundary_preserves_options(self):
        self.direct_launch("first")
        for prompt in ("--dangerously-bypass-approvals-and-sandbox", "--last", "review", "--"):
            with self.subTest(prompt=prompt):
                self.direct_launch(prompt, resume=True)

    def test_configured_tmpdir_override_is_forwarded_fresh_and_resume(self):
        native_tmp = self.root / "native-tmp"
        native_tmp.mkdir()
        self.config["adapters"]["codex"]["env"]["TMPDIR"] = str(native_tmp)
        self.driver = Driver(**self.config)
        for resume in (False, True):
            with self.subTest(resume=resume):
                found = self.drain(
                    self.driver.start(
                        "codex",
                        "headless",
                        "hello",
                        dataclasses.replace(self.spec, sid=self.sid),
                        options=StartOptions(resume=self.sid if resume else None),
                    )
                )
                self.assertEqual(found[-1], Exited(0))
                self.assertEqual(self.logged()[-1]["env"]["TMPDIR"], str(native_tmp))

    def test_configured_tmpdir_store_overlap_refused_before_fresh_spawn(self):
        alias = self.root / "home-alias"
        alias.symlink_to(self.home)
        store = Path(store_root())
        for index, root in enumerate((self.home, self.home / ".agent-pm", store, alias)):
            with self.subTest(root=root):
                self.config["adapters"]["codex"]["env"]["TMPDIR"] = str(root)
                self.driver = Driver(**self.config)
                with mock.patch(
                    "subprocess.Popen", side_effect=AssertionError("spawned before TMPDIR guard")
                ) as spawn:
                    with self.assertRaisesRegex(ConfigError, "writable"):
                        self.driver.start(
                            "codex",
                            "headless",
                            "hello",
                            dataclasses.replace(self.spec, sid=f"{self.sid}-{index}"),
                        )
                    spawn.assert_not_called()
                self.assertFalse((self.home / ".agent-pm").exists())
        self.assertEqual(self.logged(), [])

    def test_tui_factory_is_supported_without_starting_a_process(self):
        with mock.patch("subprocess.Popen") as spawn:
            self.assertIsNotNone(self.driver.registry.adapter("codex").tui())
        spawn.assert_not_called()
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_inherited_session_markers_are_removed_and_headless_defaults_retained(self):
        inherited = {key: "inherited" for key in (*fake_codex.MARKERS, *fake_codex.DEFAULTS)}
        with mock.patch.dict(os.environ, inherited):
            for resume in (False, True):
                with self.subTest(resume=resume):
                    self.drain(
                        self.driver.start(
                            "codex",
                            "headless",
                            "hello",
                            dataclasses.replace(self.spec, sid=self.sid),
                            options=StartOptions(resume=self.sid if resume else None),
                        )
                    )
                    environment = self.logged()[-1]["env"]
                    self.assertEqual(
                        {key: environment[key] for key in fake_codex.MARKERS},
                        dict.fromkeys(fake_codex.MARKERS),
                    )
                    self.assertEqual(
                        {key: environment[key] for key in fake_codex.DEFAULTS}, fake_codex.DEFAULTS
                    )
