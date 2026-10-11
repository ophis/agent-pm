"""new/core/src/driver/modes/headless_run.py: a HeadlessRun's command on pipes, over real Python children."""
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from new.tests import hermetic  # noqa: F401
from adapters.base import Adapter, HeadlessRun, Launch, Reply, Stderr
from driver.modes.headless_run import HeadlessMode

WAIT = 10   # upper bound on any wait, in seconds


class StubAdapter(Adapter):
    def headless_run(self):
        return StubRun(self)

    def tui_run(self):
        return None


class StubRun(HeadlessRun):
    """parse: {"text": t} → Reply(t), {"diag": t} → Stderr(t), {"raise": t} → RuntimeError(t)."""

    def launch(self, prompt, spec, resume):
        return Launch((sys.executable,))

    def parse(self, record):
        if "raise" in record:
            raise RuntimeError(record["raise"])
        if "diag" in record:
            yield Stderr(record["diag"])
        else:
            yield Reply(record["text"])


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.mode = HeadlessMode(StubAdapter({}).headless_run())

    def begin(self, code: str, env: dict | None = None, **kwargs):
        argv = (sys.executable, "-I", "-c", textwrap.dedent(code))
        self.mode.begin(argv, cwd=self.dir, env={**os.environ, **(env or {})}, **kwargs)
        self.addCleanup(self.mode.stop)

    def drain(self) -> list:
        """Every event up to both pipes' EOF."""
        found, deadline = [], time.monotonic() + WAIT
        while time.monotonic() < deadline:
            events, ended = self.mode.poll(0.1)
            found += events
            if ended:
                return found
        self.fail(f"not ended within {WAIT}s, got {found}")

    def promptly(self, call):
        """call()'s result or exception; fails instead of hanging when it blocks."""
        out = {}

        def run():
            try:
                out["value"] = call()
            except BaseException as e:
                out["error"] = e
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(WAIT)
        if thread.is_alive():
            self.fail("blocked")
        if "error" in out:
            raise out["error"]
        return out["value"]


class Streams(Base):
    def test_stdout_lines_become_the_runs_events_and_stderr_lines_stderr_ended_after_stdout_then_stderr(self):
        calls = []

        def popen(*args, **kwargs):
            calls.append(kwargs)
            return subprocess.Popen(*args, **kwargs)
        self.begin("""
            import json, os, sys, time
            print(json.dumps({"text": os.getcwd()}))
            print("not json")
            print(json.dumps({"diag": os.environ["HM_PROBE"]}))
            sys.stdout.flush()
            os.close(1)
            time.sleep(0.3)
            sys.stderr.buffer.write(b"bad \\xff\\n")
            sys.exit(3)
        """, env={"HM_PROBE": "probe"}, popen=popen)
        found = self.drain()
        late = Stderr("bad \ufffd")
        self.assertEqual([e for e in found if e != late], [Reply(os.path.realpath(self.dir)), Stderr("probe")])
        self.assertEqual(found.count(late), 1)
        self.assertIs(calls[0]["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.promptly(self.mode.returncode), 3)
        self.assertFalse(self.mode.alive())
        self.mode.stop()
        self.assertEqual(self.mode.returncode(), 3)

    def test_ended_only_after_stderr_then_stdout(self):
        self.begin("""
            import json, os, sys, time
            sys.stderr.write("first\\n")
            sys.stderr.flush()
            os.close(2)
            time.sleep(0.3)
            print(json.dumps({"text": "late"}))
        """)
        found = self.drain()
        self.assertCountEqual(found, [Stderr("first"), Reply("late")])
        self.assertEqual(self.promptly(self.mode.returncode), 0)

    def test_an_exception_from_parse_is_raised_from_poll(self):
        self.begin("""
            import time
            print('{"raise": "boom"}', flush=True)
            time.sleep(60)
        """)
        deadline = time.monotonic() + WAIT
        with self.assertRaisesRegex(RuntimeError, "boom"):
            while time.monotonic() < deadline:
                self.mode.poll(0.1)


class Stop(Base):
    def test_kills_a_sleeping_child_whose_pipes_then_end(self):
        self.begin("import time; time.sleep(60)")
        self.assertTrue(self.mode.alive())
        self.promptly(self.mode.stop)
        self.assertFalse(self.mode.alive())
        self.assertLess(self.promptly(self.mode.returncode), 0)
        self.assertEqual(self.drain(), [])
        self.promptly(self.mode.stop)

    def test_is_a_no_op_before_begin_and_after_a_failed_begin(self):
        self.mode.stop()
        self.assertFalse(self.mode.alive())
        with self.assertRaises(FileNotFoundError):
            self.mode.begin((sys.executable, "-c", ""), cwd=os.path.join(self.dir, "missing"), env=dict(os.environ))
        self.mode.stop()
        self.assertFalse(self.mode.alive())


if __name__ == "__main__":
    unittest.main()
