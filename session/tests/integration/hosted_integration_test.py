"""HostedSession on a private tmux server (session/tests/live_tmux.py), os.environ that server's env,
the fake adapter's Tui: Session.attach(name).close() and close(name) leave the same state (FR-4);
list and close with no class configured; a display given to start stored, read back and listed
(FR-48).
"""

import json
import os
import unittest

from agent_pm.session import Mode, Spec, close, sessions
from agent_pm.session.engine.hosted import HostedSession
from agent_pm.session.modes import tui

from session.tests.tui_support import MANAGER, Live

DISPLAYS = {"w4": f"{MANAGER}/right", "w5": "elsewhere:1.2"}


class CloseTest(Live):
    def test_attach_then_close_and_close_leave_the_same_state_fr_4(self):
        found = {}
        for name, close_it in (
            ("w1", lambda: self.driver.session_cls.attach("w1").close()),
            ("w2", lambda: close("w2")),
        ):
            handle = self.start(name, {"read": True})
            close_it()
            found[name] = (
                handle.state(),
                self.driver.sessions(name),
                os.path.lexists(tui._Store(name).dir),
            )
        self.assertEqual(found, {"w1": ("absent", [], False), "w2": ("absent", [], False)})
        self.assertEqual([row.name for row in self.driver.sessions()], [MANAGER])

    def test_list_and_close_need_no_configured_class(self):
        self.start("w3", {"read": True})
        self.assertIsNone(HostedSession._setup)
        self.assertEqual([row.name for row in HostedSession.list("w.*")], ["w3"])
        self.assertEqual(sessions("w.*"), HostedSession.list("w.*"))
        close("w3")
        self.assertEqual(HostedSession.list("w.*"), [])
        self.assertFalse(os.path.lexists(tui._Store("w3").dir))


class DisplayTest(Live):
    def test_a_display_given_to_start_is_stored_read_back_and_listed_fr_48(self):
        configured = self.driver.session_cls
        with open(self.config["adapters"]["fake"]["scenario"], "w") as f:
            json.dump([{"read": True}], f)
        for name, display in DISPLAYS.items():
            spec = Spec(
                workdir=self.workdir, name=name, runtime="fake", mode=Mode.TUI, prompt="hello"
            )
            self.addCleanup(configured.start(spec, display=display).stop)
        host = configured.host()
        for name, display in DISPLAYS.items():
            with self.subTest(name=name):
                self.assertEqual(self.server.option(name, "@display"), display)
                self.assertEqual(host.attach(name).display(), display)
                self.assertEqual(host.list(display), [name])
                self.assertEqual([row.name for row in configured.list(display=display)], [name])


if __name__ == "__main__":
    unittest.main()
