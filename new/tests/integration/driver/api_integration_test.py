"""driver.api.start() end to end over the real fake runtime (new/tests/fake_adapter/runtime.py): the frozen event set
streamed to the Driver's Exited and the run records (acceptance), the idle timer (FR-11), exits, stop(), failures, and
the launch's environment; then stop() of a process group and a process that outlives its pipes. The fake adapter, a
subclass whose launch takes env, unset and cwd from its config, and one whose runtime is this Python running its
config's script are registered in-process (sys.modules) with their tables given to registry.configure."""
import dataclasses
import io
import json
import os
import re
import signal
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

from new.tests import fake_adapter, hermetic
from adapters.base import Access, Activity, Blocked, Exited, Launch, Reply, SessionBound, Stderr, TurnEnd
from driver import api, records, registry

WAIT = 20      # upper bound on any wait, in seconds
IDLE = 0.3     # idle_timeout, in seconds
SILENCE = 0.9  # 3 × IDLE
GAP = 0.2      # between stderr lines; that test's idle_timeout is 3 × GAP
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z")


class LaunchingAdapter(fake_adapter.FakeAdapter):
    """The fake adapter whose launch also takes its env, unset and cwd from its config."""
    keys = fake_adapter.FakeAdapter.keys | {"env", "unset", "cwd"}

    def headless(self):
        return LaunchingRun(self)


class LaunchingRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, spec, resume):
        config = self.adapter.config
        return dataclasses.replace(super().launch(prompt, spec, resume), env=config.get("env", {}),
                                   unset=tuple(config.get("unset", ())), cwd=config.get("cwd", ""))


LAUNCHING = types.ModuleType("adapters.launching")
LAUNCHING.LaunchingAdapter = LaunchingAdapter


class ScriptAdapter(fake_adapter.FakeAdapter):
    """The fake adapter whose runtime is this Python running its config's `script`."""
    keys = fake_adapter.FakeAdapter.keys | {"script"}

    def headless(self):
        return ScriptRun(self)


class ScriptRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, spec, resume):
        return Launch((sys.executable, "-I", "-c", self.adapter.config["script"]))


SCRIPT = types.ModuleType("adapters.script")
SCRIPT.ScriptAdapter = ScriptAdapter


