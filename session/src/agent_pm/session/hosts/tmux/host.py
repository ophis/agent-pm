"""TmuxHost, the Host on tmux, runs every tmux call through the Tmux it holds, directly or through
its Grid and Placement, which share that Tmux. TmuxHost's display format, FORMAT, has one
encoder (fold) and one decoder (unfold). TmuxProcess, a session it hosts, keeps its Record and
display in the tmux options @record and @display, and whether it is hidden in the hidden store (hidden.py).
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import time
from collections.abc import Mapping
from dataclasses import replace

from agent_pm.session.engine.types import (
    MARKS,
    NO_DISPLAY,
    ConfigError,
    HookCallback,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NamedKey,
    Paste,
    SessionError,
    SessionRow,
    StatusState,
    TuiSettings,
)
from agent_pm.session.hosts.base import Host, InteractiveProcess, Process, Showable
from agent_pm.session.hosts.reclaimed import ReclaimedGroup
from agent_pm.session.hosts.tmux import hidden as hidden_store
from agent_pm.session.hosts.tmux import hook, transport
from agent_pm.session.hosts.tmux.grid import Grid
from agent_pm.session.hosts.tmux.iterm import Iterm
from agent_pm.session.hosts.tmux.placement import Placement
from agent_pm.session.hosts.tmux.transport import (
    NAME,
    OPENER,
    POLL,
    PROCESS_ID,
    Tmux,
    attach_command,
    display_error,
    events_file,
    validate_events,
    validate_start,
)
from agent_pm.session.hosts.tmux.types import PaneStatus, Retile, Split, TuiError
from agent_pm.utils import CONFIG_NAME, Display, ProcessControl, Record, Status, sid_error

FORMAT = "<opener>[/<split>[/<split_from>[/hidden]]]"
HIDDEN = "hidden"  # started hidden; only NO_DISPLAY sets it
NO_PANE_HINT = ": run from tmux or iTerm2, or pass split_from"
KILL_WAIT = 5
LOG = logging.getLogger("session")

Parts = tuple[str | None, str | None, str | None, bool]
TMUX_KEYS = {NamedKey.PAGE_UP: "PPage", NamedKey.PAGE_DOWN: "NPage", " ": "Space", ";": "\\;"}
# tmux 3.4 (CI) and 3.5+ both send these as the same C0 or DEL byte; other punctuation and digits
# lose Ctrl, send nothing or differ between them
CTRL_PUNCTUATION = frozenset(" /?@[\\]^_")


TERMINAL_IDENTITY = ("TMUX", "TMUX_PANE", "TERM")  # a terminal program's are its pane's (FR-47)


def _session(name) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SessionError(f"no tmux session {name!r:.80}")
    return name


def _checked(display) -> None:
    if why := display_error(display):
        raise ConfigError(why)


class TmuxProcess(Process):
    """The captured pane and its generation's controller. A pane can set its Record and display
    options: consumers must re-validate a Record's ctl or resume before running it.
    """

    def __init__(
        self, name: str, process: str, *, tmux: Tmux, control: ProcessControl | None = None
    ) -> None:
        self.name = name
        self.tmux = tmux
        self._process = process
        _, _, self._pane, _ = process.split(" ")
        path = self._option("@control") if control is None else None
        if control is None and self._option("@kind") in ("detach", "terminal") and not path:
            raise SessionError(
                f"tmux session {name}: missing @control; close sessions before upgrading"
            )
        try:
            if control is not None:
                self._control = control
            else:
                self._control = ProcessControl.attach(path) if path else None
        except OSError as error:
            raise SessionError(f"tmux session {name}: invalid @control: {error}") from error

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
            raise SessionError(f"tmux session {self.name} is gone")
        try:
            self.tmux.set_option(self._pane, "@record", record.to_line())
        except TuiError as error:
            raise SessionError(f"tmux session {self.name}: {error}") from error

    def state(self) -> Liveness:
        try:
            own, value = self._pane_state()
        except SessionError:
            return Liveness.UNKNOWN
        if not own or value is None:
            return Liveness.ABSENT
        return Liveness.UNKNOWN if value == PaneStatus.UNKNOWN else Liveness.LIVE

    def returncode(self) -> int | None:
        try:
            own, value = self._pane_state()
        except SessionError:
            return None
        if not own or value is None:
            return -signal.SIGHUP
        return value if isinstance(value, int) else None

    def session_identity(self) -> tuple[str | None, str | None]:
        return self._option("@sid"), self._option("@generation")

    def activity(self) -> StatusState | None:
        value = self._option("@state")
        return StatusState(value) if value in MARKS else None

    def hidden(self) -> bool:
        sid = self.session_identity()[0]
        return sid is not None and hidden_store.read(self.name, sid)

    def status(self) -> Status:
        found = super().status()
        return replace(found, attach=attach_command(self.name)) if found.alive else found

    def mark_hidden(self, *, hidden: bool) -> None:
        """Only a session with a sid (no harness) keeps it."""
        sid = self.session_identity()[0]
        if sid is not None:
            try:
                hidden_store.write(self.name, sid, hidden=hidden, live=self.tmux.sessions())
            except OSError as error:
                raise SessionError(
                    f"tmux session {self.name}: hidden state: {error.strerror or error}"
                ) from error

    def mark(self, word: StatusState, events: str | None) -> None:
        if self._own():
            try:
                self.tmux.mark(word, events, pane=self._pane)
            except TuiError as error:
                raise SessionError(str(error)) from error

    def alive(self) -> bool:
        own, state = self._pane_state()
        if not own:
            return False
        if state == PaneStatus.UNKNOWN:
            raise SessionError(f"tmux session {self.name}: tmux failed")
        return state == PaneStatus.RUNNING and (self._control is None or self._control.running())

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
            raise SessionError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        """Closes the pane, then ends the process group, even when the pane fails; every failure
        is in the one SessionError raised.
        """
        failures = []
        for step in (self._close_pane, self._end_group):
            try:
                step()
            except SessionError as error:
                failures.append(str(error))
        if failures:
            raise SessionError("; ".join(failures))

    def _end_group(self) -> None:
        if self._control is None:
            return
        try:
            self._control.terminate()
        except (ConnectionRefusedError, ConnectionResetError):
            return
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error
        if not self._wait_group(KILL_WAIT):
            self._kill_group()
            if not self._wait_group(KILL_WAIT):
                raise SessionError(f"process group {self.name}: did not exit")

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
                raise SessionError(str(e)) from None

    def record(self) -> Record | None:
        line = self._option("@record")
        if line is not None:
            with contextlib.suppress(ValueError):
                return Record.parse(line)
        return None

    def display(self) -> Display:
        return self._option("@display")

    def _pane_state(self):
        """Whether its captured pane still runs this process, and the pane's status(): one call."""
        try:
            found, value = self.tmux.process_status(self._pane)
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None
        return found == self._process, value

    def _own(self) -> bool:
        try:
            return self.tmux.process(self._pane) == self._process
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None

    def _group_live(self) -> bool:
        try:
            return self._control is not None and self._control.live()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error

    def _kill_group(self) -> None:
        if self._control is None:
            self._close_pane()
            return
        try:
            self._control.kill()
        except OSError as error:
            raise SessionError(f"process group {self.name}: {error}") from error

    def _option(self, key: str) -> str | None:
        if not self._own():
            return None
        try:
            return self.tmux.option(self._pane, key)
        except TuiError as e:
            raise SessionError(f"tmux session {self.name}: {e}") from None


