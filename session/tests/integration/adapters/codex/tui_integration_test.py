"""Codex interactive Driver sessions and hook trust with private tmux and fake runtimes."""

import concurrent.futures
import dataclasses
import json
import os
import queue
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.session import Driver
from agent_pm.session.adapters.codex import tui_start
from agent_pm.session.adapters.codex.sessions import store_root
from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Exited,
    LimitHit,
    SessionBound,
    Spec,
    TurnEnd,
)

from session.tests import live_tmux
from session.tests.case import PY, REPO, ROOTS, Case, child_env, settings
from session.tests.integration.adapters.codex import fake_codex, fake_tui

WAIT = 15
CHANGED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ATTACH = """import json, sys
from agent_pm.session.engine import api
from agent_pm.session import Driver, Session, TurnEnd
driver = Driver(**json.loads(sys.argv[1]))
h = driver.attach(sys.argv[2])
before = [h.sid, h.state(), h.reply, h.info().native_id]
if sys.argv[3]:
    h.send(sys.argv[3])
    turn = next(e for e in h.events() if isinstance(e, TurnEnd))
    print(json.dumps([before, [turn.reply, turn.pending], h.reply]))
else:
    print(json.dumps(before))
"""


class Stream:
    def __init__(self, case, handle):
        self.queue = queue.Queue()
        self.server = case.server
        self.thread = threading.Thread(target=self.collect, args=(handle,), daemon=True)
        self.thread.start()
        case.addCleanup(self.close, handle)

    def collect(self, handle):
        try:
            for event in handle.events():
                self.queue.put(event)
        except BaseException as exc:
            self.queue.put(exc)

    def close(self, handle):
        handle.stop()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("event consumer did not finish")

    def next(self):
        deadline = time.monotonic() + WAIT
        while True:
            try:
                event = self.queue.get(timeout=live_tmux.POLL)
                break
            except queue.Empty:
                self.server.tmux("run-shell", "-b", "true")
                if time.monotonic() > deadline:
                    raise AssertionError("no event before deadline") from None
        if isinstance(event, BaseException):
            raise event
        return event

    def until(self, kind):
        found = [self.next()]
        while not isinstance(found[-1], kind):
            found.append(self.next())
        return found

    def quiet(self):
        try:
            raise AssertionError(f"unexpected event: {self.queue.get(timeout=0.4)!r}")
        except queue.Empty:
            pass


