"""HostContract (FR-5) against TmuxHost on a private tmux server (session/tests/live_tmux.py),
os.environ that server's env; the terminal cases wait for TmuxHost.terminal (TASK-368), so their
detach halves run here. Also: a forged @record, a display completed once and stored.
"""

import os
import unittest
from unittest import mock

from agent_pm.session import DriverError
from agent_pm.session.hosts.tmux import TmuxHost

from session.tests import live_tmux
from session.tests.case import Case
from session.tests.contracts.host import EXIT_7, SLEEP, HostContract

MANAGER = "mgr"
TERMINAL = "TmuxHost.terminal comes with TASK-368"


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

    def make_host(self):
        return TmuxHost("tmux")

    def terminal_env(self, name):
        terminal = self.server.tmux("show-options", "-gv", "default-terminal")
        version = self.server.tmux("display-message", "-p", "#{version}")
        identity = self.server.tmux(
            "display-message", "-p", "-t", f"={name}:", "#{pid} #{session_id} #{pane_id}"
        )
        self.assertEqual((terminal.returncode, version.returncode, identity.returncode), (0, 0, 0))
        pid, session, pane = identity.stdout.strip().split()
        return {
            "TMUX": f"{self.server.socket},{pid},{session.removeprefix('$')}",
            "TMUX_PANE": pane,
            "TERM": terminal.stdout.strip(),
            "TERM_PROGRAM": "tmux",
            "TERM_PROGRAM_VERSION": version.stdout.strip(),
            "COLORTERM": "truecolor",
        }

    def start(self, kind, name, code, *args, **kw):
        if kind == "terminal":
            self.skipTest(TERMINAL)
        return super().start(kind, name, code, *args, **kw)

    def poll(self, check, what: str):
        def poked():
            # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died,
            # until another child of the server exits: a no-op job is one
            self.server.tmux("run-shell", "-b", "true")
            return check()

        return super().poll(poked, what)

    def test_a_terminal_started_by_another_process_is_alive_here_and_takes_a_paste(self):
        self.skipTest(TERMINAL)

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

    def test_close_twice_of_a_detached_session_is_fine_and_forgets_it(self):
        name = self.fresh()
        process = self.start("detach", name, SLEEP)
        process.close()
        process.close()
        self.assertFalse(process.alive())
        self.assertNotIn(name, self.host.list())
        with self.assertRaises(DriverError):
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
