"""session/src/agent_pm/session/hosts/pty/host.py: PtyHost's settings and what it is not; starting a
session against a fake supervisor process.
"""

import io
import itertools
import json
import os
import signal
import threading
import time
import unittest
from unittest import mock

from agent_pm.session import ConfigError, Launch, SessionError
from agent_pm.session.hosts.base import tui_lock
from agent_pm.session.hosts.pty import PtyHost, state
from agent_pm.session.hosts.pty.host import SUPERVISOR, PtyProcess
from agent_pm.utils import Record, StatusState, inode_lock

from session.tests.case import Case


class Settings(Case):
    def test_outer_terminal_is_its_only_key_a_list_of_variable_names(self):
        self.assertEqual(
            PtyHost("pty", {"outer_terminal": ["A", "B"]}).outer_terminal, frozenset({"A", "B"})
        )
        self.assertEqual(PtyHost("pty").outer_terminal, frozenset())
        self.assertTrue(PtyHost("pty", {"isolated": True})._isolated)
        for settings in ({"other": 1}, {"outer_terminal": "A"}, {"outer_terminal": ["A", ""]}):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                PtyHost("pty", settings)

    def test_it_cannot_show(self):
        host = PtyHost("pty")
        self.assertFalse(host.attachable)
        self.assertIsNone(host.attach_target("x"))


class _Connection:
    """A fake socket to a live process controller: every request answers live."""

    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, _):
        pass

    def connect(self, _):
        pass

    def sendall(self, _):
        pass

    def makefile(self, _):
        return io.BytesIO(b'{"live": true}\n')


DAY = 24 * 3600


def supervised(folder, **fields):
    """The supervisor's supervisor.json, `fields` added."""
    state.put(folder, state.SUPERVISOR, json.dumps({**state.supervisor(folder), **fields}))


class Starting(Case):
    """detach's start against a fake supervisor process (subprocess.Popen) that writes its pid and
    a runner that reports through its ProcessControl's state.json.
    """

    def start(self, *, pid=True, poll=None, started="{}", events=None, ended=False, record=None):
        """ended: the session's group is gone and its files aged as soon as it started."""

        def popen(argv, **options):
            folder = argv[5]
            if pid:
                supervised(folder, pid=4242)
            else:
                os.write(options["stderr"].fileno(), b"booting\nno pty\n")
            control = json.loads(state.read(folder, state.META))["control"]
            recorded = {} if started is None else {"started": json.loads(started)}
            if recorded or ended:
                with open(os.path.join(control, "state.json"), "w", encoding="utf-8") as file:
                    json.dump({**recorded, **({"done": 0} if ended else {})}, file)
            if ended:
                supervised(folder, rc=0)
                moment = time.time() - 31 * DAY
                for path in (folder, f"{folder}.lock", os.path.join(folder, state.OUTPUT)):
                    os.utime(path, (moment, moment))
            return mock.Mock(pid=4242, poll=mock.Mock(return_value=poll))

        with (
            mock.patch("subprocess.Popen", side_effect=popen) as spawned,
            mock.patch("socket.socket", _Connection),
            mock.patch("time.monotonic", side_effect=itertools.count(step=100)),
            mock.patch("os.killpg") as killed,
        ):
            try:
                result = PtyHost("pty").detach(
                    "x", Launch(("prog",), cwd=self.home), events=events, record=record
                )
            except SessionError as error:
                result = error
        return result, spawned, killed


