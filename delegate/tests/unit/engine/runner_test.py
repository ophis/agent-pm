import dataclasses
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime
from unittest import mock

from agent_pm.delegate import (
    Begun,
    ConfigError,
    Finished,
    Limit,
    Progress,
    Result,
    Spec,
    composer,
    run,
)
from agent_pm.delegate.engine import runner
from agent_pm.delegate.engine.runner import FIX, IDLE_TIMEOUT, NUDGE, POLICIES, STOP_LIMIT
from agent_pm.delegate.engine.types import Stopped
from agent_pm.session import MODES, Activity, Blocked, DriverError, Exited, IdleTimeout, LimitHit
from agent_pm.utils import printable

from delegate.tests.case import Case
from delegate.tests.replies import bare, block, running
from delegate.tests.unit.runs import (
    DONE,
    GAVE_UP,
    NAME,
    START,
    STOP,
    FakeHandle,
    limit,
    no_child,
    said,
    turn,
)
from session.tests import fake_adapter

SID = "11111111-2222-3333-4444-555555555555"


NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}


FAILED = {**DONE, "status": "failed", "deliverable": ""}


INVALID = {**DONE, "status": "maybe"}


INVALID_ERROR = "status 'maybe' is not one of done, needs_input, failed"


GH = {"type": "github", "repo": "o/docs", "branch": "main", "dir": "R/"}


PR = {"type": "pull-request"}


def ended(text) -> dict:
    """A step printing a result record (TurnEnd) of `text`."""
    return {"out": {"type": "result", "text": text}}


PENDING = turn("Waiting for the build.", pending=1)


