"""orchestrator sessions.py, ported from the old sessions_test: one comment written by its session id, numbered
`#<seq>`, repaired at the end after a late write; the resume command is the Driver's session_status."""

import functools
import subprocess
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.linear import linear, sessions
from agent_pm.session import ConfigError, SessionStatus, api

from .. import case

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
OTHER = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"
ISSUE, ISSUE_ID = "TASK-12", "00000000-0000-4000-8000-000000000012"
OTHER_ISSUE = "00000000-0000-4000-8000-000000000013"
CID = linear.client_id(SID, "session")
T1, T2, T3 = "2026-09-30T10:00:00+08:00", "2026-09-30T11:30:00+08:00", "2026-09-30T12:00:00+08:00"
WORKDIR, RUNTIME = "/w/TASK-12", "claude"
CMD = f"cd /w/TASK-12 && claude --resume {SID}"
NOT_FOUND = SystemExit("linear api error: [{'message': 'Entity not found: Comment'}]")


def record(started_at=T1):
    return sessions.base(sid=SID, workdir=WORKDIR, runtime=RUNTIME, started_at=started_at)


def text(line):
    return f"Run {SID} · {line}\n\n```\n{CMD}\n```"


def comment(id, body, mine=True, issue=ISSUE_ID):
    return {"id": id, "body": body, "mine": mine, "issue": issue}


class FakeLinear:
    def __init__(self, *comments, fail=None):
        self.comments = {c["id"]: dict(c) for c in comments}
        self.fail, self.calls = dict(fail or {}), []

    def __call__(self, query, **v):
        op = next(o for o in ("commentUpdate", "commentCreate", "comment") if o + "(" in query)
        self.calls.append(op)
        if op in self.fail:
            if self.fail[op] is False:
                return {op: {"success": False}}
            raise self.fail[op]
        if op == "comment":
            assert query == linear.Q_COMMENT, query
            c = self.comments.get(v["c"])
            if c is None:
                raise NOT_FOUND
            user = {"isMe": c["mine"]}
            return {
                "comment": {
                    "id": c["id"],
                    "body": c["body"],
                    "issue": {"id": c["issue"]},
                    "user": user,
                }
            }
        if op == "commentCreate":
            assert query == linear.M_COMMENT_ID and v["i"] == ISSUE, (query, v)
            assert v["c"] not in self.comments, "fake: comment id in use"
            self.comments[v["c"]] = comment(v["c"], v["b"])
        else:
            assert query == sessions.M_UPDATE, query
            if v["c"] not in self.comments:
                raise NOT_FOUND
            self.comments[v["c"]]["body"] = v["b"]
        return {op: {"success": True}}

    def only(self):
        assert len(self.comments) == 1, self.comments
        assert CID in self.comments, self.comments
        return self.comments[CID]


class Late:
    """Holds the first call until released, then sends it: a write that timed out and lands later."""

    def __init__(self, gql):
        self.gql, self.first = gql, True
        self.held, self.release, self.landed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

    def __call__(self, query, **v):
        if not self.first:
            return self.gql(query, **v)
        self.first = False
        self.held.set()
        self.release.wait(30)
        try:
            return self.gql(query, **v)
        finally:
            self.landed.set()


def start(gql, rec):
    return sessions.post(ISSUE, rec, gql=gql)


def end(gql, rec, rc, ended_at=T2):
    return sessions.post(ISSUE, rec, rc, gql=gql, ended_at=ended_at)


def bounded(limit):
    return mock.patch.object(sessions, "write", functools.partial(sessions.write, limit=limit))


class Case(case.Case):
    def setUp(self):
        patch = mock.patch.multiple(api, session_record=mock.DEFAULT, session_status=mock.DEFAULT)
        mocks = patch.start()
        self.addCleanup(patch.stop)
        self.session, self.info = mocks["session_record"], mocks["session_status"]
        self.session.return_value = None
        self.info.return_value = SessionStatus("yes", None, CMD)


