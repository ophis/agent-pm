"""TmuxHost, the Host on tmux: a thin wrap of tmux_host.py (TASK-368 moves that code in). Its display
format, FORMAT, has one encoder (fold) and one decoder (unfold). TmuxProcess, a session it hosts,
keeps its Record and display in the tmux options @record and @display.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time
from collections.abc import Mapping

from agent_pm.session.engine.types import (
    ActivityState,
    ConfigError,
    DriverError,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NamedKey,
    Paste,
    SessionRow,
    TuiSettings,
    Word,
)
from agent_pm.session.hosts.base import Displayable, Host, HostProcess, Terminal
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.tmux_host import TuiError
from agent_pm.utils import CONFIG_NAME, Display, ProcessControl, Record, sid_error

FORMAT = "<opener>[/<split>[/<split_from>]]"
NO_PANE_HINT = ": run from tmux or iTerm2, or pass split_from"
POLL = 0.1
KILL_WAIT = 5
LOG = logging.getLogger("session")

Parts = tuple[str | None, str | None, str | None]
TMUX_KEYS = {NamedKey.PAGE_UP: "PPage", NamedKey.PAGE_DOWN: "NPage", " ": "Space", ";": "\\;"}


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
        if control is None and self._option("@kind") in ("detach", "terminal") and not path:
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

    @property
    def identity(self) -> str:
        return self._process

    @property
    def token(self) -> str | None:
        return None if self._control is None else self._control.path

    def publish(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ConfigError(f"record: want a Record, got {type(record).__name__}")
        if not self._own():
            raise DriverError(f"tmux session {self.name} is gone")
        try:
            self.tmux.set_option(self._pane, "@record", record.to_line())
        except TuiError as error:
            raise DriverError(f"tmux session {self.name}: {error}") from error

    def state(self) -> Liveness:
        try:
            own = self._own()
        except DriverError:
            return Liveness.UNKNOWN
        if not own:
            return Liveness.ABSENT
        value = self.tmux.status(self._pane)
        if value == self.tmux.PaneStatus.UNKNOWN:
            return Liveness.UNKNOWN
        return Liveness.ABSENT if value is None else Liveness.LIVE

    def returncode(self) -> int | None:
        try:
            if not self._own():
                return -signal.SIGHUP
        except DriverError:
            return None
        value = self.tmux.status(self._pane)
        if value is None:
            return -signal.SIGHUP
        return value if isinstance(value, int) else None

    def session_identity(self) -> tuple[str | None, str | None]:
        return self._option("@sid"), self._option("@generation")

    def activity(self) -> Word | None:
        value = self._option("@state")
        return Word(value) if value in tuple(Word) else None

    def mark(self, word: Word, events: str | None) -> None:
        if self._own():
            try:
                self.tmux.mark(word, events, pane=self._pane)
            except TuiError as error:
                raise DriverError(str(error)) from error

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
        try:
            self._close_pane()
        finally:
            if self._control is not None:
                try:
                    self._control.terminate()
                except (ConnectionRefusedError, ConnectionResetError):
                    pass
                except OSError as error:
                    raise DriverError(f"process group {self.name}: {error}") from error
                else:
                    if not self._wait_group(KILL_WAIT):
                        self._kill_group()
                        if not self._wait_group(KILL_WAIT):
                            raise DriverError(f"process group {self.name}: did not exit")

    def _wait_group(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._group_live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def _close_pane(self) -> None:
        if not self._own():
            return
        try:
            self.tmux.kill(self._pane)
        except TuiError as e:
            if self._own():
                raise DriverError(str(e)) from None

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


class TmuxGroup(HostProcess):
    """A reclaimed process group whose tmux session may be gone: wait for it, kill it, close it."""

    def __init__(self, control: ProcessControl) -> None:
        self.name = os.path.basename(control.path)
        self._control = control

    @property
    def token(self) -> str | None:
        return self._control.path

    def alive(self) -> bool:
        try:
            return self._control.live()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        try:
            self._control.kill()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(KILL_WAIT):
            raise DriverError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        try:
            self._control.terminate()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(KILL_WAIT):
            self.kill()

    def record(self) -> Record | None:
        return None

    def display(self) -> Display:
        return None


class TmuxTerminal(TmuxProcess, Terminal):
    _pasted = False

    def send(self, action: Paste | Key) -> None:
        if not self.alive():
            raise DriverError(f"tui session {self.name} is not running")
        try:
            if isinstance(action, Paste):
                self.tmux.paste(self._pane, action.text)
                self._pasted = True
            else:
                if self._pasted:
                    time.sleep(self.tmux.PAUSE)
                self.tmux.keys(self._pane, action.name)
                self._pasted = False
        except TuiError as error:
            raise DriverError(f"send to tui session {self.name}: {error}") from error

    def press(self, keys) -> None:
        names = [_tmux_key(key) for key in keys]
        if not self.alive():
            raise DriverError(f"tui session {self.name} is not running")
        try:
            if self._pasted:
                time.sleep(self.tmux.PAUSE)
            self.tmux.press(self._pane, *names)
            self._pasted = False
        except TuiError as error:
            raise DriverError(f"send to tui session {self.name}: {error}") from error


def _tmux_key(key) -> str:
    """KeyPress → tmux's key name: modifiers as C-, M-, S-; PageUp, PageDown, space and ; renamed."""
    if not isinstance(key, KeyPress):
        raise ConfigError(f"key {key!r:.80}: want a KeyPress")
    prefixes = ("C-" * key.ctrl, "M-" * key.meta, "S-" * key.shift)
    return "".join(prefixes) + TMUX_KEYS.get(key.key, key.key)


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
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> TmuxProcess:
        return self._launch(
            name, launch, display=display, events=events, record=record, terminal=False
        )

    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> TmuxTerminal:
        process = self._launch(
            name,
            launch,
            display=display,
            events=events,
            record=record,
            terminal=True,
            metadata=metadata,
        )
        return TmuxTerminal(name, process.identity, tmux=self.tmux, control=process._control)

    def _launch(self, name, launch, *, display, events, record, terminal, metadata=None):
        self.validate(name, launch, display=display)
        events = self.events_path(events)
        events = self.prepare_events(events)
        parts = self.unfold(display)
        control = ProcessControl.prepare(
            launch.argv, launch.cwd, env=launch.env, blocked_signals=launch.blocked_signals
        )
        process = None
        created = False
        try:
            command = Launch(tuple(control.command()), cwd=launch.cwd, env=launch.env)
            if terminal:
                self.tmux.start(
                    name,
                    command,
                    events=events,
                    status_line=self.tui_settings().status_line,
                    tags={
                        "@kind": "terminal",
                        "@control": control.path,
                        **(
                            {"@sid": record.sid, "@runtime": record.kind}
                            if record is not None and record.sid is not None
                            else {}
                        ),
                        **{
                            f"@{key}": value
                            for key, value in (metadata or {}).items()
                            if key in ("sid", "runtime", "generation")
                        },
                        **({"@display": display} if display is not None else {}),
                        **({"@opener": parts[0]} if parts is not None and parts[0] else {}),
                        **({"@record": record.to_line()} if record is not None else {}),
                    },
                )
                created = True
                process = self.tmux.process(name)
                if process is None:
                    raise TuiError(f"session {name} disappeared during start")
            else:
                process = self.tmux.detach(
                    name,
                    command,
                    opener=None if parts is None else parts[0],
                    display=display,
                    events=events,
                    record=None if record is None else record.to_line(),
                    control=control.path,
                )
            control.wait_started()
        except (TuiError, OSError) as error:
            cleanup = self._cancel_launch(control, process, name if created else None)
            detail = f"; {cleanup}" if cleanup else ""
            failure = (
                f"cannot run {error.filename or launch.argv[0]}: {os.strerror(error.errno)}"
                if isinstance(error, OSError) and error.errno is not None
                else str(error)
            )
            prefix = "" if terminal else f"detach {name}: "
            raise DriverError(f"{prefix}{failure}{detail}") from error
        return TmuxProcess(name, process, tmux=self.tmux, control=control)

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        super().validate(name, launch, display=display)
        try:
            self.tmux.validate_start(name, launch.argv, launch.env, {})
        except TuiError as error:
            raise DriverError(str(error)) from error
        self.tui_settings()

    def prepare_events(self, events: str | None) -> str | None:
        try:
            return None if events is None else self.tmux.events_file(events)
        except TuiError as error:
            raise DriverError(str(error)) from error

    def events_path(self, events: str | None) -> str | None:
        try:
            return None if events is None else self.tmux.validate_events(events)
        except TuiError as error:
            raise ConfigError(str(error)) from error

    def hook_state(self, name: str, metadata: Mapping[str, str]) -> dict:
        return {
            "host_hook": "agent_pm.session.hosts.tmux.hook",
            "host_state": {
                "executable": self._executable(),
                "target": name,
                "generation": metadata["generation"],
                "environment": {
                    key: value
                    for key, value in os.environ.items()
                    if key in ("TMUX", "TMUX_TMPDIR")
                },
            },
        }

    def _executable(self) -> str:
        path = self.tmux.tmux_path()
        if path is None:
            raise DriverError("tmux not found")
        return path

    def protected_paths(self) -> tuple[str, ...]:
        return (self._executable(),)

    def tui_settings(self) -> TuiSettings:
        line = self.settings.get("status_line", False)
        per = self.settings.get("workers_per_column")
        retile = self.settings.get("grid_retile")
        if not isinstance(line, bool):
            raise ConfigError("status_line: want true or false")
        if per is not None and (
            isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= 9999
        ):
            raise ConfigError("workers_per_column: want an integer from 1 to 9999")
        if retile is not None and retile not in tuple(self.tmux.Retile):
            raise ConfigError(f"grid_retile: want one of {', '.join(self.tmux.Retile)}")
        return TuiSettings(line, per, retile)

    def _cancel_launch(
        self, control: ProcessControl, process: str | None = None, name: str | None = None
    ) -> str:
        errors = []
        try:
            control.cancel()
        except OSError as error:
            errors.append(f"cancel controller: {error}")
        if process is not None:
            pane = process.split(" ")[2]
            try:
                if self.tmux.process(pane) == process:
                    self.tmux.kill(pane)
            except TuiError as error:
                errors.append(f"close pane: {error}")
        elif name is not None:
            try:
                if self.tmux.option(name, "@control") == control.path:
                    self.tmux.kill(name)
            except TuiError as error:
                errors.append(f"close session: {error}")
        return "; ".join(errors)

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
        kind = TmuxTerminal if self.tmux.option(name, "@kind") == "terminal" else TmuxProcess
        return kind(name, process, tmux=self.tmux)

    def list(self, display: Display = None) -> list[str]:
        if display == "":
            return []
        found = self.tmux.listing()
        if found is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        parts = self.unfold(display)

        def matches(value):
            other = self.unfold(value)
            return value == display or (
                parts is not None
                and other is not None
                and parts[0] is not None
                and parts[0] == other[0]
            )

        return sorted(row["name"] for row in found if display is None or matches(row["display"]))

    def descendants(self, display: Display = None) -> list[str]:
        """list(display)'s sessions, then each session whose @opener is one of those, transitively."""
        found = set(self.list(display))
        rows = self.tmux.listing() if found else []
        if rows is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        while grown := {
            row["name"] for row in rows if row["name"] not in found and row["opener"] in found
        }:
            found |= grown
        return sorted(found)

    def reclaim(self, token: str) -> TmuxGroup | None:
        """A group-only handle on token's controller (a private /tmp/apm-group-* directory and
        socket of this user's) while its group lives; else None; DriverError if it cannot tell.
        """
        if not isinstance(token, str):
            return None
        try:
            control = ProcessControl.attach(token)
        except (OSError, ValueError):
            return None
        group = TmuxGroup(control)
        return group if group.alive() else None

    def rows(self, display: Display = None) -> list[SessionRow]:
        names = set(self.list(display))
        found = self.tmux.listing()
        if found is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        try:
            own = self.tmux.own_session() if found else None
        except TuiError as error:
            raise DriverError(f"tmux sessions unknown: {error}") from error
        return [
            SessionRow(
                name=row["name"],
                sid=None if sid_error(row["sid"]) else row["sid"],
                runtime=row["runtime"]
                if isinstance(row["runtime"], str) and CONFIG_NAME.fullmatch(row["runtime"])
                else None,
                state=ActivityState.DEAD
                if row["dead"]
                else ActivityState(row["state"])
                if row["state"] in (Word.DONE, Word.BLOCKED)
                else ActivityState.WORKING,
                display=row["display"],
                shown=row["shown"],
                last_lines=row["last_lines"],
                own=row["name"] == own,
            )
            for row in found
            if row["name"] in names
        ]

    def validate_display(self, value: Display) -> None:
        _checked(value)

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

    def screen(self, name: str | HostProcess, lines: int | None = None) -> str:
        if isinstance(name, HostProcess):
            if not isinstance(name, TmuxProcess) or name.tmux is not self.tmux or not name.alive():
                raise DriverError(f"session {name.name} is not running on host {self.name}")
            target = name._pane
        else:
            target = _session(name)
        try:
            return self.tmux.read(target, lines)
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
