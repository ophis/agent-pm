"""Tui mode behavior through nonspawning Host and Terminal boundaries."""

import errno
import fcntl
import inspect
import itertools
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
from unittest import mock

from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    DriverError,
    HookCallback,
    HookKind,
    Key,
    Launch,
    LimitHit,
    Liveness,
    Mode,
    Paste,
    Prepared,
    SessionInfo,
    Spec,
    TurnEnd,
    Word,
)
from agent_pm.session.hosts.base import HostProcess
from agent_pm.session.hosts.tmux.types import TuiError
from agent_pm.session.modes import tui, tui_hook

from session.tests import fake_adapter
from session.tests.case import SRC, Case
from session.tests.fakes.terminal import MemoryHost, ScreenHost
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.tmux_host_support import Base as TmuxBase

NAME = "w1"

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

TS = "2026-10-11T12:00:00.000+00:00"

WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}

BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n")


def record(label: str, **payload) -> dict:
    return {"ts": TS, "label": label, "payload": payload}


class Clock:
    """time.monotonic and time.sleep's stand-in: sleep moves `now`, then runs each action due by
    then; every sleep is logged in `log`.
    """

    def __init__(self, log: list):
        self.now, self.log, self.actions = 0.0, log, []

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))
        self.now += seconds
        while self.actions and self.actions[0][0] <= self.now:
            self.actions.pop(0)[1]()