class Base(Case):
    """dummy-tester's echo over the fake runtime in workdir `work` (not made), out `out`, the cwd
    the workdir.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.channel = os.path.join(self.work, "run.jsonl")
        self.scenario, self.log = (
            os.path.join(self.tmp, name) for name in ("scenario.json", "runtime.log")
        )
        sys.modules["agent_pm.session.adapters.fake"] = fake_adapter
        self.addCleanup(sys.modules.pop, "agent_pm.session.adapters.fake", None)
        self.configure_session(
            {
                "fake": {
                    "implementation": "adapters.fake:FakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )

    def go(self, *steps, fixes=None, output=None, on_event=None, **kw) -> Result:
        """run() over the fake with `steps` (`fixes`: its steps on --resume); self.events gets each
        callback, self.err the stderr.
        """
        with open(self.scenario, "w") as f:
            json.dump(
                {"steps": list(steps), **({"resume": list(fixes)} if fixes is not None else {})}, f
            )
        config = composer.resolve("dummy-tester")
        config = replace(config, output=output) if output else config
        self.events, err = [], io.StringIO()

        def on(event):
            self.events.append(event)
            if on_event:
                on_event(event)

        kw = {
            "runtime": "fake",
            "mode": "headless",
            "out": self.out,
            "cwd": self.work,
            "input": "Echo me.",
            **kw,
        }
        try:
            with redirect_stderr(err):
                return run(
                    config,
                    Spec(kw.pop("input"), self.work, task="echo", **kw),
                    on_event=on,
                    driver=self.driver,
                )
        finally:
            self.err = err.getvalue()

    def records(self) -> list[dict]:
        with open(self.channel) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def launches(self) -> list[dict]:
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def read(self, path) -> str:
        with open(path) as f:
            return f.read()

    def write(self, path, text) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def fresh(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def spy_save(self):
        patch = mock.patch("os.replace", wraps=os.replace)
        self.addCleanup(patch.stop)
        return patch.start()


LEFT_OPEN = f"session {NAME} left open: tmux attach -t '={NAME}'\n"


class Configures(Base):
    def test_the_callers_settings_reach_the_run(self):
        r = self.go(said(START, block(DONE)))
        self.assertEqual((r.outcome.status, len(self.launches())), ("done", 1))

    def test_a_fresh_driver_without_the_runtime_starts_nothing(self):
        self.configure_session({})
        with self.assertRaisesRegex(ConfigError, r"\[adapters.fake\]"):
            self.go(said(START, block(DONE)))
        self.assertFalse(os.path.exists(self.log))


class Callbacks(Base):
    def test_begun_then_a_progress_per_mark_then_finished(self):
        r = self.go(
            said(START, "Working.", "[agent-pm-progress:round] half way"), said(block(DONE))
        )
        sid = self.records()[0]["sid"]
        self.assertEqual(
            self.events,
            [Begun(sid), Progress("start", "go"), Progress("round", "half way"), Finished(r)],
        )
        self.assertEqual((r.returncode, r.outcome.status, r.error, r.limit), (0, "done", "", None))

    def test_a_turn_end_repeating_the_last_reply_is_read_once(self):
        text = "\n".join((START, block(DONE)))
        r = self.go(said(text), ended(text), ended(""))
        self.assertEqual(
            ([type(e) for e in self.events], r.outcome.status),
            ([Begun, Progress, Finished], "done"),
        )
        self.assertEqual((self.kinds().count("progress"), self.err.count(START)), (1, 1))

    def test_a_differing_turn_end_is_a_reply(self):
        r = self.go(said(START), ended("[agent-pm-progress:round] two\n" + block(DONE)))
        self.assertEqual(self.events[1:3], [Progress("start", "go"), Progress("round", "two")])
        self.assertEqual(r.outcome.status, "done")

    def test_a_deliverable_file_is_read_in_and_saved_to_out(self):
        self.write(os.path.join(self.work, "notes", "doc.md"), "# From a file\n")
        data = {k: v for k, v in DONE.items() if k != "deliverable"} | {
            "deliverable_file": "notes/doc.md"
        }
        r = self.go(said(START, block(data)))
        self.assertEqual(
            (r.outcome.status, r.outcome.deliverable, r.outcome.url),
            ("done", "# From a file\n", self.out),
        )
        self.assertEqual(self.read(self.out), "# From a file\n")
        fields = {f.name for f in dataclasses.fields(r.outcome)} - {"deliverable"}
        self.assertEqual(set(self.last("result")["outcome"]), fields)


class Fix(Base):
    """FR-21: one fix resume of the same session after an invalid outcome block, only when the
    client exited 0.
    """

    def test_an_invalid_block_is_fixed_by_one_resume_of_the_session(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(DONE))])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        first, second = (launch["argv"] for launch in self.launches())
        sid = self.events[0].sid
        self.assertEqual((first[first.index("--sid") + 1], "--resume" in first), (sid, False))
        self.assertEqual((second[second.index("--sid") + 1], "--resume" in second), (sid, True))
        self.assertEqual(second[second.index("--") + 1], FIX.format(error=INVALID_ERROR))
        self.assertEqual([type(e) for e in self.events], [Begun, Progress, Finished])
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )
        self.assertEqual(self.last("invalid")["error"], INVALID_ERROR)
        self.assertIn(f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err)
        self.assertEqual(self.read(self.out), "# Doc\n")

    def test_an_invalid_block_twice_is_the_error(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(INVALID))])
        self.assertEqual(r, Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(
            (len(self.launches()), [type(e) for e in self.events]), (2, [Begun, Progress])
        )
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )
        self.assertEqual(bare(self.last("result")), {"kind": "result", "error": r.error})

    def test_no_block_is_the_error_without_a_resume(self):
        r = self.go(said(START, "bye"))
        self.assertEqual(
            (r, len(self.launches())), (Result(0, None, "the agent run returned no outcome"), 1)
        )

    def test_an_invalid_block_from_a_failed_client_is_not_resumed(self):
        r = self.go(said(START, block(INVALID)), {"exit": 3})
        self.assertEqual((r, len(self.launches())), (Result(3, None, "the client exited 3"), 1))
        self.assertNotIn("invalid", self.kinds())

    def test_a_fix_resume_that_cannot_start_is_the_error(self):
        real, calls = subprocess.Popen, []

        def popen(*args, **kw):
            calls.append(args)
            if len(calls) > 1:
                raise FileNotFoundError(2, "No such file or directory", "x")
            return real(*args, **kw)

        with mock.patch("subprocess.Popen", popen):
            r = self.go(said(START, block(INVALID)))
        self.assertEqual((r.returncode, r.outcome), (1, None))
        self.assertTrue(
            r.error.startswith(f"invalid outcome: {INVALID_ERROR}; resume failed: cannot start "),
            r.error,
        )
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )


class Idle(Base):
    def test_silence_longer_than_the_idle_timeout_is_recorded_and_the_run_goes_on(self):
        self.assertEqual(IDLE_TIMEOUT, 2 * 60 * 60)
        start = self.driver.start
        with mock.patch.object(
            self.driver,
            "start",
            side_effect=lambda *a, **kw: start(*a, **{**kw, "idle_timeout": 0.3}),
        ):
            r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})


class UsageLimit(Base):
    """FR-58: a usage limit stops the session and ends the run: no outcome read, no fix resume, no
    Finished.
    """

    def test_a_limit_stops_the_session_and_is_the_result(self):
        at = 1767225600.0
        began = time.monotonic()
        r = self.go(said(START), limit(at), {"sleep": 30}, said(block(DONE)))
        self.assertLess(time.monotonic() - began, 20)
        self.assertFalse(running(self.launches()[0]["pid"]))
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        self.assertEqual(
            r, Result(-signal.SIGKILL, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(r.limit.resets_at, at)
        self.assertEqual(self.kinds(), ["session", "input", "progress", "limit", "result", "end"])
        self.assertEqual((self.last("limit")["resets_at"], len(self.launches())), (at, 1))
        self.assertEqual([type(e) for e in self.events], [Begun, Progress])

    def test_an_unknown_reset_time(self):
        r = self.go(said(START, block(INVALID)), limit(None))
        self.assertEqual(
            (r.outcome, r.error, r.limit), (None, "usage limit; reset time unknown", Limit(None))
        )
        self.assertEqual((self.last("limit")["resets_at"], len(self.launches())), (None, 1))


class Stops(Base):
    """
    Main's HostFailure cases: a session stopped on an exception, its record closed `stopped: …`.
    """

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        failure = RuntimeError("boom")

        def boom(event):
            if isinstance(event, Progress):
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.go(said(START), {"sleep": 30}, said(block(DONE)), on_event=boom)
        self.assertIs(caught.exception, failure)
        self.assertFalse(running(self.launches()[0]["pid"]))
        result, end = self.records()[-2:]
        self.assertEqual((result["error"], end["rc"]), ("stopped: RuntimeError: boom", 1))

    def test_an_exception_ends_the_record_stopped(self):
        for error, rc, text in (
            (Stopped(128 + signal.SIGTERM), 143, "stopped: Stopped: 143"),
            (SystemExit(129), 129, "stopped: SystemExit: 129"),
            (SystemExit("bye"), 1, "stopped: SystemExit: bye"),
            (KeyboardInterrupt(), 1, "stopped: KeyboardInterrupt"),
            (RuntimeError("one\ntwo"), 1, "stopped: RuntimeError: one two"),
        ):

            def raise_(event, error=error):
                if isinstance(event, Progress):
                    raise error

            with self.subTest(error=error):
                self.fresh()
                with self.assertRaises(type(error)) as cm:
                    self.go(said(START), {"sleep": 30}, on_event=raise_)
                self.assertIs(cm.exception, error)
                self.assertFalse(running(self.launches()[-1]["pid"]))
                self.assertEqual(self.kinds(), ["session", "input", "progress", "result", "end"])
                result, end = self.records()[-2:]
                self.assertEqual(
                    (bare(result), bare(end)),
                    (
                        {"kind": "result", "error": text},
                        {"kind": "end", "sid": end["sid"], "rc": rc},
                    ),
                )

    def test_the_closing_writes_never_mask_the_runs_exception(self):
        def swap(_event):
            os.remove(self.channel)
            os.mkdir(self.channel)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.go({"sleep": 30}, on_event=swap)
        self.assertEqual(len(re.findall(r"^delegate: .*run\.jsonl", self.err, re.M)), 2, self.err)

    def test_a_spawn_failure_is_the_result(self):
        with mock.patch(
            "subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "x")
        ):
            r = self.go(said(START, block(DONE)))
        self.assertEqual((r.returncode, r.outcome, self.events), (1, None, []))
        self.assertTrue(r.error.startswith("headless: cannot start "), r.error)
        self.assertEqual(self.kinds(), ["session", "input", "result", "end"])
        self.assertEqual((self.last("result")["error"], self.last("end")["rc"]), (r.error, 1))

    def test_a_driver_refusal_is_a_config_error_and_nothing_is_recorded(self):
        with self.assertRaises(ConfigError) as cm:
            self.go(said(START, block(DONE)), runtime="nope")
        self.assertIn("nope", str(cm.exception))
        self.assertEqual(
            (os.path.lexists(self.channel), os.path.lexists(self.log), self.events),
            (False, False, []),
        )


class InProcess(Base):
    """The in_process-only fake, its run with no child process."""

    def setUp(self):
        super().setUp()
        self.configure_session(
            {
                "fake": {
                    "implementation": "adapters.fake:InProcessFakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )

    def go(self, *steps, **kw) -> Result:
        with no_child():
            return super().go(*steps, mode="in_process", **kw)

    def test_a_policy_for_each_mode_in_process_the_headless_one(self):
        self.assertEqual(set(POLICIES), set(MODES))
        self.assertIs(POLICIES["in_process"], POLICIES["headless"])

    def test_a_done_run(self):
        r = self.go(said(START, block(DONE)))
        sid = self.events[0].sid
        self.assertEqual(self.events, [Begun(sid), Progress("start", "go"), Finished(r)])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        self.assertEqual(self.kinds(), ["session", "input", "progress", "result", "end"])
        self.assertEqual((self.read(self.out), self.launches()[0]["pid"]), ("# Doc\n", os.getpid()))

    def test_a_raise_is_the_client_exiting_1_its_traceback_on_stderr(self):
        r = self.go(said(START), {"raise": "boom"})
        self.assertEqual(r, Result(1, None, "the client exited 1"))
        self.assertEqual(
            ([type(e) for e in self.events], self.last("end")["rc"]), ([Begun, Progress], 1)
        )
        lines = self.err.splitlines()
        self.assertLess(
            lines.index("Traceback (most recent call last):"), lines.index("RuntimeError: boom")
        )
        r = self.go(said(START, block(DONE)), {"raise": "boom"})
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (1, "done", "the client exited 1")
        )

    def test_silence_longer_than_the_idle_timeout_is_recorded_and_the_run_goes_on(self):
        start = self.driver.start
        with mock.patch.object(
            self.driver,
            "start",
            side_effect=lambda *a, **kw: start(*a, **{**kw, "idle_timeout": 0.3}),
        ):
            r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})

    def test_an_invalid_block_is_fixed_by_one_resume_of_the_session(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(DONE))])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        first, second = self.launches()
        sid = self.events[0].sid
        self.assertEqual(
            (first["sid"], first["resume"], second["sid"], second["resume"]),
            (sid, False, sid, True),
        )
        self.assertEqual(second["prompt"], FIX.format(error=INVALID_ERROR))
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )


class Terminal(Base):
    """Main's Sinks case: what reaches the terminal is printable."""

    def test_printable_is_the_utils_one(self):
        self.assertIs(runner.printable, printable)

    def test_replies_progress_and_stderr_lines_are_printed_printable(self):
        self.go(
            {"err": "w\x1b[2Jarn"},
            said(
                "a\x1b]52;c;ZXZpbA==\x07b\tc",
                "",
                "[agent-pm-progress:round] x\x1b[2J",
                START,
                block(DONE),
            ),
        )
        lines = self.err.splitlines()
        for want in (
            "w[2Jarn",
            "a]52;c;ZXZpbA==b\tc",
            "[agent-pm-progress:round] x[2J",
            "Progress (round): x[2J",
            "Progress (start): go",
        ):
            self.assertIn(want, lines)
        self.assertLess(lines.index("a]52;c;ZXZpbA==b\tc"), lines.index("Progress (round): x[2J"))
        self.assertNotIn("", lines)
        self.assertFalse([c for c in self.err if not c.isprintable() and c not in "\t\n"])


