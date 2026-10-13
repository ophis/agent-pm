"""headless recovery integration test behavior."""

import dataclasses
import json
import os

from agent_pm.session import Driver, StartOptions
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    Exited,
    Reply,
    SessionBound,
    TurnEnd,
)

from session.tests.integration.adapters.codex import fake_codex
from session.tests.integration.adapters.codex.headless_fixtures import (
    CHANGED,
    NATIVE,
    HeadlessSessions,
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
