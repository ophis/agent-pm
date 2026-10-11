"""driver.api with runtime claude end to end: the real ClaudeAdapter, its [adapters.claude] table given to
registry.configure, runs new/tests/fake_claude.py (on PATH as `claude`) through api.start, api.usage and
api.session_info. HOME and CLAUDE_CONFIG_DIR are temp dirs."""
import json
import os
import shlex
import signal
import stat
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from new.tests import fake_claude, hermetic
from adapters.base import Activity, Exited, LimitHit, Reply, SessionBound, SessionStatus, Stderr, TurnEnd, Usage
from driver import api, registry

WAIT = 20    # upper bound on any wait, in seconds
IDLE = 0.5   # idle_timeout, in seconds
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
RESETS = 1760000000
TABLE = {"implementation": "adapters.claude:ClaudeAdapter",
         "flags": ["--permission-mode", "auto", "--strict-mcp-config"],
         "tiers": {"4": "haiku"}, "efforts": {"low": "low"}}
PROBE = ["-p", "Reply with OK.", "--model", "haiku", "--output-format", "stream-json", "--verbose",
         "--setting-sources", "user", "--strict-mcp-config"]


class Base(unittest.TestCase):
    def setUp(self):
        self.agent_pm = hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.file = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "claude.log")
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        patch = mock.patch.dict(os.environ, {"PATH": fake_claude.install(bin_dir),
                                             "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"),
                                             fake_claude.ENV: self.file})
        patch.start()
        self.addCleanup(patch.stop)
        registry.configure({"adapters": {"claude": TABLE}})
        self.addCleanup(registry.reset)
        self.scenario()

    def scenario(self, **fields):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"log": self.log, **fields}, f)

    def start(self, prompt="hello", spec=None, **kwargs) -> api.Handle:
        handle = api.start("claude", "headless", prompt, spec or api.Spec(workdir=self.workdir), **kwargs)
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
        return [{k: v for k, v in e.items() if k != "ts"} for e in api.records.read(api.records.path(self.workdir))]

    def resume_command(self, sid: str) -> str:
        return f"cd {shlex.quote(self.workdir)} && claude --resume {sid}"