class Live(Case):
    def setUp(self):
        parent = os.environ.get("AGENT_PM_TEST_TMP", REPO)
        tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work, self.extra = (self.root / n for n in ("home", "work", "extra"))
        self.cwd, self.codex_home = self.work / "project", self.home / ".codex"
        for path in (self.home, self.cwd, self.codex_home, self.extra):
            path.mkdir(parents=True, exist_ok=True)
        mkdtemp = tempfile.mkdtemp
        with mock.patch.object(
            live_tmux.tempfile, "mkdtemp", side_effect=lambda: mkdtemp(dir=parent)
        ):
            self.server = live_tmux.Server(self, str(self.home))
        self.server.start("mgr")
        fake_tui.install(self.server.bin)
        self.log, self.scenario, self.events = (
            self.root / n for n in ("runtime.jsonl", "scenario.json", "events")
        )
        self.scene()
        self.env = self.server.env(CODEX_HOME=str(self.codex_home), TZ="UTC")
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        self.config = settings(
            {
                "codex": {
                    "implementation": "adapters.codex:CodexAdapter",
                    "tiers": {"1": "gpt-test"},
                    "efforts": {"low": "low"},
                    "env": {
                        "FAKE_LOG": str(self.log),
                        "FAKE_SCENARIO": str(self.scenario),
                        "TZ": "UTC",
                    },
                }
            }
        )
        self.driver = Driver(**self.config)
        self.common = self.driver.registry.adapter("codex").common
        self.spec = Spec(
            flags=(
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            workdir=str(self.work),
            tier=1,
            effort="low",
            access=Access(
                cwd=str(self.cwd), project=True, dirs=(str(self.extra),), commands=("git status",)
            ),
        )
        self.sid = "caller-id"

    def scene(self, turns=None, **fields):
        self.scenario.write_text(
            json.dumps({"tui_turns": [["first"]] if turns is None else turns, **fields})
        )

    def start(self, name="w1", prompt="hello", **kwargs):
        handle = self.driver.start(
            "codex",
            "tui",
            prompt,
            dataclasses.replace(
                kwargs.pop("spec", self.spec), name=name, sid=kwargs.pop("sid", self.sid)
            ),
            **{"events": str(self.events), **kwargs},
        )
        self.addCleanup(handle.stop)
        return handle

    def logged(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def calls(self, mode="tui"):
        return [row for row in self.logged() if row.get("mode") == mode]

    def hooks(self):
        return [row for row in self.logged() if "hook" in row]

    def lines(self, count):
        def check():
            self.server.tmux("run-shell", "-b", "true")
            rows = live_tmux.events(str(self.events))
            return rows if len(rows) >= count else None

        return [
            line.split(" ", 1)[1]
            for line in live_tmux.wait(check, WAIT, what=f"{count} event lines")
        ]

    def bound(self, sid=None):
        return self.common.load(str(self.work), sid or self.sid)

    def record(self):
        return next(Path(store_root()).glob("*.json"))

    def attach(self, text="", name="w1"):
        result = subprocess.run(
            [*PY, "-c", ATTACH, json.dumps(self.config), name, text],
            env=child_env(),
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def headless(self, resume=False):
        handle = self.driver.start(
            "codex",
            "headless",
            "headless",
            dataclasses.replace(self.spec, sid=self.sid),
            resume=self.sid if resume else None,
        )
        self.addCleanup(handle.stop)
        stream = Stream(self, handle)
        return stream.until(Exited)


class TuiIntegrationTest(Live):
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

    def test_headless_to_tui_to_headless_resume_uses_one_store(self):
        self.assertEqual(self.headless()[0], SessionBound(fake_codex.NATIVE))
        handle = self.start(resume=self.sid)
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        handle.stop()
        self.assertEqual(self.headless(True)[0], SessionBound(fake_codex.NATIVE))
        self.assertEqual(self.bound().native_id, fake_codex.NATIVE)
        self.assertEqual(self.calls()[0]["argv"][:2], ["resume", fake_codex.NATIVE])
        self.assertEqual(len(list(self.record().parent.glob("*.json"))), 1)

    def test_tui_to_headless_resume_uses_the_hook_binding(self):
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        native = self.bound().native_id
        handle.stop()
        self.assertEqual(self.headless(True)[0], SessionBound(native))
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
                with mock.patch(
                    "subprocess.Popen", side_effect=AssertionError("spawned before guard")
                ):
                    with self.assertRaisesRegex(ConfigError, "writable|rewrite"):
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
        screen = "Codex > gpt-test\n■ You've hit your usage limit. Try again at Jan 2nd, 2027 3:04 AM.\n› "
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
        screen = "Codex > gpt-test\n• You have asked me to quote this error.\n• You've hit your usage limit. is a quoted example\n› "
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


class HookTrustIntegrationTest(Live):
    def test_interleaved_catalog_notifications_exact_hashes_and_cleanup(self):
        self.scene([["trusted"]])
        handle = self.start()
        self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("trusted"))
        (probe,) = self.calls("app-server")
        self.assertFalse(self.pid_exists(probe["pid"]))
        received = [r["received"] for r in self.logged() if "received" in r]
        self.assertEqual(
            [r["method"] for r in received], ["initialize", "initialized", "hooks/list"]
        )
        self.assertEqual(received[-1]["params"], {"cwds": [str(self.cwd)]})
        self.assertEqual(
            received[0],
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-pm", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        rows = next(r["catalog"] for r in self.logged() if "catalog" in r)
        argv = self.calls()[0]["argv"]
        config = fake_tui.configure([argv[i + 1] for i, word in enumerate(argv) if word == "-c"])
        self.assertEqual(
            config["hooks"]["state"],
            {row["key"]: {"trusted_hash": row["currentHash"]} for row in rows},
        )
        self.assertEqual(
            [row["eventName"] for row in rows],
            [
                "sessionStart",
                "userPromptSubmit",
                "postToolUse",
                "permissionRequest",
                "stop",
                "interrupt",
            ],
        )
        self.assertEqual([row["timeoutSec"] for row in rows], [10, 10, 10, 10, 10, 3])
        self.assertFalse((self.codex_home / "config.toml").exists())

    @staticmethod
    def pid_exists(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive(self):
        cases = (
            "off",
            "disabled",
            "foreign",
            "missing",
            "duplicate",
            "command",
            "hash",
            "malformed",
            "errors",
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                self.scene(catalog_case=case)
                handle = self.start(
                    f"bad{index}", sid=f"bad-{index}", events=str(self.root / f"events-{index}")
                )
                stream = Stream(self, handle)
                self.server.tmux("run-shell", "-b", "true")
                live_tmux.wait(
                    lambda: (
                        "codex tui:"
                        in self.server.tmux(
                            "capture-pane", "-p", "-J", "-S", "-1000", "-t", f"=bad{index}:"
                        ).stdout
                    ),
                    WAIT,
                    what="preflight diagnostic",
                )
                self.assertEqual(stream.until(Exited), [Exited(1)])
                self.assertFalse(any(row.get("mode") == "tui" for row in self.logged()))
                self.assertTrue(
                    all(not self.pid_exists(row["pid"]) for row in self.calls("app-server"))
                )
                self.assertIsNone(self.bound(f"bad-{index}").native_id)
                handle.stop()

    def test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed(self):
        for index, status in enumerate(("untrusted", "modified", "trusted", "disabled", "managed")):
            with self.subTest(status=status):
                self.scene(user_hook=status)
                before = len(self.calls())
                handle = self.start(f"u{index}", sid=f"user-{index}")
                stream = Stream(self, handle)
                if status in ("untrusted", "modified"):
                    live_tmux.wait(
                        lambda: (
                            "review it"
                            in self.server.tmux(
                                "capture-pane", "-p", "-J", "-S", "-1000", "-t", f"=u{index}:"
                            ).stdout
                        ),
                        WAIT,
                        what="user hook trust diagnostic",
                    )
                    self.assertEqual(stream.until(Exited), [Exited(1)])
                    self.assertEqual(len(self.calls()), before)
                else:
                    self.assertEqual(stream.until(TurnEnd)[-1], TurnEnd("first"))
                    self.assertEqual(len(self.calls()), before + 1)
                    if status != "disabled":
                        live_tmux.wait(
                            lambda: any(h.get("source") == "user" for h in self.hooks()),
                            WAIT,
                            what="trusted user hook result",
                        )
                        self.assertTrue(
                            all(h["exit"] == 0 for h in self.hooks() if h.get("source") == "user")
                        )
                handle.stop()
        self.assertFalse((self.codex_home / "hooks.json").exists())

    def test_appserver_exit_is_retried_once_then_cleaned_up(self):
        self.scene(catalog_case="exit")
        handle = self.start()
        live_tmux.wait(
            lambda: (
                "app-server exited"
                in self.server.tmux("capture-pane", "-p", "-J", "-S", "-1000", "-t", "=w1:").stdout
            ),
            WAIT,
            what="bounded retry diagnostic",
        )
        self.assertEqual(Stream(self, handle).until(Exited), [Exited(1)])
        probes = self.calls("app-server")
        self.assertEqual(len(probes), 2)
        self.assertTrue(all(not self.pid_exists(row["pid"]) for row in probes))
        self.assertEqual(self.calls(), [])

    def test_concurrent_independent_starts_serialize_the_same_home_probe(self):
        self.scene(catalog_delay=0.1)

        def start(index):
            return self.start(
                f"parallel{index}",
                sid=f"parallel-{index}",
                events=str(self.root / f"parallel-{index}.events"),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            handles = list(pool.map(start, (0, 1)))
        for handle in handles:
            self.assertEqual(Stream(self, handle).until(TurnEnd)[-1], TurnEnd("first"))
        logs = self.logged()
        positions = [i for i, row in enumerate(logs) if row.get("mode") == "app-server"]
        first, second = (logs[i]["pid"] for i in positions)
        closed = next(i for i, row in enumerate(logs) if row.get("probe_closed") == first)
        self.assertLess(positions[0], closed)
        self.assertLess(closed, positions[1])
        self.assertNotEqual(self.bound("parallel-0").native_id, self.bound("parallel-1").native_id)
        self.assertFalse(self.pid_exists(first))
        self.assertFalse(self.pid_exists(second))

    def test_silent_appserver_times_out_and_kills_group_with_a_fake_clock(self):
        self.scene(catalog_case="silent")
        run = self.driver.registry.adapter("codex").tui()
        launch = run.launch(
            "hello", dataclasses.replace(self.spec, sid=self.sid), False, (sys.executable, "-V")
        )
        data = json.loads(launch.argv[-1])
        env = {**dict(os.environ), **launch.env}
        clock, select, wait = (
            time.monotonic,
            tui_start.app_server.select.select,
            subprocess.Popen.wait,
        )
        started = clock()
        with (
            mock.patch.object(
                tui_start.time,
                "monotonic",
                side_effect=lambda: (
                    clock() * 100 if self.log.exists() and self.log.stat().st_size else clock()
                ),
            ),
            mock.patch.object(
                tui_start.app_server.select,
                "select",
                side_effect=lambda r, w, e, t: select(r, w, e, min(t, 0.01)),
            ),
            mock.patch.object(
                subprocess.Popen,
                "wait",
                lambda proc, timeout=None: wait(
                    proc, timeout=min(timeout, 0.01) if timeout is not None else None
                ),
            ),
        ):
            with self.assertRaisesRegex(tui_start.StartError, "no answer"):
                tui_start.hooks_list(data["argv"][0], data["hook_flags"], str(self.cwd), env)
        self.assertLess(clock() - started, 3)
        (probe,) = self.calls("app-server")
        self.assertFalse(self.pid_exists(probe["pid"]))
        self.assertEqual(self.calls(), [])

    def test_stop_and_sigterm_during_preflight_kill_probe_and_owned_descendants(self):
        def cleanup(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def running(pid):
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            )
            return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")

        for index, action in enumerate(("stop", "term")):
            with self.subTest(action=action):
                before = len(self.calls("app-server"))
                descendants_before = len([r for r in self.logged() if "descendant" in r])
                self.scene(catalog_case="silent_tree")
                handle = self.start(f"interrupted{index}", sid=f"interrupted-{index}")
                probe = live_tmux.wait(
                    lambda: self.calls("app-server")[before:] or None, WAIT, what="silent preflight"
                )[0]
                self.addCleanup(cleanup, probe["pid"])
                descendants = live_tmux.wait(
                    lambda: (
                        [r["descendant"] for r in self.logged() if "descendant" in r][
                            descendants_before:
                        ]
                        if len([r for r in self.logged() if "descendant" in r])
                        >= descendants_before + 2
                        else None
                    ),
                    WAIT,
                    what="owned descendants",
                )
                if action == "stop":
                    handle.stop()
                else:
                    os.kill(probe["parent_pid"], signal.SIGTERM)
                live_tmux.wait(
                    lambda: (
                        not self.pid_exists(probe["pid"])
                        and all(not running(pid) for pid in descendants)
                    ),
                    4,
                    what="interrupted preflight cleanup",
                )
                self.assertEqual(len(self.calls("app-server")), before + 1)
                self.assertEqual(self.calls(), [])
