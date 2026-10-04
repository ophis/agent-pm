import functools, os, re, shlex, subprocess, sys, threading, time, unittest
from datetime import datetime
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sessions  # noqa: E402

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
OTHER = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"
ISSUE = "TASK-12"
T1, T2 = "2026-09-30T10:00:00+08:00", "2026-09-30T11:30:00+08:00"
CMD = f"cd /w/TASK-12 && claude --resume {SID}"
STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")

def record(started_at=T1, cwd="/w/TASK-12"):
    return sessions.base(sid=SID, cwd=cwd, started_at=started_at)

def comment(id, created_at, body, mine=True):
    return {"id": id, "createdAt": created_at, "body": body, "mine": mine}

class FakeLinear:
    """ISSUE's comments; answers the find query by applying its filters, commentCreate and commentUpdate.
    fail maps an operation (comments, commentCreate, commentUpdate) to an exception to raise, or False for success: false."""
    def __init__(self, *comments, fail=None):
        self.comments, self.fail, self.calls = [dict(c) for c in comments], dict(fail or {}), []

    def __call__(self, query, **v):
        op = next((o for o in ("commentCreate", "commentUpdate") if o + "(" in query), "comments")
        self.calls.append(op)
        if op in self.fail:
            if self.fail[op] is False:
                return {op: {"success": False}}
            raise self.fail[op]
        if op == "comments":
            assert "isMe: { eq: true }" in query and "startsWith: $p" in query, query
            assert v["i"] == ISSUE, v
            hits = [c for c in self.comments if c["mine"] and c["body"].startswith(v["p"])]
            return {"issue": {"comments": {"nodes": [{"id": c["id"], "createdAt": c["createdAt"]} for c in reversed(hits)]}}}
        if op == "commentCreate":
            assert v["i"] == ISSUE, v
            n = len(self.comments)
            self.comments.append(comment(f"new-{n}", f"2026-10-01T00:00:{n:02d}.000Z", v["b"]))
        else:
            next(c for c in self.comments if c["id"] == v["c"])["body"] = v["b"]
        return {op: {"success": True}}

    def only(self):
        assert len(self.comments) == 1, self.comments
        return self.comments[0]

def start(gql, rec):
    return sessions.post(ISSUE, rec, gql=gql)

def end(gql, rec, rc, ended_at=T2):
    return sessions.post(ISSUE, rec, rc, gql=gql, ended_at=ended_at)


class Record(unittest.TestCase):
    def test_start_creates_one_running_comment(self):
        linear = FakeLinear()
        self.assertIsNone(start(linear, record()))
        self.assertEqual(linear.calls, ["comments", "commentCreate"])
        c = linear.only()
        self.assertEqual(c["body"], f"Run {SID} · running · {T1}\n\n```\n{CMD}\n```")

    def test_record_is_sid_cwd_and_start(self):
        self.assertEqual(record(), {"sid": SID, "cwd": "/w/TASK-12", "started_at": T1})

    def test_command_quotes_the_cwd(self):
        cwd = "/Users/x/my work/it's/TASK-12"
        self.assertEqual(shlex.split(sessions.command(record(cwd=cwd))),
                         ["cd", cwd, "&&", "claude", "--resume", SID])

    def test_recover_resume_updates_the_one_comment(self):
        linear = FakeLinear()
        start(linear, record())
        end(linear, record(), 3)
        start(linear, record(started_at=T2))
        body = linear.only()["body"]
        self.assertEqual(body.splitlines()[0], f"Run {SID} · running · {T2}")
        self.assertEqual(body, sessions.body(record(started_at=T2)))

    def test_end_sets_status_times_and_exit(self):
        for rc, status in ((0, "done"), (3, "interrupted")):
            with self.subTest(rc=rc):
                linear = FakeLinear()
                start(linear, record())
                self.assertIsNone(end(linear, record(), rc))
                self.assertEqual(linear.only()["body"], f"Run {SID} · {status} · {T1} → {T2} · exit {rc}\n\n```\n{CMD}\n```")

    def test_end_defaults_to_now(self):
        linear = FakeLinear()
        with mock.patch.object(sessions, "now", return_value=T2):
            self.assertIsNone(sessions.post(ISSUE, record(), 0, gql=linear))
        self.assertEqual(linear.only()["body"], sessions.body(record(), 0, T2))

    def test_now_is_local_iso_with_offset(self):
        stamp = sessions.now()
        self.assertIsNotNone(datetime.fromisoformat(stamp).utcoffset(), stamp)

    def test_no_cli(self):
        self.assertFalse(hasattr(sessions, "main") or hasattr(sessions, "USAGE"))

    def test_failed_start_leaves_end_to_create_the_comment(self):
        linear = FakeLinear(fail={"commentCreate": RuntimeError("down")})
        self.assertIn("registry-error", start(linear, record()))
        self.assertEqual(linear.comments, [])
        linear.fail = {}
        self.assertIsNone(end(linear, record(), 0))
        self.assertEqual(linear.calls, ["comments", "commentCreate", "comments", "commentCreate"])
        self.assertEqual(linear.only()["body"], sessions.body(record(), 0, T2))

    def test_updates_the_earliest_match_only(self):
        mine = f"Run {SID} · running · {T1}"
        others = [comment("other-sid", "2026-09-29T00:00:00.000Z", f"Run {OTHER} · running · {T1}"),
                  comment("human", "2026-09-29T00:00:01.000Z", mine, mine=False)]
        matches = [comment("b", "2026-09-30T02:00:00.000Z", mine + " b"),
                   comment("a", "2026-09-30T01:00:00.000Z", mine + " a"),
                   comment("c", "2026-09-30T03:00:00.000Z", mine + " c")]
        linear = FakeLinear(*others, *matches)
        self.assertIsNone(start(linear, record(started_at=T2)))
        self.assertEqual(linear.calls, ["comments", "commentUpdate"])
        updated = [c for c in linear.comments if c["body"] == sessions.body(record(started_at=T2))]
        self.assertEqual([c["id"] for c in updated], ["a"])
        self.assertEqual([c for c in linear.comments if c["id"] != "a"], [*others, matches[0], matches[2]])

    def test_is_comment(self):
        body = sessions.body(record())
        self.assertTrue(sessions.is_comment({"user": {"isMe": True}, "body": body}))
        for name, c in (("human, same body", {"user": {"isMe": False}, "body": body}),
                        ("harness, no prefix", {"user": {"isMe": True}, "body": "Repo check failed: no Repo: line"}),
                        ("sid not a uuid", {"user": {"isMe": True}, "body": "Run s-1 · x"}),
                        ("no user", {"user": None, "body": body}),
                        ("prefix not at the start", {"user": {"isMe": True}, "body": "see " + body})):
            with self.subTest(name):
                self.assertFalse(sessions.is_comment(c))

    def test_is_record_by_url(self):
        self.assertTrue(sessions.is_record({"title": f"Run {SID}", "url": sessions.URL + SID}))
        self.assertFalse(sessions.is_record({"title": "Run rate analysis", "url": "https://gh/x"}))

    def test_default_client_timeout_is_the_limit(self):
        found = {"issue": {"comments": {"nodes": []}}, "commentCreate": {"success": True}}
        with mock.patch.object(sessions.linear, "linear_gql", return_value=found) as gql:
            self.assertIsNone(start(None, record()))
        self.assertEqual([c.kwargs["timeout"] for c in gql.call_args_list], [sessions.LIMIT] * 2)
        self.assertEqual(sessions.LIMIT, 10)