class TmuxTerminal(TmuxProcess, InteractiveProcess):
    _pasted = False

    def send(self, action: Paste | Key) -> None:
        if not self.alive():
            raise SessionError(f"tui session {self.name} is not running")
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
            raise SessionError(f"send to tui session {self.name}: {error}") from error

    def press(self, keys) -> None:
        names = [_tmux_key(key) for key in keys]
        if not self.alive():
            raise SessionError(f"tui session {self.name} is not running")
        try:
            if self._pasted:
                time.sleep(self.tmux.PAUSE)
            self.tmux.press(self._pane, *names)
            self._pasted = False
        except TuiError as error:
            raise SessionError(f"send to tui session {self.name}: {error}") from error


def _tmux_key(key) -> str:
    """KeyPress → tmux's key name: modifiers as C-, M-, S-; PageUp, PageDown, space and ; renamed."""
    if not isinstance(key, KeyPress):
        raise ConfigError(f"key {key!r:.80}: want a KeyPress")
    char = len(key.key) == 1
    letter = key.key.isascii() and key.key.isalpha()
    if char and (key.shift or key.ctrl and not (letter or key.key in CTRL_PUNCTUATION)):
        raise ConfigError(f"key {key}: tmux cannot send it")  # it would lose its modifier
    prefixes = ("C-" * key.ctrl, "M-" * key.meta, "S-" * key.shift)
    return "".join(prefixes) + TMUX_KEYS.get(key.key, key.key)


