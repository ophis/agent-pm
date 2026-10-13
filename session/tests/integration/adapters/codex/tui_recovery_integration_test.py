"""tui recovery integration test behavior."""

import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from agent_pm.session import Driver
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Access,
    Activity,
    ConfigError,
    SessionBound,
    TurnEnd,
)

from session.tests import live_tmux
from session.tests.case import ROOTS
from session.tests.integration.adapters.codex import fake_codex
from session.tests.integration.adapters.codex.tui_fixtures import CHANGED, WAIT, Live, Stream


class TuiRecoveryIntegrationTest(Live):
    def test_hooks_persist_binding_without_any_consumer(self):
        release = self.root / "release"
        self.scene([[{"kind": "pause", "file": str(release)}, "done"]])
        handle = self.start()
        live_tmux.wait(lambda: self.bound().native_id, WAIT, what="durable root binding")
        self.assertEqual(handle.info().native_id, "")
        self.assertEqual(
            [r["kind"] for r in records.read(records.path(str(self.work)))], ["session"]
        )
        native = self.bound().native_id
        handle.stop()
        self.scene([["recovered"]])
        resumed = self.start(resume=self.sid)
        stream = Stream(self, resumed)
        self.assertEqual(
            stream.until(TurnEnd),
            [SessionBound(native), Activity(), Activity(), TurnEnd("recovered")],
        )
        self.assertEqual(self.calls()[-1]["argv"][:2], ["resume", native])
        self.assertEqual(
            list(Path(store_root()).glob("*.json")),
            [self.record()],
        )

    def test_interrupt_completes_without_stop_and_replays_previous_reply_on_attach(self):
        self.scene(
            [["kept"], ["partial", {"kind": "interrupt"}], [{"kind": "interrupt"}], ["after"]]
        )
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("kept"))
        handle.send("interrupt")
        self.assertEqual(stream.until(TurnEnd), [Activity(), TurnEnd("kept")])
        self.assertEqual(self.lines(2), ["w1 done", "w1 done"])
        native = self.bound().native_id
        self.assertEqual(
            self.attach("interrupt again"),
            [[self.sid, "live", "kept", native], ["kept", 0], "kept"],
        )
        self.assertEqual(stream.until(TurnEnd), [Activity(), TurnEnd("kept")])
        self.assertEqual(self.attach(), [self.sid, "live", "kept", native])
        handle.send("recover")
        self.assertEqual(stream.until(TurnEnd), [Activity(), TurnEnd("after")])
        live_tmux.wait(lambda: len(self.hooks()) == 9, WAIT, what="all hook results logged")
        self.assertEqual([h["hook"] for h in self.hooks()].count("Stop"), 2)
        self.assertEqual([h["hook"] for h in self.hooks()].count("Interrupt"), 2)
        self.assertEqual(self.lines(4), ["w1 done"] * 4)

    def test_interrupt_before_stop_and_null_stop_have_empty_turn_replies(self):
        self.scene([[{"kind": "interrupt"}], []])
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd())
        self.assertEqual(self.attach(), [self.sid, "live", "", self.bound().native_id])
        handle.send("silent")
        self.assertEqual(stream.until(TurnEnd), [Activity(), TurnEnd()])
        live_tmux.wait(
            lambda: any(row["hook"] == "Stop" for row in self.hooks()),
            WAIT,
            what="null-reply Stop hook logged",
        )
        self.assertIsNone(self.hooks()[-1]["payload"]["last_assistant_message"])
        self.assertEqual(self.lines(2), ["w1 done"] * 2)

    def test_root_child_cwd_and_rebinding_filters_publish_only_valid_hooks(self):
        invalid = [
            {"agent_id": CHANGED, "last_assistant_message": "child"},
            {"cwd": str(self.extra), "last_assistant_message": "foreign cwd"},
            {"session_id": CHANGED, "last_assistant_message": "rebound"},
            {"session_id": "invalid", "last_assistant_message": "invalid"},
            {"hook_event_name": "Interrupt", "last_assistant_message": "wrong label"},
        ]
        self.scene(
            [
                [
                    *(dict(kind="hook", label="Stop", payload=value) for value in invalid),
                    {
                        "kind": "hook",
                        "label": "PermissionRequest",
                        "payload": {"agent_id": CHANGED},
                    },
                    {"kind": "tool", "tool_name": "Bash"},
                    "root",
                ]
            ]
        )
        handle = self.start()
        stream = Stream(self, handle)
        found = stream.until(TurnEnd)
        self.assertEqual(
            found,
            [
                SessionBound(self.bound().native_id),
                Activity(),
                Activity(),
                Activity("Bash"),
                TurnEnd("root"),
            ],
        )
        self.assertEqual(self.lines(1), ["w1 done"])
        self.assertEqual(self.attach()[2:], ["root", self.bound().native_id])
        stream.quiet()

    def test_resume_keeps_native_id_cwd_home_model_effort_dirs_network_and_commands(self):
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first"))
        native, saved = self.bound().native_id, self.bound().snapshot
        handle.stop()
        changed = self.root / "changed"
        changed.mkdir()
        cfg = self.config["adapters"]["codex"]
        cfg.update(
            tiers={"1": "changed"},
            efforts={"low": "high"},
        )
        cfg["env"]["CODEX_HOME"] = str(changed)
        self.driver = Driver(**self.config)
        spec = dataclasses.replace(
            self.spec,
            tier=4,
            effort="max",
            access=Access(cwd=str(changed), project=False, commands=("changed command",)),
        )
        self.scene([["resumed"]])
        resumed = self.start(resume=self.sid, spec=spec)
        self.assertTrue(resumed.info().project)
        self.assertTrue(resumed.info().transcript)
        self.assertIn(native, resumed.info().resume)
        found = Stream(self, resumed).until(TurnEnd)
        self.assertEqual(found, [SessionBound(native), Activity(), Activity(), TurnEnd("resumed")])
        first, second = self.calls()
        self.assertEqual(second["argv"], ["resume", native, *first["argv"]])
        self.assertEqual(
            (second["cwd"], second["env"]["CODEX_HOME"]), (str(self.cwd), str(self.codex_home))
        )
        self.assertEqual(self.bound().snapshot, saved)
        starts = [h for h in self.hooks() if h["hook"] == "SessionStart"]
        self.assertEqual([h["payload"]["source"] for h in starts], ["startup", "resume"])
        self.assertEqual(
            starts[0]["payload"]["transcript_path"], starts[1]["payload"]["transcript_path"]
        )
        self.assertEqual(
            self.driver.session_status("codex", self.sid, str(self.cwd)).resumable, "yes"
        )

    def test_headless_to_tui_to_headless_resume_uses_one_store(self):
        self.assertEqual(self.headless()[0], SessionBound(fake_codex.NATIVE))
        handle = self.start(resume=self.sid)
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        handle.stop()
        self.assertEqual(self.headless(resume=True)[0], SessionBound(fake_codex.NATIVE))
        self.assertEqual(self.bound().native_id, fake_codex.NATIVE)
        self.assertEqual(self.calls()[0]["argv"][:2], ["resume", fake_codex.NATIVE])
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)

    def test_tui_to_headless_resume_uses_the_hook_binding(self):
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        native = self.bound().native_id
        handle.stop()
        self.assertEqual(self.headless(resume=True)[0], SessionBound(native))
        self.assertEqual(self.bound().native_id, native)
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)

    def test_resume_missing_binding_native_fifo_and_malformed_record_fail_before_spawn(self):
        with self.assertRaisesRegex(ConfigError, "missing.*state"):
            self.start(resume=self.sid)
        self.assertEqual(self.logged(), [])
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        handle.stop()
        path, saved, before = self.record(), self.record().read_bytes(), len(self.logged())
        for mode in ("unbound", "missing", "fifo", "malformed"):
            with self.subTest(mode=mode):
                path.unlink(missing_ok=True)
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                elif mode != "missing":
                    data = json.loads(saved)
                    data["native_id"] = None
                    path.write_text("invalid" if mode == "malformed" else json.dumps(data))
                    path.chmod(0o600)
                started = time.monotonic()
                with self.assertRaises(ConfigError):
                    self.start(resume=self.sid)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(len(self.logged()), before)
        path.unlink()
        path.write_bytes(saved)
        path.chmod(0o600)
        native = fake_codex.session_path(self.bound().native_id)
        original = native.read_bytes()
        for mode in ("missing", "fifo", "malformed"):
            with self.subTest(native=mode):
                native.unlink(missing_ok=True)
                if mode == "fifo":
                    os.mkfifo(native, 0o600)
                elif mode == "malformed":
                    native.write_text("invalid")
                started = time.monotonic()
                with self.assertRaises(ConfigError):
                    self.start(resume=self.sid)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(len(self.logged()), before)
        native.unlink(missing_ok=True)
        native.write_bytes(original)

    def test_protected_paths_and_untrusted_settings_are_rejected_before_tmux_or_runtime(self):
        roots = (str(self.home), self.server.bin, *ROOTS, sys.prefix)
        for index, root in enumerate(roots):
            with self.subTest(root=root):
                spec = dataclasses.replace(
                    self.spec, access=dataclasses.replace(self.spec.access, dirs=(root,))
                )
                with (
                    mock.patch(
                        "subprocess.Popen", side_effect=AssertionError("spawned before guard")
                    ),
                    self.assertRaisesRegex(ConfigError, "writable|rewrite"),
                ):
                    self.start(sid=f"unsafe-{index}", spec=spec)
        (self.cwd / ".codex").mkdir()
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, project=False)
        )
        with self.assertRaisesRegex(ConfigError, "project Codex settings"):
            self.start(spec=spec)
        self.assertEqual(self.logged(), [])
        self.assertEqual([r.name for r in self.driver.sessions()], ["mgr"])
        self.assertFalse((self.work / "run.jsonl").exists())
