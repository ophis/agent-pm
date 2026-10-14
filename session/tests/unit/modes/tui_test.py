"""modes/tui.py: its start from a Spec (checks, a fresh session dir with session.json under the
name's lock, one session line, then the tmux session), attach (a restored Tui replaying the hook
log), polling hook records and the pane, send, screen, stop and close, and the store owning a
session's files and lock, over a fake tmux module.
"""

import errno
import fcntl
import inspect
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
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    DriverError,
    HookKind,
    Key,
    Launch,
    LimitHit,
    Mode,
    Paste,
    Prepared,
    SessionInfo,
    Spec,
    TurnEnd,
    Word,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes import tui, tui_hook

from session.tests import fake_adapter
from session.tests.case import SRC, Case
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.tui_support import TMUX, fake_host

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
        self.events = os.path.join(self.tmp, "events")
        self.host = fake_host()
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

    def start(self, run=None, record=None, **fields) -> tui.TuiMode:
        """A TuiMode started with self.adapter (its tui `run` if given) on Spec(workdir self.tmp,
        name NAME, runtime fake, sid SID, events self.events, **fields), its Prepared in
        self.prepared; its session line given to record, else kept in self.recorded.
        """
        if run is not None:
            self.adapter.run = run
        spec = Spec(
            **{
                "workdir": self.tmp,
                "name": NAME,
                "runtime": "fake",
                "sid": SID,
                "events": self.events,
                **fields,
            }
        )
        mode = tui.TuiMode.start(
            spec,
            self.adapter,
            registry=self.driver.registry,
            record=record or self.recorded.append,
            host=self.host,
        )
        self.prepared = mode.prepared
        return mode

    def started(self) -> tui.TuiMode:
        """A TuiMode started (the session absent) on pane %1, the host's calls cleared, its status
        RUNNING.
        """
        self.host.status.return_value, self.host.pane.return_value = None, "%1"
        mode = self.start()
        self.host.reset_mock()
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        return mode

    def append(self, *lines) -> None:
        """Each line to hooks.jsonl: bytes as given, else a JSON line."""
        with open(self.hooks, "ab") as f:
            for line in lines:
                f.write(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())

    def state(self, **changes) -> dict:
        return {
            "runtime": "fake",
            "sid": SID,
            "workdir": self.tmp,
            "cwd": self.tmp,
            "project": False,
            "transcript": "",
            "resume": "",
            "events": self.events,
            "words": WORDS,
            "tmux_path": TMUX,
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
        self.host.option.side_effect = lambda _name, key: {"@sid": SID}.get(key)

    def attach(self, *lines, adapter=None) -> tui.TuiMode:
        """TuiMode.attach of self.adapter (else `adapter`) on a session dir with session.json and
        hooks.jsonl's lines, the host's calls cleared first.
        """
        self.write_state(self.state())
        self.append(*lines)
        self.host.reset_mock()
        return tui.TuiMode.attach(
            NAME, tui.TuiMode.snapshot(NAME), adapter or self.adapter, host=self.host
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
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%1", "@sid")]
        )
        self.append(partial[7:], record("stop", session_id="n2", last_message="r3"))
        self.assertEqual(mode.poll(0), ([Activity("next"), TurnEnd("r3", 0)], False))

    def test_its_prepared_is_session_jsons_spec_and_identity(self):
        mode = self.attach()
        self.assertEqual(
            mode.prepared,
            Prepared(
                Spec(
                    workdir=self.tmp,
                    runtime="fake",
                    mode=Mode.TUI,
                    sid=SID,
                    name=NAME,
                    events=self.events,
                ),
                SessionInfo(sid=SID, cwd=self.tmp, project=False, transcript="", resume=""),
            ),
        )

    def test_a_session_json_without_a_valid_identity_is_no_tui_session_before_tmux(self):
        with self.assertRaises(DriverError) as caught:
            tui.TuiMode.attach(NAME, self.state(cwd=""), self.adapter, host=self.host)
        path = os.path.join(self.dir, "session.json")
        self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad cwd")
        self.assertEqual(self.host.mock_calls, [])

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
                    lambda: tui.TuiMode.snapshot(NAME),
                    lambda *, field=field, value=value: tui.TuiMode.attach(
                        NAME, self.state(**{field: value}), self.adapter, host=self.host
                    ),
                ):
                    with self.assertRaises(DriverError) as caught:
                        call()
                    self.assertEqual(
                        str(caught.exception), f"no tui session w1: {path}: bad {field}"
                    )
        self.assertEqual(self.host.mock_calls, [])

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
        attached = tui.TuiMode.attach(NAME, tui.TuiMode.snapshot(NAME), fresh, host=self.host)
        self.assertEqual(attached.replayed, tuple(polled))
        self.assertEqual(len(polled), 6)

    def test_a_fresh_tui_is_restored_once_with_its_adapter_state_before_its_records(self):
        for extra, kept in (({"adapter_state": {"store": "/s"}}, {"store": "/s"}), ({}, {})):
            with self.subTest(extra=extra):
                adapter = FreshAdapter({"tui": True})
                self.write_state(self.state(**extra))
                self.append(record("tool"), record("stop"))
                mode = tui.TuiMode.attach(NAME, tui.TuiMode.snapshot(NAME), adapter, host=self.host)
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
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%1", "@sid")]
        )
        self.adapter.run = None
        with self.assertRaises(DriverError) as caught:
            self.attach()
        self.assertEqual(str(caught.exception), "no tui session w1: runtime 'fake' has no tui mode")
        self.assertEqual(self.host.mock_calls, [])

    def test_marked_blocked_is_its_panes_state(self):
        self.host.pane.return_value = "%7"
        for state, blocked in (
            ("blocked", True),
            ("working", False),
            (None, False),
            (tmux_host.TuiError("tmux: denied"), False),
        ):
            with self.subTest(state=state):

                def option(target, key, *, state=state):
                    if (target, key) == ("%7", "@state") and isinstance(state, Exception):
                        raise state
                    return {("%7", "@sid"): SID, ("%7", "@state"): state}.get((target, key))

                self.host.option.side_effect = option
                mode = self.attach()
                self.assertIs(mode.marked_blocked(), blocked)
                self.host.option.assert_called_with("%7", "@state")

    def test_another_sid_absent_or_unknown_is_no_tui_session(self):
        for found in ("other", None, tmux_host.TuiError("tmux: denied")):
            with self.subTest(found=found):
                self.host.option.side_effect = (
                    found
                    if isinstance(found, Exception)
                    else lambda _name, _key, *, found=found: found
                )
                with self.assertRaises(DriverError) as caught:
                    self.attach()
                self.assertEqual(str(caught.exception), "no tui session w1")
        self.host.option.side_effect = lambda _name, key: {"@sid": SID}.get(key)
        for pane in (None, tmux_host.TuiError("tmux: denied")):
            with self.subTest(pane=pane):
                self.host.pane.return_value = pane
                self.host.pane.side_effect = pane if isinstance(pane, Exception) else None
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
                    self.attach()
                self.host.option.assert_not_called()

    def test_it_reads_the_sid_of_the_pane_it_took_so_a_pane_swapped_in_is_never_bound(self):
        live = {"%7": SID}  # pane -> its session's @sid; the one pane holds the name

        def then_swap(answer):
            def call(*args):
                found = answer(*args)
                if self.host.pane.call_count + self.host.option.call_count == 1:
                    live.clear()
                    live["%8"] = SID  # a new session under the name
                return found

            return call

        self.host.pane.side_effect = then_swap(lambda _name: next(iter(live)))
        self.host.option.side_effect = then_swap(
            lambda target, _key: live.get(next(iter(live)) if target == NAME else target)
        )
        try:
            bound = self.attach()._pane
        except DriverError as e:
            bound = str(e)
        self.assertEqual(bound, "no tui session w1")
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%7", "@sid")]
        )

    def test_an_attached_session_is_live_and_marks_dialogs_on_its_events_file(self):
        self.host.pane.return_value = "%7"
        mode = self.attach()
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([Blocked("Sign in")], False))
        self.host.mark.assert_called_once_with("blocked", self.events, pane="%7")
        mode.stop()
        self.host.kill.assert_called_once_with("%7")

    def test_an_attached_session_ends_once_another_pane_holds_its_name(self):
        self.host.pane.return_value = "%7"
        mode = self.attach()
        self.host.status.return_value, self.host.pane.return_value = (
            tmux_host.PaneStatus.RUNNING,
            "%8",
        )
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))


