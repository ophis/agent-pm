"""A detached run: persist a session-keyed invocation, then start engine.harness in
`<role>-<sid8>-drive` (tui agent: `<role>-<sid8>`). Refusals are ConfigError before anything starts.
The harness appends its one outcome line when it ends.
"""

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass

import agent_pm.session as session_package
import agent_pm.utils as utils_package
from agent_pm.delegate import config
from agent_pm.delegate.engine.drivers import Driver
from agent_pm.delegate.engine.runner import say
from agent_pm.delegate.engine.types import ConfigError, Plan
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError, Mode, SessionStatusLog
from agent_pm.utils import append_line, import_env

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
    if isinstance(driver, Driver):
        kind = "agent driver" if driver.session_based else "instruction"
        raise ConfigError(f"{kind} cannot detach")
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
        "out": p.params.out,
        "workdir": p.params.workdir,
        "client": p.client,
        "mode": mode,
        "sid": p.sid,
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
    state, authentication = _write_invocation(p, invocation)
    argv = [
        sys.executable,
        "-P",
        "-m",
        "agent_pm.delegate.engine.harness",
        p.params.workdir,
    ]
    started = False
    try:
        driver.detach(
            harness,
            argv,
            cwd=os.getcwd(),
            env={
                **{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                **import_env(config, session_package, utils_package),
                **authentication,
            },
            opener=opener,
        )
    except SessionConfigError as e:
        raise ConfigError(str(e)) from None
    else:
        started = True
    finally:
        if not started:
            os.unlink(state)
    return Detached(harness, tui)


def _write_invocation(p: Plan, invocation: dict) -> tuple[str, dict[str, str]]:
    payload = json.dumps(invocation, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(p.sid.encode("utf-8")).hexdigest()
    try:
        os.makedirs(p.params.workdir, exist_ok=True)
        descriptor, path = tempfile.mkstemp(
            prefix=f"harness-{key}-", suffix=".json", dir=p.params.workdir
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
        except OSError:
            os.unlink(path)
            raise
    except OSError as error:
        raise ConfigError(f"harness state: {error}") from None
    return path, {
        "AGENT_PM_HARNESS_STATE": os.path.basename(path),
        "AGENT_PM_HARNESS_DIGEST": hashlib.sha256(payload).hexdigest(),
    }


def outcome(events: str, driver: str, status: str | None) -> None:
    """Appends `HH:MM:SS <driver> outcome <status>`, None as `error`; an OSError is printed, never
    raised.
    """
    try:
        SessionStatusLog(events).write(driver, "outcome", status or "error")
    except OSError as e:
        say(f"delegate: {e}")
