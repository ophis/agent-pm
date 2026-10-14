"""The old detach path, until 356-7e: `agent-pm-delegate run --detach` and engine.harness. The CLI
checks the options first.

Exits 0 on a valid outcome or detach; 1 without an outcome or on a start failure; 2 on configuration
errors. The harness exits 128 + signum on a signal.
"""

import functools
from dataclasses import replace

from agent_pm.delegate import composer, configure, plan, run
from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.runner import report, say
from agent_pm.delegate.engine.types import Begun, ConfigError, Plan, Spec
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError


class Application:
    """The configured session owner for the old detach path."""

    def __init__(self, driver=None):
        self._driver = driver

    @property
    def driver(self):
        if self._driver is None:
            self._driver = configure()
        return self._driver

    def plan(self, config, spec):
        return plan(config, spec, driver=self.driver)

    def run(self, config, spec, *, on_event):
        return run(config, spec, driver=self.driver, on_event=on_event)

    def run_cli(self, a, spec: Spec) -> tuple[int, str | None]:
        """(exit code, the valid outcome's status) of `spec`, `a` its options."""
        try:
            team = composer.team(composer.TEAM, composer.team_dirs())
            config = composer.resolve(a.role, a.task, team=team)
            driver = self.driver
            spec = replace(spec, team=team)
            if a.detach:
                return _detach(a, self.plan(config, spec), driver), None
            tui = detach.names(config.role, a.sid, a.mode)[0] if a.harness else None
            spec = replace(spec, name=tui)
            result = self.run(
                config, spec, on_event=functools.partial(_begun, a, tui, driver, config.show)
            )
        except ConfigError as e:
            say(f"delegate: {e}")
            return 2, None
        return report(result, a.out)


def _detach(a, p: Plan, driver) -> int:
    try:
        d = detach.detach(p, driver=driver, events=a.events, split=a.split, split_from=a.split_from)
    except DriverError as e:
        say(f"delegate: {e}")
        return 1
    say(f"delegate: session {p.spec.sid}")
    say(f"delegate: driver session {d.driver}: {driver.attach_command(d.driver)}")
    if d.tui is not None:
        say(f"delegate: tui session {d.tui}: {driver.attach_command(d.tui)}")
    return 0


def _begun(a, tui: str | None, driver, show: str, event) -> None:
    if not isinstance(event, Begun):
        return
    say(f"delegate: session {event.sid}")
    if tui is not None and a.opener is not None:
        try:
            if show == "none":
                say(driver.attach_command(tui))
            elif command := driver.show(tui, a.split, a.split_from, a.opener, command=show or None):
                say(command)
        except (SessionConfigError, DriverError) as e:
            say(f"delegate: show {tui}: {e}")