class RunTest(Base):
    def test_a_run_streams_its_messages_to_exited_and_records_the_session(self):
        self.scenario(steps=["one", {"kind": "tool", "name": "Read"}, "two", "three"],
                      rate_limit={"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}})
        handle = self.start(spec=api.Spec(workdir=self.workdir, tier=4, effort="low"))
        sid = handle.sid
        self.assertEqual(self.drain(handle), [SessionBound(sid), Reply("one"), Activity("Read"), Reply("two"),
                                              Reply("three"), TurnEnd("three", 0), Exited(0)])
        self.assertEqual(handle.reply, "three")
        self.assertEqual(handle.info().native_id, sid)
        (entry,) = self.logged()
        self.assertEqual(entry["cwd"], self.workdir)
        self.assertEqual(entry["argv"][:6], ["-p", "--session-id", sid, "--model", "haiku", "--effort"])
        self.assertEqual(entry["argv"][-2:], ["--", "hello"])

        transcript = handle.info().transcript
        self.assertTrue(os.path.isfile(transcript))
        session = {"kind": "session", "sid": sid, "cwd": self.workdir, "project": False, "transcript": transcript,
                   "resume": self.resume_command(sid)}
        self.assertEqual(self.written(), [session, {"kind": "client_session", "sid": sid, "client_id": sid}])
        self.assertEqual({k: v for k, v in api.session(self.workdir, sid).items() if k != "ts"}, session)

    def test_resume_continues_the_transcript(self):
        self.scenario(steps=["first"])
        first = self.start()
        self.assertEqual(self.drain(first)[-1], Exited(0))
        self.scenario(steps=["second"])
        again = self.start("more", resume=first.sid)
        self.assertEqual(again.sid, first.sid)
        self.assertEqual(self.drain(again), [SessionBound(first.sid), Reply("second"), TurnEnd("second", 0),
                                             Exited(0)])
        self.assertEqual(self.logged()[1]["argv"][:3], ["-p", "--resume", first.sid])
        self.assertEqual(again.info().transcript, first.info().transcript)
        with open(again.info().transcript, encoding="utf-8") as f:
            turns = [(r["type"], r["message"]["content"]) for r in map(json.loads, f)]
        self.assertEqual(turns, [("user", "hello"), ("assistant", [{"type": "text", "text": "first"}]),
                                 ("user", "more"), ("assistant", [{"type": "text", "text": "second"}])])
        self.assertEqual([e["kind"] for e in self.written()], ["session", "client_session"] * 2)

    def test_a_caller_sid_names_the_session(self):
        handle = self.start(sid=SID)
        self.assertEqual(handle.sid, SID)
        self.assertEqual(self.drain(handle), [SessionBound(SID), TurnEnd("", 0), Exited(0)])
        self.assertEqual(self.logged()[0]["argv"][:3], ["-p", "--session-id", SID])
        self.assertEqual(os.path.basename(handle.info().transcript), f"{SID}.jsonl")
        self.assertTrue(os.path.isfile(handle.info().transcript))

    def test_a_sid_that_is_no_uuid_starts_nothing(self):
        with self.assertRaisesRegex(api.ConfigError, "claude session ids are UUIDs"):
            self.start(sid="not-a-uuid")
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.written(), [])

    def test_a_usage_limit_is_one_limit_hit(self):
        for resets_at, want in ((RESETS, LimitHit(RESETS)), (None, LimitHit(None))):
            with self.subTest(resets_at=resets_at):
                self.scenario(steps=["working", {"kind": "limit", "resets_at": resets_at}], exit=1)
                handle = self.start()
                self.assertEqual(self.drain(handle), [SessionBound(handle.sid), Reply("working"), want,
                                                      Reply(fake_claude.LIMIT), TurnEnd("", 0), Exited(1)])

    def test_a_non_zero_exit(self):
        self.scenario(steps=["partial"], exit=3)
        handle = self.start()
        self.assertEqual(self.drain(handle), [SessionBound(handle.sid), Reply("partial"), TurnEnd("", 0), Exited(3)])

    def test_resuming_a_session_without_a_transcript_exits_1(self):
        self.assertEqual(self.drain(self.start(resume=SID)),
                         [Stderr(f"No conversation found with session ID: {SID}"), Exited(1)])

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
        self.assertEqual([e for e in found if not isinstance(e, api.IdleTimeout)],
                         [SessionBound(handle.sid), Reply("working"), Exited(-signal.SIGKILL)])
        self.assertEqual(found[-2], api.IdleTimeout(IDLE))


class UsageTest(Base):
    def test_the_probes_rate_limit_info_is_the_usage(self):
        self.scenario(rate_limit={"status": "allowed", "resetsAt": RESETS,
                                  "unifiedWindows": {"five_hour": {"utilization": 0.25},
                                                     "seven_day": {"utilization": 1.0}}})
        self.assertEqual(api.usage("claude"), Usage(25.0, True, RESETS))
        (entry,) = self.logged()
        self.assertEqual(entry["argv"], PROBE)
        probe = os.path.join(self.agent_pm, "probe")
        self.assertEqual(entry["cwd"], os.path.realpath(probe))
        self.assertEqual(stat.S_IMODE(os.lstat(probe).st_mode), 0o700)
        self.assertEqual(self.written(), [])

    def test_unknown_is_none(self):
        for name, fields in (("no event", {}), ("garbage", {"rate_limit": {"unifiedWindows": {"five_hour": "high"}}}),
                             ("no claude", {"rate_limit": {"unifiedWindows": {"five_hour": {"utilization": 0.25}}}})):
            with self.subTest(name), mock.patch.dict(os.environ):
                self.scenario(**fields)
                if name == "no claude":
                    os.environ["PATH"] = self.dir
                self.assertIsNone(api.usage("claude"))


class SessionInfoTest(Base):
    def test_a_run_session_is_resumable_another_is_not(self):
        handle = self.start(sid=SID)
        self.assertEqual(self.drain(handle)[-1], Exited(0))
        self.assertEqual(api.session_info("claude", SID, self.workdir),
                         SessionStatus("yes", os.lstat(handle.info().transcript).st_mtime, self.resume_command(SID)))
        other = str(uuid.uuid4())
        self.assertEqual(api.session_info("claude", other, self.workdir),
                         SessionStatus("no", None, self.resume_command(other)))


if __name__ == "__main__":
    unittest.main()
