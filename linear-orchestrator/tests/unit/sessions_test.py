from datetime import datetime

from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.linear import linear, sessions
from orchestrator_test_helpers.unit.sessions.data import (
    CID,
    ISSUE,
    OTHER,
    RUNTIME,
    SID,
    T1,
    T2,
    WORKDIR,
    comment,
    end,
    start,
    text,
)
from orchestrator_test_helpers.unit.sessions.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.sessions.record_fixtures import Record as RecordBase


class Record1(RecordBase):
    def test_base(self):
        self.assertEqual(
            self.record(),
            {
                "sid": SID,
                "workdir": WORKDIR,
                "runtime": RUNTIME,
                "started_at": T1,
                "seq": 0,
                "driver": self.api,
            },
        )

    def test_post_creates_by_session_id_then_updates_it(self):
        for rc, line in (
            (None, f"running · #1 · {T1}"),
            (0, f"done · #2 · {T1} → {T2} · exit 0"),
            (3, f"interrupted · #2 · {T1} → {T2} · exit 3"),
        ):
            with self.subTest(rc=rc):
                fake, rec = FakeLinear(), self.record()
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

        self.assertIsNone(start(gql, self.record()))
        body = text(f"running · #1 · {T1}")
        self.assertEqual(
            calls,
            [
                (sessions.M_UPDATE, {"c": CID, "b": body}),
                (linear.M_COMMENT_ID, {"c": CID, "i": ISSUE, "b": body}),
            ],
        )

    def test_each_body_takes_the_next_seq(self):
        rec = self.record()
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
                        self.record(),
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
        rec, asked = self.record(), Outcome("needs_input", "title", "summary")
        self.assertEqual(sessions.body(rec, limit=True), text(f"running · #1 · {T1}"))
        self.assertEqual(
            sessions.body(rec, -9, T2, limit=True), text(f"limit · #2 · {T1} → {T2} · exit -9")
        )
        self.assertEqual(
            sessions.body(rec, -9, T2, outcome=asked, limit=True),
            text(f"needs_input · #3 · {T1} → {T2} · exit -9"),
        )
        fake = FakeLinear()
        self.assertIsNone(
            sessions.post(ISSUE, self.record(), -9, gql=fake, ended_at=T2, limit=True)
        )
        self.assertEqual(fake.only()["body"], text(f"limit · #1 · {T1} → {T2} · exit -9"))

    def test_recover_resume_updates_the_one_comment(self):
        fake, rec = FakeLinear(), self.record()
        start(fake, rec)
        end(fake, rec, 3)
        start(fake, self.record(started_at=T2))
        body = fake.only()["body"]
        self.assertEqual(body.splitlines()[0], f"Run {SID} · running · #1 · {T2}")
        self.assertEqual(body, sessions.body(self.record(started_at=T2)))

    def test_end_defaults_to_now(self):
        fake = FakeLinear()
        self.assertIsNone(sessions.post(ISSUE, self.record(), 0, gql=fake, ended_at=T2))
        self.assertEqual(fake.only()["body"], sessions.body(self.record(), 0, T2))

    def test_now_is_local_iso_with_offset(self):
        stamp = sessions.now()
        self.assertIsNotNone(datetime.fromisoformat(stamp).utcoffset(), stamp)

    def test_no_cli(self):
        self.assertFalse(hasattr(sessions, "main") or hasattr(sessions, "USAGE"))

    def test_failed_start_leaves_end_to_create_the_comment(self):
        fake, rec = FakeLinear(fail={"commentCreate": RuntimeError("down")}), self.record()
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
        self.assertIsNone(start(fake, self.record(started_at=T2)))
        self.assertEqual(fake.calls, ["commentUpdate"])
        self.assertEqual(fake.comments[CID]["body"], text(f"running · #1 · {T2}"))
        self.assertEqual([fake.comments[c["id"]] for c in others], others)

    def test_is_comment(self):
        body = sessions.body(self.record())
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
            sessions.post(ISSUE, self.record())
