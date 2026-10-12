"""driver.api with runtime claude end to end: the real ClaudeAdapter, its [adapters.claude] table given to
registry.configure, runs new/tests/integration/adapters/claude/fake_claude.py (on PATH as `claude`) through api.start, api.usage and
api.session_info; one case runs the Driver in a caller process of its own (CALLER) to interrupt it. HOME and
CLAUDE_CONFIG_DIR are temp dirs."""

import json
import os
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from agent_pm.session.adapters.base import (
    Activity,
    Exited,
    LimitHit,
    Reply,
    SessionBound,
    SessionStatus,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.driver import api, registry

from new.tests.case import PY, Case, child_env
from new.tests.integration.adapters.claude import fake_claude

WAIT = 20  # upper bound on any wait, in seconds
IDLE = 0.5  # idle_timeout, in seconds
SETTLE = 0.5  # seconds a caller gets to wait in events() again after printing an event
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
RESETS = 1760000000
TABLE = {
    "implementation": "adapters.claude:ClaudeAdapter",
    "flags": ["--permission-mode", "auto", "--strict-mcp-config"],
    "tiers": {"4": "haiku"},
    "efforts": {"low": "low"},
}
PROBE = [
    "-p",
    "Reply with OK.",
    "--model",
    "haiku",
    "--output-format",
    "stream-json",
    "--verbose",
    "--setting-sources",
    "user",
    "--strict-mcp-config",
]
# A caller: argv[1] is the claude table, argv[2] the workdir. It prints each event's name; on a KeyboardInterrupt
# (SIGINT, even when inherited ignored) it prints that and stays alive, the fake's parent.
CALLER = """import json, signal, sys, time
from agent_pm.session.driver import api, registry
signal.signal(signal.SIGINT, signal.default_int_handler)
registry.configure({"adapters": {"claude": json.loads(sys.argv[1])}})
handle = api.start("claude", "headless", "hello", api.Spec(workdir=sys.argv[2]))
try:
    for event in handle.events():
        print(type(event).__name__, flush=True)
except KeyboardInterrupt:
    print("KeyboardInterrupt", flush=True)
    time.sleep(60)
"""


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


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.file = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "claude.log")
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        patch = mock.patch.dict(
            os.environ,
            {
                "PATH": fake_claude.install(bin_dir),
                "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"),
                fake_claude.ENV: self.file,
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        registry.configure({"adapters": {"claude": TABLE}})
        self.scenario()

    def scenario(self, **fields):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"log": self.log, **fields}, f)

    def start(self, prompt="hello", spec=None, **kwargs) -> api.Handle:
        handle = api.start(
            "claude", "headless", prompt, spec or api.Spec(workdir=self.workdir), **kwargs
        )
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

    def written(self) -> list[dict]:
        return [
            {k: v for k, v in e.items() if k != "ts"}
            for e in api.records.read(api.records.path(self.workdir))
        ]

    def resume_command(self, sid: str) -> str:
        return f"cd {shlex.quote(self.workdir)} && claude --resume {sid}"