class Record(Case):
    def test_base(self):
        self.assertEqual(
            record(),
            {"sid": SID, "workdir": WORKDIR, "runtime": RUNTIME, "started_at": T1, "seq": 0},
        )

    def test_post_creates_by_session_id_then_updates_it(self):
        for rc, line in (
            (None, f"running · #1 · {T1}"),
            (0, f"done · #2 · {T1} → {T2} · exit 0"),
            (3, f"interrupted · #2 · {T1} → {T2} · exit 3"),
        ):
            with self.subTest(rc=rc):
                fake, rec = FakeLinear(), record()
                self.assertIsNone(start(fake, rec))
                if rc is not None:
                    self.assertIsNone(end(fake, rec, rc))
                ended = ["commentUpdate"] if rc is not None else []
                self.assertEqual(fake.calls, ["commentUpdate", "commentCreate", *ended])
                self.assertEqual(fake.only()["body"], text(line))
                self.assertEqual(rec["last"], 1 if rc is None else 2)

    def test_a_null_update_is_not_found(self):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if query == sessions.M_UPDATE:
                return {"commentUpdate": None}
            return {"commentCreate": {"success": True}}

        self.assertIsNone(start(gql, record()))
        body = text(f"running · #1 · {T1}")
        self.assertEqual(
            calls,
            [
                (sessions.M_UPDATE, {"c": CID, "b": body}),
                (linear.M_COMMENT_ID, {"c": CID, "i": ISSUE, "b": body}),
            ],
        )

    def test_each_body_takes_the_next_seq(self):
        rec = record()
        self.assertEqual(sessions.body(rec), text(f"running · #1 · {T1}"))
        self.assertEqual(sessions.body(rec, 0, T2), text(f"done · #2 · {T1} → {T2} · exit 0"))
        self.assertEqual(rec["seq"], 2)

    def test_terminal_outcome_keeps_status_and_native_exit_diagnostic(self):
        for status in ("done", "needs_input", "failed"):
            with self.subTest(status=status):
                fake = FakeLinear()
                self.assertIsNone(
                    sessions.post(
                        ISSUE,
                        record(),
                        3,
                        gql=fake,
                        ended_at=T2,
                        outcome=Outcome(status, "title", "summary"),
                    )
                )
                line = fake.only()["body"].splitlines()[0]
                self.assertEqual(line, f"Run {SID} · {status} · #1 · {T1} → {T2} · exit 3")
                self.assertNotIn("interrupted", line)

    def test_a_limit_ended_run_says_limit_unless_it_reported_an_outcome(self):
        rec, asked = record(), Outcome("needs_input", "title", "summary")
        self.assertEqual(sessions.body(rec, limit=True), text(f"running · #1 · {T1}"))
        self.assertEqual(
            sessions.body(rec, -9, T2, limit=True), text(f"limit · #2 · {T1} → {T2} · exit -9")
        )
        self.assertEqual(
            sessions.body(rec, -9, T2, outcome=asked, limit=True),
            text(f"needs_input · #3 · {T1} → {T2} · exit -9"),
        )
        fake = FakeLinear()
        self.assertIsNone(sessions.post(ISSUE, record(), -9, gql=fake, ended_at=T2, limit=True))
        self.assertEqual(fake.only()["body"], text(f"limit · #1 · {T1} → {T2} · exit -9"))

    def test_recover_resume_updates_the_one_comment(self):
        fake, rec = FakeLinear(), record()
        start(fake, rec)
        end(fake, rec, 3)
        start(fake, record(started_at=T2))
        body = fake.only()["body"]
        self.assertEqual(body.splitlines()[0], f"Run {SID} · running · #1 · {T2}")
        self.assertEqual(body, sessions.body(record(started_at=T2)))

    def test_end_defaults_to_now(self):
        fake = FakeLinear()
        with mock.patch.object(sessions, "now", return_value=T2):
            self.assertIsNone(sessions.post(ISSUE, record(), 0, gql=fake))
        self.assertEqual(fake.only()["body"], sessions.body(record(), 0, T2))

    def test_now_is_local_iso_with_offset(self):
        stamp = sessions.now()
        self.assertIsNotNone(datetime.fromisoformat(stamp).utcoffset(), stamp)

    def test_no_cli(self):
        self.assertFalse(hasattr(sessions, "main") or hasattr(sessions, "USAGE"))

    def test_failed_start_leaves_end_to_create_the_comment(self):
        fake, rec = FakeLinear(fail={"commentCreate": RuntimeError("down")}), record()
        self.assertEqual(start(fake, rec), "RuntimeError: down")
        self.assertEqual(fake.comments, {})
        fake.fail = {}
        self.assertIsNone(end(fake, rec, 0))
        self.assertEqual(
            fake.calls, ["commentUpdate", "commentCreate", "commentUpdate", "commentCreate"]
        )
        self.assertEqual(fake.only()["body"], text(f"done · #2 · {T1} → {T2} · exit 0"))

    def test_writes_only_its_session_id(self):
        mine = f"Run {SID} · running · #1 · {T1}"
        others = [
            comment(linear.client_id(OTHER, "session"), f"Run {OTHER} · running · #1 · {T1}"),
            comment("human", mine, mine=False),
            comment("legacy", f"Run {SID} · running · {T1}"),
        ]
        fake = FakeLinear(*others, comment(CID, mine))
        self.assertIsNone(start(fake, record(started_at=T2)))
        self.assertEqual(fake.calls, ["commentUpdate"])
        self.assertEqual(fake.comments[CID]["body"], text(f"running · #1 · {T2}"))
        self.assertEqual([fake.comments[c["id"]] for c in others], others)

    def test_is_comment(self):
        body = sessions.body(record())
        self.assertTrue(sessions.is_comment({"user": {"isMe": True}, "body": body}))
        for name, c in (
            ("human, same body", {"user": {"isMe": False}, "body": body}),
            (
                "harness, no prefix",
                {"user": {"isMe": True}, "body": "Repo check failed: no Repo: line"},
            ),
            ("sid not a uuid", {"user": {"isMe": True}, "body": "Run s-1 · x"}),
            ("no user", {"user": None, "body": body}),
            ("prefix not at the start", {"user": {"isMe": True}, "body": "see " + body}),
        ):
            with self.subTest(name):
                self.assertFalse(sessions.is_comment(c))

    def test_is_record_by_url(self):
        self.assertTrue(sessions.is_record({"title": f"Run {SID}", "url": sessions.URL + SID}))
        self.assertFalse(sessions.is_record({"title": "Run rate analysis", "url": "https://gh/x"}))

    def test_limit_and_no_default_identity(self):
        self.assertEqual(sessions.LIMIT, 10)
        with self.assertRaises(TypeError):
            sessions.post(ISSUE, record())


