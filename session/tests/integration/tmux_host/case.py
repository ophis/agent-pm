"""Fixtures for tmux host tests."""

import contextlib
import functools
import io
import os
import re
import subprocess
import sys
import tempfile
import time
from unittest import mock

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

from session.tests import live_tmux
from session.tests.case import Case
from session.tests.processes import reject_command

MANAGER = "mgr"

TIMEOUT = 60

TUI = os.path.abspath(tmux_host.__file__)

IGNORED = "tui: show: grid: --split/--split-from ignored"

STEADY = 1  # seconds a check that nothing re-tiles holds

BREAKER_TIMEOUT = 30

RETILE_WAIT = 0.5

PACE = 0.02


class Live(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = reject_command("osascript")
        patch.start()
        self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = os.path.realpath(tmp.name)
        self.settings = {}
        self.configure()

    def configure(self, **settings) -> None:
        """The tui settings show() reads: these, over the earlier ones."""
        self.settings.update(settings)
        self.configure_session({}, self.settings)

    def per_column(self, n: int) -> None:
        self.configure(workers_per_column=n)

    def retile(self, mode: str) -> None:
        self.configure(grid_retile=mode)

    @contextlib.contextmanager
    def inside(self, session: str | None):
        """os.environ as a process's in `session`'s pane; None: outside tmux."""
        with mock.patch.dict(os.environ, self.server.inside(session) if session else {}):
            yield

    def show(self, name: str, inside: str | None = MANAGER, **kwargs) -> str:
        """show(name, **kwargs) from inside `inside`, a pane then showing it; its stderr."""
        err = io.StringIO()
        with self.inside(inside), contextlib.redirect_stderr(err):
            self.assertIsNone(self.driver.show(name, **kwargs))
        return err.getvalue()

    def started(self, name: str, inside: str | None = MANAGER, **kwargs) -> str:
        """Session `name` started with no pane, then show()n; show's stderr."""
        tmux_host.start(
            name, tmux_host.Command(argv=list(live_tmux.IDLE), cwd=self.cwd, env=self.server.env())
        )
        return self.show(name, inside, **kwargs)

    def tmux(self, *args: str) -> str:
        res = self.server.tmux(*args)
        self.assertEqual(res.returncode, 0, f"tmux {args}: {res.stderr}")
        return res.stdout.removesuffix("\n")

    def tui(self, *args: str) -> None:
        """tmux_host.py's CLI, run from inside the manager's pane."""
        res = subprocess.run(
            [sys.executable, "-I", TUI, *args],
            env=self.server.env(**self.server.inside(MANAGER)),
            cwd=self.cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def display(self, name: str, fmt: str) -> str:
        return self.tmux("display-message", "-p", "-t", f"={name}:", fmt)

    def window_option(self, name: str, key: str) -> str:
        return self.tmux("show-options", "-wqv", "-t", f"={name}:", key)

    def window_of(self, name: str) -> str:
        return self.display(name, "#{window_id}")

    def grid(self, columns: list, manager_width: int | None = None) -> None:
        check = functools.partial(
            live_tmux.assert_grid, self, self.server, MANAGER, columns, manager_width
        )
        with contextlib.suppress(
            AssertionError
        ):  # on timeout, check's own message below rather than wait's repr
            live_tmux.wait(check)
        check()

    def hooks(self, name: str) -> list[str]:
        out = self.tmux("show-hooks", "-w", "-t", self.window_of(name))
        return [h for h in tmux_model.GRID_HOOKS if re.search(rf"^{h}\b", out, re.M)]

    def open_close_hooks(self, name: str) -> None:
        self.assertEqual(self.hooks(name), list(tmux_model.RETILE_HOOKS["open-close"]))
        out = self.tmux("show-hooks", "-w", "-t", self.window_of(name))
        for h in tmux_model.RETILE_HOOKS["open-close"]:
            line = next(line for line in out.splitlines() if re.match(rf"{h}\b", line))
            self.assertIn(" tile --no-wait --if-panes-changed @", line)

    def pane_set(self, name: str) -> str:
        panes = self.tmux("list-panes", "-t", self.window_of(name), "-F", "#{pane_id}").split()
        return " ".join(sorted(panes, key=lambda p: int(p[1:])))

    def holds(self, read, value, what: str):
        end = time.monotonic() + STEADY
        while time.monotonic() < end:
            self.assertEqual(read(), value, what)
            time.sleep(live_tmux.POLL)
        return value
