import contextlib, functools, io, json, os, re, shlex, subprocess, sys, threading, time, unittest
from datetime import datetime
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sessions  # noqa: E402

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
ISSUE = "TASK-12"
T1, T2 = "2026-09-30T10:00:00+08:00", "2026-09-30T11:30:00+08:00"
KEYS = {"sid", "cwd", "role", "task", "status", "started_at", "resume_command", "model"}
REPO_KEYS = {"repo", "branch", "worktree"}
STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")

def record(started_at=T1, cwd="/w/TASK-12", **kw):
    return sessions.base(sid=SID, cwd=cwd, role="engineer", task="engineering", key="linear-engineer",
                         model="opus", started_at=started_at, **kw)

class FakeLinear:
    """attachmentCreate like Linear: upserts by (issueId, url), replacing metadata wholesale."""
    def __init__(self):
        self.attachments = {}
    def __call__(self, query, **v):
        assert "attachmentCreate(input: $in)" in query, query
        a = v["in"]
        self.attachments[(a["issueId"], a["url"])] = a
        return {"attachmentCreate": {"success": True}}
    def only(self):
        assert len(self.attachments) == 1, self.attachments
        return next(iter(self.attachments.values()))

def main(argv, gql):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sessions.main(argv, gql=gql)
    assert rc == 0, rc
    return out.getvalue()

def start(gql, rec):
    return main(["start", ISSUE, json.dumps(rec)], gql)

def end(gql, rec, rc):
    return main(["end", ISSUE, json.dumps(rec), str(rc)], gql)


class Record(unittest.TestCase):
    def test_start_creates_one_running_attachment(self):
        linear, rec = FakeLinear(), record()
        self.assertEqual(start(linear, rec), "")
        a = linear.only()
        cmd = f"cd /w/TASK-12 && LINEAR_KEYCHAIN_SERVICE=linear-engineer claude --resume {SID}"
        self.assertEqual((a["issueId"], a["url"], a["title"]), (ISSUE, f"https://agent-pm.invalid/run/{SID}", f"Run {SID}"))
        self.assertEqual(a["subtitle"], f"running · {T1} · {cmd}")
        self.assertEqual(a["metadata"], {"sid": SID, "cwd": "/w/TASK-12", "role": "engineer", "task": "engineering",
                                         "status": "running", "started_at": T1, "resume_command": cmd, "model": "opus"})

    def test_metadata_keys_are_the_record_fields_only(self):
        linear = FakeLinear()
        start(linear, record())
        self.assertEqual(set(linear.only()["metadata"]), KEYS)
        linear = FakeLinear()
        start(linear, record(repo="ophis/agent-pm", branch="TASK-12-registry", worktree="/w/TASK-12/worktrees/x"))
        m = linear.only()["metadata"]
        self.assertEqual(set(m), KEYS | REPO_KEYS)
        self.assertEqual((m["repo"], m["branch"], m["worktree"]), ("ophis/agent-pm", "TASK-12-registry", "/w/TASK-12/worktrees/x"))

    def test_resume_command_quotes_the_cwd(self):
        cwd = "/Users/x/my work/it's/TASK-12"
        self.assertEqual(shlex.split(record(cwd=cwd)["resume_command"]),
                         ["cd", cwd, "&&", "LINEAR_KEYCHAIN_SERVICE=linear-engineer", "claude", "--resume", SID])

    def test_recover_resume_updates_the_one_record(self):
        linear = FakeLinear()
        start(linear, record())
        end(linear, record(), 3)
        start(linear, record(started_at=T2))
        m = linear.only()["metadata"]
        self.assertEqual((m["status"], m["started_at"]), ("running", T2))
        self.assertNotIn("ended_at", m)
        self.assertNotIn("exit", m)

    def test_end_sets_status_times_and_exit(self):
        for rc, status in ((0, "done"), (3, "interrupted")):
            with self.subTest(rc=rc):
                linear, began = FakeLinear(), sessions.now()
                rec = record(started_at=began)
                start(linear, rec)
                self.assertEqual(end(linear, rec, rc), "")
                a = linear.only()
                m = a["metadata"]
                self.assertEqual((m["status"], m["exit"], m["started_at"]), (status, rc, began))
                for t in (m["started_at"], m["ended_at"]):
                    self.assertIsNotNone(datetime.fromisoformat(t).utcoffset(), t)
                self.assertEqual(a["subtitle"], f"{status} · {began} → {m['ended_at']} · exit {rc} · {rec['resume_command']}")

    def test_is_record_by_url(self):
        self.assertTrue(sessions.is_record({"title": f"Run {SID}", "url": sessions.url(SID)}))
        self.assertFalse(sessions.is_record({"title": "Run rate analysis", "url": "https://gh/x"}))

    def test_default_client_timeout_is_the_limit(self):
        with mock.patch.object(sessions.pipeline, "linear_gql", return_value={"attachmentCreate": {"success": True}}) as gql:
            self.assertEqual(start(None, record()), "")
        self.assertEqual(gql.call_args.kwargs["timeout"], sessions.LIMIT)
        self.assertEqual(sessions.LIMIT, 10)


