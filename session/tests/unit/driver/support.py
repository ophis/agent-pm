"""Caller-owned driver configuration, adapters, modes and host facts for behavior tests."""

import dataclasses
import json
import os
import tempfile
import threading
import types
from unittest import mock

from agent_pm.session import StartOptions
from agent_pm.session.engine import api, records
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    ConfigError,
    Exited,
    Launch,
    SessionRequest,
    Spec,
)
from agent_pm.session.hosts.tmux import tmux_host

from session.tests import fake_adapter
from session.tests.case import Case
from session.tests.modules import module_slot

WAIT = 10  # upper bound on any wait, in seconds


SID = "0f8fad5b-d9cb-469f-a165-70867728950e"


PYTHONPATH = "/child"


TMUX = "/opt/tmux/bin/tmux"


BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n", 5)


class BlankAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return BlankRun(self)


class BlankRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, request: SessionRequest):
        return Launch(())


class FailingAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return FailingRun(self)


class FailingRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, request: SessionRequest):
        raise LookupError("no binding")


class QuietAdapter(fake_adapter.FakeAdapter):
    runtime_note = ""


class CheckedAdapter(fake_adapter.InProcessFakeAdapter):
    def in_process(self):
        return CheckedRun(self)


class CheckedRun(fake_adapter.FakeInProcess):
    """check: refuses tier 4, records preflight, and retains the request for execution."""

    def check(self, request: SessionRequest):
        self.checked_request = request
        spec = request.spec
        resume = request.resumed
        LAUNCHED.append(spec)
        if spec.tier == 4:
            raise engine_types.ConfigError("tier 4 is unmapped")
        with open(self.adapter.config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "check": True,
                        "sid": spec.sid,
                        "resume": resume,
                        "recorded": os.path.exists(records.path(spec.workdir)),
                    }
                )
                + "\n"
            )

    def run(self, prompt, request: SessionRequest):
        assert request is self.checked_request
        yield from super().run(prompt, request)


class ProbeAdapter(fake_adapter.FakeAdapter):
    """The fake adapter with a usage probe, its config's `probe` (Launch's fields), whose stdout is
    the 5-hour window's percent (anything else raises), and a session status naming its sid and cwd.
    """

    keys = fake_adapter.FakeAdapter.keys | {"probe"}

    def usage_probe(self):
        return Launch(**self.config["probe"])

    def usage(self, output):
        return engine_types.Usage(engine_types.Window(float(output), None), None)

    def session_status(self, sid, cwd):
        return engine_types.SessionStatus("yes", 1.5, f"probe {sid} {cwd}")


LAUNCHED: list[Spec] = []


class InnerAdapter(fake_adapter.FakeAdapter):
    """A tui runtime whose sessions set INNER_SESSION in their children; its launch records its spec
    in LAUNCHED, unsets LAUNCH_DROP and sets spec.env, then LAUNCH_SET.
    """

    session_env = "INNER_SESSION"
    identity_env = ("INNER_SESSION", "INNER_ID")
    child_defaults = {"PAGER": "cat", "NO_COLOR": "1"}

    def tui(self):
        return InnerTui(self)


class InnerTui(fake_adapter.FakeTui):
    def launch(self, prompt, request: SessionRequest, hook):
        spec = request.spec
        LAUNCHED.append(spec)
        launch = super().launch(prompt, request, hook)
        env = {**spec.env, "LAUNCH_SET": "1"}
        return dataclasses.replace(launch, env=env, unset=("LAUNCH_DROP",))


class OtherAdapter(fake_adapter.FakeAdapter):
    session_env = "OTHER_SESSION"
    identity_env = ("OTHER_SESSION", "OTHER_ID")
    child_defaults = {"OTHER_MODE": "on"}


class EnvAdapter(ProbeAdapter):
    """The probe adapter whose session launches carry spec.access.env and spec.env, as claude's and
    codex's do.
    """

    def headless(self):
        return EnvRun(self)

    def tui(self):
        return EnvTui(self)


class EnvRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, request: SessionRequest):
        spec = request.spec
        LAUNCHED.append(spec)
        launch = super().launch(prompt, request)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


class EnvTui(fake_adapter.FakeTui):
    def launch(self, prompt, request: SessionRequest, hook):
        spec = request.spec
        launch = super().launch(prompt, request, hook)
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
            ("blank", BlankAdapter),
            ("failing", FailingAdapter),
            ("quiet", QuietAdapter),
            ("probe", ProbeAdapter),
            ("checked", CheckedAdapter),
            ("inner", InnerAdapter),
            ("other", OtherAdapter),
            ("env", EnvAdapter),
        )
    },
}