class Start(Base):
    """Main's drive_test Start cases."""

    def test_the_record_is_session_input_the_marks_result_and_end(self):
        r = self.go(said(START, block(DONE)), input="  Answers:\n- B\n")
        records, sid = self.records(), self.events[0].sid
        self.assertEqual(
            [e["kind"] for e in records], ["session", "input", "progress", "result", "end"]
        )
        mine = records[1:]
        self.assertEqual(
            [list(e) for e in mine],
            [
                ["ts", "kind", "text"],
                ["ts", "kind", "name", "text"],
                ["ts", "kind", "outcome"],
                ["ts", "kind", "sid", "rc"],
            ],
        )
        for e in mine:
            self.assertIsNotNone(datetime.fromisoformat(e["ts"]).utcoffset(), e["ts"])
        kept = {k: v for k, v in dataclasses.asdict(r.outcome).items() if k != "deliverable"}
        self.assertEqual(
            [bare(e) for e in mine],
            [
                {"kind": "input", "text": "  Answers:\n- B\n"},
                {"kind": "progress", "name": "start", "text": "go"},
                {"kind": "result", "outcome": kept},
                {"kind": "end", "sid": sid, "rc": 0},
            ],
        )
        self.assertEqual(
            (records[0]["sid"], kept["url"], r.outcome.deliverable), (sid, self.out, "# Doc\n")
        )
        self.assertEqual(sorted(os.listdir(self.work)), ["out.md", "run.jsonl"])

    def test_a_destination_decides_the_url_and_each_saves_a_deliverable(self):
        url, pr_url = "https://github.com/o/docs/blob/main/x.md", "https://github.com/o/n/pull/7"
        for output, data, want_url, saved in (
            ({"type": "local"}, DONE, self.out, "# Doc\n"),
            ({"type": "orchestrator"}, DONE, "", "# Doc\n"),
            (GH, {**DONE, "url": url}, url, "# Doc\n"),
            (PR, {**DONE, "url": pr_url}, pr_url, "# Doc\n"),
            ({"type": "local"}, NEEDS, "", None),
            (GH, NEEDS, "", None),
        ):
            with self.subTest(output=output, status=data["status"]):
                self.fresh()
                r = self.go(said(START, block(data)), output=output)
                self.assertEqual(r.outcome.url, want_url)
                self.assertEqual(
                    self.read(self.out) if saved else os.path.lexists(self.out), saved or False
                )
                self.assertEqual(
                    self.last("result")["outcome"]["questions"], data.get("questions", [])
                )

    def test_a_failed_save_raises_where_out_is_the_deliverables_home_else_is_noted(self):
        denied = PermissionError(13, "Permission denied")
        for output in ({"type": "local"}, {"type": "orchestrator"}):
            with self.subTest(output=output):
                self.fresh()
                with mock.patch("os.replace", side_effect=denied), self.assertRaises(OSError):
                    self.go(said(START, block(DONE)), output=output)
                self.assertEqual(
                    (self.last("result")["error"], os.path.lexists(self.out)),
                    (f"stopped: PermissionError: {denied}", False),
                )
        for output, url in (
            (GH, "https://github.com/o/docs/blob/main/x.md"),
            (PR, "https://github.com/o/n/pull/7"),
        ):
            with self.subTest(output=output):
                self.fresh()
                with mock.patch("os.replace", side_effect=denied):
                    r = self.go(said(START, block({**DONE, "url": url})), output=output)
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.outcome.url, r.error), (0, "done", url, "")
                )
                self.assertEqual(self.events[-1], Finished(r))
                self.assertEqual(
                    self.kinds(), ["session", "input", "progress", "save_error", "result", "end"]
                )
                self.assertEqual(
                    bare(self.last("save_error")), {"kind": "save_error", "error": str(denied)}
                )
                self.assertIn(
                    f"delegate: cannot save the deliverable to {self.out}: {denied}\n", self.err
                )
                self.assertFalse(os.path.lexists(self.out))

    def test_without_out_nothing_is_saved(self):
        r = self.go(said(START, block(DONE)), out=None)
        self.assertEqual(
            (r.outcome.status, r.outcome.url, r.outcome.deliverable), ("done", "", "# Doc\n")
        )
        self.assertEqual(os.listdir(self.work), ["run.jsonl"])

    def test_a_stale_out_never_stands_for_the_deliverable(self):
        save_spy = self.spy_save()
        self.write(self.out, "# Doc\n")
        seen = []
        self.go(
            said(START, block(DONE)),
            on_event=lambda e: (
                seen.append(os.path.lexists(self.out)) if isinstance(e, Begun) else None
            ),
        )
        self.assertEqual((seen, save_spy.call_count, self.read(self.out)), ([False], 1, "# Doc\n"))

    def test_a_failed_client_does_not_reuse_a_previous_runs_out(self):
        self.go(said(START, block(DONE)))
        r = self.go(said(START), {"exit": 143})
        self.assertEqual(
            (r, os.path.lexists(self.out)), (Result(143, None, "the client exited 143"), False)
        )

    def test_a_driver_refusal_leaves_out_as_it_was(self):
        for name, kw in (
            ("an unknown runtime", {"runtime": "nope"}),
            ("a bad sid", {"sid": "a/b"}),
        ):
            with self.subTest(name):
                self.write(self.out, "# Earlier\n")
                with self.assertRaises(ConfigError):
                    self.go(said(START, block(DONE)), **kw)
                self.assertEqual(self.read(self.out), "# Earlier\n")

    def test_a_stale_out_that_cannot_be_removed_stops_the_session_as_the_result(self):
        blocker, procs, popen = os.path.join(self.tmp, "file"), [], subprocess.Popen
        self.write(blocker, "")

        def spawn(*args, **kw):
            procs.append(popen(*args, **kw))
            return procs[-1]

        with mock.patch("subprocess.Popen", side_effect=spawn):
            r = self.go(
                {"sleep": 30}, said(START, block(DONE)), out=os.path.join(blocker, "out.md")
            )
        self.assertEqual((r.returncode, r.outcome, self.events), (1, None, []))
        self.assertRegex(r.error, r"^cannot remove the stale out: .*Not a directory")
        self.assertEqual(
            (procs[0].returncode, self.kinds()),
            (-signal.SIGKILL, ["session", "input", "result", "end"]),
        )
        self.assertEqual(self.last("result")["error"], r.error)

    def test_a_resume_keeps_the_draft_its_session_wrote_to_out(self):
        save_spy = self.spy_save()
        self.write(self.out, "# Draft\n")
        seen = []
        r = self.go(
            said(block({**DONE, "deliverable": "# Draft\n"})),
            sid=SID,
            resume=True,
            on_event=lambda e: seen.append(self.read(self.out)) if isinstance(e, Begun) else None,
        )
        self.assertEqual(
            (seen, save_spy.call_count, self.read(self.out)), (["# Draft\n"], 0, "# Draft\n")
        )
        argv = self.launches()[0]["argv"]
        self.assertEqual(
            (argv[argv.index("--sid") + 1], "--resume" in argv, r.outcome.status),
            (SID, True, "done"),
        )

    def test_a_deliverable_the_agent_wrote_to_out_is_not_saved_again(self):
        save_spy, written = self.spy_save(), []

        def write(event):
            if isinstance(event, Begun):
                self.write(self.out, "# Doc\n")
                written.append(os.stat(self.out))

        for dest in ("local", "orchestrator"):
            with self.subTest(dest):
                r = self.go(said(START, block(DONE)), output={"type": dest}, on_event=write)
                st = os.stat(self.out)
                self.assertEqual(
                    (st.st_ino, st.st_mtime_ns), (written[-1].st_ino, written[-1].st_mtime_ns)
                )
                self.assertEqual((save_spy.called, self.read(self.out)), (False, "# Doc\n"))
                self.assertEqual(
                    (r.outcome.url, r.outcome.deliverable),
                    (self.out if dest == "local" else "", "# Doc\n"),
                )

    def test_output_replaces_unusable_files_without_following_or_waiting(
        self,
    ):
        save_spy, target = self.spy_save(), os.path.join(self.tmp, "zshrc")

        def write(text):
            return lambda: self.write(self.out, text)

        def link(text):
            def plant():
                self.write(target, text)
                os.symlink(target, self.out)

            return plant

        cases = [(f"differs: {w!r}", write(w), None) for w in ("", "# Do", "# Doc\n\n", "# Dac\n")]
        cases += [
            ("fifo", lambda: os.mkfifo(self.out), None),
            ("symlink", link("mine"), "mine"),
            ("symlink to the deliverable", link("# Doc\n"), "# Doc\n"),
        ]
        for name, plant, kept in cases:
            with self.subTest(name):
                save_spy.reset_mock()
                self.go(
                    said(START, block(DONE)),
                    on_event=lambda e, plant=plant: plant() if isinstance(e, Begun) else None,
                )
                self.assertEqual(
                    (
                        save_spy.call_count,
                        stat.S_ISREG(os.lstat(self.out).st_mode),
                        self.read(self.out),
                    ),
                    (1, True, "# Doc\n"),
                )
                if kept is not None:
                    self.assertEqual(self.read(target), kept)

    def test_a_valid_outcome_survives_a_nonzero_exit_with_its_diagnostic(self):
        r = self.go(said(START, block(DONE)), {"exit": 7})
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (7, "done", "the client exited 7")
        )
        self.assertEqual(
            ([type(e) for e in self.events], self.read(self.out)),
            ([Begun, Progress, Finished], "# Doc\n"),
        )
        saved = self.last("result")
        self.assertEqual(
            (saved["outcome"]["url"], saved["error"], self.last("end")["rc"]),
            (self.out, r.error, 7),
        )

    def test_a_run_without_a_valid_outcome_records_its_error_and_gets_no_finished(self):
        for steps, rc, error in (
            ([said("bye")], 0, "the agent run returned no outcome"),
            ([said("bye"), {"exit": 143}], 143, "the client exited 143"),
            ([said(block(INVALID)), {"exit": 143}], 143, "the client exited 143"),
        ):
            with self.subTest(steps=steps):
                self.fresh()
                r = self.go(*steps)
                self.assertEqual(
                    (r, [type(e) for e in self.events]), (Result(rc, None, error), [Begun])
                )
                result, end = self.records()[-2:]
                self.assertEqual(
                    (bare(result), bare(end)),
                    (
                        {"kind": "result", "error": error},
                        {"kind": "end", "sid": self.events[0].sid, "rc": rc},
                    ),
                )

    def test_done_or_failed_without_start_reports_it_missing(self):
        for data in (DONE, FAILED):
            with self.subTest(data["status"]):
                self.fresh()
                r = self.go(said("[agent-pm-progress:round] one", block(data)))
                self.assertEqual(r.outcome.status, data["status"])
                self.assertIn("delegate: missing progress mark: start\n", self.err)
                self.assertEqual(
                    self.kinds(), ["session", "input", "progress", "missing", "result", "end"]
                )
                self.assertEqual(bare(self.last("missing")), {"kind": "missing", "name": "start"})

    def test_no_missing_report_when_start_was_seen_needs_input_resumed_or_no_outcome(self):
        for steps, kw in (
            ([said(START, block(DONE))], {}),
            ([said(block(NEEDS))], {}),
            ([said(block(DONE))], {"sid": SID, "resume": True}),
            ([said("bye")], {}),
        ):
            with self.subTest(steps=steps, **kw):
                self.fresh()
                self.go(*steps, **kw)
                self.assertNotIn("missing", self.err)
                self.assertNotIn("missing", self.kinds())