def out(kind, **fields) -> dict:
    """A scenario step printing record {"type": kind, **fields}."""
    return {"out": {"type": kind, **fields}}


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill(pid: int) -> None:
    """SIGKILL to a test's stray process, if it still runs."""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "runtime.log")
        self.steps()
        patch = mock.patch.dict(sys.modules, {"adapters.fake": fake_adapter, "adapters.launching": LAUNCHING,
                                              "adapters.script": SCRIPT})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.reset)
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, **extra):
        """Tables fake and launching, each with this test's scenario and log; `extra` keys go into launching's."""
        tables = {name: {"implementation": f"adapters.{name}:{name.capitalize()}Adapter", "scenario": self.scenario,
                         "log": self.log} for name in ("fake", "launching")}
        tables["launching"].update(extra)
        registry.configure({"adapters": tables})

    def start(self, runtime="fake", spec=None, **kwargs) -> api.Handle:
        handle = api.start(runtime, "headless", "hello", spec or api.Spec(workdir=self.workdir), **kwargs)
        self.addCleanup(handle.stop)
        return handle

    def drain(self, handle, on=lambda event: None) -> list:
        """handle.events() to its end, each event passed to on(event) as it comes; fails, stopping the handle, instead
        of hanging past WAIT."""
        found, errors = [], []

        def run():
            try:
                for event in handle.events():
                    found.append(event)
                    on(event)
            except BaseException as e:
                errors.append(e)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(WAIT)
        if thread.is_alive():
            handle.stop()
            self.fail(f"events() did not end within {WAIT}s, got {found}")
        if errors:
            raise errors[0]
        return found

    def logged(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def forget(self):
        """Drops the runtime's log."""
        if os.path.exists(self.log):
            os.unlink(self.log)

    def written(self) -> list[dict]:
        return records.read(records.path(self.workdir))


class AcceptanceTest(Base):
    def test_streams_the_frozen_event_set_to_exited_and_records_the_session(self):
        marker = "[agent-pm-progress:start] x"
        self.steps(out("session", id="native-1"), out("activity", name="Read"), out("message", text="one"),
                   out("message", text=marker), out("message", text="three"), out("blocked", reason="approve"),
                   out("result", text="done", pending=2), {"err": "warning: slow"}, {"exit": 0})
        handle = self.start()
        self.assertIsNone(handle.reply)
        self.assertEqual(handle.info().native_id, "")
        seen = []
        found = self.drain(handle, lambda event: seen.append((event, handle.reply, handle.info().native_id)))
        self.assertEqual([e for e in found if not isinstance(e, Stderr)],
                         [SessionBound("native-1"), Activity("Read"), Reply("one"), Reply(marker), Reply("three"),
                          Blocked("approve"), TurnEnd("done", 2), Exited(0)])
        self.assertEqual(found.count(Stderr("warning: slow")), 1)
        self.assertEqual(found[-1], Exited(0))
        for event, reply, native in seen:
            if isinstance(event, Reply):
                self.assertEqual((reply, native), (event.text, "native-1"))
        self.assertEqual(handle.reply, "done")
        self.assertEqual(handle.info().native_id, "native-1")

        written = self.written()
        for entry in written:
            self.assertRegex(entry["ts"], STAMP)
        bare = [{k: v for k, v in e.items() if k != "ts"} for e in written]
        self.assertEqual(bare[0], {"kind": "session", "sid": handle.sid, "cwd": self.workdir, "project": False,
                                   "transcript": os.path.join(self.workdir, f"fake-{handle.sid}.jsonl"),
                                   "resume": f"fake --resume {handle.sid}"})
        self.assertCountEqual(bare[1:], [{"kind": "client_session", "sid": handle.sid, "client_id": "native-1"},
                                         {"kind": "stderr", "text": "warning: slow"}])


class IdleTest(Base):
    def test_silence_yields_idle_timeouts_while_the_process_lives_then_exited(self):
        self.steps(out("activity", name="Read"), {"sleep": SILENCE})
        handle = self.start(idle_timeout=IDLE)
        marks, alive = [], []

        def on(event):
            marks.append((time.monotonic(), event))
            if isinstance(event, api.IdleTimeout):
                alive.append(running(self.logged()[0]["pid"]))
        found = self.drain(handle, on)
        self.assertEqual((found[0], found[-1]), (Activity("Read"), Exited(0)))
        idles = [(at, e) for at, e in marks if isinstance(e, api.IdleTimeout)]
        self.assertGreaterEqual(len(idles), 1)
        self.assertEqual(len(found), len(idles) + 2)
        self.assertEqual({e for _, e in idles}, {api.IdleTimeout(IDLE)})
        self.assertEqual(alive, [True] * len(idles))
        times = [marks[0][0]] + [at for at, _ in idles]
        for before, after in zip(times, times[1:]):
            self.assertGreaterEqual(after - before, IDLE - 0.01)

    def test_none_after_blocked(self):
        self.steps(out("blocked", reason="approve"), {"sleep": SILENCE})
        self.assertEqual(self.drain(self.start(idle_timeout=IDLE)), [Blocked("approve"), Exited(0)])

    def test_none_without_an_idle_timeout(self):
        self.steps(out("activity", name="Read"), {"sleep": SILENCE})
        self.assertEqual(self.drain(self.start()), [Activity("Read"), Exited(0)])

    def test_a_stderr_line_after_blocked_starts_no_timer(self):
        self.steps(out("blocked", reason="approve"), {"sleep": 0.1}, {"err": "still waiting"}, {"sleep": SILENCE})
        found = self.drain(self.start(idle_timeout=IDLE))
        self.assertCountEqual(found[:-1], [Blocked("approve"), Stderr("still waiting")])
        self.assertEqual(found[-1], Exited(0))

    def test_stderr_lines_restart_the_timer(self):
        self.steps({"err": "1"}, {"sleep": GAP}, {"err": "2"}, {"sleep": GAP}, {"err": "3"}, {"sleep": GAP},
                   {"err": "4"})
        self.assertEqual(self.drain(self.start(idle_timeout=3 * GAP)),
                         [Stderr("1"), Stderr("2"), Stderr("3"), Stderr("4"), Exited(0)])


class ExitTest(Base):
    def test_an_exited_from_the_adapter_is_dropped(self):
        self.steps(out("exit"), {"exit": 0})
        self.assertEqual(self.drain(self.start()), [Exited(0)])

    def test_a_non_zero_exit(self):
        self.steps(out("activity", name="Read"), {"exit": 3})
        self.assertEqual(self.drain(self.start()), [Activity("Read"), Exited(3)])

    def test_stop_mid_sleep_ends_with_a_negative_exited_and_is_idempotent(self):
        self.steps(out("activity", name="Read"), {"sleep": 60})
        handle = self.start()
        found = self.drain(handle, lambda event: handle.stop() if isinstance(event, Activity) else None)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0], Activity("Read"))
        self.assertIsInstance(found[1], Exited)
        self.assertLess(found[1].returncode, 0)
        handle.stop()
        self.assertEqual(handle.state(), "absent")
        self.assertEqual(list(handle.events()), [])