class Failure(unittest.TestCase):
    def error(self, out, rest):
        """out is one registry-error line ending in rest."""
        self.assertRegex(out, r"\A" + STAMP.pattern)
        self.assertEqual(out[20:], f"registry-error {rest}\n")

    def failing(self, exc):
        def gql(query, **v):
            raise exc
        return gql

    def test_gql_raising_logs_one_line(self):
        for exc, reason in ((RuntimeError("boom"), "RuntimeError: boom"),
                            (SystemExit("linear api error: [{'message': 'nope'}]"),
                             "SystemExit: linear api error: [{'message': 'nope'}]"),
                            (RuntimeError("two\nlines"), "RuntimeError: two lines")):
            with self.subTest(reason=reason):
                self.error(start(self.failing(exc), record()), f"{ISSUE} session={SID}: {reason}")

    def test_success_false_logs_one_line(self):
        out = start(lambda q, **v: {"attachmentCreate": {"success": False}}, record())
        self.error(out, f"{ISSUE} session={SID}: success: false")

    def test_blocking_gql_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        with mock.patch.object(sessions, "write", functools.partial(sessions.write, limit=0.1)):
            out = end(lambda q, **v: release.wait(30), record(), 0)
        self.assertLess(time.monotonic() - t, 5)
        self.error(out, f"{ISSUE} session={SID}: timed out after 0.1s")

    def test_bad_input_logs_one_line_without_writing(self):
        rec = json.dumps(record())
        def gql(query, **v):
            raise AssertionError("no write")
        for argv, rest in (([], "? session=?: ValueError: usage: "),
                           (["start", ISSUE], f"{ISSUE} session=?: ValueError: usage: "),
                           (["stop", ISSUE, rec], f"{ISSUE} session=?: ValueError: usage: "),
                           (["end", ISSUE, rec], f"{ISSUE} session=?: ValueError: usage: "),
                           (["start", ISSUE, rec, "0"], f"{ISSUE} session=?: ValueError: usage: "),
                           (["start", ISSUE, "{not json"], f"{ISSUE} session=?: JSONDecodeError: "),
                           (["start", ISSUE, "[]"], f"{ISSUE} session=?: ValueError: RECORD_JSON is not an object"),
                           (["end", ISSUE, rec, "x"], f"{ISSUE} session={SID}: ValueError: invalid literal")):
            with self.subTest(argv=argv):
                out = main(argv, gql)
                self.assertRegex(out, r"\A" + STAMP.pattern + "registry-error " + re.escape(rest))
                self.assertEqual(out.count("\n"), 1)
                self.assertTrue(out.endswith("\n"))

    def test_secret_outside_the_message_does_not_leak(self):
        exc = subprocess.CalledProcessError(1, ["security", "find-generic-password", "-s", "svc", "-w"],
                                            output="lin_api_SECRET", stderr="lin_api_SECRET")
        exc.headers = {"Authorization": "lin_api_SECRET"}
        out = start(self.failing(exc), record())
        self.assertIn("registry-error", out)
        self.assertNotIn("lin_api_SECRET", out)


if __name__ == "__main__":
    unittest.main()