class StartTest(Base):
    def nothing_hosted(self):
        """No tmux call but the checks', no path made, no session line."""
        self.assertEqual(
            [c for c in self.host.mock_calls if c[0] not in ("events_file", "tmux_path")], []
        )
        self.assertFalse(os.path.lexists(self.root))
        self.assertEqual(self.recorded, [])

    def test_a_display_is_kept_as_its_tmux_option(self):
        tui.TuiMode.start(
            Spec(workdir=self.tmp, name=NAME, runtime="fake", sid=SID),
            self.adapter,
            registry=self.driver.registry,
            record=self.recorded.append,
            host=self.host,
            display="mgr/below",
        )
        self.assertEqual(
            self.host.start.call_args.kwargs["tags"],
            {"@sid": SID, "@runtime": "fake", "@display": "mgr/below"},
        )

    def test_starts_the_session_tagged_with_its_sid_and_runtime_per_the_tui_settings(self):
        self.configure_session({}, tmux={"status_line": True})
        mode = self.start()
        self.assertEqual(
            [c[0] for c in self.host.mock_calls],
            ["events_file", "tmux_path", "status", "start", "pane"],
        )
        (call,) = self.host.start.call_args_list
        self.assertEqual(
            call,
            mock.call(
                NAME,
                ["/bin/run", *tui._Store(NAME).hook_argv(PACKAGE_ROOTS)],
                events=self.events,
                status_line=True,
                tags={"@sid": SID, "@runtime": "fake"},
                cwd=self.tmp,
                env=dict(self.prepared.env),
            ),
        )
        self.assertEqual(self.run.roots, PACKAGE_ROOTS)
        self.assertEqual(
            (mode.name, mode.prepared.info.sid, mode.prepared.spec.events, mode._store.dir),
            (NAME, SID, self.events, self.dir),
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.configure_session({})
        self.start(events=None)
        self.assertEqual(
            {
                k: v
                for k, v in self.host.start.call_args.kwargs.items()
                if k in ("events", "status_line")
            },
            {"events": None, "status_line": False},
        )

    def test_one_session_line_between_session_json_and_tmux_under_the_names_lock(self):
        lines = []

        def record(info):
            lines.append(
                (info, tui.TuiMode.snapshot(NAME)["sid"], self.host.start.called, self.locked())
            )

        self.start(record=record)
        self.assertEqual(lines, [(self.prepared.info, SID, False, True)])
        self.host.start.assert_called_once()
        self.assertEqual(
            self.prepared.info,
            SessionInfo(sid=SID, cwd=self.tmp, project=False, transcript="", resume=""),
        )

    def test_the_names_lock_is_held_from_session_dir_to_tmux(self):
        held = []
        self.host.status.side_effect = self.host.start.side_effect = lambda *_a, **_k: held.append(
            self.locked()
        )
        self.start()
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError):
            self.start()
        self.assertEqual((held, self.locked()), ([True, True, True], False))

    def test_the_spec_as_started_names_the_session_and_protects_the_hook_paths(self):
        self.start(protect=("/c",))
        self.assertEqual(
            (self.prepared.spec.name, self.prepared.spec.protect),
            (NAME, ("/c", TMUX, self.events, self.root)),
        )
        mode = self.start(name=None, events=None)
        self.assertEqual(
            (mode.name, self.prepared.spec.name, self.prepared.spec.protect),
            ("fake-0f8fad5b", "fake-0f8fad5b", (TMUX, self.root)),
        )
        self.assertEqual(self.host.start.call_args.args[0], "fake-0f8fad5b")

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
            self.host.start.call_args.kwargs["env"],
            {"HOME": os.environ["HOME"], "KEEP": "k", "LAUNCH_SET": "1", "PWD": self.tmp},
        )

    def test_a_bad_name_given_or_made_from_the_sid_is_refused_before_tmux_or_a_path(self):
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
        self.assertEqual(self.host.mock_calls, [])
        self.nothing_hosted()

    def test_a_bad_events_file_is_refused(self):
        os.mkdir(self.events)
        sharp = os.path.join(self.tmp, "a#b")
        for bad, message in (
            (self.events, f"events file {self.events}: Is a directory"),
            (sharp, f"events file {sharp}: tmux would misread it"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.start(events=bad)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_no_tmux_on_path_is_a_driver_error_with_nothing_started(self):
        self.host.tmux_path.return_value = None
        with self.assertRaises(DriverError) as caught:
            self.start(events=None)
        self.assertEqual(str(caught.exception), "tmux not found")
        self.assertEqual(self.host.mock_calls, [mock.call.tmux_path()])
        self.nothing_hosted()

    def test_a_bad_tui_setting_or_no_tui_mode_is_refused_before_anything(self):
        self.configure_session({}, tmux={"status_line": "yes"})
        with self.assertRaises(ConfigError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "status_line: want true or false")
        self.adapter.run = None
        with self.assertRaises(ConfigError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "runtime 'fake' does not support tui mode")
        self.assertEqual(self.host.mock_calls, [])
        self.nothing_hosted()

    def test_session_selection_flags_are_refused_before_launch(self):
        self.adapter.forbidden_selection = ("--resume",)
        with self.assertRaises(ConfigError) as caught:
            self.start(flags=("--resume", SID))
        self.assertEqual(str(caught.exception), "--resume: picks the session; use sid= or resume=")
        self.nothing_hosted()

    def test_a_failed_tmux_start_is_a_driver_error_after_the_session_line_removing_only_its_dir(
        self,
    ):
        os.makedirs(os.path.join(self.root, "w2"))
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.assertEqual(len(self.recorded), 1)
        self.assertNoSessionDir()
        self.assertEqual(sorted(os.listdir(self.root)), ["w1.lock", "w2"])
        self.host.kill.assert_not_called()

    def test_a_failed_start_keeps_a_dir_it_did_not_make(self):
        def swap(_info):
            os.rename(self.dir, os.path.join(self.tmp, "ours"))  # kept: no inode reuse
            os.mkdir(self.dir)
            open(os.path.join(self.dir, "theirs"), "w").close()

        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError):
            self.start(record=swap)
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_failed_record_or_a_start_failing_without_a_tmux_error_leaves_its_dir(self):
        def fail(_info):
            raise OSError("disk full")

        for record, failure in ((fail, None), (None, ValueError("boom"))):
            with self.subTest(failure=failure):
                self.host.reset_mock()
                self.host.start.side_effect = failure
                with self.assertRaises(OSError if failure is None else ValueError):
                    self.start(record=record)
                self.assertEqual(os.listdir(self.dir), ["session.json"])
                self.assertEqual(len(self.host.start.mock_calls), 0 if failure is None else 1)
                self.assertFalse(self.locked())

    def test_a_pane_it_cannot_take_closes_the_session(self):
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.host.kill.assert_called_once_with(NAME)
        self.assertNoSessionDir()

    def test_a_pane_it_cannot_take_closes_without_waiting_for_the_names_lock_it_holds(self):
        caught = []
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")

        def start():
            try:
                self.start()
            except DriverError as e:
                caught.append(str(e))

        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        thread.join(5)
        self.assertEqual(caught, ["cannot start tui session w1: tmux: boom"])
        self.host.kill.assert_called_once_with(NAME)
        self.assertNoSessionDir()

    def test_start_returns_its_session_live_at_once_with_its_prepared(self):
        mode = tui.TuiMode.start(
            Spec(workdir=self.tmp, name=NAME, runtime="fake", sid=SID),
            self.adapter,
            registry=self.driver.registry,
            record=self.recorded.append,
            host=self.host,
        )
        self.assertIsInstance(mode, tui.TuiMode)
        self.assertEqual((mode.prepared.info, mode.prepared.spec.name), (self.recorded[0], NAME))
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.assertEqual((mode.state(), mode.poll(0)), ("live", ([], False)))
        mode.stop()
        self.host.kill.assert_called_once_with("%1")
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))


