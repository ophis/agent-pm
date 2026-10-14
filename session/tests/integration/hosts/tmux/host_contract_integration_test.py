"""HostContract (FR-5) against TmuxHost on a private tmux server (session/tests/live_tmux.py),
os.environ that server's env; the terminal cases wait for TmuxHost.terminal (TASK-368), so their
detach halves run here. Also: a forged @record, a display completed once and stored.
"""

import os
import signal
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.session import SessionError
from agent_pm.session.hosts.tmux import TmuxHost

from session.tests import live_tmux
from session.tests.case import Case
from session.tests.contracts.host import DEADLINE, EXIT_7, SLEEP, HostContract, gone, read

MANAGER = "mgr"


class TmuxHostContractTest(HostContract, Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        colored = self.server.tmux("set-environment", "-g", "COLORTERM", "truecolor")
        self.assertEqual(colored.returncode, 0, colored.stderr)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        super().setUp()

    def creator_env(self):
        return {**super().creator_env(), "TMUX_TMPDIR": self.server.root}

    def make_host(self):
        return TmuxHost("tmux")

    def poll(self, check, what: str):
        def poked():
            # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died,
            # until another child of the server exits: a no-op job is one
            self.server.tmux("run-shell", "-b", "true")
            return check()

        return super().poll(poked, what)

    def assert_terminal_identity(self, env):
        terminal = self.server.tmux("show-options", "-gv", "default-terminal")
        self.assertEqual(terminal.returncode, 0, terminal.stderr)
        self.assertEqual(env["TERM"], terminal.stdout.strip())
        self.assertEqual(env["TMUX"].split(",")[0], self.server.socket)
        self.assertRegex(env["TMUX_PANE"], r"\A%[0-9]+\Z")

    def test_list_without_a_display_names_every_detached_session_sorted(self):
        first, second = self.fresh(), self.fresh()
        self.start("detach", second, SLEEP)
        self.start("detach", first, SLEEP, display="elsewhere:1.2")
        self.assertEqual(self.host.list(), sorted([MANAGER, first, second]))

    def test_the_exited_hook_then_kills_the_session_it_kept(self):
        name, events = self.fresh(), self.file("events")
        self.start("detach", name, EXIT_7, events=events)
        self.event(events, name, "exited 7")
        self.poll(lambda: name not in self.host.list(), f"{name} gone")

    def test_killing_the_pane_stops_its_program_and_keeps_ownership_of_leftover_children(self):
        name, ready, stopping, finish, finished = (
            self.fresh(),
            self.file("ready"),
            self.file("stopping"),
            self.file("finish"),
            self.file("finished"),
        )
        script = """import os, signal, sys, time
ready, stopping, finish, finished = sys.argv[1:]
signal.signal(signal.SIGHUP, signal.SIG_IGN)
def stop(signum, frame):
    with open(stopping, 'a') as stream: stream.write('term\\n')
    while not os.path.exists(finish): time.sleep(0.01)
    with open(finished, 'w') as stream: stream.write('cleanup complete')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(ready + '.child', 'w'): pass
    while True: time.sleep(1)
while not os.path.exists(ready + '.child'): time.sleep(0.01)
with open(ready, 'w') as stream: stream.write(f'{os.getpid()} {child}')
while True: time.sleep(1)
"""
        process = self.start("detach", name, script, ready, stopping, finish, finished)
        self.addCleanup(process.close)
        own, child = map(int, self.poll(lambda: read(ready), "program and child ready").split())
        control = self.server.option(name, "@control")
        self.assertTrue(os.path.isdir(control))
        pane = self.server.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_pid}")
        self.assertEqual(pane.returncode, 0, pane.stderr)
        pane_pid = int(pane.stdout.strip())
        self.assertNotIn(pane_pid, (own, child))
        os.kill(pane_pid, signal.SIGKILL)
        self.poll(lambda: name not in self.host.list(), "killed pane session gone")
        self.poll(lambda: read(stopping), "program SIGTERM cleanup")
        self.assertFalse(process.wait_exit(0.2))
        self.assertFalse(gone(own))
        self.assertIsNone(read(finished))
        Path(finish).touch()
        self.poll(lambda: read(finished) == "cleanup complete", "program cleanup complete")
        self.poll(lambda: gone(own), "program exit")
        self.assertEqual(read(stopping), "term\n")
        self.assertFalse(gone(child))
        self.assertFalse(process.wait_exit(0.2))
        self.assertTrue(os.path.isdir(control))
        process.close()
        self.poll(lambda: gone(child), "leftover child exit")
        self.poll(lambda: not os.path.exists(control), "controller generation retired")
        self.assertTrue(process.wait_exit(0))

    def test_a_group_that_outlives_its_session_is_reclaimed_by_its_token_and_killed(self):
        name, ready = self.fresh(), self.file("ready")
        script = """import os, signal, sys, time
for each in (signal.SIGHUP, signal.SIGTERM):
    signal.signal(each, signal.SIG_IGN)
signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))
child = os.fork()
if child == 0:
    while True: time.sleep(1)
with open(sys.argv[1], 'w') as stream: stream.write(f'{os.getpid()} {child}')
while True: time.sleep(1)
"""
        terminal = self.start("terminal", name, script, ready)
        token = terminal.token
        own, child = map(int, self.poll(lambda: read(ready), "program and child ready").split())
        killed = self.server.tmux("kill-session", "-t", f"={name}")
        self.assertEqual(killed.returncode, 0, killed.stderr)
        self.poll(lambda: name not in self.host.list(), f"{name} gone")
        with self.assertRaises(SessionError):
            self.host.attach(name)
        handle = TmuxHost("tmux").reclaim(token)
        self.assertIsNotNone(handle)
        self.assertTrue(handle.alive())
        self.assertFalse(handle.wait_exit(0.3))
        self.assertFalse(gone(own) or gone(child))
        handle.kill()
        self.assertTrue(handle.wait_exit(DEADLINE))
        self.poll(lambda: gone(own) and gone(child), "program and child exit")
        self.assertIsNone(TmuxHost("tmux").reclaim(token))

    def test_reclaim_refuses_a_token_that_names_no_controller_of_ours(self):
        for token in ("", "junk", self.home, "/tmp", "/tmp/apm-group-none", None, 5):
            with self.subTest(token=token):
                self.assertIsNone(self.host.reclaim(token))

    def test_close_twice_of_a_detached_session_is_fine_and_forgets_it(self):
        name = self.fresh()
        process = self.start("detach", name, SLEEP)
        process.close()
        process.close()
        self.assertFalse(process.alive())
        self.assertNotIn(name, self.host.list())
        with self.assertRaises(SessionError):
            self.host.attach(name)

    def test_a_forged_record_is_none(self):
        name = self.fresh()
        self.start("detach", name, SLEEP, record=self.a_record(name))
        for forged in ('{"v":1}', "{not json"):
            with self.subTest(forged=forged):
                self.server.tmux("set-option", "-t", f"={name}:", "@record", forged)
                self.assertIsNone(self.host.attach(name).record())

    def test_a_display_completed_once_is_stored_with_its_opener_and_listed(self):
        with mock.patch.dict(os.environ, self.server.inside(MANAGER)):
            display = self.host.display("/right")
        self.assertEqual(display, f"{MANAGER}/right")
        name = self.fresh()
        self.start("detach", name, SLEEP, display=display)
        self.assertEqual(self.host.attach(name).display(), display)
        self.assertEqual(self.server.option(name, "@opener"), MANAGER)
        self.assertEqual(self.host.list(display), [name])


if __name__ == "__main__":
    unittest.main()