class TuiPolicy(Base):
    """The tui policy over a FakeHandle (start patched as delegate.engine.runner sees it)."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("TUI_ATTACH_PREFIX", None)

    def faked(self, *events, fail=None, stop_fail=None, mode="tui", **kw) -> Result:
        self.handle = FakeHandle(events, fail, stop_fail)
        with mock.patch.object(self.driver, "start", side_effect=self.handle.start) as self.started:
            return self.go(mode=mode, **kw)

    def test_the_policies_values(self):
        self.assertEqual(STOP_LIMIT, 3)
        self.assertEqual(
            NUDGE,
            "Finish your task, then end your last reply with your outcome block "
            "(`agent-pm-outcome`), as your instructions' Return says. If you are waiting for "
            "background work, wait for it first.",
        )

    def test_start_hands_the_plan_to_the_driver_and_prints_the_attach_command(self):
        events = os.path.join(self.tmp, "events.log")
        for kw, want, name in (
            ({}, ({"resume": None, "events": None}, None), NAME),
            (
                {"sid": SID, "resume": True, "name": "mine", "events": events},
                ({"resume": SID, "events": events}, "mine"),
                "mine",
            ),
        ):
            with self.subTest(kw=kw):
                self.fresh()
                r = self.faked(turn(START, block(DONE)), **kw)
                args, given = self.started.call_args
                self.assertEqual((args[:2], r.outcome.status), (("fake", "tui"), "done"))
                self.assertEqual(
                    (given, args[3].sid, args[3].name),
                    (
                        {"idle_timeout": IDLE_TIMEOUT, **want[0]},
                        self.events[0].sid,
                        want[1],
                    ),
                )
                self.assertIn(f"delegate: tui session {name}: tmux attach -t '={name}'\n", self.err)

    def test_a_valid_outcome_at_a_turn_end_is_done_and_leaves_the_session_even_with_work_pending(
        self,
    ):
        for pending in (0, 2):
            with self.subTest(pending=pending):
                self.fresh()
                r = self.faked(turn(START, block(DONE), pending=pending))
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.error, self.handle.calls),
                    (0, "done", "", []),
                )
                self.assertEqual(
                    self.events, [Begun(self.events[0].sid), Progress("start", "go"), Finished(r)]
                )
                self.assertEqual((self.read(self.out), self.last("end")["rc"]), ("# Doc\n", 0))
                self.assertEqual(self.kinds(), ["input", "progress", "result", "end"])

    def test_the_first_turn_end_without_an_outcome_nudges_once(self):
        r = self.faked(turn(START), STOP, turn(block(DONE)))
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", [("send", NUDGE)]))
        self.assertEqual(self.kinds(), ["input", "progress", "nudge", "result", "end"])
        self.assertEqual(bare(self.last("nudge")), {"kind": "nudge"})

    def test_it_gives_up_after_stop_limit_counted_turn_ends_and_leaves_the_session(self):
        r = self.faked(turn(START), *[STOP] * STOP_LIMIT)
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn(f"delegate: no outcome after 3 turn ends; {LEFT_OPEN}", self.err)
        self.assertEqual(self.kinds(), ["input", "progress", "nudge", "give_up", "result", "end"])
        self.assertEqual(
            bare(self.last("give_up")),
            {"kind": "give_up", "reason": "no outcome after 3 turn ends"},
        )
        self.assertEqual(
            (self.last("end")["rc"], [type(e) for e in self.events]), (0, [Begun, Progress])
        )

    def test_a_mark_resets_the_count_before_its_turn_end_counts_and_never_the_nudge(self):
        mark = turn("[agent-pm-progress:round] half way")
        for name, steps in (("once", [mark]), ("repeated on the next turn", [mark, mark])):
            with self.subTest(name):
                self.fresh()
                r = self.faked(
                    turn(START),
                    *[STOP] * (STOP_LIMIT - 1),
                    *steps,
                    *[STOP] * (STOP_LIMIT - 2),
                    turn(block(DONE)),
                )
                self.assertEqual((r.outcome.status, self.handle.calls), ("done", [("send", NUDGE)]))
                self.assertEqual(self.err.count("Progress (round): half way"), len(steps))

    def test_turn_ends_with_work_pending_neither_nudge_nor_count_however_many(self):
        r = self.faked(*[PENDING] * (2 + STOP_LIMIT), turn(START, block(DONE)))
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        r = self.faked(PENDING, PENDING, STOP, *[PENDING, STOP] * STOP_LIMIT)
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))

    def test_an_invalid_block_is_fixed_by_one_send_that_is_neither_the_nudge_nor_counted(self):
        fix = ("send", FIX.format(error=INVALID_ERROR))
        for later, sends in (([], [fix]), ([STOP] * STOP_LIMIT, [fix, ("send", NUDGE)])):
            with self.subTest(later=len(later)):
                self.fresh()
                r = self.faked(turn(START, block(INVALID)), *later, turn(block(DONE)))
                self.assertEqual(
                    (r.returncode, r.outcome.status, self.handle.calls), (0, "done", sends)
                )
                self.assertEqual(
                    (self.started.call_count, self.kinds()[:3]),
                    (1, ["input", "progress", "invalid"]),
                )
                self.assertIn(
                    f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err
                )

    def test_an_invalid_block_twice_or_a_failed_fix_send_is_the_error(self):
        r = self.faked(turn(START, block(INVALID)), turn(block(INVALID)))
        self.assertEqual(r, Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(self.handle.calls, [("send", FIX.format(error=INVALID_ERROR))])
        r = self.faked(turn(START, block(INVALID)), fail=DriverError("gone"))
        self.assertEqual(
            r, Result(1, None, f"invalid outcome: {INVALID_ERROR}; resume failed: gone")
        )

    def test_an_idle_timeout_gives_up_and_leaves_the_session(self):
        r = self.faked(turn(START), IdleTimeout(IDLE_TIMEOUT))
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn(f"delegate: no event for 2 h; {LEFT_OPEN}", self.err)
        self.assertEqual(
            self.kinds(), ["input", "progress", "nudge", "idle", "give_up", "result", "end"]
        )
        self.assertEqual(
            (self.last("idle")["seconds"], self.last("give_up")["reason"]),
            (IDLE_TIMEOUT, "no event for 2 h"),
        )

    def test_hours_of_activity_or_blocked_without_an_idle_timeout_never_give_up(self):
        now = [0.0]

        def hours_apart(*events):
            for event in events:
                now[0] += 3 * 60 * 60
                yield event

        handle = FakeHandle(
            hours_apart(
                Activity("Bash"),
                Blocked("permission"),
                Activity(""),
                Blocked("question"),
                turn(START, block(DONE)),
            )
        )
        with (
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch.object(self.driver, "start", side_effect=handle.start),
        ):
            r = self.go(mode="tui")
        self.assertEqual((r.outcome.status, handle.calls, now[0]), ("done", [], 15 * 60 * 60))
        self.assertEqual(self.kinds(), ["input", "progress", "result", "end"])

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        r = self.faked(*[STOP] * (1 + STOP_LIMIT), fail=DriverError("no pane"))
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn("delegate: tui: no pane\n", self.err)
        self.assertIn("nudge", self.kinds())

    def test_a_usage_limit_stops_the_session_without_a_nudge(self):
        at = 1767225600.0
        r = self.faked(LimitHit(at), STOP, Exited(-signal.SIGHUP))
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        self.assertEqual(
            r, Result(-signal.SIGHUP, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(
            (self.handle.calls, self.kinds()), ([("stop",)], ["input", "limit", "result", "end"])
        )

    def test_a_session_exiting_without_an_outcome_is_the_error(self):
        for events, result in (
            ([STOP, Exited(3)], Result(3, None, "the client exited 3")),
            ([Exited(0)], GAVE_UP),
        ):
            with self.subTest(events=events):
                self.fresh()
                self.assertEqual(self.faked(*events), result)
                self.assertNotIn(("stop",), self.handle.calls)

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        failure = RuntimeError("boom")

        def boom(event):
            if isinstance(event, Progress):
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.faked(turn(START), on_event=boom)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.handle.calls, [("stop",)])
        self.assertEqual(self.last("result")["error"], "stopped: RuntimeError: boom")

    def test_a_failed_stop_is_printed_and_the_runs_exception_raised(self):
        for error in (RuntimeError("boom"), KeyboardInterrupt()):

            def raise_(event, error=error):
                if isinstance(event, Progress):
                    raise error

            with self.subTest(error=error):
                self.fresh()
                with self.assertRaises(type(error)) as cm:
                    self.faked(turn(START), on_event=raise_, stop_fail=DriverError("gone"))
                self.assertIs(cm.exception, error)
                self.assertEqual(
                    (self.handle.calls, self.err.count("delegate: tui: gone\n")), ([("stop",)], 1)
                )
                self.assertTrue(
                    self.last("result")["error"].startswith(f"stopped: {type(error).__name__}")
                )

    def test_a_failed_stop_is_printed_and_never_replaces_the_limit_or_stale_out_result(self):
        gone = DriverError("gone")
        r = self.faked(LimitHit(None), stop_fail=gone)
        self.assertEqual(r, Result(0, None, "usage limit; reset time unknown", Limit(None)))
        self.assertEqual(
            (self.handle.calls, self.err.count("delegate: tui: gone\n")), ([("stop",)], 1)
        )
        self.assertEqual(self.kinds(), ["input", "limit", "result", "end"])
        blocker = os.path.join(self.tmp, "file")
        self.write(blocker, "")
        self.fresh()
        r = self.faked(
            turn(START, block(DONE)), stop_fail=gone, out=os.path.join(blocker, "out.md")
        )
        self.assertEqual(
            (r.returncode, r.outcome, self.events, self.handle.calls), (1, None, [], [("stop",)])
        )
        self.assertRegex(r.error, r"^cannot remove the stale out: ")
        self.assertEqual(self.err.count("delegate: tui: gone\n"), 1)

    def test_a_driver_failure_at_start_is_the_result_and_a_runtime_without_tui_a_config_error(self):
        os.makedirs(self.work)
        with mock.patch.object(self.driver, "start", side_effect=DriverError("no tmux")):
            self.assertEqual(self.go(mode="tui"), Result(1, None, "tui: no tmux"))
        self.fresh()
        with self.assertRaises(ConfigError) as cm:
            self.go(mode="tui")
        self.assertEqual(
            (str(cm.exception), os.path.lexists(self.work)),
            ("runtime 'fake' does not support tui mode", False),
        )

    def test_headless_turn_ends_never_nudge_and_an_idle_timeout_is_only_recorded(self):
        r = self.faked(
            *[STOP] * (2 + STOP_LIMIT),
            IdleTimeout(IDLE_TIMEOUT),
            turn(START, block(DONE)),
            Exited(0),
            mode="headless",
        )
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        self.assertEqual(self.kinds(), ["input", "idle", "progress", "result", "end"])
        args, given = self.started.call_args
        self.assertEqual(
            (given, args[3].sid),
            ({"resume": None, "idle_timeout": IDLE_TIMEOUT, "events": None}, self.events[0].sid),
        )
