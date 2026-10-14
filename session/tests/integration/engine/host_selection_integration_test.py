"""Concurrent starts on different Hosts reserve one session name and retain its generation."""

import contextlib
import os
import threading

from agent_pm.session import DriverError, Host, Mode, Spec
from agent_pm.session.modes.tui import TuiMode

from session.tests.fakes.terminal import MemoryHost
from session.tests.hosted_support import SID, WAIT, HostedTuiBase
from session.tests.threads import capture_errors


class HostSelection(HostedTuiBase):
    def test_simultaneous_hosts_cannot_overwrite_the_first_sessions_state(self):
        second = MemoryHost("second")
        hosts = {"tmux": self.host, "second": second}
        self.session.configure(
            adapters=self.session._config().registry.settings.adapters,
            hosts=Host.parse_hosts(
                {
                    "default": "tmux",
                    **{
                        name: {"implementation": "session.tests.fakes.terminal:MemoryHost"}
                        for name in hosts
                    },
                }
            ),
            class_loader=self.driver_dependencies["class_loader"],
            host_factory=lambda _kind, name, _settings: hosts[name],
        )
        entered, checked, release = (threading.Event() for _ in range(3))
        sessions, refusals, errors = {}, {}, []
        lookups = []

        def launch(*_args, **_kwargs):
            entered.set()
            if not release.wait(WAIT):
                raise AssertionError("first launch was not released")

        def listing(_display):
            if threading.current_thread() is later and not lookups:
                lookups.append("second checked first host before its terminal existed")
                checked.set()
                return []
            return None

        def start(host):
            try:
                sessions[host] = self.session.start(
                    Spec(
                        workdir=os.path.join(self.dir, host),
                        runtime="fake",
                        mode=Mode.TUI,
                        sid=SID,
                        name="shared",
                    ),
                    host=host,
                )
            except DriverError as error:
                refusals[host] = str(error)

        first = threading.Thread(target=start, args=("tmux",), daemon=True)
        later = threading.Thread(target=start, args=("second",), daemon=True)
        self.host.calls.terminal.side_effect = launch
        self.host.calls.list.side_effect = listing
        with contextlib.ExitStack() as stack:
            for thread in (first, later):
                stack.enter_context(capture_errors(thread, errors.append))
            try:
                first.start()
                self.assertTrue(entered.wait(WAIT), "first terminal launch did not begin")
                original = TuiMode.snapshot("shared")
                later.start()
                self.assertTrue(checked.wait(WAIT), "second start did not check the first Host")
            finally:
                release.set()
                first.join(WAIT)
                if later.ident is not None:
                    later.join(WAIT)
                for session in sessions.values():
                    self.addCleanup(session.stop)
        self.assertFalse(first.is_alive())
        self.assertFalse(later.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(set(sessions), {"tmux"})
        self.assertEqual(refusals, {"second": "session shared exists"})
        second.calls.terminal.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "second")))
        self.assertEqual(TuiMode.snapshot("shared"), original)
        self.assertEqual(self.session.attach("shared").record(), sessions["tmux"].record())