class Start(Starting):
    def test_detach_stores_the_record_sid_as_private_identity(self):
        record = Record(
            v=1,
            kind="worker",
            name="x",
            sessions=("x",),
            sid="sid",
            workdir=None,
            ctl=("agent-pm-session",),
            resume=("agent-pm-session", "resume", "x"),
            display=None,
            detail=None,
        )
        process, _, _ = self.start(record=record)
        self.assertEqual(process.session_identity(), (record.sid, None))

    def lock_held(self) -> bool:
        with inode_lock(os.path.join(state.root(), "x.lock"), wait=False) as got:
            return not got

    def test_a_started_session_has_its_meta_and_control_link(self):
        events = os.path.join(self.home, "events.jsonl")
        process, spawned, killed = self.start(events=events)
        folder = os.path.join(state.root(), "x")
        meta = json.loads(state.read(folder, state.META))
        self.assertEqual(
            {key: meta[key] for key in ("kind", "display", "record", "metadata")},
            {"kind": "detach", "display": None, "record": None, "metadata": {}},
        )
        self.assertEqual((process.identity, process.token), (meta["id"], meta["control"]))
        self.assertEqual(
            os.readlink(os.path.join(folder, state.CONTROL)),
            os.path.join(meta["control"], state.SOCKET),
        )
        self.assertEqual(spawned.call_args.args[0][-3:], [SUPERVISOR, folder, events])
        self.assertTrue(spawned.call_args.kwargs["start_new_session"])
        self.assertTrue(os.path.exists(events))
        killed.assert_not_called()

    def test_a_failed_start_kills_a_running_supervisor_and_removes_its_dir(self):
        missing = json.dumps({"error": "No such file or directory", "errno": 2, "filename": "prog"})
        for name, options, message, kills in (
            ("exited", {"pid": False, "poll": 1}, "did not start: no pty", []),
            ("missing", {"started": missing}, "cannot run prog: No such file or directory", [4242]),
            ("timeout", {"started": None}, "process controller did not start", [4242]),
        ):
            with self.subTest(name=name):
                error, _, killed = self.start(**options)
                self.assertEqual(str(error), f"detach x: {message}")
                self.assertEqual(
                    killed.call_args_list, [mock.call(pid, signal.SIGKILL) for pid in kills]
                )
                self.assertFalse(os.path.exists(os.path.join(state.root(), "x")))

    def test_start_holds_the_names_lock_until_it_returns(self):
        held = []
        real_replace = os.replace

        def replace(*args, **kwargs):
            held.append(self.lock_held())
            return real_replace(*args, **kwargs)

        with mock.patch("os.replace", side_effect=replace):
            process, _, _ = self.start()
        self.assertIsNotNone(process.identity)
        self.assertEqual((set(held), self.lock_held()), ({True}, False))

    def test_a_lock_it_cannot_take_is_a_session_error(self):
        os.makedirs(os.path.join(state.root(), "x.lock"))
        error, spawned, _ = self.start()
        self.assertRegex(str(error), r"^detach x: .*Is a directory")
        spawned.assert_not_called()

    def test_a_live_session_of_that_name_is_refused_and_a_dead_one_replaced(self):
        folder = os.path.join(state.root(), "x")
        control = os.path.join(self.home, "old-control")
        os.makedirs(folder)
        os.makedirs(control)
        state.put(folder, state.META, json.dumps({"id": "old", "control": control}))
        error, spawned, _ = self.start()
        self.assertEqual(str(error), "pty session x is live")
        spawned.assert_not_called()
        supervised(folder, rc=0)
        with open(os.path.join(control, "state.json"), "w", encoding="utf-8") as file:
            file.write('{"done": 0}')
        process, _, _ = self.start()
        self.assertNotEqual(process.identity, "old")

    def test_its_supervisor_json_holds_the_supervisor_pid_then_the_rc(self):
        folder = os.path.join(state.root(), "x")
        os.makedirs(folder)
        state.put(folder, state.META, json.dumps({"id": "g1", "kind": "detach"}))
        supervised(folder, pid=4242)
        process = PtyHost("pty").attach("x")
        with mock.patch("os.kill") as probed:
            self.assertIsNone(process.returncode())
        probed.assert_called_once_with(4242, 0)
        supervised(folder, rc=3)
        self.assertEqual(
            (process.returncode(), process.alive(), state.supervisor(folder)),
            (3, False, {"pid": 4242, "rc": 3}),
        )
        state.put(folder, state.SUPERVISOR, "[1]")
        self.assertEqual(state.supervisor(folder), {})

    def test_a_listed_session_whose_client_exited_is_dead(self):
        folder = os.path.join(state.root(), "x")
        os.makedirs(folder)
        state.put(folder, state.META, json.dumps({"id": "g1", "kind": "detach"}))
        supervised(folder, rc=0)
        found = PtyHost("pty").attach("x").status()
        self.assertEqual((found.alive, found.state), (False, StatusState.DEAD))


class Close(Starting):
    def test_closing_the_names_session_removes_its_dir_and_lock(self):
        process, _, _ = self.start(ended=True)
        process.close()
        self.assertEqual(os.listdir(state.root()), [])

    def test_closing_a_session_whose_root_is_gone_is_a_no_op(self):
        PtyProcess("x", os.path.join(self.home, "gone", "x")).close()
        self.assertFalse(os.path.exists(os.path.join(self.home, "gone")))

    def test_closing_an_earlier_generation_keeps_the_later_ones_dir_and_lock(self):
        process, _, _ = self.start(ended=True)
        later = {**state.meta(process.folder), "id": "later"}
        state.put(process.folder, state.META, json.dumps(later))
        process.close()
        self.assertEqual(sorted(os.listdir(state.root())), ["x", "x.lock"])


