"""delegate/runner.py over the fake runtime (new/tests/fake_adapter/, registered in-process): callbacks and replies
(FR-17, FR-18), deliverable_file (FR-19), the fix resume (FR-21), idle (FR-22), a usage limit (FR-58), and main's
drive_test Start, HostFailure and Sinks cases: records, saving, the missing start mark, stops, printable lines."""

from new.tests import fake_adapter, hermetic

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
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime
from unittest import mock

import composer
from delegate import Begun, ConfigError, Finished, Limit, Progress, Result, run, runner
from delegate.outcome import save
from delegate.runner import FIX, IDLE_TIMEOUT, printable
from driver import registry

SID = "11111111-2222-3333-4444-555555555555"
DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}
FAILED = {**DONE, "status": "failed", "deliverable": ""}
INVALID = {**DONE, "status": "maybe"}
INVALID_ERROR = "status 'maybe' is not one of done, needs_input, failed"
START = "[agent-pm-progress:start] go"


def block(data) -> str:
    return f"```agent-pm-outcome\n{json.dumps(data)}\n```"


def said(*lines) -> dict:
    """A step printing a message record (Reply) of `lines`."""
    return {"out": {"type": "message", "text": "\n".join(lines)}}


def ended(text) -> dict:
    """A step printing a result record (TurnEnd) of `text`."""
    return {"out": {"type": "result", "text": text}}


def limit(resets_at) -> dict:
    return {"out": {"type": "limit", "resets_at": resets_at}}


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def bare(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "ts"}


class Base(unittest.TestCase):
    """dummy-tester's echo over the fake runtime in workdir `work` (not made), out `out`, the cwd the workdir."""

    def setUp(self):
        hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.channel = os.path.join(self.work, "run.jsonl")
        self.scenario, self.log = (
            os.path.join(self.tmp, name) for name in ("scenario.json", "runtime.log")
        )
        patch = mock.patch.dict(sys.modules, {"adapters.fake": fake_adapter})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.reset)
        registry.configure(
            {
                "adapters": {
                    "fake": {
                        "implementation": "adapters.fake:FakeAdapter",
                        "scenario": self.scenario,
                        "log": self.log,
                    }
                }
            }
        )

    def go(self, *steps, fixes=None, output=None, on_event=None, **kw) -> Result:
        """run() over the fake with `steps` (`fixes`: its steps on --resume); self.events gets each callback, self.err
        the stderr."""
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
                return run(config, "echo", kw.pop("input"), self.work, on_event=on, **kw)
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
        patch = mock.patch.object(runner, "save", wraps=save)
        self.addCleanup(patch.stop)
        return patch.start()


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
    """FR-21: one fix resume of the same session after an invalid outcome block, only when the client exited 0."""

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
        with mock.patch.object(runner, "IDLE_TIMEOUT", 0.3):
            r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})


class UsageLimit(Base):
    """FR-58: a usage limit stops the session and ends the run: no outcome read, no fix resume, no Finished."""

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

    def test_a_destination_decides_the_url_and_whether_the_deliverable_is_saved(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main", "dir": "R/"}
        url = "https://github.com/o/docs/blob/main/x.md"
        for output, data, want_url, saved in (
            ({"type": "local"}, DONE, self.out, "# Doc\n"),
            ({"type": "orchestrator"}, DONE, "", "# Doc\n"),
            (gh, {**DONE, "url": url}, url, None),
            ({"type": "local"}, NEEDS, "", None),
        ):
            with self.subTest(output=output, status=data["status"]):
                r = self.go(said(START, block(data)), output=output)
                self.assertEqual(r.outcome.url, want_url)
                self.assertEqual(
                    self.read(self.out) if saved else os.path.lexists(self.out), saved or False
                )
                self.assertEqual(
                    self.last("result")["outcome"]["questions"], data.get("questions", [])
                )

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
            resume=SID,
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

    def test_an_out_not_a_regular_file_holding_the_deliverable_is_replaced_never_followed_or_waited_on(
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
                    on_event=lambda e: plant() if isinstance(e, Begun) else None,
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
            ([said(block(DONE))], {"resume": SID}),
            ([said("bye")], {}),
        ):
            with self.subTest(steps=steps, **kw):
                self.fresh()
                self.go(*steps, **kw)
                self.assertNotIn("missing", self.err)
                self.assertNotIn("missing", self.kinds())


class Stops(Base):
    """Main's HostFailure cases: a session stopped on an exception, its record closed `stopped: …`."""

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        def boom(event):
            if isinstance(event, Progress):
                raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.go(said(START), {"sleep": 30}, said(block(DONE)), on_event=boom)
        self.assertFalse(running(self.launches()[0]["pid"]))
        result, end = self.records()[-2:]
        self.assertEqual((result["error"], end["rc"]), ("stopped: RuntimeError: boom", 1))

    def test_an_exception_ends_the_record_stopped(self):
        for error, rc, text in (
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
        def swap(event):
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


class Terminal(Base):
    """Main's Sinks case: what reaches the terminal is printable."""

    def test_printable_drops_control_characters_but_tabs(self):
        self.assertEqual(printable("a\x1b]52;c;ZXZpbA==\x07b\tc\r\n"), "a]52;c;ZXZpbA==b\tc")

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


if __name__ == "__main__":
    unittest.main()