class RunTest(Base):
    def test_a_run_streams_its_messages_to_exited_and_records_the_session(self):
        self.scenario(
            steps=["one", {"kind": "tool", "name": "Read"}, "two", "three"],
            rate_limit={"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}},
        )
        handle = self.start(spec=api.Spec(workdir=self.workdir, tier=4, effort="low"))
        sid = handle.sid
        self.assertEqual(
            self.drain(handle),
            [
                SessionBound(sid),
                Reply("one"),
                Activity("Read"),
                Reply("two"),
                Reply("three"),
                TurnEnd("three", 0),
                Exited(0),
            ],
        )
        self.assertEqual(handle.reply, "three")
        self.assertEqual(handle.info().native_id, sid)
        (entry,) = self.logged()
        self.assertEqual(entry["cwd"], self.workdir)
        self.assertEqual(
            entry["argv"][:6], ["-p", "--session-id", sid, "--model", "haiku", "--effort"]
        )
        self.assertEqual(entry["argv"][-2:], ["--", "hello"])

        transcript = handle.info().transcript
        self.assertTrue(os.path.isfile(transcript))
        session = {
            "kind": "session",
            "sid": sid,
            "cwd": self.workdir,
            "project": False,
            "transcript": transcript,
            "resume": self.resume_command(sid),
        }
        self.assertEqual(
            self.written(), [session, {"kind": "client_session", "sid": sid, "client_id": sid}]
        )
        self.assertEqual(
            {k: v for k, v in api.session(self.workdir, sid).items() if k != "ts"}, session
        )

    def test_resume_continues_the_transcript(self):
        self.scenario(steps=["first"])
        first = self.start()
        self.assertEqual(self.drain(first)[-1], Exited(0))
        self.scenario(steps=["second"])
        again = self.start("more", resume=first.sid)
        self.assertEqual(again.sid, first.sid)
        self.assertEqual(
            self.drain(again),
            [SessionBound(first.sid), Reply("second"), TurnEnd("second", 0), Exited(0)],
        )
        self.assertEqual(self.logged()[1]["argv"][:3], ["-p", "--resume", first.sid])
        self.assertNotIn("--session-id", self.logged()[1]["argv"])
        self.assertEqual(again.info().transcript, first.info().transcript)
        with open(again.info().transcript, encoding="utf-8") as f:
            turns = [(r["type"], r["message"]["content"]) for r in map(json.loads, f)]
        self.assertEqual(
            turns,
            [
                ("user", "hello"),
                ("assistant", [{"type": "text", "text": "first"}]),
                ("user", "more"),
                ("assistant", [{"type": "text", "text": "second"}]),
            ],
        )
        self.assertEqual([e["kind"] for e in self.written()], ["session", "client_session"] * 2)

    def test_a_run_in_its_own_cwd_is_recorded_there_and_resumes_there(self):
        proj = os.path.join(self.dir, "proj")
        os.mkdir(proj)
        first = self.start(spec=api.Spec(workdir=self.workdir, access=api.Access(cwd=proj)))
        sid, transcript = first.sid, first.info().transcript
        self.assertEqual(self.drain(first)[-1], Exited(0))
        self.assertTrue(os.path.isfile(transcript))
        self.assertEqual(os.listdir(self.workdir), ["run.jsonl"])
        resume = (
            f"cd {shlex.quote(proj)} && claude --resume {sid} --add-dir {shlex.quote(self.workdir)}"
        )
        recorded = api.session(self.workdir, sid)
        self.assertEqual(
            {k: v for k, v in recorded.items() if k != "ts"},
            {
                "kind": "session",
                "sid": sid,
                "cwd": proj,
                "project": False,
                "transcript": transcript,
                "resume": resume,
            },
        )

        again = self.start(
            "more",
            api.Spec(workdir=self.workdir, access=api.Access(cwd=recorded["cwd"])),
            resume=sid,
        )
        self.assertEqual(self.drain(again)[-1], Exited(0))
        calls = self.logged()
        self.assertEqual([call["cwd"] for call in calls], [proj, proj])
        self.assertEqual(calls[1]["argv"][:3], ["-p", "--resume", sid])
        self.assertNotIn("--session-id", calls[1]["argv"])
        with open(transcript, encoding="utf-8") as f:
            users = [r["message"]["content"] for r in map(json.loads, f) if r["type"] == "user"]
        self.assertEqual(users, ["hello", "more"])
        self.assertEqual([e["cwd"] for e in self.written() if e["kind"] == "session"], [proj, proj])

    def test_a_session_killed_outside_the_driver_ends_with_its_signal_and_resumes(self):
        self.scenario(steps=["first"], hang=True)
        first = self.start()
        sid = first.sid

        def on(event):
            if isinstance(event, Reply):
                os.kill(self.logged()[0]["pid"], signal.SIGKILL)

        self.assertEqual(
            self.drain(first, on), [SessionBound(sid), Reply("first"), Exited(-signal.SIGKILL)]
        )
        self.assertEqual(api.session_info("claude", sid, self.workdir).resumable, "yes")
        self.scenario(steps=["second"])
        again = self.start("more", resume=sid)
        self.assertEqual(
            self.drain(again), [SessionBound(sid), Reply("second"), TurnEnd("second", 0), Exited(0)]
        )
        self.assertEqual(self.logged()[1]["argv"][:3], ["-p", "--resume", sid])

    def test_a_caller_sid_names_the_session(self):
        handle = self.start(sid=SID)
        self.assertEqual(handle.sid, SID)
        self.assertEqual(self.drain(handle), [SessionBound(SID), TurnEnd("", 0), Exited(0)])
        self.assertEqual(self.logged()[0]["argv"][:3], ["-p", "--session-id", SID])
        self.assertEqual(os.path.basename(handle.info().transcript), f"{SID}.jsonl")
        self.assertTrue(os.path.isfile(handle.info().transcript))

    def test_a_sid_that_is_no_uuid_starts_nothing(self):
        with self.assertRaisesRegex(api.ConfigError, "session ids are UUIDs"):
            self.start(sid="not-a-uuid")
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.written(), [])

    def test_a_usage_limit_is_one_limit_hit(self):
        for resets_at, want in ((RESETS, LimitHit(RESETS)), (None, LimitHit(None))):
            with self.subTest(resets_at=resets_at):
                self.scenario(steps=["working", {"kind": "limit", "resets_at": resets_at}], exit=1)
                handle = self.start()
                self.assertEqual(
                    self.drain(handle),
                    [
                        SessionBound(handle.sid),
                        Reply("working"),
                        want,
                        Reply(fake_claude.LIMIT),
                        TurnEnd("", 0),
                        Exited(1),
                    ],
                )

    def test_a_non_zero_exit(self):
        self.scenario(steps=["partial"], exit=3)
        handle = self.start()
        self.assertEqual(
            self.drain(handle),
            [SessionBound(handle.sid), Reply("partial"), TurnEnd("", 0), Exited(3)],
        )

    def test_resuming_a_session_without_a_transcript_exits_1_and_its_stderr_is_recorded(self):
        said = f"No conversation found with session ID: {SID}"
        self.assertEqual(self.drain(self.start(resume=SID)), [Stderr(said), Exited(1)])
        self.assertEqual([e["kind"] for e in self.written()], ["session", "stderr"])
        self.assertEqual(self.written()[1]["text"], said)

    def test_a_silent_session_yields_idle_timeouts_until_stop_ends_it(self):
        self.scenario(steps=["working"], hang=True)
        handle = self.start(idle_timeout=IDLE)
        replied = []

        def on(event):
            if isinstance(event, Reply):
                replied.append(event)
            elif isinstance(event, api.IdleTimeout) and replied:
                handle.stop()

        found = self.drain(handle, on)
        self.assertEqual(
            [e for e in found if not isinstance(e, api.IdleTimeout)],
            [SessionBound(handle.sid), Reply("working"), Exited(-signal.SIGKILL)],
        )
        self.assertEqual(found[-2], api.IdleTimeout(IDLE))


