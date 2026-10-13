"""A detached run: persist its invocation in harness.json, then start engine.harness in
`<role>-<sid8>-drive` (tui agent: `<role>-<sid8>`). Refusals are ConfigError before anything starts.
The harness appends its one outcome line when it ends.
"""

import json
import os
import sys
from dataclasses import dataclass

from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.delegate.engine.drivers import InstructionDriver
from agent_pm.delegate.engine.runner import say
from agent_pm.delegate.engine.types import ConfigError, Plan
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError, Mode, SessionStatusLog
from agent_pm.utils import append_line, save

MODES = frozenset({Mode.HEADLESS, Mode.TUI})


def names(role: str, sid: str, mode: str) -> tuple[str | None, str]:
    """(tui, driver): the TUI session's name, None outside tui mode, and the Driver session's."""
    tui = f"{role}-{sid[:8]}"
    return (tui if mode == "tui" else None), f"{tui}-drive"


@dataclass(frozen=True)
class Detached:
    driver: str
    tui: str | None


def detach(
    p: Plan,
    *,
    driver,
    events: str,
    split: str | None = None,
    split_from: str | None = None,
) -> Detached:
    """The events file must be one you own (created if missing); a tui run's pane opener from
    layout, None when the caller has no pane and split_from is None. The owner's DriverError
    propagates.
    """
    if isinstance(driver, InstructionDriver):
        raise ConfigError("instruction cannot detach")
    mode = p.spec.mode
    if mode not in MODES:
        raise ConfigError(f"--detach: mode must be headless or tui, got {mode!r}")
    if mode != "tui" and (split is not None or split_from is not None):
        raise ConfigError("split and split_from: tui mode only")
    tui, harness = names(p.run.role, p.sid, mode)
    events = os.path.abspath(events)
    try:
        append_line(events, "", own=True)
    except OSError as e:
        raise ConfigError(f"events file {events}: {e.strerror or e}") from None
    opener = None
    if tui is not None:
        try:
            opener = driver.layout(split, split_from)
        except DriverError as e:
            if split_from is not None:
                raise ConfigError(str(e)) from None
        except SessionConfigError as e:
            raise ConfigError(str(e)) from None
    invocation = {
        "harness": harness,
        "role": p.run.role,
        "input": p.params.input,
        "out": p.spec.out,
        "workdir": p.params.workdir,
        "runtime": p.spec.runtime,
        "mode": mode,
        "sid": p.spec.sid,
        "events": events,
        "task": p.run.task or None,
        "repo": p.spec.repo,
        "split": split,
        "split_from": split_from,
        "opener": opener,
        "resume": p.spec.resume,
        "detach": False,
        "dry_run": False,
        "list": False,
    }
    try:
        save(os.path.join(p.params.workdir, "harness.json"), json.dumps(invocation))
    except OSError as error:
        raise ConfigError(f"harness state: {error}") from None
    argv = [
        sys.executable,
        "-P",
        "-m",
        "agent_pm.delegate.engine.harness",
        p.params.workdir,
    ]
    try:
        driver.detach(
            harness,
            argv,
            cwd=os.getcwd(),
            env={**os.environ, "PYTHONPATH": PYTHONPATH},
            opener=opener,
        )
    except SessionConfigError as e:
        raise ConfigError(str(e)) from None
    return Detached(harness, tui)


def outcome(events: str, driver: str, status: str | None) -> None:
    """Appends `HH:MM:SS <driver> outcome <status>`, None as `error`; an OSError is printed, never
    raised.
    """
    try:
        SessionStatusLog(events).write(driver, "outcome", status or "error")
    except OSError as e:
        say(f"delegate: {e}")