class Command(Case):
    def test_the_drivers_resume_command_for_the_session_cwd(self):
        for entry, cwd in (
            (None, WORKDIR),
            ({"kind": "session", "cwd": "/data/my repo", "resume": "rm -rf ~"}, "/data/my repo"),
        ):
            with self.subTest(entry=entry):
                self.session.return_value = entry
                self.info.reset_mock()
                self.assertEqual(sessions.command(record()), CMD)
                self.session.assert_called_with(WORKDIR, SID)
                self.info.assert_called_once_with(RUNTIME, SID, cwd)

    def test_built_fresh_each_time_never_read_from_the_record(self):
        rec = record()
        rec["command"] = "rm -rf ~"
        self.assertEqual(sessions.body(rec), text(f"running · #1 · {T1}"))
        self.info.return_value = SessionStatus("yes", None, "resume it")
        self.assertEqual(
            sessions.body(rec), f"Run {SID} · running · #2 · {T1}\n\n```\nresume it\n```"
        )
        self.assertEqual(self.info.call_count, 2)

    def test_no_command_block(self):
        for name, effect in (
            ("raises", ConfigError("bad sid")),
            ("unknown", SessionStatus("unknown", None, CMD)),
            ("empty", SessionStatus("yes", None, "")),
        ):
            with self.subTest(name):
                self.info.side_effect = effect if isinstance(effect, Exception) else None
                self.info.return_value = effect
                rec = record()
                self.assertIsNone(sessions.command(rec))
                self.assertEqual(sessions.body(rec), f"Run {SID} · running · #1 · {T1}")


