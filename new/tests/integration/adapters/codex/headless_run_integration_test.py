import dataclasses
import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from adapters.base import Access, Activity, ConfigError, Exited, LimitHit, Reply, SessionBound, Spec, Stderr, TurnEnd
from adapters.codex import CodexAdapter
from driver import api, records, registry
from new.tests import NEW
from new.tests.integration.adapters.codex import fake_codex

NATIVE = fake_codex.NATIVE
CHANGED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class HeadlessIntegrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", NEW))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work, self.bin = (self.root / name for name in ("home", "work", "bin"))
        self.cwd, self.extra = self.work / "project", self.root / "extra"
        self.tasks = self.root / "team" / "tasks"
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.cwd, self.extra, self.tasks, self.codex_home, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        executable = self.bin / "codex"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                              + shlex.quote(fake_codex.__file__) + ' "$@"\n')
        executable.chmod(0o755)
        self.log, self.scenario = self.root / "exec.jsonl", self.root / "scenario.json"
        self.scene()
        self.adapter = CodexAdapter({"tiers": {"1": "gpt-test"}, "efforts": {"low": "low"},
                                     "shell_network": False})
        self.common = self.adapter.common
        self.sid = "caller-id"
        self.spec = Spec(workdir=str(self.work), tier=1, effort="low",
                         access=Access(cwd=str(self.cwd), project=True, dirs=(str(self.extra), str(self.tasks)),
                                       commands=("git status",)))
        cfg = self.adapter.config | {"env": {"FAKE_LOG": str(self.log), "FAKE_SCENARIO": str(self.scenario)}}
        self.config = {"adapters": {"codex": {"implementation": "adapters.codex:CodexAdapter", **cfg}}}
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home),
                                                      "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}))
        registry.configure(self.config)
        self.addCleanup(registry.reset)

    def scene(self, steps=None, **fields):
        if steps is not None:
            fields["steps"] = steps
        self.scenario.write_text(json.dumps(fields))

    def drain(self, handle, on=lambda event: None):
        self.addCleanup(handle.stop)
        found, errors = [], []

        def collect():
            try:
                for event in handle.events():
                    found.append(event)
                    on(event)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        thread.join(10)
        if thread.is_alive():
            handle.stop()
            thread.join(2)
            self.fail("Driver did not reach Exited within 10s")
        if errors:
            raise errors[0]
        return found

    def logged(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def written(self):
        return records.read(records.path(str(self.work)))

    def record(self):
        return next((self.home / ".agent-pm" / "core-new-sessions" / "codex").glob("*.json"))

    def expected_args(self, prompt, resume=False):
        return ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write",
                "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"',
                "-c", "sandbox_workspace_write.network_access=false", "-c", "sandbox_workspace_write.writable_roots=[]",
                "-m", "gpt-test", "-c", "model_reasoning_effort=low", "--add-dir", str(self.extra),
                "--add-dir", str(self.tasks), *(["resume", NATIVE] if resume else []),
                prompt + "\n\n" + self.adapter.runtime_note]

    def test_fixture_cleanup_removes_owned_home_outside_writable_roots(self):
        self.assertNotEqual(os.path.commonpath((self.home, tempfile.gettempdir())), tempfile.gettempdir())
        self.assertNotEqual(os.path.commonpath((self.home, self.work)), str(self.work))
        root = self.root
        self.doCleanups()
        self.assertFalse(root.exists())

    def test_driver_replies_native_sid_and_snapshot(self):
        handle = api.start("codex", "headless", "hello", self.spec, sid=self.sid)

        def bound(event):
            if isinstance(event, SessionBound):
                self.assertEqual(self.common.load(str(self.work), self.sid).native_id, event.native_id)

        self.assertEqual(self.drain(handle, bound), [SessionBound(NATIVE), Reply("one\n\nnext line\n"), Reply("two"),
                                                    Reply("three"), TurnEnd("three"), Exited(0)])
        self.assertEqual(handle.sid, self.sid)
        self.assertEqual(handle.info().native_id, NATIVE)
        self.assertEqual(handle.reply, "three")
        entry, = self.logged()
        self.assertEqual((entry["snapshot"]["sid"], entry["snapshot"]["native_id"]), (self.sid, None))
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual([(entry["kind"], entry.get("client_id")) for entry in self.written()],
                         [("session", None), ("client_session", NATIVE)])

    def test_fresh_launch_has_exact_flags_prompt_cwd_and_stdin_eof(self):
        prompt = "\n# Input\n\nHello.\n"
        self.drain(api.start("codex", "headless", prompt, self.spec, sid=self.sid))
        entry, = self.logged()
        self.assertEqual(entry["argv"], self.expected_args(prompt))
        self.assertEqual(entry["prompt"], self.expected_args(prompt)[-1])
        self.assertTrue(entry["stdin_eof"])
        self.assertEqual((entry["cwd"], entry["env"]["PWD"], entry["env"]["CODEX_HOME"]),
                         (str(self.cwd), str(self.cwd), str(self.codex_home)))

    def test_resume_same_native_and_saved_settings(self):
        self.drain(api.start("codex", "headless", "one", self.spec, sid=self.sid))
        alternate = self.root / "changed"
        alternate.mkdir()
        cfg = self.config["adapters"]["codex"]
        cfg.update(tiers={"1": "changed"}, efforts={"low": "high"}, shell_network=True)
        cfg["env"]["CODEX_HOME"] = str(alternate)
        registry.configure(self.config)
        access = Access(cwd=str(alternate), dirs=(str(alternate),), commands=("changed command",), project=False)
        spec = dataclasses.replace(self.spec, tier=4, effort="max", access=access)
        handle = api.start("codex", "headless", "again", spec, resume=self.sid, sid=self.sid)
        self.assertEqual(self.drain(handle)[0], SessionBound(NATIVE))
        first, second = self.logged()
        self.assertEqual(second["snapshot"]["snapshot"], first["snapshot"]["snapshot"])
        self.assertEqual(second["snapshot"]["native_id"], NATIVE)
        self.assertEqual(second["argv"], self.expected_args("again", resume=True))
        self.assertEqual((second["cwd"], second["env"]["CODEX_HOME"], second["resume_id"], second["stdin_eof"]),
                         (str(self.cwd), str(self.codex_home), NATIVE, True))
        transcript = [json.loads(line) for line in fake_codex.session_path(NATIVE).read_text().splitlines()]
        self.assertEqual([entry["text"] for entry in transcript if entry["type"] == "user"],
                         [first["prompt"], second["prompt"]])

    def test_add_dir_order_and_tasks_prompt_survive_resume(self):
        prompt = f"Read `{self.tasks}`."
        for resume in (False, True):
            with self.subTest(resume=resume):
                spec = self.spec if not resume else dataclasses.replace(
                    self.spec, access=dataclasses.replace(self.spec.access, dirs=()))
                self.drain(api.start("codex", "headless", prompt, spec, resume=self.sid if resume else None, sid=self.sid))
                entry = self.logged()[-1]
                dirs = [entry["argv"][i + 1] for i, arg in enumerate(entry["argv"]) if arg == "--add-dir"]
                self.assertEqual(dirs, [str(self.extra), str(self.tasks)])
                self.assertIn(f"`{self.tasks}`", entry["prompt"])
                self.assertFalse((self.work / "tasks").exists())

    def test_raw_progress_outcome_and_json_reply_are_uninterpreted(self):
        texts = ["[agent-pm-progress:start] working\n\n", json.dumps({"kind": "outcome", "outcome": DONE}),
                 json.dumps({"status": "needs_input", "questions": ["Which greeting?"]}),
                 json.dumps({"kind": "outcome", "outcome": {"status": "maybe"}}), json.dumps(DONE)]
        self.scene([*(fake_codex.message(text) for text in texts), {"out": {"type": "turn.completed"}}])
        handle = api.start("codex", "headless", "hello", self.spec, sid=self.sid)
        self.assertEqual(self.drain(handle), [SessionBound(NATIVE), *(Reply(text) for text in texts),
                                             TurnEnd(texts[-1]), Exited(0)])
        self.assertEqual(handle.reply, texts[-1])
        self.assertEqual([entry["kind"] for entry in self.written()], ["session", "client_session"])
        self.assertFalse((self.work / "out.md").exists())

    def test_interrupted_first_turn_keeps_binding_and_resumes(self):
        before, after = "[agent-pm-progress:start] before interruption", "[agent-pm-progress:review] recovered"
        self.scene([fake_codex.message(before), {"sleep": 60}])
        handle = api.start("codex", "headless", "first", self.spec, sid=self.sid)
        found = self.drain(handle, lambda event: handle.stop() if isinstance(event, Reply) else None)
        self.assertEqual(found[:-1], [SessionBound(NATIVE), Reply(before)])
        self.assertIsInstance(found[-1], Exited)
        self.assertLess(found[-1].returncode, 0)
        self.assertEqual(handle.state(), "absent")
        handle.stop()
        self.assertEqual(list(handle.events()), [])
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.scene([fake_codex.message(after), {"out": {"type": "turn.completed"}}])
        resumed = api.start("codex", "headless", "recover", self.spec, resume=self.sid, sid=self.sid)
        self.assertEqual(self.drain(resumed), [SessionBound(NATIVE), Reply(after), TurnEnd(after), Exited(0)])
        self.assertEqual(self.logged()[-1]["resume_id"], NATIVE)
        self.assertEqual([entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"],
                         [NATIVE, NATIVE])

    def test_different_native_id_on_resume_is_not_published_or_saved(self):
        self.drain(api.start("codex", "headless", "first", self.spec, sid=self.sid))
        self.scene(thread_id=CHANGED)
        handle = api.start("codex", "headless", "again", self.spec, resume=self.sid, sid=self.sid)
        with self.assertRaisesRegex(ConfigError, "already bound to a different native ID"):
            self.drain(handle)
        self.assertEqual(handle.state(), "absent")
        self.assertEqual(handle.info().native_id, "")
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)
        self.assertEqual([entry["client_id"] for entry in self.written() if entry["kind"] == "client_session"], [NATIVE])

    def test_missing_mapping_does_not_spawn(self):
        with self.assertRaisesRegex(ConfigError, "missing Codex recovery state"):
            api.start("codex", "headless", "again", self.spec, resume=self.sid, sid=self.sid)
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_missing_binding_record_and_fifo_do_not_spawn(self):
        self.drain(api.start("codex", "headless", "one", self.spec, sid=self.sid))
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
                    api.start("codex", "headless", "again", self.spec, resume=self.sid, sid=self.sid)
                self.assertEqual(len(self.logged()), 1)

    def test_missing_session_does_not_spawn(self):
        self.drain(api.start("codex", "headless", "one", self.spec, sid=self.sid))
        fake_codex.session_path(NATIVE).unlink()
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            api.start("codex", "headless", "again", self.spec, resume=self.sid, sid=self.sid)
        self.assertEqual(len(self.logged()), 1)

    def test_fifo_and_malformed_native_files_do_not_spawn(self):
        self.drain(api.start("codex", "headless", "one", self.spec, sid=self.sid))
        path = fake_codex.session_path(NATIVE)
        for mode in ("fifo", "malformed", "mismatched"):
            with self.subTest(mode=mode):
                path.unlink()
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    metadata = {"type": "session_meta", "payload": {"id": CHANGED, "cwd": str(self.cwd)}}
                    path.write_text("bad json\n" if mode == "malformed" else json.dumps(metadata) + "\n")
                with self.assertRaises(ConfigError):
                    api.start("codex", "headless", "again", self.spec, resume=self.sid, sid=self.sid)
                self.assertEqual(len(self.logged()), 1)

    def test_untrusted_project_settings_are_rejected_before_spawn(self):
        (self.cwd / ".codex").mkdir()
        spec = dataclasses.replace(self.spec, access=dataclasses.replace(self.spec.access, project=False))
        with self.assertRaisesRegex(ConfigError, "project Codex settings"):
            api.start("codex", "headless", "hello", spec, sid=self.sid)
        self.assertEqual(self.logged(), [])

    def test_stderr_is_an_event_and_run_record(self):
        self.scene([{"err": "Codex diagnostic\nsecond line"}, fake_codex.message("done"),
                    {"out": {"type": "turn.completed"}}])
        found = self.drain(api.start("codex", "headless", "hello", self.spec, sid=self.sid))
        self.assertEqual([event for event in found if isinstance(event, Stderr)],
                         [Stderr("Codex diagnostic"), Stderr("second line")])
        self.assertEqual([entry["text"] for entry in self.written() if entry["kind"] == "stderr"],
                         ["Codex diagnostic", "second line"])
        self.assertEqual(found[-1], Exited(0))

    def test_tui_is_rejected_without_spawn(self):
        with self.assertRaisesRegex(ConfigError, "does not support tui"):
            api.start("codex", "tui", "hello", self.spec, sid=self.sid)
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_inherited_session_markers_are_removed_and_headless_defaults_retained(self):
        inherited = {key: "inherited" for key in (*fake_codex.MARKERS, *fake_codex.DEFAULTS)}
        with mock.patch.dict(os.environ, inherited):
            for resume in (False, True):
                with self.subTest(resume=resume):
                    self.drain(api.start("codex", "headless", "hello", self.spec,
                                         resume=self.sid if resume else None, sid=self.sid))
                    environment = self.logged()[-1]["env"]
                    self.assertEqual({key: environment[key] for key in fake_codex.MARKERS},
                                     dict.fromkeys(fake_codex.MARKERS))
                    self.assertEqual({key: environment[key] for key in fake_codex.DEFAULTS}, fake_codex.DEFAULTS)

    def test_nonzero_exit_preserves_raw_reply_and_final_turn(self):
        text = json.dumps(DONE)
        self.scene([fake_codex.message(text), {"out": {"type": "turn.completed"}}, {"exit": 7}])
        handle = api.start("codex", "headless", "hello", self.spec, sid=self.sid)
        self.assertEqual(self.drain(handle), [SessionBound(NATIVE), Reply(text), TurnEnd(text), Exited(7)])
        self.assertEqual(handle.reply, text)
        self.assertEqual(self.common.load(str(self.work), self.sid).native_id, NATIVE)

    def test_nonzero_exit_without_final_turn_is_not_inferred(self):
        self.scene([fake_codex.message("Thinking."), {"exit": 7}])
        self.assertEqual(self.drain(api.start("codex", "headless", "hello", self.spec, sid=self.sid)),
                         [SessionBound(NATIVE), Reply("Thinking."), Exited(7)])

    def test_activity_and_usage_limit_pass_through_driver(self):
        error = "You’ve hit your usage limit. Try again later."
        self.scene([{"out": {"type": "item.started", "item": {"type": "command_execution"}}},
                    {"out": {"type": "error", "message": error}}, {"out": {"type": "turn.completed"}}])
        self.assertEqual(self.drain(api.start("codex", "headless", "one", self.spec, sid=self.sid)),
                         [SessionBound(NATIVE), Activity("item.started"), Stderr(error), LimitHit(None), TurnEnd(""),
                          Exited(0)])

    def test_caller_sid_and_handle_info_inspect_native_binding(self):
        spec = dataclasses.replace(self.spec, sid=None)
        handle = api.start("codex", "headless", "hello", spec, sid="caller-owned")
        self.drain(handle)
        info = handle.info()
        self.assertEqual((handle.sid, info.sid, info.native_id, info.cwd, info.project, info.transcript, info.resume),
                         ("caller-owned", "caller-owned", NATIVE, str(self.cwd), True, "", ""))
        status = api.session_info("codex", handle.sid, str(self.cwd))
        self.assertEqual(status.resumable, "yes")
        self.assertIsNotNone(status.last_active)
        self.assertIn(NATIVE, status.resume_command)
        fake_codex.session_path(NATIVE).unlink()
        self.assertEqual(api.session_info("codex", handle.sid, str(self.cwd)).resumable, "no")
        self.record().write_text("bad json")
        self.assertEqual(api.session_info("codex", handle.sid, str(self.cwd)).resumable, "unknown")
