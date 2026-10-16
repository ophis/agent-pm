"""session/src/agent_pm/session/hosts/pty/host.py: PtyHost's settings and what it is not; starting a
session against a fake supervisor process.
"""

import io
import itertools
import json
import os
import signal
import unittest
from unittest import mock

from agent_pm.session import ConfigError, Displayable, Launch, SessionError
from agent_pm.session.hosts.pty import PtyHost, state
from agent_pm.session.hosts.pty.host import SUPERVISOR
from agent_pm.utils import inode_lock

from session.tests.case import Case


class Settings(Case):
    def test_outer_terminal_is_its_only_key_a_list_of_variable_names(self):
        self.assertEqual(
            PtyHost("pty", {"outer_terminal": ["A", "B"]}).outer_terminal, frozenset({"A", "B"})
        )
        self.assertEqual(PtyHost("pty").outer_terminal, frozenset())
        for settings in ({"other": 1}, {"outer_terminal": "A"}, {"outer_terminal": ["A", ""]}):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                PtyHost("pty", settings)

    def test_it_cannot_display_and_keeps_a_display_as_given(self):
        host = PtyHost("pty")
        self.assertNotIsInstance(host, Displayable)
        self.assertEqual((host.display("x:1.2"), host.display_help()), ("x:1.2", ""))


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


class Start(Case):
    """detach's start against a fake supervisor process (subprocess.Popen) that writes its pid and
    a runner that reports through started.json.
    """

    def start(self, *, pid=True, poll=None, started="{}", events=None):
        def popen(argv, **options):
            folder = argv[5]
            if pid:
                state.put(folder, state.PID, "4242")
            else:
                os.write(options["stderr"].fileno(), b"booting\nno pty\n")
            if started is not None:
                control = json.loads(state.read(folder, state.META))["control"]
                with open(os.path.join(control, "started.json"), "w", encoding="utf-8") as file:
                    file.write(started)
            return mock.Mock(pid=4242, poll=mock.Mock(return_value=poll))

        with (
            mock.patch("subprocess.Popen", side_effect=popen) as spawned,
            mock.patch("socket.socket", _Connection),
            mock.patch("time.monotonic", side_effect=itertools.count(step=100)),
            mock.patch("os.killpg") as killed,
        ):
            try:
                result = PtyHost("pty").detach("x", Launch(("prog",), cwd=self.home), events=events)
            except SessionError as error:
                result = error
        return result, spawned, killed

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

    def lock_held(self) -> bool:
        with inode_lock(os.path.join(state.root(), "x.lock"), wait=False) as got:
            return not got

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
        state.put(folder, state.RC, "0")
        with open(os.path.join(control, "done.json"), "w", encoding="utf-8") as file:
            file.write("{}")
        process, _, _ = self.start()
        self.assertNotEqual(process.identity, "old")


if __name__ == "__main__":
    unittest.main()