class ScanTui(fake_adapter.FakeTui):
    """The fake Tui launching /bin/run with its hook (other Launch fields: `launched`), with two
    dialogs and a usage-limit message `LIMIT <epoch>`; activity is the payload's tool, the blocked
    reason its why; each screen limit() sees is kept.
    """

    dialogs = ("Trust  this\nfolder?", "Sign in")

    def __init__(self, adapter):
        super().__init__(adapter)
        self.screens, self.launched, self.roots = [], {}, None

    def launch(self, spec, hook, *, source_roots):
        self.roots = source_roots
        return Launch(("/bin/run", *hook), **self.launched)

    def limit(self, screen):
        self.screens.append(screen)
        found = re.search(r"LIMIT ([0-9]+)", screen)
        return LimitHit(float(found[1])) if found else None

    def activity(self, payload):
        return payload.get("tool", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class TuiAdapter(fake_adapter.FakeAdapter):
    """The fake Adapter whose tui() is always its one `run`, a ScanTui unless replaced."""

    def __init__(self, config):
        super().__init__(config)
        self.run = ScanTui(self)

    def tui(self):
        return self.run


class IdentityAdapter(fake_adapter.FakeAdapter):
    identity_env = ("INNER_ID",)


class Restored(ScanTui):
    """A ScanTui logging its restore and hook_events calls in its adapter's `calls`."""

    def restore(self, spec, state):
        self.adapter.calls.append(("restore", spec, dict(state)))

    def hook_events(self, label, payload):
        self.adapter.calls.append(("hook_events", label))
        return super().hook_events(label, payload)


class FreshAdapter(fake_adapter.FakeAdapter):
    """The fake Adapter whose tui() is a new Restored each call, kept in `made`."""

    def __init__(self, config):
        super().__init__(config)
        self.calls, self.made = [], []

    def tui(self):
        self.made.append(Restored(self))
        return self.made[-1]


class Base(Case):
    def setUp(self):
        self.root = os.path.join(self.agent_pm, "tui")
        self.dir = os.path.join(self.root, NAME)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.events = os.path.join(self.home, "events")
        self.host = ScreenHost(
            protected=("/host/tool",), hook=HookCallback("fake_host_hook", {"selected": True})
        )
        self.log = []
        self.clock = Clock(self.log)
        for patch in (
            mock.patch("time.monotonic", lambda: self.clock.now),
            mock.patch("time.sleep", self.clock.sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.adapter = TuiAdapter({"tui": True})
        self.run, self.recorded = self.adapter.run, []

    def start(self, run=None, record=None, **fields) -> tui.TuiRuntime:
        """A TuiRuntime started with self.adapter (its tui `run` if given) on Spec(workdir self.tmp,
        name NAME, client fake, sid SID, events self.events, **fields), its Prepared in
        self.prepared; its session line given to record, else kept in self.recorded.
        """
        if run is not None:
            self.adapter.run = run
        spec = Spec(
            **{
                "workdir": self.tmp,
                "name": NAME,
                "client": "fake",
                "sid": SID,
                "events": self.events,
                **fields,
            }
        )
        mode = tui.TuiRuntime.start(
            spec,
            self.adapter,
            registry=self.driver.registry,
            record=record or self.recorded.append,
            host=self.host,
        )
        self.prepared = mode.prepared
        return mode

    def started(self):
        self.host.processes.clear()
        mode = self.start()
        self.host.calls.reset_mock()
        mode._terminal.calls.reset_mock()
        return mode

    def append(self, *lines) -> None:
        """Each line to hooks.jsonl: bytes as given, else a JSON line."""
        with open(self.hooks, "ab") as f:
            for line in lines:
                f.write(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())

    def state(self, **changes) -> dict:
        process = self.host.processes.get(NAME)
        generation = (
            None if process is None else process.session_identity()[1]
        ) or "fixture-generation"
        return {
            "client": "fake",
            "sid": SID,
            "workdir": self.tmp,
            "cwd": self.tmp,
            "project": False,
            "transcript": "",
            "resume": "",
            "events": self.events,
            "words": WORDS,
            "generation": generation,
            "host": self.host.name,
            "host_hook": {"module": "fake_host_hook", "state": {"selected": True}, "roots": []},
            **changes,
        }

    def write_state(self, value) -> None:
        os.makedirs(self.dir, 0o700, exist_ok=True)
        with open(os.path.join(self.dir, "session.json"), "wb") as f:
            f.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def assertNoSessionDir(self):
        self.assertFalse(os.path.lexists(self.dir))

    def locked(self, name: str = NAME) -> bool:
        try:
            fd = os.open(os.path.join(self.root, f"{name}.lock"), os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False


class Keys(ScanTui):
    def submit(self, text):
        return Key("Escape"), Paste(text), Key("Enter"), Key("Enter"), Paste("b"), Key("Tab")


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.process = self.host.create(NAME, SID)

    def test_a_nonterminal_host_process_cannot_be_attached_as_tui(self):
        state = self.state()
        process = mock.create_autospec(HostProcess, instance=True)
        process.record.return_value = self.process.record()
        self.host.processes[NAME] = process
        with self.assertRaisesRegex(DriverError, "session w1 has no terminal"):
            tui.TuiRuntime.attach(NAME, state, self.adapter, host=self.host)
        process.close.assert_not_called()

    def attach(self, *lines, adapter=None) -> tui.TuiRuntime:
        """TuiRuntime.attach of self.adapter (else `adapter`) on a session dir with session.json and
        hooks.jsonl's lines, the host's calls cleared first.
        """
        self.write_state(self.state())
        self.append(*lines)
        self.host.calls.reset_mock()
        return tui.TuiRuntime.attach(
            NAME,
            tui.TuiRuntime.snapshot(NAME),
            adapter or self.adapter,
            host=self.host,
        )

    def test_its_replay_bound_and_the_next_record_from_the_files_end(self):
        partial = (json.dumps(record("tool", tool="next")) + "\n").encode()
        mode = self.attach(
            record("tool", session_id="n1"),
            record("stop", session_id="n1", last_message="r1"),
            record("stop", session_id="n2", last_message="r2"),
            record("ask"),
            b"bad\n",
            partial[:7],
        )
        self.assertEqual(
            (mode.name, mode.prepared.info.sid, mode.prepared.spec.events),
            (NAME, SID, self.events),
        )
        self.assertEqual(
            mode.replayed,
            (
                ClientBound("n1"),
                Activity(""),
                TurnEnd("r1", 0),
                ClientBound("n2"),
                TurnEnd("r2", 0),
                Blocked(""),
            ),
        )
        self.assertEqual(self.host.calls.mock_calls, [mock.call.list(None), mock.call.attach(NAME)])
        self.append(partial[7:], record("stop", session_id="n2", last_message="r3"))
        self.assertEqual(mode.poll(0), ([Activity("next"), TurnEnd("r3", 0)], False))

    def test_its_prepared_is_session_jsons_spec_and_identity(self):
        mode = self.attach()
        self.assertEqual(
            mode.prepared,
            Prepared(
                Spec(
                    workdir=self.tmp,
                    client="fake",
                    mode=Mode.TUI,
                    sid=SID,
                    name=NAME,
                    events=self.events,
                ),
                SessionInfo(sid=SID, cwd=self.tmp, project=False, transcript="", resume=""),
            ),
        )

    def test_a_session_json_without_a_valid_identity_is_no_tui_session_before_host_access(self):
        with self.assertRaises(DriverError) as caught:
            tui.TuiRuntime.attach(NAME, self.state(cwd=""), self.adapter, host=self.host)
        path = os.path.join(self.dir, "session.json")
        self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad cwd")
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_an_identity_is_refused_alike_in_session_json_and_run_jsonl(self):
        """SessionInfo.from_fields decodes both; the hook reads none of these keys."""
        valid = SessionInfo(
            sid=SID, cwd=self.tmp, project=False, transcript="", resume=""
        ).persisted()
        for field, value in (
            ("cwd", ""),
            ("cwd", "rel"),
            ("transcript", "rel"),
            ("project", "no"),
            ("resume", None),
        ):
            with self.subTest(field=field, value=value):
                RunLog(self.tmp).note("session", **{**valid, field: value})
                self.assertIsNone(RunLog(self.tmp).session(SID))
                self.write_state(self.state(**{field: value}))
                path = os.path.join(self.dir, "session.json")
                for call in (
                    lambda: tui.TuiRuntime.snapshot(NAME),
                    lambda *, field=field, value=value: tui.TuiRuntime.attach(
                        NAME, self.state(**{field: value}), self.adapter, host=self.host
                    ),
                ):
                    with self.assertRaises(DriverError) as caught:
                        call()
                    self.assertEqual(
                        str(caught.exception), f"no tui session w1: {path}: bad {field}"
                    )
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_no_records_yet(self):
        mode = self.attach()
        self.assertEqual(mode.replayed, ())
        self.append(record("stop", session_id="n1"))
        self.assertEqual(mode.poll(0), ([ClientBound("n1"), TurnEnd("", 0)], False))

    def test_its_records_replay_as_the_events_they_give_live(self):
        lines = (
            record("tool", session_id="n1", tool="t"),
            record("ask", why="perm"),
            record("other", session_id="n2"),
            record("stop", session_id="n2", last_message="r", tasks=[1]),
            b"bad\n",
            record("stop", last_message=""),
        )
        live = self.started()
        self.append(*lines)
        polled, _ = live.poll(0)
        fresh = FreshAdapter({"tui": True})
        attached = tui.TuiRuntime.attach(NAME, tui.TuiRuntime.snapshot(NAME), fresh, host=self.host)
        self.assertEqual(attached.replayed, tuple(polled))
        self.assertEqual(len(polled), 6)

    def test_a_fresh_tui_is_restored_once_with_its_adapter_state_before_its_records(self):
        restored = {"module": "fake_adapter_hook", "state": {"store": "/s"}, "roots": []}
        for extra, kept in (({"adapter_hook": restored}, {"store": "/s"}), ({}, {})):
            with self.subTest(extra=extra):
                adapter = FreshAdapter({"tui": True})
                self.write_state(self.state(**extra))
                self.append(record("tool"), record("stop"))
                mode = tui.TuiRuntime.attach(
                    NAME, tui.TuiRuntime.snapshot(NAME), adapter, host=self.host
                )
                self.assertEqual(adapter.made, [mode.run])
                self.assertEqual(
                    adapter.calls,
                    [
                        ("restore", mode.prepared.spec, kept),
                        ("hook_events", "tool"),
                        ("hook_events", "stop"),
                    ],
                )
                os.unlink(self.hooks)

    def test_a_tui_it_cannot_restore_or_no_tui_mode_is_no_tui_session(self):
        class Unrestorable(ScanTui):
            def restore(self, spec, state):
                raise ConfigError("no recovery state")

        self.adapter.run = Unrestorable(self.adapter)
        with self.assertRaises(DriverError) as caught:
            self.attach(record("tool"))
        self.assertEqual(str(caught.exception), "no tui session w1: no recovery state")
        self.assertEqual(self.host.calls.mock_calls, [mock.call.list(None), mock.call.attach(NAME)])
        self.adapter.run = None
        with self.assertRaises(DriverError) as caught:
            self.attach()
        self.assertEqual(str(caught.exception), "no tui session w1: client 'fake' has no tui mode")
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_marked_blocked_is_its_terminals_activity(self):
        mode = self.attach()
        for state, blocked in ((Word.BLOCKED, True), (Word.WORKING, False), (None, False)):
            with self.subTest(state=state):
                self.process.calls.activity.return_value = state
                self.assertIs(mode.marked_blocked(), blocked)
        self.assertEqual(self.process.calls.activity.call_count, 3)

    def test_another_sid_absent_or_unknown_is_no_tui_session(self):
        for sid in ("other", None):
            with self.subTest(sid=sid):
                self.host.create(NAME, sid)
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
                    self.attach()
        self.host.processes.clear()
        with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
            self.attach()
        self.host.calls.attach.assert_not_called()
        self.host.create(NAME, SID).calls.session_identity.return_value = (None, None)
        with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
            self.attach()

    def test_attach_checks_the_private_identity_of_its_captured_terminal(self):
        old = self.process
        old.calls.session_identity.side_effect = lambda: self.host.create(
            NAME, "other"
        ).session_identity()
        with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
            self.attach()
        self.host.calls.attach.assert_called_once_with(NAME)

    def test_an_attached_session_is_live_and_marks_dialogs_on_its_events_file(self):
        mode = self.attach()
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([Blocked("Sign in")], False))
        self.process.calls.mark.assert_called_once_with(Word.BLOCKED, self.events)
        mode.stop()
        self.process.calls.close.assert_called_once_with()

    def test_an_attached_session_ends_once_another_terminal_holds_its_name(self):
        mode = self.attach()
        self.host.create(NAME, SID)
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))


class StartTest(Base):
    def test_failed_adapter_commit_removes_its_tui_state_without_launching(self):
        class Conflict(ScanTui):
            def commit(self):
                raise ConfigError("commit conflict")

        with self.assertRaisesRegex(ConfigError, "commit conflict"):
            self.start(run=Conflict(self.adapter))
        self.assertNoSessionDir()
        self.assertEqual(self.recorded, [])
        self.host.calls.terminal.assert_not_called()

    def nothing_hosted(self):
        self.host.calls.terminal.assert_not_called()
        self.host.calls.attach.assert_not_called()
        self.assertFalse(os.path.lexists(self.root))
        self.assertEqual(self.recorded, [])

    def test_display_and_private_identity_are_separate_from_the_host_receipt(self):
        mode = tui.TuiRuntime.start(
            Spec(workdir=self.tmp, name=NAME, client="fake", sid=SID),
            self.adapter,
            registry=self.driver.registry,
            record=self.recorded.append,
            host=self.host,
            display="opaque-location",
        )
        call = self.host.calls.terminal.call_args
        self.assertEqual(call.kwargs["display"], "opaque-location")
        self.assertIsNone(call.kwargs["record"])
        self.assertEqual(call.kwargs["metadata"]["sid"], SID)
        self.assertEqual(call.kwargs["metadata"]["client"], "fake")
        self.assertEqual(mode._terminal.display(), "opaque-location")

    def test_the_selected_host_receives_the_launch_and_identity_receipt(self):
        mode = self.start()
        call = self.host.calls.terminal.call_args
        self.assertEqual(
            call.args,
            (
                NAME,
                Launch(
                    ("/bin/run", *tui._Store(NAME, host=self.host).hook_argv(PACKAGE_ROOTS)),
                    cwd=self.tmp,
                    env=self.prepared.env,
                ),
            ),
        )
        self.assertEqual(call.kwargs["events"], self.events)
        self.assertEqual(call.kwargs["metadata"]["sid"], SID)
        self.assertEqual(call.kwargs["metadata"]["client"], "fake")
        self.assertEqual(self.run.roots, PACKAGE_ROOTS)
        self.assertEqual(
            (mode.name, mode.prepared.info.sid, mode._store.dir), (NAME, SID, self.dir)
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        mode.stop()
        self.start(events=None)
        self.assertIsNone(self.host.calls.terminal.call_args.kwargs["events"])

    def test_one_session_line_between_session_json_and_terminal_launch_under_the_names_lock(self):
        lines = []

        def record(info):
            lines.append(
                (
                    info,
                    tui.TuiRuntime.snapshot(NAME)["sid"],
                    self.host.calls.terminal.called,
                    self.locked(),
                )
            )

        self.start(record=record)
        self.assertEqual(lines, [(self.prepared.info, SID, False, True)])
        self.host.calls.terminal.assert_called_once()
        self.assertEqual(
            self.prepared.info,
            SessionInfo(sid=SID, cwd=self.tmp, project=False, transcript="", resume=""),
        )

    def test_the_names_lock_is_held_from_session_dir_to_terminal_launch(self):
        held = []
        self.host.calls.list.side_effect = lambda _display: held.append(self.locked()) or []
        self.host.calls.terminal.side_effect = lambda *_args, **_kwargs: held.append(self.locked())
        self.start()
        self.host.calls.terminal.side_effect = DriverError("host launch failed")
        with self.assertRaisesRegex(DriverError, "host launch failed"):
            self.start()
        self.assertEqual((held, self.locked()), ([True, True, True], False))

    def test_the_spec_as_started_names_the_session_and_protects_the_hook_paths(self):
        self.start(protect=("/c",))
        self.assertEqual(
            (self.prepared.spec.name, self.prepared.spec.protect),
            (NAME, ("/c", "/host/tool", self.events, self.root)),
        )
        mode = self.start(name=None, events=None)
        self.assertEqual(
            (mode.name, self.prepared.spec.name, self.prepared.spec.protect),
            ("fake-0f8fad5b", "fake-0f8fad5b", ("/host/tool", self.root)),
        )
        self.assertEqual(self.host.calls.terminal.call_args.args[0], "fake-0f8fad5b")

    def test_added_hook_paths_reject_overlapping_cwd_and_grants_before_launch(self):
        for protected, field in itertools.product(
            ("/host/tool", self.events, self.root), ("cwd", "dirs")
        ):
            for path in (protected, os.path.dirname(protected), os.path.join(protected, "child")):
                access = Access(cwd=path) if field == "cwd" else Access(dirs=(path,))
                with (
                    self.subTest(path=path, field=field),
                    self.assertRaisesRegex(ConfigError, "overlaps protected path"),
                ):
                    self.start(access=access)
                self.assertEqual(self.recorded, [])
                self.assertNoSessionDir()
        self.host.calls.terminal.assert_not_called()

    def test_a_declared_hook_callback_root_rejects_an_overlapping_cwd_before_launch(self):
        self.host.hook = HookCallback("fake_host_hook", {}, roots=("/declared/root",))
        with self.assertRaisesRegex(
            ConfigError, "writable path /declared/root/child overlaps protected path /declared/root"
        ):
            self.start(access=Access(cwd="/declared/root/child"))
        self.assertEqual(self.recorded, [])
        self.assertNoSessionDir()
        self.host.calls.terminal.assert_not_called()
        self.host.hook = HookCallback("fake_host_hook", {}, roots=("/elsewhere",))
        self.start(access=Access(cwd="/declared/root/child"))
        self.host.calls.terminal.assert_called_once()

    def test_its_child_env_drops_adapter_identities_then_takes_the_launch_env(self):
        self.driver_dependencies = {"class_loader": lambda _path, _base: IdentityAdapter}
        self.configure_session(
            {"inner": {"implementation": "agent_pm.session.adapters.inner:InnerAdapter"}}
        )
        self.run.launched = {"env": {"LAUNCH_SET": "1"}, "unset": ("LAUNCH_DROP",)}
        with mock.patch.dict(
            os.environ,
            {"HOME": os.environ["HOME"], "INNER_ID": "i", "LAUNCH_DROP": "x", "KEEP": "k"},
            clear=True,
        ):
            self.start()
        self.assertEqual(
            self.host.calls.terminal.call_args.args[1].env,
            {"HOME": os.environ["HOME"], "KEEP": "k", "LAUNCH_SET": "1", "PWD": self.tmp},
        )

    def test_a_bad_name_given_or_made_from_the_sid_is_refused_before_host_launch_or_a_path(self):
        for name in BAD_NAMES:
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                self.start(name=name)
            self.assertEqual(
                str(caught.exception), f"name must match [A-Za-z0-9_-]+, got {name!r:.80}"
            )
        for fields, message in (
            ({"name": None, "sid": "a b"}, "name must match [A-Za-z0-9_-]+, got 'fake-a b'"),
            ({"name": 5}, "name must be None or a string"),
        ):
            with self.subTest(fields=fields), self.assertRaises(ConfigError) as caught:
                self.start(**fields)
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.host.calls.mock_calls, [])
        self.nothing_hosted()

    def test_a_bad_events_file_is_refused(self):
        self.host.calls.validate.side_effect = ConfigError("events destination refused")
        with self.assertRaisesRegex(ConfigError, "events destination refused"):
            self.start()
        self.host.calls.validate.assert_called_once()
        self.nothing_hosted()

    def test_an_unavailable_host_is_a_driver_error_with_nothing_started(self):
        failure = DriverError("host unavailable")
        self.host.calls.validate.side_effect = failure
        with self.assertRaisesRegex(
            DriverError, rf"\Acannot start tui session {NAME}: host unavailable\Z"
        ) as caught:
            self.start(events=None)
        self.assertIs(caught.exception.__cause__, failure)
        self.host.calls.validate.assert_called_once()
        self.nothing_hosted()

    def test_an_unsupported_tui_mode_is_refused_before_anything(self):
        self.adapter.run = None
        with self.assertRaisesRegex(ConfigError, "client 'fake' does not support tui mode"):
            self.start()
        self.assertEqual(self.host.calls.mock_calls, [])
        self.nothing_hosted()

    def test_session_selection_flags_are_refused_before_launch(self):
        self.adapter.forbidden_selection = ("--resume",)
        with self.assertRaises(ConfigError) as caught:
            self.start(flags=("--resume", SID))
        self.assertEqual(str(caught.exception), "--resume: picks the session; use sid= or resume=")
        self.nothing_hosted()

    def test_a_failed_host_start_is_a_driver_error_after_the_session_line_removing_only_its_dir(
        self,
    ):
        os.makedirs(os.path.join(self.root, "w2"))
        self.host.calls.terminal.side_effect = DriverError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.assertEqual(len(self.recorded), 1)
        self.assertNoSessionDir()
        self.assertEqual(sorted(os.listdir(self.root)), ["w1.lock", "w2"])
        self.host.calls.attach.assert_not_called()

    def test_a_failed_start_keeps_a_dir_it_did_not_make(self):
        def swap(_info):
            os.rename(self.dir, os.path.join(self.tmp, "ours"))  # kept: no inode reuse
            os.mkdir(self.dir)
            open(os.path.join(self.dir, "theirs"), "w").close()

        self.host.calls.terminal.side_effect = DriverError("tmux: boom")
        with self.assertRaises(DriverError):
            self.start(record=swap)
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_failed_record_or_a_start_failing_without_a_driver_error_leaves_its_dir(self):
        def fail(_info):
            raise OSError("disk full")

        for record, failure in ((fail, None), (None, ValueError("boom"))):
            with self.subTest(failure=failure):
                self.host.calls.reset_mock()
                self.host.calls.terminal.side_effect = failure
                with self.assertRaises(OSError if failure is None else ValueError):
                    self.start(record=record)
                self.assertEqual(os.listdir(self.dir), ["session.json"])
                self.assertEqual(
                    len(self.host.calls.terminal.mock_calls), 0 if failure is None else 1
                )
                self.assertFalse(self.locked())

    def test_a_terminal_acquisition_failure_leaves_no_session(self):
        self.host.calls.terminal.side_effect = DriverError("terminal could not be acquired")
        with self.assertRaisesRegex(
            DriverError, r"\Acannot start tui session w1: terminal could not be acquired\Z"
        ):
            self.start()
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.host.processes, {})
        self.assertNoSessionDir()

    def test_terminal_acquisition_failure_does_not_wait_for_its_held_lock(self):
        caught = []
        self.host.calls.terminal.side_effect = DriverError("terminal could not be acquired")

        def start():
            try:
                self.start()
            except DriverError as error:
                caught.append(str(error))

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(caught, ["cannot start tui session w1: terminal could not be acquired"])
        self.assertEqual(self.host.processes, {})
        self.assertNoSessionDir()

    def test_start_returns_its_session_live_at_once_with_its_prepared(self):
        mode = self.start(events=None)
        self.assertIsInstance(mode, tui.TuiRuntime)
        self.assertEqual((mode.prepared.info, mode.prepared.spec.name), (self.recorded[0], NAME))
        self.assertEqual((mode.state(), mode.poll(0)), (Liveness.LIVE, ([], False)))
        process = mode._terminal
        mode.stop()
        process.calls.close.assert_called_once_with()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))


