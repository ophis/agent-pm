"""Shared fake-runtime fixtures for hosted and forwarding tests."""

import dataclasses
import json
import os
import tempfile
import threading
import types
from unittest import mock

from agent_pm.session import Session
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import Access, ConfigError, Exited, Launch, Spec
from agent_pm.session.hosts.tmux import tmux_host

from session.tests import fake_adapter
from session.tests.case import Case
from session.tests.modules import adapter_loader
from session.tests.threads import capture_errors

WAIT = 10

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

PYTHONPATH = "/child"

TMUX = "/opt/tmux/bin/tmux"

BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n", 5)

PLACEMENTS = (
    ({"split": "left"}, "split must be right or below, got 'left'"),
    ({"split_from": "a b"}, "split_from must match [A-Za-z0-9_-]+, got 'a b'"),
    ({"split_from": 5}, "split_from must match [A-Za-z0-9_-]+, got 5"),
)

OPENERS = (
    ("x y", "opener must be a tmux session name or $ITERM_SESSION_ID's value, got 'x y'"),
    (
        "w0t0p0:a/b",
        "opener must be a tmux session name or $ITERM_SESSION_ID's value, got 'w0t0p0:a/b'",
    ),
)

NO_PANE = "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)"

LAUNCHED: list[Spec] = []


class QuietAdapter(fake_adapter.FakeAdapter):
    runtime_note = ""


class CheckedAdapter(fake_adapter.InProcessFakeAdapter):
    def in_process(self):
        return CheckedRun(self)


class CheckedRun(fake_adapter.FakeInProcess):
    """check: refuses tier 4, records preflight, and retains the request for execution."""

    def __init__(self, adapter):
        super().__init__(adapter)
        self.failure = ConfigError("tier 4 is unmapped")

    def check(self, spec: Spec):
        self.checked = spec

        LAUNCHED.append(spec)
        if spec.tier == 4:
            raise self.failure
        with open(self.adapter.config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "check": True,
                        "sid": spec.sid,
                        "resume": spec.resume,
                        "recorded": os.path.exists(RunLog(spec.workdir).path),
                    }
                )
                + "\n"
            )

    def run(self, spec: Spec):
        assert spec == self.checked
        yield from super().run(spec)


class ProbeAdapter(fake_adapter.FakeAdapter):
    """Fake runtime with recorded usage output."""

    keys = fake_adapter.FakeAdapter.keys | {"probe"}

    def usage_probe(self):
        return Launch(**self.config["probe"])

    def usage(self, output):
        return engine_types.Usage(engine_types.Window(float(output), None), None)

    def session_status(self, sid, cwd):
        return engine_types.SessionStatus("yes", 1.5, f"probe {sid} {cwd}")


class InnerAdapter(fake_adapter.FakeAdapter):
    """Records requests and injects INNER_SESSION into tui children."""

    session_env = "INNER_SESSION"
    identity_env = ("INNER_SESSION", "INNER_ID")
    child_defaults = {"PAGER": "cat", "NO_COLOR": "1"}

    def tui(self):
        return InnerTui(self)


class InnerTui(fake_adapter.FakeTui):
    def launch(self, spec: Spec, hook, *, source_roots: tuple[str, ...]):
        LAUNCHED.append(spec)
        launch = super().launch(spec, hook, source_roots=source_roots)
        env = {**spec.env, "LAUNCH_SET": "1"}
        return dataclasses.replace(launch, env=env, unset=("LAUNCH_DROP",))


class EnvAdapter(ProbeAdapter):
    """Launches merge spec.access.env and spec.env."""

    def headless(self):
        return EnvRun(self)

    def tui(self):
        return EnvTui(self)


class EnvRun(fake_adapter.FakeHeadless):
    def launch(self, spec: Spec):
        LAUNCHED.append(spec)
        launch = super().launch(spec)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


class EnvTui(fake_adapter.FakeTui):
    def launch(self, spec: Spec, hook, *, source_roots: tuple[str, ...]):
        launch = super().launch(spec, hook, source_roots=source_roots)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


def package(name, cls) -> types.ModuleType:
    """An in-memory module agent_pm.session.adapters.<name> holding `cls`."""
    module = types.ModuleType(f"agent_pm.session.adapters.{name}")
    setattr(module, cls.__name__, cls)
    return module


PACKAGES = {
    "fake": fake_adapter,
    **{
        name: package(name, cls)
        for name, cls in (
            ("quiet", QuietAdapter),
            ("probe", ProbeAdapter),
            ("checked", CheckedAdapter),
            ("inner", InnerAdapter),
            ("env", EnvAdapter),
        )
    },
}