class ProcessGroupTest(Base):
    def script(self, code: str, **kwargs) -> api.Handle:
        """A started session of runtime `script` running `code`."""
        registry.configure({"adapters": {"script": {"implementation": "adapters.script:ScriptAdapter",
                                                     "script": textwrap.dedent(code)}}})
        return self.start("script", **kwargs)

    def test_stop_kills_a_grandchild_holding_the_pipes_so_events_end_with_exited(self):
        handle = self.script("""
            import json, subprocess, sys, time
            grandchild = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
            print(json.dumps({"type": "message", "text": str(grandchild.pid)}), flush=True)
            time.sleep(60)
        """)

        def on(event):
            if isinstance(event, Reply):
                self.addCleanup(kill, int(event.text))
                handle.stop()
        found = self.drain(handle, on)
        self.assertEqual(len(found), 2)
        self.assertIsInstance(found[0], Reply)
        self.assertEqual(found[1], Exited(-signal.SIGKILL))

    def test_a_process_that_closed_its_pipes_and_lives_yields_idle_timeouts_until_stop_ends_it(self):
        handle = self.script("""
            import json, os, time
            print(json.dumps({"type": "activity", "name": "Read"}), flush=True)
            os.close(1)
            os.close(2)
            time.sleep(60)
        """, idle_timeout=IDLE)
        found = self.drain(handle, lambda event: handle.stop() if isinstance(event, api.IdleTimeout) else None)
        self.assertEqual(found, [Activity("Read"), api.IdleTimeout(IDLE), Exited(-signal.SIGKILL)])


class FailureTest(Base):
    def test_a_raising_parse_stops_the_process_and_propagates(self):
        self.steps(out("session"), {"sleep": 60})
        handle = self.start()
        with self.assertRaises(KeyError):
            self.drain(handle)
        self.assertFalse(running(self.logged()[0]["pid"]))
        self.assertEqual(handle.state(), "absent")
        self.assertEqual(list(handle.events()), [])

    def test_a_failed_client_session_record_stops_the_process_and_propagates(self):
        self.steps(out("session", id="native-1"), {"sleep": 60})
        handle = self.start()
        os.replace(self.redirected(), records.path(self.workdir))
        with self.assertRaises(OSError):
            self.drain(handle)
        self.assertFalse(running(self.logged()[0]["pid"]))
        self.assertEqual(handle.info().native_id, "")

    def test_a_failed_stderr_record_loses_the_line_not_the_run(self):
        self.steps({"err": "lost"})
        handle = self.start()
        os.replace(self.redirected(), records.path(self.workdir))
        with mock.patch("sys.stderr", io.StringIO()) as printed:
            self.assertEqual(self.drain(handle), [Stderr("lost"), Exited(0)])
        self.assertIn("records: ", printed.getvalue())

    def redirected(self) -> str:
        """A symlink to a file that is not there, which a record write refuses."""
        link = os.path.join(self.dir, "link")
        os.symlink(os.path.join(self.dir, "absent"), link)
        return link


class LaunchEnvironmentTest(Base):
    def test_unset_then_env_apply_to_the_callers_environment(self):
        for extra, shown in (({}, "caller"), ({"unset": ["FAKE_ENV"]}, None),
                             ({"unset": ["FAKE_ENV"], "env": {"FAKE_ENV": "launch"}}, "launch")):
            with self.subTest(extra=extra), mock.patch.dict(os.environ, {"FAKE_ENV": "caller"}):
                self.forget()
                self.configure(**extra)
                self.assertEqual(self.drain(self.start("launching")), [Exited(0)])
                self.assertEqual(self.logged()[0]["env"]["FAKE_ENV"], shown)

    def test_the_child_runs_in_cwd_with_pwd_set(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        other = os.path.join(self.dir, "other")
        os.mkdir(other)
        for runtime, extra, access, want in (("launching", {"cwd": cwd}, Access(cwd=other), cwd),
                                             ("fake", {}, Access(cwd=other), other),
                                             ("fake", {}, Access(), self.workdir)):
            with self.subTest(runtime=runtime, want=want), mock.patch.dict(os.environ, {"PWD": "/"}):
                self.forget()
                self.configure(**extra)
                handle = self.start(runtime, api.Spec(workdir=self.workdir, access=access))
                self.assertEqual(self.drain(handle), [Exited(0)])
                (entry,) = self.logged()
                self.assertEqual((entry["cwd"], entry["env"]["PWD"]), (want, want))
                self.assertEqual(handle.info().cwd, want)
                self.assertEqual(self.written()[-1]["cwd"], want)


if __name__ == "__main__":
    unittest.main()
