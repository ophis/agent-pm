"""Session behavior tests with caller-owned dependencies."""

import dataclasses
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.session import Driver, StartOptions
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Access,
    Activity,
    ConfigError,
    Exited,
    LimitHit,
    Reply,
    SessionBound,
    Stderr,
    TurnEnd,
)

from session.tests.integration.adapters.codex import fake_codex
from session.tests.integration.adapters.codex.headless_fixtures import (
    CHANGED,
    DONE,
    NATIVE,
    HeadlessSessions,
)


class HeadlessEventsIntegrationTest(HeadlessSessions):
    def test_driver_replies_native_sid_and_snapshot(self):
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
        )

        def bound(event):
            if isinstance(event, SessionBound):
                self.assertEqual(
                    self.common.load(str(self.work), self.sid).native_id, event.native_id
                )

        self.assertEqual(
            self.drain(handle, bound),
            [
                SessionBound(NATIVE),
                Reply("one\n\nnext line\n"),
                Reply("two"),
                Reply("three"),
                TurnEnd("three"),
                Exited(0),
            ],
        )
        self.assertEqual(handle.sid, self.sid)
        self.assertEqual(handle.info().native_id, NATIVE)
        self.assertEqual(handle.reply, "three")
        (entry,) = self.logged()
        self.assertEqual(
            (entry["snapshot"]["sid"], entry["snapshot"]["native_id"]), (self.sid, None)
        )
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual(
            [
                (entry["kind"], entry.get("client_id"))
                for entry in self.written()
                if entry["kind"] != "session_metadata"
            ],
            [("session", None), ("client_session", NATIVE)],
        )

    def test_raw_progress_outcome_and_json_reply_are_uninterpreted(self):
        texts = [
            "[agent-pm-progress:start] working\n\n",
            json.dumps({"kind": "outcome", "outcome": DONE}),
            json.dumps({"status": "needs_input", "questions": ["Which greeting?"]}),
            json.dumps({"kind": "outcome", "outcome": {"status": "maybe"}}),
            json.dumps(DONE),
        ]
        self.scene(
            [*(fake_codex.message(text) for text in texts), {"out": {"type": "turn.completed"}}]
        )
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
        )
        self.assertEqual(
            self.drain(handle),
            [SessionBound(NATIVE), *(Reply(text) for text in texts), TurnEnd(texts[-1]), Exited(0)],
        )
        self.assertEqual(handle.reply, texts[-1])
        self.assertEqual(
            [entry["kind"] for entry in self.written() if entry["kind"] != "session_metadata"],
            ["session", "client_session"],
        )
        self.assertFalse((self.work / "out.md").exists())

    def test_stderr_is_an_event_and_run_record(self):
        self.scene(
            [
                {"err": "Codex diagnostic\nsecond line"},
                fake_codex.message("done"),
                {"out": {"type": "turn.completed"}},
            ]
        )
        found = self.drain(
            self.driver.start(
                "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        self.assertEqual(
            [event for event in found if isinstance(event, Stderr)],
            [Stderr("Codex diagnostic"), Stderr("second line")],
        )
        self.assertEqual(
            [entry["text"] for entry in self.written() if entry["kind"] == "stderr"],
            ["Codex diagnostic", "second line"],
        )
        self.assertEqual(found[-1], Exited(0))

    def test_nonzero_exit_preserves_raw_reply_and_final_turn(self):
        text = json.dumps(DONE)
        self.scene([fake_codex.message(text), {"out": {"type": "turn.completed"}}, {"exit": 7}])
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
        )
        self.assertEqual(
            self.drain(handle), [SessionBound(NATIVE), Reply(text), TurnEnd(text), Exited(7)]
        )
        self.assertEqual(handle.reply, text)
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)

    def test_nonzero_exit_without_final_turn_is_not_inferred(self):
        self.scene([fake_codex.message("Thinking."), {"exit": 7}])
        self.assertEqual(
            self.drain(
                self.driver.start(
                    "codex", "headless", "hello", dataclasses.replace(self.spec, sid=self.sid)
                )
            ),
            [SessionBound(NATIVE), Reply("Thinking."), Exited(7)],
        )

    def test_activity_and_usage_limit_pass_through_driver(self):
        error = "You’ve hit your usage limit. Try again later."
        self.scene(
            [
                {"out": {"type": "item.started", "item": {"type": "command_execution"}}},
                {"out": {"type": "error", "message": error}},
                {"out": {"type": "turn.completed"}},
            ]
        )
        self.assertEqual(
            self.drain(
                self.driver.start(
                    "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
                )
            ),
            [
                SessionBound(NATIVE),
                Activity("item.started"),
                Stderr(error),
                LimitHit(None),
                TurnEnd(""),
                Exited(0),
            ],
        )

    def test_caller_sid_and_handle_info_inspect_native_binding(self):
        spec = dataclasses.replace(self.spec, sid=None)
        handle = self.driver.start(
            "codex", "headless", "hello", dataclasses.replace(spec, sid="caller-owned")
        )
        self.drain(handle)
        info = handle.info()
        self.assertEqual(
            (
                handle.sid,
                info.sid,
                info.native_id,
                info.cwd,
                info.project,
                info.transcript,
                info.resume,
            ),
            (
                "caller-owned",
                "caller-owned",
                NATIVE,
                str(self.cwd),
                True,
                str(fake_codex.session_path(NATIVE)),
                info.resume,
            ),
        )
        self.assertIn(NATIVE, info.resume)
        self.assertEqual(
            records.session_record(str(self.work), handle.sid)["transcript"], info.transcript
        )
        status = self.driver.session_status("codex", handle.sid, str(self.cwd))
        self.assertEqual(status.resumable, "yes")
        self.assertIsNotNone(status.last_active)
        self.assertIn(NATIVE, status.resume_command)
        fake_codex.session_path(NATIVE).unlink()
        self.assertEqual(
            self.driver.session_status("codex", handle.sid, str(self.cwd)).resumable, "no"
        )
        self.record().write_text("bad json")
        self.assertEqual(
            self.driver.session_status("codex", handle.sid, str(self.cwd)).resumable, "unknown"
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


class HeadlessRecoveryIntegrationTest(HeadlessSessions):
    def test_resume_ignores_changed_config_environment(self):
        self.drain(
            self.driver.start(
                "codex", "headless", "first", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        saved = self.common.load(str(self.work), self.sid).snapshot
        self.config["adapters"]["codex"]["env"]["TMPDIR"] = str(self.home)
        self.driver = Driver(**self.config)
        changed = dataclasses.replace(self.spec, env={"NEW_POLICY_KEY": "fresh"})
        self.drain(
            self.driver.start(
                "codex", "headless", "again", changed, options=StartOptions(resume=self.sid)
            )
        )
        self.assertEqual(self.common.load(str(self.work), self.sid).snapshot, saved)
        self.assertEqual(len(self.logged()), 2)
        self.assertIsNone(self.logged()[-1]["env"]["NEW_POLICY_KEY"])

    def test_resume_same_native_and_saved_settings(self):
        self.drain(
            self.driver.start(
                "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        alternate = self.root / "changed"
        alternate.mkdir()
        cfg = self.config["adapters"]["codex"]
        cfg.update(
            tiers={"1": "changed"},
            efforts={"low": "high"},
        )
        cfg["env"]["CODEX_HOME"] = str(alternate)
        self.driver = Driver(**self.config)
        access = Access(
            cwd=str(alternate), dirs=(str(alternate),), commands=("changed command",), project=False
        )
        spec = dataclasses.replace(self.spec, tier=4, effort="max", access=access)
        handle = self.driver.start(
            "codex",
            "headless",
            "again",
            dataclasses.replace(spec, sid=self.sid),
            options=StartOptions(resume=self.sid),
        )
        self.assertTrue(handle.info().project)
        self.assertEqual(handle.info().transcript, str(fake_codex.session_path(NATIVE)))
        self.assertIn(NATIVE, handle.info().resume)
        self.assertEqual(self.drain(handle)[0], SessionBound(NATIVE))
        first, second = self.logged()
        self.assertEqual(second["snapshot"]["snapshot"], first["snapshot"]["snapshot"])
        self.assertEqual(second["snapshot"]["native_id"], NATIVE)
        self.assertEqual(second["argv"], self.expected_args("again", resume=True))
        self.assertEqual(
            (second["cwd"], second["env"]["CODEX_HOME"], second["resume_id"], second["stdin_eof"]),
            (str(self.cwd), str(self.codex_home), NATIVE, True),
        )
        transcript = [
            json.loads(line) for line in fake_codex.session_path(NATIVE).read_text().splitlines()
        ]
        self.assertEqual(
            [entry["text"] for entry in transcript if entry["type"] == "user"],
            [first["prompt"], second["prompt"]],
        )

    def test_add_dir_order_survives_resume_and_the_tasks_dir_is_read_unadded(self):
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
        prompt = f"Read `{self.tasks}`."
        for resume in (False, True):
            with self.subTest(resume=resume):
                spec = (
                    self.spec
                    if not resume
                    else dataclasses.replace(
                        self.spec, access=dataclasses.replace(self.spec.access, dirs=())
                    )
                )
                self.drain(
                    self.driver.start(
                        "codex",
                        "headless",
                        prompt,
                        dataclasses.replace(spec, sid=self.sid),
                        options=StartOptions(resume=self.sid if resume else None),
                    )
                )
                entry = self.logged()[-1]
                dirs = [
                    entry["argv"][i + 1]
                    for i, arg in enumerate(entry["argv"])
                    if arg == "--add-dir"
                ]
                self.assertEqual(dirs, [str(self.extra)])
                self.assertNotIn(str(self.tasks), dirs)
                self.assertIn(f"`{self.tasks}`", entry["prompt"])
                self.assertFalse((self.work / "tasks").exists())

    def test_interrupted_first_turn_keeps_binding_and_resumes(self):
        before, after = (
            "[agent-pm-progress:start] before interruption",
            "[agent-pm-progress:review] recovered",
        )
        self.scene([fake_codex.message(before), {"sleep": 60}])
        handle = self.driver.start(
            "codex", "headless", "first", dataclasses.replace(self.spec, sid=self.sid)
        )
        found = self.drain(
            handle, lambda event: handle.stop() if isinstance(event, Reply) else None
        )
        self.assertEqual(found[:-1], [SessionBound(NATIVE), Reply(before)])
        self.assertIsInstance(found[-1], Exited)
        self.assertLess(found[-1].returncode, 0)
        self.assertEqual(handle.state(), "absent")
        handle.stop()
        self.assertEqual(list(handle.events()), [])
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.scene([fake_codex.message(after), {"out": {"type": "turn.completed"}}])
        resumed = self.driver.start(
            "codex",
            "headless",
            "recover",
            dataclasses.replace(self.spec, sid=self.sid),
            options=StartOptions(resume=self.sid),
        )
        self.assertEqual(
            self.drain(resumed), [SessionBound(NATIVE), Reply(after), TurnEnd(after), Exited(0)]
        )
        self.assertEqual(self.logged()[-1]["resume_id"], NATIVE)
        self.assertEqual(
            [entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"],
            [NATIVE, NATIVE],
        )

    def test_the_fakes_resume_steps_run_only_on_resume(self):
        self.scene(
            [fake_codex.message("first"), {"out": {"type": "turn.completed"}}],
            resume=[fake_codex.message("again"), {"out": {"type": "turn.completed"}}],
        )
        for resume, said in ((False, "first"), (True, "again")):
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
                self.assertEqual(found[1:], [Reply(said), TurnEnd(said), Exited(0)])

    def test_different_native_id_on_resume_is_not_published_or_saved(self):
        self.drain(
            self.driver.start(
                "codex", "headless", "first", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        self.scene(thread_id=CHANGED)
        handle = self.driver.start(
            "codex",
            "headless",
            "again",
            dataclasses.replace(self.spec, sid=self.sid),
            options=StartOptions(resume=self.sid),
        )
        with self.assertRaisesRegex(ConfigError, "already bound to a different native ID"):
            self.drain(handle)
        self.assertEqual(handle.state(), "absent")
        self.assertEqual(handle.info().native_id, "")
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual(
            [entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"],
            [NATIVE],
        )

    def test_missing_mapping_does_not_spawn(self):
        with self.assertRaisesRegex(ConfigError, "missing Codex recovery state"):
            self.driver.start(
                "codex",
                "headless",
                "again",
                dataclasses.replace(self.spec, sid=self.sid),
                options=StartOptions(resume=self.sid),
            )
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_missing_binding_record_and_fifo_do_not_spawn(self):
        self.drain(
            self.driver.start(
                "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        path = self.record()
        original = path.read_text()
        for mode in ("unbound", "missing", "fifo", "malformed"):
            with self.subTest(mode=mode):
                if path.exists():
                    path.unlink()
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                elif mode != "missing":
                    value = json.loads(original)
                    if mode == "unbound":
                        value["native_id"] = None
                    path.write_text("bad json" if mode == "malformed" else json.dumps(value))
                    path.chmod(0o600)
                with self.assertRaises(ConfigError):
                    self.driver.start(
                        "codex",
                        "headless",
                        "again",
                        dataclasses.replace(self.spec, sid=self.sid),
                        options=StartOptions(resume=self.sid),
                    )
                self.assertEqual(len(self.logged()), 1)

    def test_missing_session_does_not_spawn(self):
        self.drain(
            self.driver.start(
                "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        fake_codex.session_path(NATIVE).unlink()
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            self.driver.start(
                "codex",
                "headless",
                "again",
                dataclasses.replace(self.spec, sid=self.sid),
                options=StartOptions(resume=self.sid),
            )
        self.assertEqual(len(self.logged()), 1)

    def test_fifo_and_malformed_native_files_do_not_spawn(self):
        self.drain(
            self.driver.start(
                "codex", "headless", "one", dataclasses.replace(self.spec, sid=self.sid)
            )
        )
        path = fake_codex.session_path(NATIVE)
        for mode in ("fifo", "malformed", "mismatched"):
            with self.subTest(mode=mode):
                path.unlink()
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    metadata = {
                        "type": "session_meta",
                        "payload": {"id": CHANGED, "cwd": str(self.cwd)},
                    }
                    path.write_text(
                        "bad json\n" if mode == "malformed" else json.dumps(metadata) + "\n"
                    )
                with self.assertRaises(ConfigError):
                    self.driver.start(
                        "codex",
                        "headless",
                        "again",
                        dataclasses.replace(self.spec, sid=self.sid),
                        options=StartOptions(resume=self.sid),
                    )
                self.assertEqual(len(self.logged()), 1)

    def test_untrusted_project_settings_are_rejected_before_spawn(self):
        (self.cwd / ".codex").mkdir()
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, project=False)
        )
        with self.assertRaisesRegex(ConfigError, "project Codex settings"):
            self.driver.start("codex", "headless", "hello", dataclasses.replace(spec, sid=self.sid))
        self.assertEqual(self.logged(), [])
