"""new/core/src/driver/api.py: start()'s checks, its session record, sid and Runtime note, and a headless Handle's
surface, over the fake adapter and test adapters registered in-process (sys.modules) with [adapters.<name>] tables
given to registry.configure. Wherever nothing may start, subprocess.Popen is patched to fail."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

from new.tests import fake_adapter, hermetic
from adapters import base
from adapters.base import Access, Launch
from driver import api, records, registry

WAIT = 10   # upper bound on any wait, in seconds


class BlankAdapter(fake_adapter.FakeAdapter):
    def headless_run(self):
        return BlankRun(self)


class BlankRun(fake_adapter.FakeHeadlessRun):
    def launch(self, prompt, spec, resume):
        return Launch(())


class FailingAdapter(fake_adapter.FakeAdapter):
    def headless_run(self):
        return FailingRun(self)


class FailingRun(fake_adapter.FakeHeadlessRun):
    def launch(self, prompt, spec, resume):
        raise LookupError("no binding")


class QuietAdapter(fake_adapter.FakeAdapter):
    runtime_note = ""


def package(name, cls) -> types.ModuleType:
    """An in-memory module adapters.<name> holding `cls`."""
    module = types.ModuleType(f"adapters.{name}")
    setattr(module, cls.__name__, cls)
    return module


PACKAGES = {"fake": fake_adapter, **{name: package(name, cls) for name, cls in
                                     (("blank", BlankAdapter), ("failing", FailingAdapter), ("quiet", QuietAdapter))}}


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
        patch = mock.patch.dict(sys.modules, {f"adapters.{name}": m for name, m in PACKAGES.items()})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.reset)
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, **extra):
        """A table per PACKAGES name, each with this test's scenario and log; `extra` keys go into fake's."""
        tables = {name: {"implementation": f"adapters.{name}:{name.capitalize()}Adapter", "scenario": self.scenario,
                         "log": self.log} for name in PACKAGES}
        tables["fake"].update(extra)
        registry.configure({"adapters": tables})

    def spec(self, **fields) -> api.Spec:
        return api.Spec(workdir=self.workdir, **fields)

    def start(self, runtime="fake", mode="headless", prompt="hello", spec=None, **kwargs) -> api.Handle:
        handle = api.start(runtime, mode, prompt, spec or self.spec(), **kwargs)
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
        self.assertEqual(self.drain(handle), [api.Exited(0)])
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


class ChecksTest(Base):
    def test_refuses_an_unknown_mode(self):
        for bad in ("pane", "", "Headless", None):
            with self.subTest(bad=bad):
                self.refused(api.ConfigError, f"mode must be headless or tui, got {bad!r}",
                             lambda: self.start(mode=bad))

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(api.ConfigError, f"idle_timeout must be a positive number of seconds, got {bad!r}",
                             lambda: self.start(idle_timeout=bad))

    def test_refuses_a_resume_that_is_no_session_id(self):
        for bad in ("", 1, b"x" * 100):
            with self.subTest(bad=bad):
                self.refused(api.ConfigError, f"resume must be a session id, got {bad!r:.80}",
                             lambda: self.start(resume=bad))

    def test_refuses_a_resume_other_than_spec_sid(self):
        self.refused(api.ConfigError, "resume r1 differs from spec.sid s1",
                     lambda: self.start(spec=self.spec(sid="s1"), resume="r1"))

    def test_a_registry_error_passes_through(self):
        self.refused(api.ConfigError, f"no [adapters.nope] table in {registry.CONFIG} or {registry.LOCAL}",
                     lambda: self.start(runtime="nope"))
        self.configure(model="m")
        self.refused(api.ConfigError, "unknown key 'model' in FakeAdapter's config", lambda: self.start())

    def test_tui_on_a_runtime_without_it_is_unsupported(self):
        self.refused(api.ConfigError, "runtime 'fake' does not support tui mode", lambda: self.start(mode="tui"))

    def test_tui_on_a_runtime_with_it_is_not_available_yet(self):
        self.configure(tui=True)
        self.refused(api.ConfigError, "tui mode is not available yet", lambda: self.start(mode="tui"))
        self.refused(api.ConfigError, "tui mode is not available yet", lambda: self.start(mode="tui", name="w1"))

    def test_headless_takes_no_name(self):
        self.refused(api.ConfigError, "name: tui mode only", lambda: self.start(name="w1"))

    def test_refuses_a_launch_without_a_command(self):
        self.refused(api.ConfigError, "runtime 'blank' gave no command", lambda: self.start(runtime="blank"))

    def test_an_exception_from_launch_propagates(self):
        self.refused(LookupError, "no binding", lambda: self.start(runtime="failing"))

    def test_a_failed_session_record_propagates_before_the_start(self):
        os.mkdir(self.workdir)
        target = os.path.join(self.dir, "elsewhere")
        os.symlink(target, records.path(self.workdir))
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen:
            with self.assertRaises(OSError):
                self.start()
        popen.assert_not_called()
        self.assertFalse(os.path.exists(target))


class SpawnTest(Base):
    def test_a_spawn_failure_is_a_driver_error_after_the_session_record(self):
        for error in (OSError("no exec"), ValueError("bad argument")):
            with self.subTest(error=error):
                self.workdir = os.path.join(self.dir, type(error).__name__)
                with mock.patch("subprocess.Popen", side_effect=error) as popen:
                    with self.assertRaises(api.DriverError) as caught:
                        self.start()
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                popen.assert_called_once()
                self.assertEqual([e["kind"] for e in self.written()], ["session"])


class SidTest(Base):
    def test_resume_continues_that_session(self):
        handle, argv = self.run_once(resume="r1")
        self.assertEqual(handle.sid, "r1")
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--sid") + 1], "r1")
        self.assertEqual(self.written()[0]["sid"], "r1")

    def test_resume_may_repeat_spec_sid(self):
        handle, argv = self.run_once(spec=self.spec(sid="s1"), resume="s1")
        self.assertEqual(handle.sid, "s1")
        self.assertIn("--resume", argv)

    def test_spec_sid_starts_a_new_session(self):
        handle, argv = self.run_once(spec=self.spec(sid="s1"))
        self.assertEqual(handle.sid, "s1")
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[argv.index("--sid") + 1], "s1")
        self.assertEqual(self.written()[0]["sid"], "s1")

    def test_neither_picks_a_uuid4(self):
        handle, argv = self.run_once()
        self.assertEqual(uuid.UUID(handle.sid).version, 4)
        self.assertEqual(str(uuid.UUID(handle.sid)), handle.sid)
        self.assertEqual(argv[argv.index("--sid") + 1], handle.sid)
        self.assertEqual(self.written()[0]["sid"], handle.sid)


class RuntimeNoteTest(Base):
    def test_with_commands_the_note_follows_the_prompt(self):
        _, argv = self.run_once(spec=self.spec(access=Access(commands=("git",))))
        self.assertEqual(argv[-1], "hello\n\nFAKE NOTE")

    def test_without_commands_the_prompt_is_unchanged(self):
        _, argv = self.run_once()
        self.assertEqual(argv[-1], "hello")

    def test_an_empty_note_leaves_the_prompt_unchanged(self):
        _, argv = self.run_once(runtime="quiet", spec=self.spec(access=Access(commands=("git",))))
        self.assertEqual(argv[-1], "hello")


class HandleTest(Base):
    def test_send_and_screen_are_tui_only(self):
        handle = self.start()
        for call, message in ((lambda: handle.send("x"), "send: tui mode only"),
                              (handle.screen, "screen: tui mode only")):
            with self.subTest(message=message), self.assertRaises(api.DriverError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.drain(handle), [api.Exited(0)])

    def test_state_is_live_while_the_process_runs_then_absent(self):
        self.steps({"sleep": 1})
        handle = self.start()
        self.assertEqual(handle.state(), "live")
        self.assertEqual(self.drain(handle), [api.Exited(0)])
        self.assertEqual(handle.state(), "absent")

    def test_attributes_info_reply_and_usage(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        handle = self.start(spec=self.spec(sid="s1", access=Access(cwd=cwd, project=True)))
        self.assertEqual((handle.sid, handle.runtime, handle.mode), ("s1", "fake", "headless"))
        info = api.SessionInfo("s1", "", cwd, True, os.path.join(self.workdir, "fake-s1.jsonl"), "fake --resume s1")
        self.assertEqual(handle.info(), info)
        self.assertIsNone(handle.reply)
        self.assertIsNone(handle.usage())
        self.assertEqual(self.drain(handle), [api.Exited(0)])
        (session,) = self.written()
        self.assertEqual({k: v for k, v in session.items() if k != "ts"},
                         {"kind": "session", "sid": "s1", "cwd": cwd, "project": True,
                          "transcript": info.transcript, "resume": info.resume})

    def test_attach_is_not_available_yet(self):
        with self.assertRaises(api.ConfigError) as caught:
            api.attach("w1")
        self.assertEqual(str(caught.exception), "tui mode is not available yet")


class ExportsTest(unittest.TestCase):
    def test_reexports_and_constants(self):
        for name in ("Access", "Spec", "SessionInfo", "ConfigError", "SessionBound", "Activity", "Reply", "TurnEnd",
                     "Blocked", "Exited", "Event", "Stderr"):
            with self.subTest(name=name):
                self.assertIs(getattr(api, name), getattr(base, name))
        self.assertIs(api.records, records)
        self.assertEqual((api.MODES, api.STATES, api.POLL), (("headless", "tui"), ("live", "absent", "unknown"), 0.5))
        self.assertEqual(api.IdleTimeout(0.3).seconds, 0.3)


if __name__ == "__main__":
    unittest.main()
