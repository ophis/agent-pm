"""tui launch integration test behavior."""

import dataclasses
import os
import signal
import stat
import time
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.session import Driver
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    Exited,
    LimitHit,
    SessionBound,
    TurnEnd,
)

from session.tests import live_tmux
from session.tests.integration.adapters.codex import fake_codex
from session.tests.integration.adapters.codex.tui_fixtures import WAIT, Live, Stream


class TuiLaunchIntegrationTest(Live):
    def test_start_permission_reply_send_and_separate_process_attach(self):
        raw = "[agent-pm-progress:start] raw reply\n\nfinal"
        self.scene([[{"kind": "permission"}, raw], ["second"], ["third"]])
        handle = self.start(prompt="quoted ' prompt\nnext")
        stream = Stream(self, handle)
        found = stream.until(TurnEnd)
        native = self.bound().native_id
        self.assertEqual(
            found, [SessionBound(native), Activity(), Activity(), Blocked("Bash"), TurnEnd(raw)]
        )
        self.assertEqual(self.lines(2), ["w1 blocked", "w1 done"])
        self.assertEqual(handle.reply, raw)
        self.assertEqual(len(self.server.panes("=mgr:")), 1)
        self.assertEqual([r.name for r in self.driver.sessions("w1")], ["w1"])
        self.assertEqual(self.server.option("w1", "@sid"), self.sid)
        self.assertEqual(stat.S_IMODE(self.record().stat().st_mode), 0o600)
        hook_store = self.home / ".agent-pm" / "tui" / "w1" / "hooks.jsonl"
        self.assertEqual(stat.S_IMODE(hook_store.stat().st_mode), 0o600)
        handle.send("more")
        self.assertEqual(stream.until(TurnEnd), [Activity(), TurnEnd("second")])
        self.assertEqual(
            self.attach("last"), [[self.sid, "live", "second", native], ["third", 0], "third"]
        )
        self.assertEqual(stream.until(TurnEnd), [Activity(), TurnEnd("third")])
        self.assertEqual(self.lines(4), ["w1 blocked", "w1 done", "w1 done", "w1 done"])
        (call,) = self.calls()
        self.assertEqual(
            call["argv"][:5],
            ["--no-daemon", "-C", str(self.cwd), "-c", 'tui.notification_method="bel"'],
        )
        self.assertEqual(
            call["argv"][5:11],
            [
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ],
        )
        self.assertIn("--add-dir", call["argv"])
        self.assertFalse(any(word.startswith("sandbox_workspace_write.") for word in call["argv"]))
        self.assertEqual(
            call["argv"][-2:],
            [
                "--",
                "quoted ' prompt\nnext\n\n" + self.driver.registry.adapter("codex").runtime_note,
            ],
        )
        self.assertEqual(
            [h["hook"] for h in self.hooks()],
            [
                "SessionStart",
                "UserPromptSubmit",
                "PermissionRequest",
                "Stop",
                "UserPromptSubmit",
                "Stop",
                "UserPromptSubmit",
                "Stop",
            ],
        )
        self.assertTrue(
            all((h["exit"], h["stdout"], h["cwd"]) == (0, "", str(self.cwd)) for h in self.hooks())
        )
        self.assertEqual(
            [h["payload"]["prompt"] for h in self.hooks() if h["hook"] == "UserPromptSubmit"],
            [call["argv"][-1], "more", "last"],
        )
        kinds = [r["kind"] for r in records.read(records.path(str(self.work)))]
        self.assertEqual(
            [kind for kind in kinds if kind != "session_metadata"], ["session", "client_session"]
        )
        self.assertTrue(handle.info().transcript)
        self.assertIn(self.bound().native_id, handle.info().resume)
        handle.stop()
        self.assertEqual(stream.until(Exited), [Exited(-signal.SIGHUP)])
        self.assertEqual(self.driver.sessions("w1"), [])

    def test_workspace_write_flags_and_saved_policy_through_driver(self):
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
        handle = self.start()
        Stream(self, handle).until(TurnEnd)
        native = self.bound().native_id
        handle.stop()
        self.spec = dataclasses.replace(self.spec, flags=("-s", "danger-full-access"))
        self.driver = Driver(**self.config)
        resumed = self.start(resume=self.sid)
        Stream(self, resumed).until(TurnEnd)
        first, second = self.calls()
        expected = [
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
            "-m",
            "gpt-test",
            "-c",
            "model_reasoning_effort=low",
            "--add-dir",
            str(self.extra),
        ]
        self.assertEqual(first["argv"][5 : 5 + len(expected)], expected)
        self.assertEqual(second["argv"], ["resume", native, *first["argv"]])
        self.assertEqual(self.bound().snapshot["flags"][1], "workspace-write")

    def test_session_start_waits_for_first_submission_fresh_and_resumed(self):
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, commands=())
        )
        first = self.start(prompt="", spec=spec)
        live_tmux.wait(lambda: len(self.calls()) == 1, WAIT, what="interactive startup waits")
        time.sleep(0.3)
        self.assertEqual(self.hooks(), [])
        first.send("first")
        self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("first"))
        first.stop()
        hooks = len(self.hooks())
        spec = dataclasses.replace(
            self.spec, access=dataclasses.replace(self.spec.access, commands=())
        )
        resumed = self.start(prompt="", resume=self.sid, spec=spec)
        live_tmux.wait(lambda: len(self.calls()) == 2, WAIT, what="interactive resume starts")
        time.sleep(0.3)
        self.assertEqual(len(self.hooks()), hooks)
        resumed.send("again")
        self.assertEqual(Stream(self, resumed).until(TurnEnd)[-1], TurnEnd("first"))
        self.assertEqual(self.hooks()[hooks]["payload"]["source"], "resume")

    def test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested(self):
        inherited = {**dict.fromkeys(fake_codex.MARKERS, "inherited"), **fake_codex.DEFAULTS}
        self.assert_nested_environment(inherited, dict.fromkeys(inherited))

    def test_external_environment_is_inherited_fresh_resumed_and_nested(self):
        markers = dict.fromkeys(fake_codex.MARKERS, "inherited")
        external = {
            **dict.fromkeys(fake_codex.DEFAULTS, "less"),
            "NO_COLOR": "0",
            "CODEX_CI": "operator",
        }
        self.assert_nested_environment(
            {**markers, **external}, {**dict.fromkeys(markers), **external}
        )

    def test_explicit_operator_environment_is_forwarded_and_not_written_to_config(self):
        cfg = self.config["adapters"]["codex"]
        cfg["env"].update(
            NO_COLOR="0", CODEX_CI="operator", PAGER="less", FAKE_ADDITION="configured"
        )
        self.driver = Driver(**self.config)
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        env = self.calls()[0]["env"]
        self.assertEqual(
            {key: env[key] for key in ("NO_COLOR", "CODEX_CI", "PAGER", "FAKE_ADDITION")},
            {
                "NO_COLOR": "0",
                "CODEX_CI": "operator",
                "PAGER": "less",
                "FAKE_ADDITION": "configured",
            },
        )
        self.assertFalse((self.codex_home / "config.toml").exists())
        self.assertFalse((self.codex_home / "hooks.json").exists())

    def test_explicit_no_color_controls_interactive_rendering(self):
        for index, value in enumerate((None, "", "1", "0")):
            with self.subTest(value=value):
                env = self.config["adapters"]["codex"]["env"]
                env.pop("NO_COLOR", None)
                if value is not None:
                    env["NO_COLOR"] = value
                self.driver = Driver(**self.config)
                handle = self.start(f"color{index}", sid=f"color-{index}")
                self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
                screen = self.server.tmux(
                    "capture-pane", "-p", "-e", "-t", f"=color{index}:"
                ).stdout
                self.assertEqual("\x1b[36mfirst" in screen, value in (None, "0"))
                self.assertEqual(self.calls()[-1]["env"]["NO_COLOR"], value)
                handle.stop()

    def test_startup_dialog_screens_are_driver_blocked_events(self):
        for index, dialog in enumerate(("Trust this folder?", "Hooks need review")):
            with self.subTest(dialog=dialog):
                release = self.root / f"release-{index}"
                self.scene(
                    [[{"kind": "screen", "text": dialog}, {"kind": "pause", "file": str(release)}]]
                )
                handle = self.start(f"w{index}", sid=f"dialog-{index}")
                found = Stream(self, handle).until(Blocked)
                self.assertEqual(found[-1], Blocked(dialog))
                handle.stop()
        self.assertEqual(self.lines(2), ["w0 blocked", "w1 blocked"])

    def test_realistic_usage_limit_screen_emits_reset_and_deduplicates(self):
        release = self.root / "release"
        reset = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        screen = (
            "Codex > gpt-test\n■ You've hit your usage limit. Try again at Jan 2nd,"
            " 2027 3:04 AM.\n› "
        )
        self.scene([[{"kind": "screen", "text": screen}, {"kind": "pause", "file": str(release)}]])
        handle = self.start()
        stream = Stream(self, handle)
        self.assertEqual(stream.until(LimitHit)[-1], LimitHit(reset))
        self.assertIn("You've hit your usage limit", handle.screen())
        stream.quiet()
        handle.stop()

    def test_usage_limit_without_reset_is_still_an_event(self):
        release = self.root / "release"
        self.scene(
            [
                [
                    {
                        "kind": "screen",
                        "text": "■ Quota exceeded. Check your plan and billing details.",
                    },
                    {"kind": "pause", "file": str(release)},
                ]
            ]
        )
        self.assertEqual(Stream(self, self.start()).until(LimitHit)[-1], LimitHit())

    def test_quoted_assistant_limit_screen_does_not_emit_limit_hit(self):
        screen = (
            "Codex > gpt-test\n• You have asked me to quote this error.\n• You've "
            "hit your usage limit. is a quoted example\n› "
        )
        self.scene([[{"kind": "screen", "text": screen}, "quoted reply"]])
        stream = Stream(self, self.start())
        self.assertEqual(
            stream.until(TurnEnd),
            [SessionBound(self.bound().native_id), Activity(), Activity(), TurnEnd("quoted reply")],
        )
        stream.quiet()

    def test_configured_home_keeps_caller_store_for_stop_binding_and_resume(self):
        runtime_home = self.root / "runtime-home"
        runtime_home.mkdir()
        self.config["adapters"]["codex"]["env"]["HOME"] = str(runtime_home)
        self.driver = Driver(**self.config)
        self.scene([["configured home"]])
        first = self.start()
        self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("configured home"))
        native = self.bound().native_id
        first.stop()
        second = self.start(resume=self.sid)
        self.assertEqual(Stream(self, second).until(TurnEnd)[-1], TurnEnd("configured home"))
        self.assertEqual(self.bound().native_id, native)
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)
        self.assertFalse((runtime_home / ".agent-pm").exists())
        self.assertTrue(all(call["env"]["HOME"] == str(runtime_home) for call in self.calls()))

    def test_configured_home_in_writable_roots_keeps_caller_store_fresh_and_resumed(self):
        self.scene([["writable runtime home"]])
        for index, grant in enumerate((self.work, self.extra)):
            with self.subTest(grant=grant):
                runtime_home = grant / "runtime-home"
                runtime_home.mkdir()
                self.config["adapters"]["codex"]["env"]["HOME"] = str(runtime_home)
                self.driver = Driver(**self.config)
                name, sid = f"configured{index}", f"configured-{index}"
                before = len(self.calls())
                first = self.start(name, sid=sid)
                first_stream = Stream(self, first)
                found = [first_stream.next()]
                while not isinstance(found[-1], TurnEnd):
                    self.assertNotIsInstance(found[-1], Exited)
                    found.append(first_stream.next())
                self.assertEqual(found[-1], TurnEnd("writable runtime home"))
                native = self.bound(sid).native_id
                first.stop()
                second = self.start(name, sid=sid, resume=sid)
                self.assertEqual(
                    Stream(self, second).until(TurnEnd)[-1], TurnEnd("writable runtime home")
                )
                second.stop()
                self.assertEqual(self.bound(sid).native_id, native)
                self.assertFalse((runtime_home / ".agent-pm").exists())
                self.assertTrue(
                    all(call["env"]["HOME"] == str(runtime_home) for call in self.calls()[before:])
                )
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 2)

    def test_writable_dirs_api_queries_tui_saved_roots_and_environment(self):
        first_tmp, next_tmp, ignored_home = (
            self.root / n for n in ("first-tmp", "next-tmp", "ignored-home")
        )
        first_tmp.mkdir()
        next_tmp.mkdir()
        self.config["adapters"]["codex"]["env"]["TMPDIR"] = str(first_tmp)
        self.driver = Driver(**self.config)
        fresh = self.driver.writable_dirs("codex", self.spec)
        self.assertEqual(list(fresh[:3]), [str(p) for p in (self.cwd, self.codex_home, self.extra)])
        self.assertIn(str(first_tmp), fresh)
        self.assertFalse(Path(store_root()).parent.exists())
        self.assertEqual(self.logged(), [])
        first = self.start()
        self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("first"))
        saved = self.bound().writable_roots
        first.stop()
        self.config["adapters"]["codex"]["env"].update(
            {"TMPDIR": str(next_tmp), "CODEX_HOME": str(ignored_home)}
        )
        self.driver = Driver(**self.config)
        changed_spec = dataclasses.replace(
            self.spec, access=Access(cwd=str(self.extra), project=True)
        )
        before = self.record().read_bytes()
        resumed = self.driver.writable_dirs("codex", changed_spec, resume=self.sid)
        self.assertEqual(list(resumed[: len(saved)]), saved)
        self.assertNotIn(str(next_tmp), resumed)
        self.assertNotIn(str(ignored_home), resumed)
        self.assertEqual(self.record().read_bytes(), before)
        second = self.start(resume=self.sid)
        self.assertEqual(Stream(self, second).until(TurnEnd)[-1], TurnEnd("first"))
        self.assertEqual(self.calls()[-1]["env"]["TMPDIR"], str(first_tmp))
        self.assertEqual(self.calls()[-1]["env"]["CODEX_HOME"], str(self.codex_home))

    def assert_nested_environment(self, inherited, expected):
        with mock.patch.dict(os.environ, inherited):
            first = self.start()
            self.assertEqual(Stream(self, first).until(TurnEnd)[-1], TurnEnd("first"))
            first.stop()
            second = self.start(resume=self.sid)
            self.assertEqual(Stream(self, second).until(TurnEnd)[-1], TurnEnd("first"))
            with mock.patch.dict(os.environ, self.server.inside("w1")):
                nested = self.start("z1", sid="nested", events=str(self.root / "nested.events"))
                self.assertEqual(Stream(self, nested).until(TurnEnd)[-1], TurnEnd("first"))
        for call in self.calls():
            self.assertEqual({key: call["env"][key] for key in inherited}, expected)
            self.assertEqual(call["env"]["CODEX_HOME"], str(self.codex_home))
        self.assertNotEqual(self.bound().native_id, self.bound("nested").native_id)
        screen = self.server.tmux("capture-pane", "-p", "-e", "-t", "=z1:").stdout
        self.assertRegex(screen, r"\x1b\[[0-9;]*36mfirst")
