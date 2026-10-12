"""new/core/src/driver/modes/headless.py: a Headless's command on pipes, its state, its process group's kill,
bounded waits, send and screen refused, and probe(), over real Python children."""

from new.tests.case import Case

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from agent_pm.adapters.base import Adapter, Headless, Launch, Reply, Stderr
from agent_pm.driver.errors import DriverError
from agent_pm.driver.modes import headless
from agent_pm.driver.modes.headless import HeadlessMode

WAIT = 10  # upper bound on any wait, in seconds
PROBE_TIMEOUT = 2  # probe()'s timeout where it must pass, in seconds
SLEEPER = "import time; time.sleep(60)"


def python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", textwrap.dedent(code))


def spawning(then: str = "") -> str:
    """Child code: starts a grandchild running SLEEPER on the child's pipes, prints its pid as {"text"}, then runs
    `then`."""
    return (
        "import json, subprocess, sys, time\n"
        f"grandchild = subprocess.Popen([sys.executable, '-I', '-c', {SLEEPER!r}])\n"
        "print(json.dumps({'text': str(grandchild.pid)}), flush=True)\n" + then
    )


def kill(pid: int) -> None:
    """SIGKILL to a test's stray process, if it still runs."""
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def gone(pid: int) -> bool:
    """Whether pid has exited: no such process, or a zombie its new parent has not reaped yet."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = subprocess.run(
        ("ps", "-o", "stat=", "-p", str(pid)), capture_output=True, text=True
    ).stdout
    return stat.strip().startswith("Z")


def until(check) -> bool:
    """Whether check() holds within WAIT s."""
    deadline = time.monotonic() + WAIT
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class StubAdapter(Adapter):
    def headless(self):
        return StubRun(self)

    def tui(self):
        return None


class StubRun(Headless):
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


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.mode = HeadlessMode(StubAdapter({}).headless())

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

    def first(self):
        """The next event, polled for up to WAIT s."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            events, _ = self.mode.poll(0.1)
            if events:
                return events[0]
        self.fail(f"no event within {WAIT}s")

    def grandchild(self, then: str = "") -> None:
        """begin(spawning(then)) up to the grandchild's pid; the grandchild is killed at cleanup."""
        self.begin(spawning(then))
        self.addCleanup(kill, int(self.first().text))

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
    def test_stdout_lines_become_the_runs_events_and_stderr_lines_stderr_ended_after_stdout_then_stderr(
        self,
    ):
        calls = []

        def popen(*args, **kwargs):
            calls.append(kwargs)
            return subprocess.Popen(*args, **kwargs)

        self.begin(
            """
            import json, os, sys, time
            print(json.dumps({"text": os.getcwd()}))
            print("not json")
            print(json.dumps({"diag": os.environ["HM_PROBE"]}))
            sys.stdout.flush()
            os.close(1)
            time.sleep(0.3)
            sys.stderr.buffer.write(b"bad \\xff\\n")
            sys.exit(3)
        """,
            env={"HM_PROBE": "probe"},
            popen=popen,
        )
        found = self.drain()
        late = Stderr("bad \ufffd")
        self.assertEqual(
            [e for e in found if e != late], [Reply(os.path.realpath(self.dir)), Stderr("probe")]
        )
        self.assertEqual(found.count(late), 1)
        self.assertIs(calls[0]["stdin"], subprocess.DEVNULL)
        self.assertEqual(calls[0]["process_group"], 0)
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 3)
        self.assertEqual(self.mode.state(), "absent")
        self.mode.stop()
        self.assertEqual(self.mode.returncode(0), 3)

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
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)

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
        self.assertEqual(self.mode.state(), "live")
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.state(), "absent")
        self.assertLess(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.assertEqual(self.drain(), [])
        self.promptly(self.mode.stop)

    def test_kills_the_group_so_a_grandchild_on_the_pipes_ends_them(self):
        self.grandchild("time.sleep(60)\n")
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)
        self.assertEqual(self.drain(), [])

    def test_kills_the_group_after_the_child_exited_while_a_grandchild_holds_the_pipes(self):
        self.grandchild()
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.assertEqual(self.mode.poll(0.2), ([], False))
        self.promptly(self.mode.stop)
        self.assertEqual(self.drain(), [])

    def test_falls_back_to_killing_the_process_when_killpg_fails(self):
        for error in (ProcessLookupError, PermissionError):
            with self.subTest(error=error.__name__):
                self.mode = HeadlessMode(StubAdapter({}).headless())
                self.begin(SLEEPER)
                with mock.patch("os.killpg", side_effect=error) as killpg:
                    self.promptly(self.mode.stop)
                killpg.assert_called_once_with(self.mode.proc.pid, signal.SIGKILL)
                self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)

    def test_waits_at_most_stop_wait_and_ignores_a_vanished_process(self):
        self.begin(SLEEPER)
        with (
            mock.patch("os.killpg", side_effect=ProcessLookupError),
            mock.patch.object(self.mode.proc, "kill", side_effect=ProcessLookupError) as killed,
            mock.patch.object(headless, "STOP_WAIT", 0.3),
        ):
            began = time.monotonic()
            self.promptly(self.mode.stop)
            self.assertGreaterEqual(time.monotonic() - began, 0.3)
        killed.assert_called_once_with()
        self.assertEqual(self.mode.state(), "live")

    def test_skips_killpg_once_the_child_is_reaped_and_both_pipes_ended(self):
        self.begin("pass")
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        with mock.patch("os.killpg") as killpg:
            self.promptly(self.mode.stop)
        killpg.assert_not_called()
        self.assertEqual(self.mode.returncode(0), 0)

    def test_is_a_no_op_before_begin_and_after_a_failed_begin(self):
        self.mode.stop()
        self.assertEqual(self.mode.state(), "absent")
        with self.assertRaises(FileNotFoundError):
            self.mode.begin(
                (sys.executable, "-c", ""),
                cwd=os.path.join(self.dir, "missing"),
                env=dict(os.environ),
            )
        self.mode.stop()
        self.assertEqual(self.mode.state(), "absent")