class Status(Case):
    def test_the_drivers_status_at_the_records_cwd_else_the_workdir(self):
        st = SessionStatus("no", 5.0, "")
        self.info.return_value = st
        for entry, cwd in ((None, WORKDIR), ({"kind": "session", "cwd": "/data/r"}, "/data/r")):
            with self.subTest(entry=entry):
                self.session.return_value = entry
                self.info.reset_mock()
                self.assertIs(sessions.status(WORKDIR, "rt", SID), st)
                self.session.assert_called_with(WORKDIR, SID)
                self.info.assert_called_once_with("rt", SID, cwd)

    def test_errors_propagate(self):
        for target in (self.session, self.info):
            with self.subTest(target=target):
                self.session.side_effect = self.info.side_effect = None
                target.side_effect = ConfigError("bad")
                with self.assertRaises(ConfigError):
                    sessions.status(WORKDIR, RUNTIME, SID)


class Failure(Case):
    def failing(self, exc):
        def gql(query, **v):
            raise exc

        return gql

    def test_update_failure_never_creates(self):
        for exc, reason in (
            (RuntimeError("boom"), "RuntimeError: boom"),
            (
                SystemExit("linear api error: [{'message': 'nope'}]"),
                "SystemExit: linear api error: [{'message': 'nope'}]",
            ),
        ):
            with self.subTest(reason=reason):
                fake = FakeLinear(fail={"commentUpdate": exc})
                self.assertEqual(start(fake, record()), reason)
                self.assertEqual(fake.calls, ["commentUpdate"])
        calls = []

        def malformed(query, **v):
            calls.append(query)
            return {}

        self.assertRegex(end(malformed, record(), 0), r"\AKeyError: [^\n]+\Z")
        self.assertEqual(len(calls), 1)

    def test_write_failure_logs_one_line(self):
        existing = comment(CID, f"Run {SID} · running · #1 · {T1}")
        for op, seed, calls in (
            ("commentCreate", (), ["commentUpdate", "commentCreate"]),
            ("commentUpdate", (existing,), ["commentUpdate"]),
        ):
            for exc, reason in (
                (RuntimeError("boom"), "RuntimeError: boom"),
                (
                    SystemExit("linear api error: [{'message': 'nope'}]"),
                    "SystemExit: linear api error: [{'message': 'nope'}]",
                ),
                (RuntimeError("two\nlines"), "RuntimeError: two lines"),
                (False, "success: false"),
            ):
                with self.subTest(op=op, reason=reason):
                    fake = FakeLinear(*seed, fail={op: exc})
                    self.assertEqual(start(fake, record()), reason)
                    self.assertEqual(fake.calls, calls)

    def test_blocking_gql_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        with bounded(0.1):
            out = end(lambda q, **v: release.wait(30), record(), 0)
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(out, "timed out after 0.1s")

    def test_secret_outside_the_message_does_not_leak(self):
        exc = subprocess.CalledProcessError(
            1,
            ["security", "find-generic-password", "-s", "svc", "-w"],
            output="lin_api_SECRET",
            stderr="lin_api_SECRET",
        )
        exc.headers = {"Authorization": "lin_api_SECRET"}
        out = start(self.failing(exc), record())
        self.assertTrue(out.startswith("CalledProcessError: "), out)
        self.assertNotIn("lin_api_SECRET", out)


