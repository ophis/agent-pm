"""TmuxHost, the Host on tmux: a thin wrap of tmux_host.py (TASK-368 moves that code in). Its display
format, FORMAT, has one encoder (fold) and one decoder (unfold). TmuxProcess, a session it hosts,
keeps its Record and display in the tmux options @record and @display.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Mapping, Sequence

from agent_pm.session.engine.types import ConfigError, DriverError
from agent_pm.session.hosts.base import SIGNALS, Displayable, Host, HostProcess
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.tmux_host import TuiError
from agent_pm.utils import Display, ProcessControl, Record

FORMAT = "<opener>[/<split>[/<split_from>]]"
NO_PANE_HINT = ": run from tmux or iTerm2, or pass split_from"
POLL = 0.1
KILL_WAIT = 5
LOG = logging.getLogger("session")

Parts = tuple[str | None, str | None, str | None]


def _session(name) -> str:
    if not isinstance(name, str) or not tmux_host.NAME.fullmatch(name):
        raise DriverError(f"no tmux session {name!r:.80}")
    return name


def _checked(display) -> None:
    if why := tmux_host.display_error(display):
        raise ConfigError(why)


class TmuxProcess(HostProcess):
    """The captured pane and its generation's controller. A pane can set its Record and display
    options: consumers must re-validate a Record's ctl or resume before running it.
    """

    def __init__(
        self, name: str, process: str, *, tmux=tmux_host, control: ProcessControl | None = None
    ) -> None:
        self.name = name
        self.tmux = tmux
        self._process = process
        _, _, self._pane, _ = process.split(" ")
        path = self._option("@control") if control is None else None
        if control is None and self._option("@kind") == "detach" and not path:
            raise DriverError(
                f"tmux session {name}: missing @control; close sessions before upgrading"
            )
        try:
            if control is not None:
                self._control = control
            else:
                self._control = ProcessControl.attach(path) if path else None
        except OSError as error:
            raise DriverError(f"tmux session {name}: invalid @control: {error}") from error

    def alive(self) -> bool:
        if not self._own():
            return False
        state = self.tmux.status(self._pane)
        if state == self.tmux.PaneStatus.UNKNOWN:
            raise DriverError(f"tmux session {self.name}: tmux failed")
        return state == self.tmux.PaneStatus.RUNNING and (
            self._control is None or self._control.running()
        )

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive() or self._group_live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        self._kill_group()
        if not self.wait_exit(KILL_WAIT):
            raise DriverError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        if self._control is None:
            self._close_pane()
            return
        try:
            self._kill_group()
        except DriverError as error:
            if not isinstance(error.__cause__, ConnectionRefusedError | ConnectionResetError):
                raise
            self._close_pane()
            return
        deadline = time.monotonic() + KILL_WAIT
        while self._group_live():
            if time.monotonic() >= deadline:
                raise DriverError(f"process group {self.name}: did not exit")
            time.sleep(POLL)
        self._close_pane()

    def _close_pane(self) -> None:
        if not self._own():
            return
        try:
            self.tmux.kill(self._pane)
        except TuiError as e:
            if self._own():
                raise DriverError(f"close {self.name}: {e}") from None

    def record(self) -> Record | None:
        line = self._option("@record")
        if line is not None:
            with contextlib.suppress(ValueError):
                return Record.parse(line)
        return None

    def display(self) -> Display:
        return self._option("@display")

    def _own(self) -> bool:
        try:
            return self.tmux.process(self._pane) == self._process
        except TuiError as e:
            raise DriverError(f"tmux session {self.name}: {e}") from None

    def _group_live(self) -> bool:
        try:
            return self._control is not None and self._control.live()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error

    def _kill_group(self) -> None:
        if self._control is None:
            self._close_pane()
            return
        try:
            self._control.kill()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error

    def _option(self, key: str) -> str | None:
        if not self._own():
            return None
        try:
            return self.tmux.option(self._pane, key)
        except TuiError as e:
            raise DriverError(f"tmux session {self.name}: {e}") from None


class TmuxHost(Host, Displayable):
    """settings: [hosts.<name>]'s workers_per_column and grid_retile shape show's grid. A display
    not in FORMAT is kept as given.
    """

    def __init__(self, name: str, settings: Mapping | None = None, *, tmux=tmux_host) -> None:
        super().__init__(name, settings)
        self.tmux = tmux

    @classmethod
    def fold(cls, opener, split, split_from) -> Display:
        """FORMAT's one encoder; None when every part is. ConfigError for a bad part."""
        if opener is not None and not (
            isinstance(opener, str) and tmux_host.OPENER.fullmatch(opener)
        ):
            raise ConfigError(
                f"opener must be a tmux session name or $ITERM_SESSION_ID's value, got {opener!r:.80}"
            )
        if split is not None and split not in tuple(tmux_host.Split):
            raise ConfigError(f"split must be {' or '.join(tmux_host.Split)}, got {split!r:.80}")
        if split_from is not None and not (
            isinstance(split_from, str) and tmux_host.NAME.fullmatch(split_from)
        ):
            raise ConfigError(f"split_from must match [A-Za-z0-9_-]+, got {split_from!r:.80}")
        return "/".join((opener or "", split or "", split_from or "")).rstrip("/") or None

    @classmethod
    def unfold(cls, display) -> Parts | None:
        """FORMAT's one decoder, re-validating a display read back: (opener, split, split_from), an
        empty part None; None when display is not in FORMAT.
        """
        if display is None:
            return None, None, None
        if tmux_host.display_error(display):
            return None
        given = display.split("/")
        if len(given) > 3:
            return None
        opener, split, split_from = (part or None for part in (*given, "", "")[:3])
        try:
            cls.fold(opener, split, split_from)
        except ConfigError:
            return None
        return opener, split, split_from

    def detach(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> TmuxProcess:
        _checked(display)
        if events is not None:
            try:
                events = self.tmux.events_file(events)
            except TuiError as e:
                raise ConfigError(str(e)) from None
        parts = self.unfold(display)
        try:
            control = ProcessControl.prepare(argv, cwd)
            process = self.tmux.detach(
                name,
                control.command(),
                cwd=cwd,
                env=dict(env),
                block=SIGNALS,
                opener=None if parts is None else parts[0],
                display=display,
                events=events,
                record=None if record is None else record.to_line(),
                control=control.path,
            )
        except TuiError as e:
            raise DriverError(f"detach {name}: {e}") from e
        try:
            control.wait_started()
        except OSError as error:
            why = os.strerror(error.errno) if error.errno is not None else str(error)
            raise DriverError(
                f"detach {name}: cannot run {error.filename or argv[0]}: {why}"
            ) from None
        return TmuxProcess(name, process, tmux=self.tmux, control=control)

    def terminal(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> TmuxProcess:
        raise NotImplementedError("TmuxHost.terminal comes with TASK-368")

    def attach(self, name: str) -> TmuxProcess:
        state = self.tmux.status(_session(name))
        if state is None:
            raise DriverError(f"no tmux session {name}")
        if state == self.tmux.PaneStatus.UNKNOWN:
            raise DriverError(f"tmux session {name}: tmux failed")
        try:
            process = self.tmux.process(name)
        except TuiError as e:
            raise DriverError(f"tmux session {name}: {e}") from None
        if process is None:
            raise DriverError(f"no tmux session {name}")
        return TmuxProcess(name, process, tmux=self.tmux)

    def list(self, display: Display = None) -> list[str]:
        if display == "":
            return []
        found = self.tmux.listing()
        if found is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        return [row["name"] for row in found if display is None or row["display"] == display]

    def display(self, value: Display) -> Display:
        _checked(value)
        parts = self.unfold(value)
        if parts is None or parts[0] is not None:
            return value
        try:
            opener = self.tmux.opener()
        except TuiError:
            return value
        return self.fold(opener, parts[1], parts[2])

    def display_help(self) -> str:
        return (
            f"{FORMAT}: opener a tmux session name or $ITERM_SESSION_ID's value (empty: the "
            f"caller's), split {' or '.join(tmux_host.Split)}, split_from the tmux session whose "
            "pane it goes beside; any other printable value not ending in ; is kept as given"
        )

    def show(self, name: str, *, display: Display = None) -> str | None:
        opener, split, split_from = self._parts(display)
        if self.tmux.status(_session(name)) is None:
            raise DriverError(f"no tmux session {name}")
        try:
            why = self.tmux.open_pane(
                name,
                split=split,
                split_from=split_from,
                opener=opener,
                per_column=self.settings.get("workers_per_column"),
                retile=self.settings.get("grid_retile"),
            )
        except TuiError as e:
            raise ConfigError(str(e)) from None
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return self.tmux.attach_command(name)

    def attach_command(self, name: str) -> str:
        return self.tmux.attach_command(_session(name))

    def screen(self, name: str, lines: int | None = None) -> str:
        try:
            return self.tmux.read(_session(name), lines)
        except TuiError as e:
            raise DriverError(f"tmux session {name}: {e}") from None

    def layout(self, *, display: Display = None) -> str | None:
        """display, its opener part the caller's once the pane it goes beside (split_from's, else
        the caller's) is found; that part stays empty only beside split_from.
        """
        opener, split, split_from = self._parts(display)
        if split_from is not None and self.tmux.status(split_from) is None:
            raise ConfigError(f"no tmux session {split_from}")
        try:
            if opener is None or split_from is not None:
                self.tmux.anchor(split_from)
            if opener is None:
                try:
                    opener = self.tmux.opener()
                except TuiError:
                    if split_from is None:
                        raise
        except TuiError as e:
            raise DriverError(
                f"no pane to show the TUI beside ({e})" + ("" if split_from else NO_PANE_HINT)
            ) from None
        return self.fold(opener, split, split_from)

    def tile(self, window: str) -> None:
        try:
            self.tmux.tile(window)
        except TuiError as e:
            raise DriverError(f"tile {window}: {e}") from None

    def _parts(self, display) -> Parts:
        parts = self.unfold(display)
        if parts is None:
            raise ConfigError(f"display {display!r:.80}: not {FORMAT}")
        return parts
