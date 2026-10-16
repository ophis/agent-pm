"""HostedSession on a private tmux server (session/tests/live_tmux.py), os.environ that server's env,
the fake adapter's Tui: Session.attach(name).close() and Session.retire(name) leave the same state
(FR-4); list and retire refused until a class is configured (FR-2: the session reads no config
itself); a display given to start stored, read back and listed (FR-48).
"""

import json
import os
import subprocess
import unittest

from agent_pm.session import Access, Host, Mode, Session, Spec
from agent_pm.session.engine.hosted_session import UNCONFIGURED, HostedSession
from agent_pm.session.runtimes import tui

from session.tests import fake_adapter
from session.tests.case import HOSTS, PY
from session.tests.modules import adapter_loader
from session.tests.tui_support import MANAGER, Live

DISPLAYS = {"w4": f"{MANAGER}/right", "w5": "elsewhere:1.2"}
UNCONFIGURED_CALLS = """import json
from agent_pm.session import ConfigError, Session
from agent_pm.session.engine.hosted_session import HostedSession
found = []
for call in (
    lambda: HostedSession.list("w.*"), lambda: Session.list("w.*"), lambda: Session.retire("w3")
):
    try:
        call()
        found.append(None)
    except ConfigError as error:
        found.append(str(error))
print(json.dumps(found))
"""


class CloseTest(Live):
    def configure_hosted(self) -> None:
        self.addCleanup(setattr, HostedSession, "_setup", None)
        HostedSession.configure(
            adapters=self.adapters,
            hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
            class_loader=adapter_loader({"agent_pm.session.adapters.fake": fake_adapter}),
        )

    def test_attach_then_close_and_retire_leave_the_same_state_fr_4(self):
        self.configure_hosted()
        found = {}
        for name, close_it in (
            ("w1", lambda: self.session_cls.attach("w1").close()),
            ("w2", lambda: Session.retire("w2")),
        ):
            handle = self.start(name, {"read": True})
            close_it()
            found[name] = (
                handle.alive(),
                self.session_cls.list(name),
                os.path.lexists(tui._Store(name, host=self.session_cls.host()).dir),
            )
        self.assertEqual(found, {"w1": (False, [], False), "w2": (False, [], False)})
        self.assertEqual([row.name for row in self.session_cls.list()], [MANAGER])

    def test_list_and_retire_need_a_configured_class(self):
        """Unconfigured in a child process: another test may have configured HostedSession here."""
        self.start("w3", {"read": True})
        res = subprocess.run(
            [*PY, "-c", UNCONFIGURED_CALLS],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [UNCONFIGURED] * 3)
        self.assertEqual([row.name for row in self.session_cls.list("w.*")], ["w3"])
        self.configure_hosted()
        self.assertEqual([row.name for row in HostedSession.list("w.*")], ["w3"])
        self.assertEqual(Session.list("w.*"), HostedSession.list("w.*"))
        Session.retire("w3")
        self.assertEqual(HostedSession.list("w.*"), [])
        self.assertFalse(os.path.lexists(tui._Store("w3", host=self.session_cls.host()).dir))


class DisplayTest(Live):
    def test_a_display_given_to_start_is_stored_read_back_and_listed_fr_48(self):
        configured = self.session_cls
        with open(self.adapters["fake"]["scenario"], "w") as f:
            json.dump([{"read": True}], f)
        for name, display in DISPLAYS.items():
            spec = Spec(
                workdir=self.workdir,
                access=Access(cwd=self.workdir),
                name=name,
                client="fake",
                mode=Mode.TUI,
                prompt="hello",
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