class TmuxHost(Host, Showable):
    """settings: [hosts.<name>]'s workers_per_column and grid_retile shape show's grid. A display
    not in FORMAT is kept as given.
    """

    def __init__(
        self, name: str, settings: Mapping | None = None, *, tmux: Tmux | None = None
    ) -> None:
        super().__init__(name, settings)
        if extra := sorted(
            set(self.settings) - {"status_line", "workers_per_column", "grid_retile"}
        ):
            raise ConfigError(f"unknown key {extra[0]!r} in [hosts.{name}]")
        self.tmux = Tmux() if tmux is None else tmux
        self.grid = Grid(self.tmux)
        self.placement = Placement(tmux=self.tmux, grid=self.grid, iterm=Iterm(self.tmux.runner))

    @classmethod
    def fold(cls, opener, split, split_from, *, hidden=False) -> Display:
        """FORMAT's one encoder; None when every part is. ConfigError for a bad part."""
        if opener is not None and not (isinstance(opener, str) and OPENER.fullmatch(opener)):
            raise ConfigError(
                f"opener must be a tmux session name or $ITERM_SESSION_ID's value, got {opener!r:.80}"
            )
        if split is not None and split not in tuple(Split):
            raise ConfigError(f"split must be {' or '.join(Split)}, got {split!r:.80}")
        if split_from is not None and not (
            isinstance(split_from, str) and NAME.fullmatch(split_from)
        ):
            raise ConfigError(f"split_from must match [A-Za-z0-9_-]+, got {split_from!r:.80}")
        parts = (opener or "", split or "", split_from or "", HIDDEN if hidden else "")
        return "/".join(parts).rstrip("/") or None

    @classmethod
    def unfold(cls, display) -> Parts | None:
        """FORMAT's one decoder, re-validating a display read back: (opener, split, split_from,
        hidden), an empty part None; NO_DISPLAY no opener, hidden; None when display is not in FORMAT.
        """
        if display is None:
            return None, None, None, False
        if display == NO_DISPLAY:
            return None, None, None, True
        if display_error(display):
            return None
        given = display.split("/")
        if given[3:] not in ([], [HIDDEN]):
            return None
        opener, split, split_from = (part or None for part in (*given, "", "")[:3])
        try:
            cls.fold(opener, split, split_from)
        except ConfigError:
            return None
        return opener, split, split_from, given[3:] == [HIDDEN]

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
            launch.argv,
            launch.cwd,
            env=launch.env,
            inherit_env=TERMINAL_IDENTITY if terminal else (),
            blocked_signals=launch.blocked_signals,
        )
        process = None
        try:
            command = Launch(tuple(control.command()), cwd=launch.cwd, env=launch.env)
            if terminal:
                process = self.tmux.start(
                    name,
                    command,
                    events=events,
                    status_line=self.tui_settings().status_line,
                    tags={
                        "@kind": "terminal",
                        "@control": control.path,
                        **(
                            {"@sid": record.sid, "@client": record.kind}
                            if record is not None and record.sid is not None
                            else {}
                        ),
                        **{
                            f"@{key}": value
                            for key, value in (metadata or {}).items()
                            if key in ("sid", "client", "generation")
                        },
                        **({"@display": display} if display is not None else {}),
                        **({"@opener": parts[0]} if parts is not None and parts[0] else {}),
                        **({"@record": record.to_line()} if record is not None else {}),
                    },
                )
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
            cleanup = self._cancel_launch(control, process)
            detail = f"; {cleanup}" if cleanup else ""
            failure = (
                f"cannot run {error.filename or launch.argv[0]}: {os.strerror(error.errno)}"
                if isinstance(error, OSError) and error.errno is not None
                else str(error)
            )
            prefix = "" if terminal else f"detach {name}: "
            raise SessionError(f"{prefix}{failure}{detail}") from error
        sid = record.sid if record is not None and record.sid else (metadata or {}).get("sid")
        if terminal and parts is not None and parts[3] and sid:
            try:
                hidden_store.start(name, sid, live=self.tmux.sessions())
            except OSError as error:
                cleanup = self._cancel_launch(control, process)
                detail = f"; {cleanup}" if cleanup else ""
                raise SessionError(
                    f"tui session {name}: hidden state: {error.strerror or error}{detail}"
                ) from error
        return TmuxProcess(name, process, tmux=self.tmux, control=control)

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        super().validate(name, launch, display=display)
        try:
            validate_start(name, launch.argv, launch.env, {})
        except TuiError as error:
            raise SessionError(str(error)) from error
        self.tui_settings()

    def prepare_events(self, events: str | None) -> str | None:
        try:
            return None if events is None else events_file(events)
        except TuiError as error:
            raise SessionError(str(error)) from error

    def events_path(self, events: str | None) -> str | None:
        try:
            return None if events is None else validate_events(events)
        except TuiError as error:
            raise ConfigError(str(error)) from error

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback:
        return HookCallback(
            hook.__name__,
            {
                "executable": self._executable(),
                "target": name,
                "generation": metadata["generation"],
                "environment": {
                    key: value
                    for key, value in os.environ.items()
                    if key in ("TMUX", "TMUX_TMPDIR")
                },
            },
        )

    def _executable(self) -> str:
        path = transport.tmux_path()
        if path is None:
            raise SessionError("tmux not found")
        return path

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
        if retile is not None and retile not in tuple(Retile):
            raise ConfigError(f"grid_retile: want one of {', '.join(Retile)}")
        return TuiSettings(line, per, retile)

    def _cancel_launch(self, control: ProcessControl, process: str | None = None) -> str:
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
        return "; ".join(errors)

    def attach(self, name: str) -> TmuxProcess:
        """A session it launched is the pane its launch recorded (@process), whichever pane is
        active; any other session is its active pane.
        """
        state = self.tmux.status(_session(name))
        if state is None:
            raise SessionError(f"no tmux session {name}")
        if state == PaneStatus.UNKNOWN:
            raise SessionError(f"tmux session {name}: tmux failed")
        try:
            kind = self.tmux.option(name, "@kind")
            launched = kind in ("detach", "terminal")
            process = self.tmux.option(name, "@process") if launched else self.tmux.process(name)
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if launched and process is None:
            raise SessionError(
                f"tmux session {name}: missing @process; close sessions before upgrading"
            )
        if launched and not PROCESS_ID.fullmatch(process or ""):
            raise SessionError(f"tmux session {name}: bad @process {process!r:.80}")
        if process is None:
            raise SessionError(f"no tmux session {name}")
        return (TmuxTerminal if kind == "terminal" else TmuxProcess)(name, process, tmux=self.tmux)

    def list(self, display: Display = None) -> list[str]:
        if display == "":
            return []
        found = self.tmux.listing()
        if found is None:
            raise SessionError("tmux sessions unknown: tmux failed")
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
            raise SessionError("tmux sessions unknown: tmux failed")
        while grown := {
            row["name"] for row in rows if row["name"] not in found and row["opener"] in found
        }:
            found |= grown
        return sorted(found)

    def reclaim(self, token: str) -> ReclaimedGroup | None:
        return ReclaimedGroup.reclaim(token)

    def rows(self, display: Display = None) -> list[SessionRow]:
        names = set(self.list(display))
        found = self.tmux.listing()
        if found is None:
            raise SessionError("tmux sessions unknown: tmux failed")
        try:
            own = self.placement.own_session() if found else None
        except TuiError as error:
            raise SessionError(f"tmux sessions unknown: {error}") from error
        return [
            SessionRow(
                name=row["name"],
                sid=None if sid_error(row["sid"]) else row["sid"],
                client=row["client"]
                if isinstance(row["client"], str) and CONFIG_NAME.fullmatch(row["client"])
                else None,
                state=StatusState.DEAD
                if row["dead"]
                else StatusState(row["state"])
                if row["state"] in (StatusState.DONE, StatusState.BLOCKED)
                else StatusState.WORKING,
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
        if parts is None:
            return value
        opener, split, split_from, hidden = self._given(value, parts)
        if opener is not None:
            return value
        try:
            opener = self.placement.opener()
        except TuiError:
            return self.fold(None, split, split_from, hidden=True) if hidden else value
        return self.fold(opener, split, split_from, hidden=hidden)

    @staticmethod
    def _given(value: Display, parts: Parts) -> Parts:
        """parts, unless a caller gave the hidden part itself: only NO_DISPLAY sets it."""
        if parts[3] and value != NO_DISPLAY:
            raise ConfigError(
                f"display {value!r:.80}: its hidden part comes only from {NO_DISPLAY}"
            )
        return parts

    def display_help(self) -> str:
        return (
            f"{FORMAT}: opener a tmux session name or $ITERM_SESSION_ID's value (empty: the "
            f"caller's), split {' or '.join(Split)}, split_from the tmux session whose "
            f"pane it goes beside; {NO_DISPLAY}: start without a pane, the caller still its opener "
            f"(recorded as the part {HIDDEN}, which only {NO_DISPLAY} sets); any other printable "
            "value not ending in ; is kept as given"
        )

    def show(self, name: str, *, display: Display = None, command: str | None = None) -> str | None:
        if self.tmux.status(_session(name)) is None:
            raise SessionError(f"no tmux session {name}")
        try:
            if command is None:
                opener, split, split_from, _ = self._parts(display)
                why = self.placement.open_pane(
                    name,
                    opener=opener,
                    split=split,
                    split_from=split_from,
                    per_column=self.settings.get("workers_per_column"),
                    retile=self.settings.get("grid_retile"),
                )
            else:
                why = self.placement.run_command(name, command)
        except TuiError as e:
            raise ConfigError(str(e)) from None
        placed = why is None
        if not placed and command is None:
            with contextlib.suppress(TuiError):
                placed = self.placement.shown(name) is not None
        if placed:
            if command is None:
                self.placement.watch(name)
            self.attach(name).mark_hidden(hidden=False)
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return attach_command(name)

    def hide(self, name: str, *, if_closed: bool = False) -> None:
        if self.tmux.status(_session(name)) is None:
            raise SessionError(f"no tmux session {name}")
        try:
            if if_closed:
                # the judged pane is gone: kill nothing, and forget it only while still recorded
                closed = self.placement.closed(name)
                if closed is None or not self.placement.forget(name, closed):
                    return
            else:
                pane = self.placement.shown(name)
                if pane is not None:
                    self.tmux.kill(pane)
                self.placement.forget(name)
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        process = self.attach(name)
        process.mark_hidden(hidden=True)
        try:
            # a show meanwhile may have written hidden=False before this mark
            shown = if_closed and self.placement.shown(name) is not None
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if shown:
            process.mark_hidden(hidden=False)

    def attach_command(self, name: str) -> str:
        return attach_command(_session(name))

    def screen(self, name: str | Process, lines: int | None = None) -> str:
        if isinstance(name, Process):
            if not isinstance(name, TmuxProcess) or name.tmux is not self.tmux or not name.alive():
                raise SessionError(f"session {name.name} is not running on host {self.name}")
            target = name._pane
        else:
            target = self._launched_pane(name)
        try:
            return self.tmux.read(target, lines)
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None

    def _launched_pane(self, name: str) -> str:
        """The pane name's launch recorded (@process), whichever pane is active; a session without
        @process: name, its active pane.
        """
        try:
            process = self.tmux.option(_session(name), "@process")
        except TuiError as e:
            raise SessionError(f"tmux session {name}: {e}") from None
        if process is None:
            return name
        if not PROCESS_ID.fullmatch(process):
            raise SessionError(f"tmux session {name}: bad @process {process!r:.80}")
        return process.split(" ")[2]

    def layout(self, *, display: Display = None) -> str | None:
        """display, its opener part the caller's once the pane it goes beside (split_from's, else
        the caller's) is found; that part stays empty only beside split_from. NO_DISPLAY needs no
        pane: the caller's opener when it has one, started hidden.
        """
        opener, split, split_from, hidden = self._given(display, self._parts(display))
        if hidden:
            with contextlib.suppress(TuiError):
                opener = self.placement.opener()
            return self.fold(opener, None, None, hidden=True)
        if split_from is not None and self.tmux.status(split_from) is None:
            raise ConfigError(f"no tmux session {split_from}")
        try:
            if opener is None or split_from is not None:
                self.placement.anchor(split_from)
            if opener is None:
                try:
                    opener = self.placement.opener()
                except TuiError:
                    if split_from is None:
                        raise
        except TuiError as e:
            raise SessionError(
                f"no pane to show the TUI beside ({e})" + ("" if split_from else NO_PANE_HINT)
            ) from None
        return self.fold(opener, split, split_from)

    def tile(self, window: str, *, wait: bool = True, if_panes_changed: bool = False) -> None:
        try:
            self.grid.tile(window, wait=wait, if_panes_changed=if_panes_changed)
        except TuiError as e:
            raise SessionError(f"tile {window}: {e}") from None

    def _parts(self, display) -> Parts:
        parts = self.unfold(display)
        if parts is None:
            raise ConfigError(f"display {display!r:.80}: not {FORMAT}")
        return parts