class SnapshotTest(Base):
    def test_a_valid_state(self):
        self.start()
        self.assertEqual(tui.TuiMode.snapshot(NAME), self.state())
        self.write_state(
            self.state(events=None, transcript="/t", project=True, resume="r", words={}, later=1)
        )
        self.assertEqual(tui.TuiMode.snapshot(NAME)["later"], 1)

    def test_a_runtime_is_lowercase_word_characters_led_by_a_letter(self):
        for runtime, ok in (
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
            with self.subTest(runtime=runtime):
                self.write_state(self.state(runtime=runtime))
                if ok:
                    self.assertEqual(tui.TuiMode.snapshot(NAME)["runtime"], runtime)
                else:
                    with self.assertRaises(DriverError) as caught:
                        tui.TuiMode.snapshot(NAME)
                    path = os.path.join(self.dir, "session.json")
                    self.assertEqual(
                        str(caught.exception), f"no tui session w1: {path}: bad runtime"
                    )

    def test_missing_or_invalid_is_a_driver_error(self):
        with self.assertRaisesRegex(DriverError, r"\Ano tui session w1: "):
            tui.TuiMode.snapshot(NAME)
        bad = {
            "words": {"stop": "dead"},
            "events": "rel",
            "sid": "a/b",
            "runtime": "Fake",
            "workdir": "rel",
            "tmux_path": "tmux",
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
                        tui.TuiMode.snapshot(NAME)
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
                    tui.TuiMode.snapshot(NAME)
                self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad {field}")
        for data in (b"", b"[]", b"not json"):
            with self.subTest(data):
                self.write_state(data)
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1: "):
                    tui.TuiMode.snapshot(NAME)

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
            tui.TuiMode.snapshot(NAME)
        except DriverError:
            return True
        return False


class BoundTest(Base):
    """A begun Mode is its pane's (%1): another pane under its name, or stop(), ends it."""

    def test_another_pane_under_the_name_ends_it_at_once_unscanned_unmarked_and_unread(self):
        for status in (tmux_host.PaneStatus.RUNNING, 3):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value, self.host.pane.return_value = status, "%2"
                self.host.read.return_value = "Sign in LIMIT 9"
                self.log.clear()
                self.assertEqual(mode.poll(5), ([], True))
                self.assertEqual(
                    (self.run.screens, self.log, mode.returncode(0)), ([], [], -signal.SIGHUP)
                )
                self.host.mark.assert_not_called()
                self.append(record("tool"))
                self.host.reset_mock()
                self.assertEqual(mode.poll(5), ([], True))
                self.assertEqual(self.host.mock_calls, [])

    def test_records_under_another_pane_are_never_read(self):
        mode = self.started()
        self.host.pane.return_value = "%2"
        self.append(record("stop", last_message="theirs"))
        self.assertEqual(mode.poll(5), ([], True))
        mode = self.started()

        def theirs():
            self.host.pane.return_value = "%2"
            self.append(record("stop", last_message="theirs"))

        self.clock.actions.append((self.clock.now + 0.25, theirs))
        self.assertEqual((mode.poll(5), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_ends_it_at_once_its_code_the_one_seen_else_sighup(self):
        mode = self.started()
        mode.stop()
        os.mkdir(self.dir)
        self.append(record("tool"))
        self.host.reset_mock()
        self.assertEqual((mode.poll(5), mode.returncode(5)), (([], True), -signal.SIGHUP))
        self.assertEqual((self.host.mock_calls, self.log), ([], []))
        mode = self.started()
        self.host.status.return_value = 3
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.assertEqual(mode.returncode(0), 3)

    def test_stop_kills_its_pane_while_it_holds_the_name_the_name_when_tmux_fails(self):
        for holder, kills, removed in (
            ("%1", [mock.call("%1")], True),
            ("%2", [], False),
            (tmux_host.TuiError("tmux: boom"), [mock.call(NAME)], True),
        ):
            with self.subTest(holder=holder):
                mode = self.started()
                self.host.pane.return_value = holder
                self.host.pane.side_effect = holder if isinstance(holder, Exception) else None
                mode.stop()
                self.assertEqual(
                    (self.host.kill.call_args_list, os.path.isdir(self.dir)), (kills, not removed)
                )
                self.assertEqual(mode.poll(0), ([], True))
                self.host.pane.side_effect = None

    def test_stop_with_the_name_absent_kills_nothing_as_a_new_server_may_reuse_its_pane_id(self):
        mode = self.started()
        self.host.pane.return_value = None
        self.host.status.side_effect = lambda target: (
            tmux_host.PaneStatus.RUNNING if target == "%1" else None
        )
        mode.stop()
        self.assertEqual(self.host.mock_calls, [mock.call.pane(NAME)])
        self.assertNoSessionDir()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_checks_the_holder_and_closes_under_the_names_lock(self):
        held = []
        for holder, closed in (("%1", True), ("%2", False)):
            with self.subTest(holder=holder):
                self.host.pane.side_effect = self.host.kill.side_effect = None
                mode = self.started()
                held.clear()
                self.host.pane.side_effect = lambda _name, *, holder=holder: (
                    held.append(self.locked()) or holder
                )
                self.host.kill.side_effect = lambda _name: held.append(self.locked())
                mode.stop()
                self.assertEqual((held, self.locked()), ([True] * (1 + closed), False))

    def test_another_pane_under_the_name_is_absent_to_state_send_and_screen(self):
        mode = self.started()
        self.host.pane.return_value = "%2"
        self.assertEqual(mode.state(), "absent")
        for call in (lambda: mode.send("hi"), mode.screen):
            with self.assertRaisesRegex(DriverError, r"\Atui session w1 is not running\Z"):
                call()
        self.host.paste.assert_not_called()
        self.host.read.assert_not_called()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_with_no_pane_bound_it_reads_no_screen_and_stops_killing_nothing(self):
        self.host.pane.return_value = None  # start found no pane
        mode = self.start()
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.host.read.assert_not_called()
        self.host.kill.assert_not_called()
        self.assertNoSessionDir()

    def test_its_pane_unknown_is_no_scan_and_no_end(self):
        mode = self.started()
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            (self.run.screens, mode.returncode(0), mode.state()), ([], None, "unknown")
        )


class SwapTest(Base):
    """A new session takes the name right after the Mode's pane check: every later call targets its
    pane (%1), so none reaches the new pane (%2).
    """

    def swapped(self, screen: str = "") -> list:
        """After started(): the next pane(NAME) answers %1, then %2 holds the name and %1 is gone
        (absent to status and option, a TuiError to the rest). Returns (call, pane) for each call
        that reached a pane.
        """
        holder, reached = ["%1"], []

        def at(target):
            return holder[0] if target in (NAME, holder[0]) else None

        def check(_name):
            found, holder[0] = holder[0], "%2"
            return found

        def act(method, result):
            def call(target, *_args):
                pane = at(target)
                if pane is None and method != "option":
                    raise tmux_host.TuiError(f"tmux: can't find pane: {target}")
                if pane is not None:
                    reached.append((method, pane))
                return result

            return call

        self.host.pane.side_effect = check
        self.host.status.side_effect = lambda target: (
            tmux_host.PaneStatus.RUNNING if at(target) else None
        )
        for method, result in (
            ("paste", None),
            ("keys", None),
            ("read", screen),
            ("option", None),
            ("kill", None),
        ):
            getattr(self.host, method).side_effect = act(method, result)
        return reached

    def error(self, call) -> str | None:
        try:
            call()
        except DriverError as e:
            return str(e)
        return None

    def test_send_never_types_into_the_new_pane(self):
        mode = self.started()
        reached = self.swapped()
        self.assertEqual(
            (self.error(lambda: mode.send("hi")), reached),
            ("send to tui session w1: tmux: can't find pane: %1", []),
        )

    def test_screen_never_reads_the_new_pane(self):
        mode = self.started()
        reached = self.swapped()
        self.assertEqual(
            (self.error(mode.screen), reached),
            ("screen of tui session w1: tmux: can't find pane: %1", []),
        )

    def test_poll_reads_and_marks_by_its_pane_only(self):
        mode = self.started()
        reached = self.swapped("Sign in")
        self.assertEqual((mode.poll(0), mode.poll(0)), (([Blocked("Sign in")], False), ([], True)))
        self.assertEqual(reached, [("read", "%1")])
        self.host.mark.assert_called_once_with("blocked", self.events, pane="%1")

    def test_stop_never_kills_the_new_session(self):
        mode = self.started()
        reached = self.swapped()
        mode.stop()
        self.assertEqual((reached, os.path.isdir(self.dir)), ([], False))
        self.host.kill.assert_called_once_with("%1")


class SendScreenTest(Base):
    def test_send_pastes_and_presses_the_runs_keys_waiting_pause_after_a_paste(self):
        mode = self.started()
        self.host.paste.side_effect = lambda *args: self.log.append(("paste", *args))
        self.host.keys.side_effect = lambda *args: self.log.append(("keys", *args))
        mode.send("hi\nthere")
        self.assertEqual(
            self.log,
            [("paste", "%1", "hi\nthere"), ("sleep", tmux_host.PAUSE), ("keys", "%1", "Enter")],
        )
        self.log.clear()
        mode.run = Keys(self.run.adapter)
        mode.send("a")
        self.assertEqual(
            self.log,
            [
                ("keys", "%1", "Escape"),
                ("paste", "%1", "a"),
                ("sleep", tmux_host.PAUSE),
                ("keys", "%1", "Enter"),
                ("keys", "%1", "Enter"),
                ("paste", "%1", "b"),
                ("sleep", tmux_host.PAUSE),
                ("keys", "%1", "Tab"),
            ],
        )

    def test_send_or_screen_to_a_session_not_running_is_refused(self):
        mode = self.started()
        for status in (0, -9, None, tmux_host.PaneStatus.UNKNOWN):
            for call in (lambda: mode.send("hi"), mode.screen):
                with self.subTest(status=status):
                    self.host.status.return_value = status
                    with self.assertRaises(DriverError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), "tui session w1 is not running")
        self.host.paste.assert_not_called()
        self.host.read.assert_not_called()

    def test_a_tmux_error_is_a_driver_error(self):
        mode = self.started()
        for method, call in (
            ("paste", lambda: mode.send("hi")),
            ("keys", lambda: mode.send("hi")),
            ("read", mode.screen),
        ):
            with self.subTest(method):
                getattr(self.host, method).side_effect = tmux_host.TuiError("tmux: boom")
                with self.assertRaisesRegex(DriverError, r"\A.*tui session w1: tmux: boom\Z"):
                    call()
                getattr(self.host, method).side_effect = None

    def test_screen_reads_the_pane(self):
        mode = self.started()
        self.host.read.return_value = "a\nb"
        self.assertEqual((mode.screen(), mode.screen(5)), ("a\nb", "a\nb"))
        self.assertEqual(self.host.read.call_args_list, [mock.call("%1", None), mock.call("%1", 5)])


class CloseStopTest(Base):
    def test_close_kills_the_session_and_removes_its_dir(self):
        self.started()
        self.append(record("tool"))
        tui.TuiMode.close(NAME, host=self.host)
        self.assertEqual(self.host.mock_calls, [mock.call.kill(NAME)])
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))

    def test_close_kills_and_removes_under_the_names_lock(self):
        self.started()
        held, remove = [], shutil.rmtree
        self.host.kill.side_effect = lambda _name: held.append(self.locked())
        with mock.patch.object(
            shutil,
            "rmtree",
            lambda path, **kwargs: (held.append(self.locked()), remove(path, **kwargs)),
        ):
            tui.TuiMode.close(NAME, host=self.host)
        self.assertEqual((held, self.locked()), ([True, True], False))
        self.assertNoSessionDir()

    def test_close_of_an_absent_session_removes_its_dir(self):
        self.started()
        self.host.kill.side_effect = tmux_host.TuiError("tmux: can't find session: w1")
        self.host.status.return_value = None
        tui.TuiMode.close(NAME, host=self.host)
        self.assertNoSessionDir()
        tui.TuiMode.close(NAME, host=self.host)

    def test_a_kill_that_fails_on_a_live_or_unknown_session_is_a_driver_error(self):
        self.started()
        self.host.kill.side_effect = tmux_host.TuiError("tmux: boom")
        for status in (tmux_host.PaneStatus.RUNNING, tmux_host.PaneStatus.UNKNOWN, 0):
            with self.subTest(status=status):
                self.host.status.return_value = status
                with self.assertRaises(DriverError) as caught:
                    tui.TuiMode.close(NAME, host=self.host)
                self.assertEqual(str(caught.exception), "cannot close tui session w1: tmux: boom")
                self.assertTrue(os.path.isdir(self.dir))

    def test_close_never_follows_a_symlink(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for make in (lambda: os.symlink(target, self.dir), lambda: open(self.dir, "w").close()):
            make()
            tui.TuiMode.close(NAME, host=self.host)
            self.assertTrue(os.path.lexists(self.dir))
            os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_stop_closes_the_session(self):
        self.start().stop()
        self.host.kill.assert_called_once_with("%1")
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
        self.assertEqual(self.host.mock_calls, [mock.call.pane(NAME)] * 2)
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

    def test_empty_poll_checks_status_and_pane_then_waits_in_slices(
        self,
    ):
        mode = self.started()
        self.assertEqual(mode.poll(0.5), ([], False))
        self.assertEqual(
            self.host.mock_calls,
            [mock.call.status(NAME), mock.call.read("%1"), mock.call.pane(NAME)],
        )
        self.assertTrue(all(0 < s <= 0.1 for _, s in self.log), self.log)
        self.assertAlmostEqual(self.clock.now, 0.5)

    def test_a_record_while_waiting_returns_at_once(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertAlmostEqual(self.clock.now, 0.3)

    def test_dead_or_absent_drains_the_records_then_ends(self):
        for status, code in ((3, 3), (-15, -15), (0, 0), (None, -signal.SIGHUP)):
            with self.subTest(status=status):
                mode = self.started()

                def ended(_name, status=status):
                    self.append(record("stop", last_message="last"))  # written before the pane died
                    return status

                self.host.status.side_effect = ended
                self.assertEqual(mode.poll(0.5), ([TurnEnd("last", 0)], True))
                self.host.read.assert_not_called()
                self.host.status.side_effect = AssertionError("status after the end")
                self.assertEqual(mode.returncode(1), code)
                self.host.status.side_effect = None

    def test_unknown_keeps_waiting_with_no_scan(self):
        mode = self.started()
        self.host.status.return_value = tmux_host.PaneStatus.UNKNOWN
        self.assertEqual(mode.poll(0.2), ([], False))
        self.host.read.assert_not_called()

    def test_exact_events_per_poll_over_a_session(self):
        mode = self.started()
        self.host.read.side_effect = ["", "Sign in", "Sign in", "x", "LIMIT 9", "x"]
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
    def scans(self, mode, *screens) -> list:
        """mode.poll's events for each screen in turn."""
        found = []
        for screen in screens:
            self.host.read.return_value = screen
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

    def test_a_dialog_marks_the_pane_blocked_unless_it_is(self):
        mode = self.started()
        self.host.option.return_value = "done"
        self.scans(mode, "Sign in Trust this folder?")
        self.assertEqual(
            self.host.mock_calls[3:],
            [mock.call.option("%1", "@state"), mock.call.mark("blocked", self.events, pane="%1")],
        )
        self.host.reset_mock()
        self.host.option.return_value = "blocked"
        self.scans(mode, "", "Sign in")
        self.host.mark.assert_not_called()

    def test_a_name_with_no_pane_is_absent_unscanned(self):
        mode = self.started()
        self.host.pane.return_value = None
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
        self.host.mark.assert_not_called()

    def test_a_failed_mark_still_yields_the_dialog(self):
        mode = self.started()
        for method in ("option", "mark"):
            with self.subTest(method):
                getattr(self.host, method).side_effect = tmux_host.TuiError("tmux: boom")
                self.assertEqual(
                    self.scans(mode, "Sign in", ""), [[Blocked("Sign in")], [Activity("")]]
                )
                getattr(self.host, method).side_effect = None

    def test_a_limit_is_one_event_per_appearance_its_reset_changing_or_not(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "LIMIT 100", "LIMIT  100", "LIMIT 200", "", "LIMIT 200", "LIMIT 300"),
            [[LimitHit(100.0)], [], [], [], [LimitHit(200.0)], []],
        )

    def test_a_failed_read_is_no_event(self):
        mode = self.started()
        self.host.read.side_effect = tmux_host.TuiError("tmux: gone")
        self.assertEqual(mode.poll(0), ([], False))


class ReturncodeStateTest(Base):
    def test_returncode_maps_the_status(self):
        for status, code in (
            (0, 0),
            (2, 2),
            (-9, -9),
            (None, -signal.SIGHUP),
            (tmux_host.PaneStatus.RUNNING, None),
            (tmux_host.PaneStatus.UNKNOWN, None),
        ):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value = status
                self.assertEqual(mode.returncode(0), code)

    def test_returncode_waits_its_timeout_in_slices_while_running(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: setattr(self.host.status, "return_value", 4)))
        self.assertEqual(mode.returncode(5), 4)
        self.assertAlmostEqual(self.clock.now, 0.3)
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        mode = self.started()
        self.clock.now = 0
        self.assertIsNone(mode.returncode(0.35))
        self.assertAlmostEqual(self.clock.now, 0.35)

    def test_state_maps_the_status(self):
        for status, state in (
            (tmux_host.PaneStatus.RUNNING, "live"),
            (0, "live"),
            (-15, "live"),
            (None, "absent"),
            (tmux_host.PaneStatus.UNKNOWN, "unknown"),
        ):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value = status
                self.assertEqual(mode.state(), state)