class Base(Case):
    """Caller-owned Driver and HostedSession fixtures."""

    def setUp(self):
        self.probe_dir = os.path.join(self.agent_pm, "probe")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "runtime.log")
        self.steps()
        self.driver_dependencies = {
            "class_loader": adapter_loader(
                {f"agent_pm.session.adapters.{name}": module for name, module in PACKAGES.items()}
            )
        }
        self.configure()

    def configure_session(self, adapters, tmux=None) -> None:
        super().configure_session(adapters, tmux)
        self.session = self.driver.session_cls

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, runtime="fake", settings=None, tables=None, **extra):
        """Configure fake adapters from this fixture's files."""
        tables = {
            **{
                name: {
                    "implementation": f"agent_pm.session.adapters.{name}:{name.capitalize()}Adapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
                for name in PACKAGES
            },
            **(tables or {}),
        }
        tables[runtime].update(extra)
        self.configure_session(tables, settings or {})

    def spec(self, **fields) -> Spec:
        return Spec(workdir=self.workdir, **fields)

    def start(
        self,
        runtime="fake",
        mode="headless",
        prompt="hello",
        spec=None,
        *,
        idle_timeout=None,
        display=None,
        host=None,
        **fields,
    ) -> Session:
        """self.session.start of `spec` (else self.spec()) with runtime, mode, prompt and `fields`."""
        spec = dataclasses.replace(
            spec or self.spec(), runtime=runtime, mode=mode, prompt=prompt, **fields
        )
        session = self.session.start(spec, idle_timeout=idle_timeout, display=display, host=host)
        self.addCleanup(session.stop)
        return session

    def drain(self, session) -> list:
        """session.events() to its end; fails, stopping the session, instead of hanging past WAIT."""
        found, errors = [], []

        def run():
            found.extend(session.events())

        thread = threading.Thread(target=run, daemon=True)
        with capture_errors(thread, errors.append):
            thread.start()
            thread.join(WAIT)
        if thread.is_alive():
            session.stop()
            self.fail(f"events() did not end within {WAIT}s")
        if errors:
            raise errors[0]
        return found

    def run_once(self, **kwargs) -> tuple[Session, list[str]]:
        """A started session run to its end, and the argv its runtime logged."""
        session = self.start(**kwargs)
        self.assertEqual(self.drain(session), [Exited(0)])
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        return session, entry["argv"]

    def written(self) -> list[dict]:
        return RunLog(self.workdir).read()

    def refused(self, error, message, call):
        """call() raises `error` with `message`, nothing started and no run record written."""
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen,
            self.assertRaises(error) as caught,
        ):
            call()
        self.assertEqual(str(caught.exception), message)
        popen.assert_not_called()
        self.assertFalse(os.path.exists(RunLog(self.workdir).path))
        self.assertFalse(os.path.exists(self.log))


def drive(case, runtime="fake", mode="headless", prompt="hello", spec=None, **kwargs):
    """case.driver.start, `sid` and `name` going into the spec as Driver callers pass them."""
    given = {k: kwargs.pop(k) for k in ("sid", "name") if k in kwargs}
    spec = dataclasses.replace(spec or case.spec(), **given)
    session = case.driver.start(runtime, mode, prompt, spec, **kwargs)
    case.addCleanup(session.stop)
    return session


def fake_tmux() -> mock.Mock:
    """Recorded tmux responses; defaults: status None, listing [], pane %1, PAUSE 0."""
    host = mock.create_autospec(tmux_host)
    for name in ("PaneStatus", "NAME", "OPENER", "Split", "TuiError"):
        setattr(host, name, getattr(tmux_host, name))
    host.PAUSE = 0
    host.attach_command.side_effect = tmux_host.attach_command
    host.events_file.side_effect = tmux_host.events_file
    host.display_error.side_effect = tmux_host.display_error
    host.status.return_value = None
    host.listing.return_value = []
    host.own_session.return_value = None
    host.option.return_value = None
    host.pane.return_value = "%1"
    host.detach.return_value = host.process.return_value = "1 $1 %1 4242"
    host.tmux_path.return_value = TMUX
    return host


class TuiBase(Base):
    """Tui fixtures with injected tmux responses."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(os.path.dirname(self.probe_dir), "tui")
        self.events = os.path.join(self.dir, "events")
        self.host = fake_tmux()
        self.driver_dependencies = {**self.driver_dependencies, "host": self.host}
        self.configure(tui=True)

    def tui(self, **kwargs) -> Session:
        return self.start(mode="tui", **kwargs)

    def nothing_hosted(self):
        self.assertEqual(self.host.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))

    def name_refused(self, call, bad=(*BAD_NAMES, None)):
        """call(name) refuses each bad name before any tmux call or path use."""
        for name in bad:
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                call(name)
            self.assertEqual(
                str(caught.exception), f"name must match [A-Za-z0-9_-]+, got {name!r:.80}"
            )
        self.nothing_hosted()


class EnvBase(TuiBase):
    """Recorded probe and isolated caller environment."""

    def setUp(self):
        super().setUp()
        self.probe = mock.Mock(return_value="7")
        self.driver_dependencies = {**self.driver_dependencies, "probe": self.probe}
        self.configure("env", probe={"argv": ("probe-cmd",)})
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": "/parent"}))
        self.access = Access(commands=("git",), env={"PYTHONPATH": PYTHONPATH})


def listed(name, **fields) -> dict:
    """A tmux_host.listing row of a live session with no tags, but `fields`."""
    return {
        "name": name,
        "sid": None,
        "runtime": None,
        "state": None,
        "pane": None,
        "opener": None,
        "display": None,
        "dead": False,
        "shown": None,
        "last_lines": None,
        **fields,
    }