class InterruptTest(Base):
    def until(self, caller: subprocess.Popen, check, what: str) -> None:
        """Returns once check() holds; fails naming `what` once the caller ends first or WAIT s pass."""
        deadline = time.monotonic() + WAIT
        while not check():
            if caller.poll() is not None or time.monotonic() > deadline:
                self.fail(f"the caller never {what} (rc {caller.poll()}): {self.printed()}")
            time.sleep(0.05)

    def printed(self) -> list[str]:
        with open(self.out, encoding="utf-8") as f:
            return f.read().split()

    def test_an_interrupt_while_events_waits_kills_the_hung_session(self):
        self.scenario(steps=["working"], hang=True)
        self.out = os.path.join(self.dir, "caller.out")
        with open(self.out, "w", encoding="utf-8") as f:
            caller = subprocess.Popen(
                [*PY, "-c", CALLER, json.dumps(TABLE), self.workdir],
                env=child_env(),
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
        self.addCleanup(caller.wait)
        self.addCleanup(caller.kill)
        self.until(caller, lambda: "Reply" in self.printed(), "printed the Reply")
        (entry,) = self.logged()
        self.addCleanup(kill, entry["pid"])
        # SIGINT must land while events() waits, not in the loop body, where it would only close the iterator.
        time.sleep(SETTLE)
        caller.send_signal(signal.SIGINT)
        self.until(
            caller, lambda: "KeyboardInterrupt" in self.printed(), "caught the KeyboardInterrupt"
        )
        self.assertEqual(self.printed(), ["SessionBound", "Reply", "KeyboardInterrupt"])
        self.assertFalse(running(entry["pid"]))


class UsageTest(Base):
    def test_the_probes_rate_limit_info_is_the_usage(self):
        self.scenario(
            rate_limit={
                "status": "allowed",
                "resetsAt": RESETS,
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.25, "resetsAt": RESETS},
                    "seven_day": {"utilization": 1.0, "resetsAt": RESETS + 1},
                },
            }
        )
        self.assertEqual(
            api.usage("claude"), Usage(Window(25.0, RESETS), Window(100.0, RESETS + 1))
        )
        (entry,) = self.logged()
        self.assertEqual(entry["argv"], PROBE)
        probe = os.path.join(self.agent_pm, "probe")
        self.assertEqual(entry["cwd"], os.path.realpath(probe))
        self.assertEqual(stat.S_IMODE(os.lstat(probe).st_mode), 0o700)
        self.assertEqual(self.written(), [])

    def test_a_reading_without_windows_has_none(self):
        self.scenario(rate_limit={"status": "allowed", "unifiedWindows": {"five_hour": "high"}})
        self.assertEqual(api.usage("claude"), Usage(None, None))

    def test_unknown_is_none(self):
        for name, fields in (
            ("no event", {}),
            ("no claude", {"rate_limit": {"unifiedWindows": {"five_hour": {"utilization": 0.25}}}}),
        ):
            with self.subTest(name), mock.patch.dict(os.environ):
                self.scenario(**fields)
                if name == "no claude":
                    os.environ["PATH"] = self.dir
                self.assertIsNone(api.usage("claude"))


class SessionInfoTest(Base):
    def test_a_run_session_is_resumable_another_is_not(self):
        handle = self.start(sid=SID)
        self.assertEqual(self.drain(handle)[-1], Exited(0))
        self.assertEqual(
            api.session_info("claude", SID, self.workdir),
            SessionStatus(
                "yes", os.lstat(handle.info().transcript).st_mtime, self.resume_command(SID)
            ),
        )
        other = str(uuid.uuid4())
        self.assertEqual(
            api.session_info("claude", other, self.workdir),
            SessionStatus("no", None, self.resume_command(other)),
        )


if __name__ == "__main__":
    unittest.main()