class Repair(Case):
    DONE = f"{T1} → {T2} · exit 0"

    def setUp(self):
        super().setUp()
        self.fake, self.rec = FakeLinear(), record()

    def repair(self, gql=None, **kw):
        return sessions.repair(ISSUE, ISSUE_ID, self.rec, gql=gql or self.fake, **kw)

    def test_a_late_start_write_is_repaired_with_a_higher_seq(self):
        late = Late(self.fake)
        self.addCleanup(late.release.set)
        with bounded(0.1):
            self.assertEqual(start(late, self.rec), "timed out after 0.1s")
            self.assertTrue(late.held.wait(5))
            self.assertIsNone(end(late, self.rec, 0))
        self.assertEqual(self.fake.only()["body"], text(f"done · #2 · {self.DONE}"))
        late.release.set()
        self.assertTrue(late.landed.wait(5))
        self.assertEqual(self.fake.only()["body"], text(f"running · #1 · {T1}"))
        self.fake.calls.clear()
        self.assertIsNone(self.repair(late))
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"done · #3 · {self.DONE}"))

    def test_no_late_write_reads_and_does_not_rewrite(self):
        start(self.fake, self.rec)
        end(self.fake, self.rec, 0)
        self.fake.calls.clear()
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment"])
        self.assertEqual(self.fake.only()["body"], text(f"done · #2 · {self.DONE}"))

    def test_no_parseable_seq_rewrites(self):
        start(self.fake, self.rec)
        end(self.fake, self.rec, 0)
        self.fake.comments[CID]["body"] = f"Run {SID} · done · {self.DONE}"
        self.fake.calls.clear()
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"done · #3 · {self.DONE}"))

    def test_not_found_creates_it_by_session_id(self):
        down = FakeLinear(fail={"commentCreate": RuntimeError("down")})
        self.assertEqual(end(down, self.rec, 0), "RuntimeError: down")
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate", "commentCreate"])
        self.assertEqual(self.fake.only()["body"], text(f"done · #2 · {self.DONE}"))

    def test_a_limit_end_is_rewritten_as_limit(self):
        down = FakeLinear(fail={"commentCreate": RuntimeError("down")})
        self.assertEqual(
            sessions.post(ISSUE, self.rec, -9, gql=down, ended_at=T2, limit=True),
            "RuntimeError: down",
        )
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.only()["body"], text(f"limit · #2 · {T1} → {T2} · exit -9"))

    def test_a_previous_runs_body_is_rewritten_on_a_resume(self):
        down = FakeLinear(fail={"commentUpdate": RuntimeError("down")})
        self.rec = record(started_at=T2)
        self.assertEqual(start(down, self.rec), "RuntimeError: down")
        self.assertEqual(end(down, self.rec, 0, ended_at=T3), "RuntimeError: down")
        self.fake = FakeLinear(comment(CID, text(f"done · #3 · {self.DONE}")))
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"done · #3 · {T2} → {T3} · exit 0"))

    def test_errors_return_a_string_and_write_nothing(self):
        theirs = f"RuntimeError: comment {CID} is not ours"
        body = f"Run {SID} · running · #1 · {T1}"
        for name, seed, fail, reason in (
            ("find fails", (), {"comment": RuntimeError("boom")}, "RuntimeError: boom"),
            ("another issue", (comment(CID, body, issue=OTHER_ISSUE),), {}, theirs),
            ("another author", (comment(CID, body, mine=False),), {}, theirs),
        ):
            with self.subTest(name):
                self.rec = record()
                end(FakeLinear(), self.rec, 0)
                self.fake = FakeLinear(*seed, fail=fail)
                self.assertEqual(self.repair(), reason)
                self.assertEqual(self.fake.calls, ["comment"])

    def test_a_failed_rewrite_returns_its_error(self):
        end(FakeLinear(), self.rec, 0)
        self.fake.fail = {"commentCreate": RuntimeError("down")}
        self.assertEqual(self.repair(), "RuntimeError: down")

    def test_a_blocking_find_times_out(self):
        end(self.fake, self.rec, 0)
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        self.assertEqual(
            self.repair(lambda q, **v: release.wait(30), limit=0.1), "timed out after 0.1s"
        )
        self.assertLess(time.monotonic() - t, 5)


if __name__ == "__main__":
    unittest.main()