class TuiOnly(Base):
    def test_send_and_screen_raise_driver_error_before_and_after_begin(self):
        for when in ("before", "after"):
            if when == "after":
                self.begin(SLEEPER)
            for call, message in (
                (lambda: self.mode.send("x"), "send: tui mode only"),
                (self.mode.screen, "screen: tui mode only"),
                (lambda: self.mode.screen(5), "screen: tui mode only"),
            ):
                with (
                    self.subTest(when=when, message=message),
                    self.assertRaises(DriverError) as caught,
                ):
                    call()
                self.assertEqual(str(caught.exception), message)


class Returncode(Base):
    def test_none_while_the_child_runs_after_waiting_at_most_timeout(self):
        self.begin(SLEEPER)
        began = time.monotonic()
        self.assertIsNone(self.promptly(lambda: self.mode.returncode(0.3)))
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertIsNone(self.promptly(lambda: self.mode.returncode(0)))


class Probe(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def probe(self, argv, timeout=WAIT, env=None):
        """headless.probe(argv) in self.dir, the Popen call's mock and the processes it made."""
        made, real = [], subprocess.Popen

        def popen(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        with mock.patch("subprocess.Popen", side_effect=popen) as spy:
            out = headless.probe(
                argv, cwd=self.dir, env={**os.environ, **(env or {})}, timeout=timeout
            )
        return out, spy, made

    def test_its_stdout_alone_run_in_cwd_with_env_leading_its_own_group(self):
        out, spy, _ = self.probe(
            python("""
            import os, sys
            print(os.getcwd())
            print(os.environ["HM_PROBE"])
            print(os.getpgrp() == os.getpid())
            sys.stderr.write("noise\\n")
        """),
            env={"HM_PROBE": "probe"},
        )
        self.assertEqual(out, f"{os.path.realpath(self.dir)}\nprobe\nTrue\n")
        self.assertIs(spy.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(spy.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_a_spawn_error_is_none(self):
        for argv in ((os.path.join(self.dir, "missing"),), (sys.executable, "-c", "pass\0")):
            with self.subTest(argv=argv):
                self.assertIsNone(self.probe(argv)[0])

    def test_after_timeout_its_group_is_killed_it_is_reaped_and_the_result_none(self):
        pids = os.path.join(self.dir, "grandchild.json")
        out, _, made = self.probe(
            python(f"""
            import json, os, subprocess, sys, time
            grandchild = subprocess.Popen([sys.executable, "-I", "-c", {SLEEPER!r}])
            with open({pids!r} + ".tmp", "w") as f:
                json.dump(grandchild.pid, f)
            os.replace({pids!r} + ".tmp", {pids!r})
            print("partial", flush=True)
            time.sleep(60)
        """),
            timeout=PROBE_TIMEOUT,
        )
        with open(pids, encoding="utf-8") as f:
            grandchild = json.load(f)
        self.addCleanup(kill, grandchild)
        self.assertIsNone(out)
        self.assertEqual([proc.returncode for proc in made], [-signal.SIGKILL])
        self.assertTrue(until(lambda: gone(grandchild)), "grandchild still runs")


if __name__ == "__main__":
    unittest.main()