class Base(Case):
    def setUp(self):
        self.probe_dir = os.path.join(self.agent_pm, "probe")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "runtime.log")
        self.steps()
        for name, m in PACKAGES.items():
            self.enterContext(module_slot(f"agent_pm.session.adapters.{name}", m))
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, runtime="fake", settings=None, tables=None, **extra):
        """A table per PACKAGES name, each with this test's scenario and log, plus `tables`; `extra`
        keys go into runtime's; settings: the config's global keys.
        """
        tables = {
            **{
                name: {
                    "implementation": f"adapters.{name}:{name.capitalize()}Adapter",
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
        self, runtime="fake", mode="headless", prompt="hello", spec=None, **kwargs
    ) -> api.Handle:
        """`sid` and `name` go into the spec, as self.driver.start takes them."""
        given = {k: kwargs.pop(k) for k in ("sid", "name") if k in kwargs}
        spec = dataclasses.replace(spec or self.spec(), **given)
        handle = self.driver.start(runtime, mode, prompt, spec, options=StartOptions(**kwargs))
        self.addCleanup(handle.stop)
        return handle

    def drain(self, handle) -> list:
        """handle.events() to its end; fails, stopping the handle, instead of hanging past WAIT."""
        found, errors = [], []

        def run():
            try:
                found.extend(handle.events())
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(WAIT)
        if thread.is_alive():
            handle.stop()
            self.fail(f"events() did not end within {WAIT}s")
        if errors:
            raise errors[0]
        return found

    def run_once(self, **kwargs) -> tuple[api.Handle, list[str]]:
        """A started session run to its end, and the argv its runtime logged."""
        handle = self.start(**kwargs)
        self.assertEqual(self.drain(handle), [Exited(0)])
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        return handle, entry["argv"]

    def written(self) -> list[dict]:
        return records.read(records.path(self.workdir))

    def refused(self, error, message, call):
        """call() raises `error` with `message`, nothing started and no run record written."""
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen:
            with self.assertRaises(error) as caught:
                call()
        self.assertEqual(str(caught.exception), message)
        popen.assert_not_called()
        self.assertFalse(os.path.exists(records.path(self.workdir)))
        self.assertFalse(os.path.exists(self.log))


class Clock:
    """time.monotonic's stand-in: `now` moves only when a StubMode waits."""

    now = 0.0


class StubMode:
    """A Mode on a Clock: poll(timeout) gives the next script item (at, events, ended) once the
    clock reaches `at`, else ([], False), moving the clock to `at` or on by timeout, whichever is
    sooner; past LATE s it raises, so a Handle that never ends fails. returncode() gives `code`;
    every call is logged in `calls`.
    """

    LATE = 60

    def __init__(self, clock, script, code):
        self.clock, self.script, self.code, self.calls = clock, list(script), code, []

    def poll(self, timeout):
        self.calls.append(("poll", timeout))
        if self.clock.now > self.LATE:
            raise AssertionError("polled past the script")
        if self.script and self.script[0][0] <= self.clock.now + timeout:
            at, events, ended = self.script.pop(0)
            self.clock.now = max(self.clock.now, at)
            return list(events), ended
        self.clock.now += timeout
        return [], False

    def returncode(self, timeout):
        self.calls.append(("returncode", timeout))
        return self.code

    def state(self):
        self.calls.append(("state",))
        return "unknown"

    def send(self, text):
        self.calls.append(("send", text))

    def screen(self, lines=None):
        self.calls.append(("screen", lines))
        return "pane"

    def stop(self):
        self.calls.append(("stop",))


def fake_host() -> mock.Mock:
    """tmux_host autospecced, its constants, NAME, TuiError, attach_command and events_file real,
    PAUSE 0: status None (absent), listing [], own_session None, option None, pane %1, tmux_path
    TMUX.
    """
    host = mock.create_autospec(tmux_host)
    for name in ("RUNNING", "UNKNOWN", "NAME", "OPENER", "SPLITS", "TuiError"):
        setattr(host, name, getattr(tmux_host, name))
    host.PAUSE = 0
    host.attach_command.side_effect = tmux_host.attach_command
    host.events_file.side_effect = tmux_host.events_file
    host.status.return_value = None
    host.listing.return_value = []
    host.own_session.return_value = None
    host.option.return_value = None
    host.pane.return_value = "%1"
    host.tmux_path.return_value = TMUX
    return host


class TuiBase(Base):
    def setUp(self):
        super().setUp()
        self.configure(tui=True)
        self.root = os.path.join(os.path.dirname(self.probe_dir), "tui")
        self.events = os.path.join(self.dir, "events")
        self.host = fake_host()
        self.driver_dependencies = {"host": self.host}
        self.driver.host = self.host

    def tui(self, **kwargs) -> api.Handle:
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


def listed(name, **fields) -> dict:
    """A tmux_host.listing row of a live session with no tags, but `fields`."""
    return {
        "name": name,
        "sid": None,
        "runtime": None,
        "state": None,
        "pane": None,
        "opener": None,
        "dead": False,
        "shown": None,
        "last_lines": None,
        **fields,
    }


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