class SnapshotTest(Base):
    def test_a_valid_state(self):
        self.start()
        self.assertEqual(tui.TuiRuntime.snapshot(NAME), self.state())
        self.write_state(
            self.state(events=None, transcript="/t", project=True, resume="r", words={}, later=1)
        )
        self.assertEqual(tui.TuiRuntime.snapshot(NAME)["later"], 1)

    def test_a_client_is_lowercase_word_characters_led_by_a_letter(self):
        for client, ok in (
            ("a", True),
            ("a_1", True),
            ("z9_", True),
            ("", False),
            ("A", False),
            ("1a", False),
            ("_a", False),
            ("a-b", False),
            ("a\n", False),
            (1, False),
        ):
            with self.subTest(client=client):
                self.write_state(self.state(client=client))
                if ok:
                    self.assertEqual(tui.TuiRuntime.snapshot(NAME)["client"], client)
                else:
                    with self.assertRaises(DriverError) as caught:
                        tui.TuiRuntime.snapshot(NAME)
                    path = os.path.join(self.dir, "session.json")
                    self.assertEqual(
                        str(caught.exception), f"no tui session w1: {path}: bad client"
                    )

    def test_missing_or_invalid_is_a_driver_error(self):
        with self.assertRaisesRegex(DriverError, r"\Ano tui session w1: "):
            tui.TuiRuntime.snapshot(NAME)
        bad = {
            "words": {"stop": "dead"},
            "events": "rel",
            "sid": "a/b",
            "client": "Fake",
            "workdir": "rel",
            "generation": "",
        }
        for field, value in bad.items():
            for state in (
                self.state(**{field: value}),
                {k: v for k, v in self.state().items() if k != field},
            ):
                with self.subTest(field=field, missing=field not in state):
                    self.write_state(state)
                    with self.assertRaisesRegex(
                        DriverError, rf"\Ano tui session w1: .*bad {field}"
                    ):
                        tui.TuiRuntime.snapshot(NAME)
        path = os.path.join(self.dir, "session.json")
        for state, field in (
            (self.state(cwd="rel"), "cwd"),
            (self.state(transcript="rel"), "transcript"),
            (self.state(project="no"), "project"),
            (self.state(resume=None), "resume"),
            ({k: v for k, v in self.state().items() if k not in ("cwd", "project")}, "cwd"),
        ):
            with self.subTest(state=state):
                self.write_state(state)
                with self.assertRaises(DriverError) as caught:
                    tui.TuiRuntime.snapshot(NAME)
                self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad {field}")
        for data in (b"", b"[]", b"not json"):
            with self.subTest(data):
                self.write_state(data)
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1: "):
                    tui.TuiRuntime.snapshot(NAME)

    def test_a_session_json_that_is_no_regular_file_is_refused_without_blocking(self):
        path = os.path.join(self.dir, "session.json")
        os.makedirs(self.dir)
        real = os.path.join(self.tmp, "real.json")
        with open(real, "w") as f:
            json.dump(self.state(), f)
        for make in (
            lambda: os.symlink(real, path),
            lambda: os.mkfifo(path),
            lambda: os.mkdir(path),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                out = {}
                thread = threading.Thread(
                    target=lambda *, out=out: out.setdefault("error", self.refused()), daemon=True
                )
                thread.start()
                thread.join(10)
                self.assertFalse(thread.is_alive(), "blocked")
                self.assertTrue(out["error"])
            (os.rmdir if stat.S_ISDIR(os.lstat(path).st_mode) else os.unlink)(path)

    def refused(self) -> bool:
        try:
            tui.TuiRuntime.snapshot(NAME)
        except DriverError:
            return True
        return False


class BoundTest(Base):
    """A mode keeps its Terminal generation when the Host reuses a name."""

    def test_another_terminal_under_the_name_ends_it_at_once_unscanned_unmarked_and_unread(self):
        for code in (None, 3):
            with self.subTest(code=code):
                mode = self.started()
                process = mode._terminal
                process.calls.returncode.return_value = code
                self.host.create(NAME, SID)
                self.host.calls.screen.return_value = "Sign in LIMIT 9"
                self.assertEqual(mode.poll(0), ([], True))
                self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
                process.calls.mark.assert_not_called()
                self.host.calls.screen.assert_not_called()
                self.append(record("tool"))
                self.assertEqual(mode.poll(0), ([], True))

    def test_records_under_another_terminal_are_never_read(self):
        mode = self.started()
        self.host.create(NAME, SID)
        self.append(record("stop", last_message="theirs"))
        self.assertEqual(mode.poll(5), ([], True))
        mode = self.started()

        def theirs():
            self.host.create(NAME, SID)
            self.append(record("stop", last_message="theirs"))

        self.clock.actions.append((self.clock.now + 0.25, theirs))
        self.assertEqual((mode.poll(5), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_ends_it_at_once_its_code_the_one_seen_else_sighup(self):
        mode = self.started()
        mode.stop()
        os.mkdir(self.dir)
        self.append(record("tool"))
        self.host.calls.reset_mock()
        self.assertEqual((mode.poll(5), mode.returncode(5)), (([], True), -signal.SIGHUP))
        self.assertEqual((self.host.calls.mock_calls, self.log), ([], []))
        mode = self.started()
        mode._terminal.calls.returncode.return_value = 3
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.assertEqual(mode.returncode(0), 3)

    def test_stop_closes_only_the_generation_still_holding_its_name(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                mode = self.started()
                original = mode._terminal
                if replacement:
                    current = self.host.create(NAME, SID)
                mode.stop()
                self.assertEqual(original.calls.close.call_count, int(not replacement))
                self.assertEqual(os.path.isdir(self.dir), replacement)
                self.assertEqual(mode.poll(0), ([], True))
                if replacement:
                    self.assertIs(self.host.processes[NAME], current)
                    current.calls.close.assert_not_called()

    def test_stop_with_the_name_absent_never_closes_a_stale_terminal(self):
        mode = self.started()
        self.host.processes.clear()
        mode.stop()
        self.assertEqual(self.host.calls.mock_calls, [mock.call.list(None)])
        mode._terminal.calls.close.assert_not_called()
        self.assertNoSessionDir()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_checks_the_holder_and_closes_under_the_names_lock(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                mode = self.started()
                if replacement:
                    self.host.create(NAME, SID)
                held = []
                self.host.calls.list.side_effect = lambda _display, *, held=held: (
                    held.append(self.locked()) or [NAME]
                )
                mode._terminal.calls.close.side_effect = lambda *, held=held: held.append(
                    self.locked()
                )
                mode.stop()
                self.assertEqual(
                    (held, self.locked()), ([True] * (1 + 2 * int(not replacement)), False)
                )
                self.host.calls.list.side_effect = None

    def test_another_terminal_under_the_name_is_absent_to_state_send_and_screen(self):
        mode = self.started()
        self.host.create(NAME, SID)
        self.assertEqual(mode.state(), Liveness.ABSENT)
        for call in (lambda: mode.send("hi"), mode.screen):
            with self.assertRaisesRegex(DriverError, r"\Atui session w1 is not running\Z"):
                call()
        mode._terminal.calls.send.assert_not_called()
        self.host.calls.screen.assert_not_called()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_an_absent_terminal_is_not_scanned_or_closed(self):
        mode = self.started()
        self.host.processes.clear()
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.host.calls.screen.assert_not_called()
        mode._terminal.calls.close.assert_not_called()
        self.assertNoSessionDir()

    def test_an_unknown_terminal_is_not_scanned_or_ended(self):
        mode = self.started()
        mode._terminal.calls.state.return_value = Liveness.UNKNOWN
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            (self.run.screens, mode.returncode(0), mode.state()), ([], None, Liveness.UNKNOWN)
        )


class SwapTest(Base):
    """Operations on a captured Terminal never reach its replacement."""

    def test_send_never_types_into_the_replacement_terminal(self):
        mode = self.started()
        original = mode._terminal

        def replace():
            self.host.create(NAME, SID)
            return True

        original.calls.alive.side_effect = replace
        with self.assertRaisesRegex(DriverError, "terminal w1 is not running"):
            mode.send("hi")
        self.host.processes[NAME].calls.send.assert_not_called()
        original.calls.send.assert_not_called()

    def test_screen_never_reads_the_replacement_terminal(self):
        mode = self.started()

        def replace():
            self.host.create(NAME, SID)
            return True

        mode._terminal.calls.alive.side_effect = replace
        with self.assertRaises(DriverError):
            mode.screen()
        self.host.calls.screen.assert_not_called()

    def test_a_screen_callback_replacement_never_receives_the_old_mark(self):
        mode = self.started()
        original = mode._terminal

        def replace(_name, _lines):
            self.host.create(NAME, SID)
            return "Sign in"

        self.host.calls.screen.side_effect = replace
        self.assertEqual((mode.poll(0), mode.poll(0)), (([], True), ([], True)))
        self.assertEqual(self.run.screens, [])
        original.calls.mark.assert_not_called()
        self.host.processes[NAME].calls.mark.assert_not_called()

    def test_stop_never_kills_the_new_session(self):
        mode = self.started()
        original = mode._terminal

        def swap():
            self.host.create(NAME, SID)

        original.calls.close.side_effect = swap
        mode.stop()
        self.assertIsNot(self.host.processes[NAME], original)
        self.host.processes[NAME].calls.close.assert_not_called()
        self.assertNoSessionDir()


class SendScreenTest(Base):
    def test_a_nondisplayable_selected_host_runs_without_screen_scanning(self):
        self.host = MemoryHost("selected")
        mode = self.started()
        self.assertIs(mode.host, self.host)
        self.assertEqual(mode.poll(0), ([], False))
        self.append(record("stop", last_message="reply"))
        self.assertEqual(mode.poll(0), ([TurnEnd("reply", 0)], False))
        mode.send("hello")
        self.assertEqual(
            mode._terminal.calls.send.call_args_list,
            [mock.call(Paste("hello")), mock.call(Key("Enter"))],
        )
        with self.assertRaisesRegex(DriverError, r"\Ahost selected cannot display\Z"):
            mode.screen()
        self.assertNotIn("screen", [call[0] for call in self.host.calls.mock_calls])

    def test_send_preserves_the_adapter_actions_at_the_terminal_boundary(self):
        mode = self.started()
        mode.send("hi\nthere")
        self.assertEqual(
            mode._terminal.calls.send.call_args_list,
            [mock.call(Paste("hi\nthere")), mock.call(Key("Enter"))],
        )
        mode._terminal.calls.send.reset_mock()
        mode.run = Keys(self.run.adapter)
        mode.send("a")
        self.assertEqual(
            mode._terminal.calls.send.call_args_list,
            [
                mock.call(Key("Escape")),
                mock.call(Paste("a")),
                mock.call(Key("Enter")),
                mock.call(Key("Enter")),
                mock.call(Paste("b")),
                mock.call(Key("Tab")),
            ],
        )
        self.assertEqual(self.log, [])

    def test_send_or_screen_to_a_session_not_running_is_refused(self):
        mode = self.started()
        mode._terminal.calls.alive.return_value = False
        for call in (lambda: mode.send("hi"), mode.screen):
            with self.assertRaisesRegex(DriverError, r"\Atui session w1 is not running\Z"):
                call()
        mode._terminal.calls.send.assert_not_called()
        self.host.calls.screen.assert_not_called()

    def test_terminal_and_display_errors_propagate(self):
        mode = self.started()
        for method, call in (
            (mode._terminal.calls.send, lambda: mode.send("hi")),
            (self.host.calls.screen, mode.screen),
        ):
            with self.subTest(method=method):
                method.side_effect = DriverError("host operation failed")
                with self.assertRaisesRegex(DriverError, r"\Ahost operation failed\Z"):
                    call()
                method.side_effect = None

    def test_screen_uses_the_selected_displayable_host(self):
        mode = self.started()
        self.host.calls.screen.return_value = "a\nb"
        self.assertEqual((mode.screen(), mode.screen(5)), ("a\nb", "a\nb"))
        self.assertEqual(
            self.host.calls.screen.call_args_list,
            [mock.call(mode._terminal, None), mock.call(mode._terminal, 5)],
        )


class CloseStopTest(Base):
    def test_close_accepts_a_nonterminal_host_process_and_removes_mode_state(self):
        self.write_state(self.state())
        process = mock.create_autospec(HostProcess, instance=True)
        self.host.processes[NAME] = process
        tui.TuiRuntime.close(NAME, host=self.host)
        process.close.assert_called_once_with()
        self.assertNoSessionDir()

    def test_close_kills_the_session_and_removes_its_dir(self):
        mode = self.started()
        self.append(record("tool"))
        tui.TuiRuntime.close(NAME, host=self.host)
        mode._terminal.calls.close.assert_called_once_with()
        self.assertEqual(self.host.processes, {})
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))

    def test_close_kills_and_removes_under_the_names_lock(self):
        mode = self.started()
        held, remove = [], shutil.rmtree
        mode._terminal.calls.close.side_effect = lambda: held.append(self.locked())
        with mock.patch.object(
            shutil,
            "rmtree",
            lambda path, **kwargs: (held.append(self.locked()), remove(path, **kwargs)),
        ):
            tui.TuiRuntime.close(NAME, host=self.host)
        self.assertEqual((held, self.locked()), ([True, True], False))
        self.assertNoSessionDir()

    def test_close_of_an_absent_session_removes_its_dir(self):
        mode = self.started()
        self.host.processes.clear()
        tui.TuiRuntime.close(NAME, host=self.host)
        mode._terminal.calls.close.assert_not_called()
        self.assertNoSessionDir()
        tui.TuiRuntime.close(NAME, host=self.host)

    def test_a_kill_that_fails_on_a_live_or_unknown_session_is_a_driver_error(self):
        mode = self.started()
        mode._terminal.calls.close.side_effect = DriverError("host close failed")
        for state in (Liveness.LIVE, Liveness.UNKNOWN):
            with self.subTest(state=state):
                mode._terminal.calls.state.return_value = state
                with self.assertRaisesRegex(
                    DriverError, r"\Acannot close tui session w1: host close failed\Z"
                ):
                    tui.TuiRuntime.close(NAME, host=self.host)
                self.assertTrue(os.path.isdir(self.dir))

    def test_close_never_follows_a_symlink(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for make in (lambda: os.symlink(target, self.dir), lambda: open(self.dir, "w").close()):
            make()
            tui.TuiRuntime.close(NAME, host=self.host)
            self.assertTrue(os.path.lexists(self.dir))
            os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_stop_closes_the_session(self):
        mode = self.start()
        mode.stop()
        mode._terminal.calls.close.assert_called_once_with()
        self.assertNoSessionDir()


class PollTest(Base):
    def test_records_become_the_runs_events_in_order_bound_once(self):
        mode = self.started()
        self.append(
            record("tool", session_id="n1", tool="Bash"),
            record("stop", session_id="n1", last_message="hi", tasks=[1, 2]),
        )
        self.assertEqual(
            mode.poll(0.5), ([ClientBound("n1"), Activity("Bash"), TurnEnd("hi", 2)], False)
        )
        self.append(
            record("ask", session_id="n2", why="perm"),
            record("other", session_id="n2"),
            record("tool"),
        )
        self.assertEqual(
            mode.poll(0.5), ([ClientBound("n2"), Blocked("perm"), Activity("")], False)
        )
        self.assertFalse(hasattr(mode, "bound"))
        self.assertEqual(self.host.calls.mock_calls, [])
        self.assertEqual(self.log, [])

    def test_a_partial_last_line_waits_for_its_end(self):
        mode = self.started()
        line = (json.dumps(record("stop", last_message="r")) + "\n").encode()
        self.append(record("tool"), line[:10])
        self.assertEqual(mode.poll(0), ([Activity("")], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.append(line[10:])
        self.assertEqual(mode.poll(0), ([TurnEnd("r", 0)], False))

    def test_bad_lines_are_skipped(self):
        mode = self.started()
        self.append(
            b"not json\n",
            b"[1]\n",
            b'{"label": 1, "payload": {}}\n',
            b'{"label": "stop", "payload": []}\n',
            b'{"label": "stop"}\n',
            b"\xff\xfe\n",
            b"\n",
            b"[" * 100000 + b"\n",
            record("tool", tool="t"),
        )
        self.assertEqual(mode.poll(0), ([Activity("t")], False))

    def test_a_hooks_file_that_is_no_regular_file_gives_no_records(self):
        mode = self.started()
        real = os.path.join(self.tmp, "real.jsonl")
        with open(real, "w") as f:
            f.write(json.dumps(record("tool")) + "\n")
        for make in (lambda: os.symlink(real, self.hooks), lambda: os.mkfifo(self.hooks)):
            make()
            with self.subTest(stat.filemode(os.lstat(self.hooks).st_mode)):
                self.assertEqual(mode.poll(0), ([], False))
            os.unlink(self.hooks)

    def test_empty_poll_checks_the_terminal_and_waits_in_slices(self):
        mode = self.started()
        self.assertEqual(mode.poll(0.5), ([], False))
        self.assertEqual(self.host.calls.mock_calls, [mock.call.screen(mode._terminal, None)])
        mode._terminal.calls.returncode.assert_called_once_with()
        self.assertEqual(
            {call[0] for call in mode._terminal.calls.mock_calls}, {"state", "returncode"}
        )
        self.assertTrue(all(0 < seconds <= 0.1 for _, seconds in self.log), self.log)
        self.assertAlmostEqual(self.clock.now, 0.5)

    def test_a_quiet_wait_checks_the_terminal_once_per_poll_however_long(self):
        for timeout in (0.5, 5):
            with self.subTest(timeout=timeout):
                mode = self.started()
                self.assertEqual(mode.poll(timeout), ([], False))
                self.assertEqual(
                    self.host.calls.mock_calls, [mock.call.screen(mode._terminal, None)]
                )
                self.assertEqual(
                    sorted(call[0] for call in mode._terminal.calls.mock_calls),
                    ["returncode", "state", "state"],
                )
        mode = self.started()
        late = self.clock.now + 2.05
        self.clock.actions.append((late, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertEqual(mode._terminal.calls.state.call_count, 3)

    def test_a_record_while_waiting_returns_at_once(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertAlmostEqual(self.clock.now, 0.3)

    def test_dead_or_absent_drains_the_records_then_ends(self):
        for code in (3, -15, 0, -signal.SIGHUP):
            with self.subTest(code=code):
                mode = self.started()
                process = mode._terminal

                def ended(*, code=code):
                    self.append(record("stop", last_message="last"))
                    if code == -signal.SIGHUP:
                        self.host.processes.clear()
                    return code

                process.calls.returncode.side_effect = ended
                self.assertEqual(mode.poll(0.5), ([TurnEnd("last", 0)], True))
                self.host.calls.screen.assert_not_called()
                self.assertEqual(mode.returncode(1), code)

    def test_unknown_keeps_waiting_with_no_scan(self):
        mode = self.started()
        mode._terminal.calls.state.return_value = Liveness.UNKNOWN
        self.assertEqual(mode.poll(0.2), ([], False))
        self.host.calls.screen.assert_not_called()

    def test_exact_events_per_poll_over_a_session(self):
        mode = self.started()
        self.host.calls.screen.side_effect = ["", "Sign in", "Sign in", "x", "LIMIT 9", "x"]
        script = [
            [record("tool", session_id="n1")],
            [],
            [],
            [record("ask", session_id="n1")],
            [],
            [],
            [record("stop", session_id="n1", last_message="done")],
        ]
        got = []
        for lines in script:
            self.append(*lines)
            got.append(mode.poll(0.1))
        self.assertEqual(
            got,
            [
                ([ClientBound("n1"), Activity("")], False),
                ([], False),
                ([Blocked("Sign in")], False),
                ([Blocked("")], False),
                ([], False),
                ([Activity("")], False),
                ([TurnEnd("done", 0)], False),
            ],
        )
        self.assertEqual(mode.poll(0.1), ([LimitHit(9.0)], False))
        self.assertEqual(mode.poll(0.1), ([], False))


class ScanTest(Base):
    def test_a_hook_arriving_during_capture_precedes_the_limit_seen_on_screen(self):
        mode = self.started()

        def capture(_terminal, _lines):
            self.append(record("tool", tool="submitted"))
            self.host.calls.screen.side_effect = None
            self.host.calls.screen.return_value = "LIMIT 100"
            return "LIMIT 100"

        self.host.calls.screen.side_effect = capture
        self.assertEqual(mode.poll(0), ([Activity("submitted")], False))
        self.assertEqual(mode.poll(0), ([LimitHit(100.0)], False))
        self.assertEqual(mode.poll(0), ([], False))

    def scans(self, mode, *screens) -> list:
        """mode.poll's events for each screen in turn."""
        found = []
        for screen in screens:
            self.host.calls.screen.return_value = screen
            found.append(mode.poll(0)[0])
        return found

    def test_a_dialog_is_blocked_once_and_its_end_activity(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "x", "a Trust this \n  folder? b", "Trust this folder?", "y", "y"),
            [[], [Blocked("Trust  this\nfolder?")], [], [Activity("")], []],
        )
        self.assertEqual(self.run.screens[1], "a Trust this folder? b")

    def test_two_dialogs_one_going_as_the_other_comes(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "Sign in; Trust this folder?", "Sign in", "Trust this folder?", ""),
            [
                [Blocked("Trust  this\nfolder?"), Blocked("Sign in")],
                [Activity("")],
                [Activity(""), Blocked("Trust  this\nfolder?")],
                [Activity("")],
            ],
        )

    def test_a_dialog_marks_the_terminal_blocked_unless_it_is(self):
        mode = self.started()
        mode._terminal.calls.activity.return_value = Word.DONE
        self.scans(mode, "Sign in Trust this folder?")
        mode._terminal.calls.mark.assert_called_once_with(Word.BLOCKED, self.events)
        mode._terminal.calls.mark.reset_mock()
        mode._terminal.calls.activity.return_value = Word.BLOCKED
        self.scans(mode, "", "Sign in")
        mode._terminal.calls.mark.assert_not_called()

    def test_an_absent_name_is_not_scanned(self):
        mode = self.started()
        self.host.processes.clear()
        self.host.calls.screen.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
        mode._terminal.calls.mark.assert_not_called()

    def test_a_failed_mark_still_yields_the_dialog(self):
        mode = self.started()
        for method in (mode._terminal.calls.activity, mode._terminal.calls.mark):
            with self.subTest(method=method):
                method.side_effect = DriverError("host mark failed")
                self.assertEqual(
                    self.scans(mode, "Sign in", ""), [[Blocked("Sign in")], [Activity("")]]
                )
                method.side_effect = None

    def test_a_limit_is_one_event_per_appearance_its_reset_changing_or_not(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "LIMIT 100", "LIMIT  100", "LIMIT 200", "", "LIMIT 200", "LIMIT 300"),
            [[LimitHit(100.0)], [], [], [], [LimitHit(200.0)], []],
        )

    def test_a_failed_read_is_no_event(self):
        mode = self.started()
        self.host.calls.screen.side_effect = DriverError("tmux: gone")
        self.assertEqual(mode.poll(0), ([], False))


class ReturncodeStateTest(Base):
    def test_returncode_maps_the_status(self):
        for state, code in (
            (Liveness.LIVE, 0),
            (Liveness.LIVE, 2),
            (Liveness.LIVE, -9),
            (Liveness.ABSENT, -signal.SIGHUP),
            (Liveness.LIVE, None),
            (Liveness.UNKNOWN, None),
        ):
            with self.subTest(state=state, code=code):
                mode = self.started()
                mode._terminal.calls.state.return_value = state
                mode._terminal.calls.returncode.return_value = code
                self.assertEqual(mode.returncode(0), code)

    def test_returncode_waits_its_timeout_in_slices_while_running(self):
        mode = self.started()
        self.clock.actions.append(
            (0.25, lambda: setattr(mode._terminal.calls.returncode, "return_value", 4))
        )
        self.assertEqual(mode.returncode(5), 4)
        self.assertAlmostEqual(self.clock.now, 0.3)
        mode = self.started()
        self.clock.now = 0
        self.assertIsNone(mode.returncode(0.35))
        self.assertAlmostEqual(self.clock.now, 0.35)

    def test_state_maps_the_status(self):
        for state in Liveness:
            with self.subTest(state=state):
                mode = self.started()
                mode._terminal.calls.state.return_value = state
                self.assertEqual(mode.state(), state)


class PathsTest(Base):
    def test_a_sessions_dir_is_under_tui_dir_in_home(self):
        self.assertEqual(tui.TUI_DIR, "~/.agent-pm/tui")
        self.assertEqual(tui._Store(NAME, host=self.host).dir, self.dir)

    def test_its_hook_argv_runs_tui_hook_isolated_on_its_dir(self):
        hook = os.path.join(SRC["session"], "agent_pm", "session", "modes", "tui_hook.py")
        argv = tui._Store(NAME, host=self.host).hook_argv(PACKAGE_ROOTS)
        self.assertEqual(argv, (sys.executable, "-I", argv[2], self.dir, argv[4]))
        self.assertEqual(json.loads(argv[4]), [SRC["session"], SRC["utils"]])
        self.assertTrue(os.path.isabs(argv[2]))
        self.assertTrue(os.path.samefile(argv[2], hook))


class PrepareTest(Base):
    """start's session dir and session.json, before its session line."""

    def test_writes_session_json_0600_in_a_0700_session_dir(self):
        self.run.launched = {
            "cwd": "/cwd",
            "project": True,
            "transcript": "/t/x.jsonl",
            "resume": "fake --resume x",
        }
        self.start(workdir="/work")
        with open(os.path.join(self.dir, "session.json")) as f:
            self.assertEqual(
                json.load(f),
                self.state(
                    workdir="/work",
                    cwd="/cwd",
                    project=True,
                    transcript="/t/x.jsonl",
                    resume="fake --resume x",
                ),
            )
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        for path, mode_bits in (
            (self.root, 0o700),
            (self.dir, 0o700),
            (os.path.join(self.dir, "session.json"), 0o600),
        ):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), mode_bits, path)

    def test_its_identity_is_the_session_infos_persisted_fields_and_reads_back(self):
        self.run.launched = {"cwd": "/cwd", "project": True, "transcript": "/t", "resume": "r"}
        self.start()
        state, info = tui.TuiRuntime.snapshot(NAME), self.prepared.info
        persisted = info.persisted()
        self.assertEqual({key: state[key] for key in persisted}, persisted)
        self.assertEqual(set(persisted) & set(tui_hook.SESSION_STATE), {"sid"})
        self.assertEqual(
            SessionInfo.from_fields(state),
            SessionInfo(sid=SID, cwd="/cwd", project=True, transcript="/t", resume="r"),
        )

    def test_each_hook_kind_has_its_word(self):
        self.assertEqual(
            tui.WORDS,
            {
                HookKind.TURN_END: Word.DONE,
                HookKind.BLOCKED: Word.BLOCKED,
                HookKind.ACTIVITY: Word.WORKING,
            },
        )
        for kind, word in tui.WORDS.items():
            self.assertIs(type(kind), HookKind)
            self.assertIs(type(word), Word)

    def test_only_labels_of_a_known_kind_get_a_word_and_events_may_be_none(self):
        class Labels(ScanTui):
            hooks = {
                "end": HookKind.TURN_END,
                "perm": HookKind.BLOCKED,
                "note": HookKind.BLOCKED,
                "use": HookKind.ACTIVITY,
                "odd": "other",
            }

        self.start(run=Labels(self.adapter), events=None)
        self.assertEqual(
            tui.TuiRuntime.snapshot(NAME),
            self.state(
                events=None,
                words={"end": "done", "perm": "blocked", "note": "blocked", "use": "working"},
            ),
        )

    def test_a_dead_or_absent_session_is_prepared(self):
        for code in (0, 2, -15, None):
            with self.subTest(code=code):
                self.host.processes.clear()
                previous = self.host.create(NAME, SID) if code is not None else None
                if previous is not None:
                    previous.calls.alive.return_value = False
                    previous.calls.returncode.return_value = code
                self.start()
                self.assertTrue(os.path.isfile(os.path.join(self.dir, "session.json")))

    def test_a_running_or_unknown_session_is_refused_with_nothing_made(self):
        for failure, message in (
            (None, "tui session w1 is running"),
            (DriverError("host state unknown"), "host state unknown"),
        ):
            with self.subTest(failure=failure):
                process = self.host.create(NAME, SID)
                process.calls.alive.side_effect = failure
                with self.assertRaisesRegex(DriverError, message):
                    self.start()
                self.assertNoSessionDir()
        self.assertEqual(self.recorded, [])
        self.host.calls.terminal.assert_not_called()

    def test_a_bad_session_state_is_refused_before_host_launch_or_a_path(self):
        class BadLabel(ScanTui):
            hooks = {"a b": HookKind.TURN_END}

        for run, launched, fields, bad in (
            (ScanTui, {"transcript": "rel.jsonl"}, {}, "transcript 'rel.jsonl'\\Z"),
            (ScanTui, {"cwd": "rel"}, {}, "cwd 'rel'\\Z"),
            (BadLabel, {}, {}, "words "),
            (ScanTui, {}, {"client": None}, "client None"),
        ):
            with (
                self.subTest(bad),
                self.assertRaisesRegex(DriverError, f"\\Atui session w1: bad {bad}"),
            ):
                self.adapter.run = run(self.adapter)
                self.adapter.run.launched = launched
                self.start(**fields)
        self.assertEqual(
            [
                c
                for c in self.host.calls.mock_calls
                if c[0] not in ("protected_paths", "hook_callback", "validate")
            ],
            [],
        )
        self.assertFalse(os.path.lexists(self.root))
        self.assertEqual(self.recorded, [])

    def test_tui_dir_and_home_dir_are_made_when_missing(self):
        os.rmdir(self.agent_pm)
        self.start(events=None)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)

    def test_an_existing_tui_dir_is_set_to_0700(self):
        os.mkdir(self.root, 0o755)
        os.chmod(self.root, 0o755)
        self.start()
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)

    def test_a_tui_dir_that_is_no_real_directory_of_yours_is_refused(self):
        target = os.path.join(self.home, "target")
        os.mkdir(target, 0o755)
        os.chmod(target, 0o755)
        cases = (
            ("symlink", lambda: os.symlink(target, self.root), None),
            ("file", lambda: open(self.root, "w").close(), None),
            ("another user's", lambda: os.mkdir(self.root, 0o700), os.getuid() + 1),
        )
        for case, make, uid in cases:
            with self.subTest(case):
                make()
                with (
                    mock.patch("os.getuid", return_value=uid if uid is not None else os.getuid()),
                    self.assertRaisesRegex(DriverError, re.escape(self.root)),
                ):
                    self.start(events=None)
                self.assertTrue(os.path.lexists(self.root))
                (os.rmdir if case == "another user's" else os.unlink)(self.root)
        self.assertEqual((os.listdir(target), stat.S_IMODE(os.stat(target).st_mode)), ([], 0o755))
        self.host.calls.terminal.assert_not_called()

    def test_an_old_session_dir_is_replaced(self):
        os.makedirs(os.path.join(self.dir, "sub"))
        for name in ("hooks.jsonl", "session.json", "other"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("old")
        self.start()
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        self.assertEqual(tui.TuiRuntime.snapshot(NAME), self.state())

    def test_an_old_entry_that_is_no_real_directory_is_refused(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for case, make in (
            ("symlink", lambda: os.symlink(target, self.dir)),
            ("file", lambda: open(self.dir, "w").close()),
            ("fifo", lambda: os.mkfifo(self.dir)),
        ):
            with self.subTest(case):
                make()
                with self.assertRaisesRegex(
                    DriverError, rf"\A{re.escape(self.dir)}: not a directory\Z"
                ):
                    self.start()
                self.assertTrue(os.path.lexists(self.dir))
                os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_a_session_dir_another_start_makes_first_is_starting_and_kept(self):
        real = os.mkdir

        def racing(path, mode=0o777, **kwargs):
            if os.fspath(path) == self.dir:  # another start's mkdir lands first
                real(path, 0o700)
                open(os.path.join(path, "theirs"), "w").close()
            return real(path, mode, **kwargs)

        with mock.patch("os.mkdir", racing), self.assertRaises(DriverError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "tui session w1 is starting")
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_failed_write_is_a_driver_error_and_removes_the_dir(self):
        with (
            mock.patch(
                "tempfile.mkstemp", side_effect=OSError(errno.ENOSPC, "No space left on device")
            ),
            self.assertRaisesRegex(DriverError, "No space left on device"),
        ):
            self.start()
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(self.recorded, [])


class LockTest(Base):
    def test_a_0600_lock_file_per_name_in_tui_dir_held_till_the_block_ends(self):
        os.rmdir(self.agent_pm)
        with tui._Store(NAME, host=self.host)._locked():
            self.assertTrue(self.locked())
            with tui._Store("w2", host=self.host)._locked():
                self.assertTrue(self.locked("w2"))
            self.assertFalse(self.locked("w2"))
        self.assertFalse(self.locked())
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "w1.lock")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)
        self.assertEqual(self.host.calls.mock_calls, [])

    def test_a_second_holder_waits_for_the_first(self):
        entered = threading.Event()

        def second():
            with tui._Store(NAME, host=self.host)._locked():
                entered.set()

        with tui._Store(NAME, host=self.host)._locked():
            thread = threading.Thread(target=second)
            thread.start()
            self.assertFalse(entered.wait(0.2))
        thread.join(5)
        self.assertTrue(entered.is_set())

    def test_a_lock_path_that_is_no_regular_file_is_refused_at_once(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        open(target, "w").close()
        path = os.path.join(self.root, "w1.lock")
        for case, make in (
            ("symlink", lambda: os.symlink(target, path)),
            ("fifo", lambda: os.mkfifo(path)),
            ("directory", lambda: os.mkdir(path)),
        ):
            with self.subTest(case):
                make()
                with (
                    self.assertRaisesRegex(DriverError, rf"\A{re.escape(path)}: "),
                    tui._Store(NAME, host=self.host)._locked(),
                ):
                    pass
                (os.rmdir if case == "directory" else os.unlink)(path)


class StoreTest(Base):
    def test_its_operations_take_no_lock_from_their_caller(self):
        found = {
            name: list(inspect.signature(method).parameters)
            for name, method in inspect.getmembers(tui._Store, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual(
            sorted(found),
            ["close", "holder", "hook_argv", "process", "read", "records", "start", "stop"],
        )
        for name, parameters in found.items():
            with self.subTest(name=name):
                self.assertEqual([p for p in parameters if "lock" in p], [])

    def test_no_module_function_reaches_a_sessions_files_by_name(self):
        for gone in ("close", "lock", "read_state", "session_dir", "hook_argv"):
            with self.subTest(gone=gone):
                self.assertFalse(hasattr(tui, gone))

    def test_records_are_whole_lines_from_an_offset_and_the_offset_past_them(self):
        store = tui._Store(NAME, host=self.host)
        self.assertEqual(store.records(0), ([], 0))
        os.makedirs(self.dir)
        first = (json.dumps(record("tool", tool="t")) + "\n").encode()
        self.append(first, b"bad\n", record("stop"), b'{"label": "ask"')
        found, end = store.records(0)
        self.assertEqual(found, [("tool", {"tool": "t"}), ("stop", {})])
        self.assertEqual(store.records(len(first)), ([("stop", {})], end))
        self.assertEqual(store.records(end), ([], end))


class TransportErrorTest(TmuxBase):
    def test_real_host_launch_keeps_the_modes_exact_error_prefix(self):
        self.tmux.tmux_path.return_value = self.tool
        self.tmux.start.side_effect = TuiError("tmux: boom")
        workdir = os.path.join(self.dir, "work")
        os.mkdir(workdir)

        class Launchable(ScanTui):
            def launch(self, spec, hook, *, source_roots):
                return Launch((sys.executable,))

        adapter = TuiAdapter({"tui": True})
        adapter.run = Launchable(adapter)
        with self.assertRaises(DriverError) as caught:
            tui.TuiRuntime.start(
                Spec(workdir=workdir, name=NAME, client="fake", sid=SID),
                adapter,
                registry=self.driver.registry,
                record=lambda _info: None,
                host=self.host,
            )
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")

    def test_real_host_close_keeps_the_modes_exact_error_prefix(self):
        self.controlled, self.kind = False, None
        self.tmux.listing.return_value = [{"name": NAME, "display": None}]
        self.tmux.kill.side_effect = TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            tui.TuiRuntime.close(NAME, host=self.host)
        self.assertEqual(str(caught.exception), "cannot close tui session w1: tmux: boom")