class PathsTest(Base):
    def test_a_sessions_dir_is_under_tui_dir_in_home(self):
        self.assertEqual(tui.TUI_DIR, "~/.agent-pm/tui")
        self.assertEqual(tui._Store(NAME).dir, self.dir)

    def test_its_hook_argv_runs_tui_hook_isolated_on_its_dir(self):
        hook = os.path.join(SRC["session"], "agent_pm", "session", "modes", "tui_hook.py")
        argv = tui._Store(NAME).hook_argv(PACKAGE_ROOTS)
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
        state, info = tui.TuiMode.snapshot(NAME), self.prepared.info
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
            tui.TuiMode.snapshot(NAME),
            self.state(
                events=None,
                words={"end": "done", "perm": "blocked", "note": "blocked", "use": "working"},
            ),
        )

    def test_a_dead_or_absent_session_is_prepared(self):
        for status in (0, 2, -15, None):
            with self.subTest(status=status):
                self.host.status.return_value = status
                self.start()
                self.assertTrue(os.path.isfile(os.path.join(self.dir, "session.json")))

    def test_a_running_or_unknown_session_is_refused_with_nothing_made(self):
        for status, message in (
            (tmux_host.PaneStatus.RUNNING, "tui session w1 is running; tmux attach -t '=w1'"),
            (tmux_host.PaneStatus.UNKNOWN, "tui session w1: its tmux state is unknown"),
        ):
            with self.subTest(status=status):
                self.host.status.return_value = status
                with self.assertRaises(DriverError) as caught:
                    self.start()
                self.assertEqual(str(caught.exception), message)
                self.assertNoSessionDir()
        self.assertEqual(self.recorded, [])
        self.host.start.assert_not_called()

    def test_a_bad_session_state_is_refused_before_tmux_or_a_path(self):
        class BadLabel(ScanTui):
            hooks = {"a b": HookKind.TURN_END}

        for run, launched, fields, bad in (
            (ScanTui, {"transcript": "rel.jsonl"}, {}, "transcript 'rel.jsonl'\\Z"),
            (ScanTui, {"cwd": "rel"}, {}, "cwd 'rel'\\Z"),
            (BadLabel, {}, {}, "words "),
            (ScanTui, {}, {"runtime": None}, "runtime None"),
        ):
            with (
                self.subTest(bad),
                self.assertRaisesRegex(DriverError, f"\\Atui session w1: bad {bad}"),
            ):
                self.adapter.run = run(self.adapter)
                self.adapter.run.launched = launched
                self.start(**fields)
        self.assertEqual(
            [c for c in self.host.mock_calls if c[0] not in ("events_file", "tmux_path")], []
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
        target = os.path.join(self.tmp, "target")
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
        self.host.start.assert_not_called()

    def test_an_old_session_dir_is_replaced(self):
        os.makedirs(os.path.join(self.dir, "sub"))
        for name in ("hooks.jsonl", "session.json", "other"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("old")
        self.start()
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        self.assertEqual(tui.TuiMode.snapshot(NAME), self.state())

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
        with tui._Store(NAME)._locked():
            self.assertTrue(self.locked())
            with tui._Store("w2")._locked():
                self.assertTrue(self.locked("w2"))
            self.assertFalse(self.locked("w2"))
        self.assertFalse(self.locked())
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "w1.lock")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)
        self.assertEqual(self.host.mock_calls, [])

    def test_a_second_holder_waits_for_the_first(self):
        entered = threading.Event()

        def second():
            with tui._Store(NAME)._locked():
                entered.set()

        with tui._Store(NAME)._locked():
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
                    tui._Store(NAME)._locked(),
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
            sorted(found), ["close", "holder", "hook_argv", "read", "records", "start", "stop"]
        )
        for name, parameters in found.items():
            with self.subTest(name=name):
                self.assertEqual([p for p in parameters if "lock" in p], [])

    def test_no_module_function_reaches_a_sessions_files_by_name(self):
        for gone in ("close", "lock", "read_state", "session_dir", "hook_argv"):
            with self.subTest(gone=gone):
                self.assertFalse(hasattr(tui, gone))

    def test_records_are_whole_lines_from_an_offset_and_the_offset_past_them(self):
        store = tui._Store(NAME)
        self.assertEqual(store.records(0), ([], 0))
        os.makedirs(self.dir)
        first = (json.dumps(record("tool", tool="t")) + "\n").encode()
        self.append(first, b"bad\n", record("stop"), b'{"label": "ask"')
        found, end = store.records(0)
        self.assertEqual(found, [("tool", {"tool": "t"}), ("stop", {})])
        self.assertEqual(store.records(len(first)), ([("stop", {})], end))
        self.assertEqual(store.records(end), ([], end))
