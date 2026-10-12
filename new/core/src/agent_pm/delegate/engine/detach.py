"""A detached run: the Delegate CLI's driver form (--driver) for a Plan, started through Driver.detach() in its Driver
session `<role>-<sid8>-drive` (a tui run's agent session: `<role>-<sid8>`). Every check first: a refusal is
ConfigError, nothing started. When the driver form ends, outcome() appends its one outcome line to the events file."""

import os
import sys
from dataclasses import dataclass

from agent_pm.delegate.engine.plan import ConfigError, Plan
from agent_pm.session import SessionStatusLog, api
from agent_pm.utils.files import append_line
from agent_pm.utils.paths import IMPORT_ROOT
from agent_pm.utils.text import printable

MODES = ("headless", "tui")


def names(role: str, sid: str, mode: str) -> tuple[str | None, str]:
    """(tui, driver): the TUI session's name, None outside tui mode, and the Driver session's."""
    tui = f"{role}-{sid[:8]}"
    return (tui if mode == "tui" else None), f"{tui}-drive"


@dataclass(frozen=True)
class Detached:
    driver: str
    tui: str | None
    opener: str | None


def detach(
    p: Plan,
    *,
    events: str,
    repo: str | None = None,
    split: str | None = None,
    split_from: str | None = None,
) -> Detached:
    """The events file must be one you own (created if missing); a tui run's pane opener from api.layout, None when
    the caller has no pane and split_from is None. api.DriverError from api.detach propagates."""
    if p.mode not in MODES:
        raise ConfigError(f"--detach: mode must be headless or tui, got {p.mode!r}")
    if p.mode != "tui" and (split is not None or split_from is not None):
        raise ConfigError("split and split_from: tui mode only")
    tui, driver = names(p.run.role, p.sid, p.mode)
    events = os.path.abspath(events)
    try:
        append_line(events, "", own=True)
    except OSError as e:
        raise ConfigError(f"events file {events}: {e.strerror or e}") from None
    opener = None
    if tui is not None:
        try:
            opener = api.layout(split, split_from)
        except api.DriverError as e:
            if split_from is not None:
                raise ConfigError(str(e)) from None
        except api.ConfigError as e:
            raise ConfigError(str(e)) from None
    given = {
        "driver": driver,
        "role": p.run.role,
        "input": p.params.input,
        "out": p.out,
        "workdir": p.params.workdir,
        "runtime": p.runtime,
        "mode": p.mode,
        "sid": p.sid,
        "events": events,
    }
    optional = {
        "task": p.run.task,
        "repo": repo,
        "split": split,
        "split-from": split_from,
        "opener": opener,
    }
    # One `--k=v` item each: a value led by `-` is never read as an option.
    argv = [
        sys.executable,
        "-P",
        "-m",
        "agent_pm.delegate.cli",
        *(f"--{k}={v}" for k, v in given.items()),
        *(f"--{k}={v}" for k, v in optional.items() if v),
        *(["--resume"] if p.resume else []),
    ]
    try:
        api.detach(
            driver,
            argv,
            cwd=os.getcwd(),
            env={**os.environ, "PYTHONPATH": IMPORT_ROOT},
            opener=opener,
        )
    except api.ConfigError as e:
        raise ConfigError(str(e)) from None
    return Detached(driver, tui, opener)


def outcome(events: str, driver: str, status: str | None) -> None:
    """Appends `HH:MM:SS <driver> outcome <status>`, None as `error`; an OSError is printed, never raised."""
    try:
        SessionStatusLog(events).write(driver, "outcome", status or "error")
    except OSError as e:
        print(printable(f"delegate: {e}"), file=sys.stderr, flush=True)