class Sweep(Starting):
    """Other names' leftovers swept by a start of x."""

    def leftover(self, name, *, days=31, meta=True, group=False, pid=None, tui=True):
        """pty/<name>/ with output and activity, its lock and (tui) tui/<name>.lock, all `days` old;
        its process group live with `group`, its pid file `pid`.
        """
        folder = os.path.join(state.root(), name)
        os.makedirs(folder)
        control = os.path.join(self.home, f"{name}-control")
        os.makedirs(control)
        if not group:
            with open(os.path.join(control, "state.json"), "w", encoding="utf-8") as file:
                file.write('{"done": 0}')
        if meta:
            state.put(folder, state.META, json.dumps({"id": name, "control": control}))
            supervised(folder, rc=0)
        if pid is not None:
            supervised(folder, pid=pid)
        paths = [os.path.join(folder, state.OUTPUT), os.path.join(folder, state.ACTIVITY)]
        paths += [f"{folder}.lock", *([tui_lock(name)] if tui else [])]
        for path in paths:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "a").close()
        self.age(days, folder, *paths)

    def age(self, days, *paths):
        moment = time.time() - days * DAY
        for path in paths:
            os.utime(path, (moment, moment), follow_symlinks=False)

    def names(self):
        return sorted(os.listdir(state.root()))

    def test_an_idle_ended_name_goes_with_its_lock_leaving_tui_s_lock(self):
        self.leftover("a")
        self.leftover("b", meta=False, tui=False)
        os.makedirs(state.root(), exist_ok=True)
        open(os.path.join(state.root(), "c.lock"), "w").close()
        self.age(31, os.path.join(state.root(), "c.lock"))
        self.start()
        self.assertEqual(self.names(), ["x", "x.lock"])
        self.assertTrue(os.path.exists(tui_lock("a")))

    def test_a_name_recent_running_or_held_stays(self):
        self.leftover("young", days=29)
        self.leftover("group", group=True)
        self.leftover("supervised", pid=os.getpid())
        self.leftover("held")
        self.leftover("tuiheld")
        with (
            inode_lock(os.path.join(state.root(), "held.lock")),
            inode_lock(tui_lock("tuiheld")),
            mock.patch("os.kill") as signalled,
        ):
            self.start()
        signalled.assert_called_once_with(os.getpid(), 0)
        kept = ["group", "held", "supervised", "tuiheld", "young"]
        self.assertEqual(self.names(), sorted([*kept, *(f"{n}.lock" for n in kept), "x", "x.lock"]))

    def test_the_starting_name_is_never_swept(self):
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            process, _, _ = self.start(ended=True)
        self.assertEqual(self.names(), ["x", "x.lock"])
        self.assertTrue(os.path.exists(os.path.join(process.folder, state.META)))

    def test_a_symlinked_name_and_its_target_stay(self):
        self.leftover("a")
        target = os.path.join(self.home, "target")
        os.rename(os.path.join(state.root(), "a"), target)
        os.symlink(target, os.path.join(state.root(), "a"))
        self.age(31, os.path.join(state.root(), "a"))
        self.start()
        self.assertEqual(self.names(), ["a", "a.lock", "x", "x.lock"])
        self.assertTrue(os.path.exists(os.path.join(target, state.OUTPUT)))

    def test_a_failure_on_one_name_is_logged_and_the_rest_swept(self):
        self.leftover("a")
        self.leftover("b")
        real_unlink = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path).endswith("a.lock"):
                raise PermissionError("denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("os.unlink", side_effect=unlink), self.assertLogs("session") as logs:
            process, _, _ = self.start()
        self.assertNotIsInstance(process, SessionError)
        self.assertEqual(self.names(), ["a.lock", "x", "x.lock"])
        self.assertEqual(logs.output, ["WARNING:session:pty sweep a: denied"])

    def test_an_unreadable_root_is_logged_and_the_start_stands(self):
        real_scandir = os.scandir

        def scandir(path=None):
            if path == state.root():
                raise PermissionError("denied")
            return real_scandir(path)

        with mock.patch("os.scandir", side_effect=scandir), self.assertLogs("session") as logs:
            process, _, _ = self.start()
        self.assertNotIsInstance(process, SessionError)
        self.assertEqual(logs.output, ["WARNING:session:pty sweep: denied"])

    def test_a_start_waiting_on_a_sweep_rebuilds_the_dir_on_the_new_lock(self):
        lock, found = os.path.join(state.root(), "x.lock"), []
        os.makedirs(state.root())
        with inode_lock(lock):
            thread = threading.Thread(target=lambda: found.append(self.start()[0]), daemon=True)
            thread.start()
            thread.join(0.2)
            self.assertTrue(thread.is_alive(), "did not wait")
            os.unlink(lock)
        thread.join(5)
        self.assertEqual(
            found[0].identity, json.loads(state.read(found[0].folder, state.META))["id"]
        )
        with inode_lock(lock, wait=False) as free:
            self.assertTrue(free)


if __name__ == "__main__":
    unittest.main()