class Failure(unittest.TestCase):
    def error(self, out, rest):
        """out is one registry-error line, no newline, ending in rest."""
        self.assertRegex(out, r"\A" + STAMP.pattern)
        self.assertEqual(out[20:], f"registry-error {rest}")

    def failing(self, exc):
        def gql(query, **v):
            raise exc
        return gql

    def test_find_failure_never_writes(self):
        linear = FakeLinear(fail={"comments": RuntimeError("boom")})
        self.error(start(linear, record()), f"{ISSUE} session={SID}: RuntimeError: boom")
        self.assertEqual(linear.calls, ["comments"])
        calls = []

        def no_issue(query, **v):
            calls.append(query)
            return {"issue": None}
        out = end(no_issue, record(), 0)
        self.assertRegex(out, r"\A" + STAMP.pattern + re.escape(f"registry-error {ISSUE} session={SID}: ") + r".+\Z")
        self.assertEqual(len(calls), 1)

    def test_write_raising_logs_one_line(self):
        existing = comment("a", "2026-09-30T01:00:00.000Z", f"Run {SID} · running · {T1}")
        for op, seed in (("commentCreate", ()), ("commentUpdate", (existing,))):
            for exc, reason in ((RuntimeError("boom"), "RuntimeError: boom"),
                                (SystemExit("linear api error: [{'message': 'nope'}]"),
                                 "SystemExit: linear api error: [{'message': 'nope'}]"),
                                (RuntimeError("two\nlines"), "RuntimeError: two lines")):
                with self.subTest(op=op, reason=reason):
                    linear = FakeLinear(*seed, fail={op: exc})
                    self.error(start(linear, record()), f"{ISSUE} session={SID}: {reason}")
                    self.assertEqual(linear.calls, ["comments", op])

    def test_success_false_logs_one_line(self):
        existing = comment("a", "2026-09-30T01:00:00.000Z", f"Run {SID} · running · {T1}")
        for op, seed in (("commentCreate", ()), ("commentUpdate", (existing,))):
            with self.subTest(op):
                self.error(start(FakeLinear(*seed, fail={op: False}), record()), f"{ISSUE} session={SID}: success: false")

    def test_blocking_gql_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        with mock.patch.object(sessions, "write", functools.partial(sessions.write, limit=0.1)):
            out = end(lambda q, **v: release.wait(30), record(), 0)
        self.assertLess(time.monotonic() - t, 5)
        self.error(out, f"{ISSUE} session={SID}: timed out after 0.1s")

    def test_secret_outside_the_message_does_not_leak(self):
        exc = subprocess.CalledProcessError(1, ["security", "find-generic-password", "-s", "svc", "-w"],
                                            output="lin_api_SECRET", stderr="lin_api_SECRET")
        exc.headers = {"Authorization": "lin_api_SECRET"}
        out = start(self.failing(exc), record())
        self.assertIn("registry-error", out)
        self.assertNotIn("lin_api_SECRET", out)


if __name__ == "__main__":
    unittest.main()
