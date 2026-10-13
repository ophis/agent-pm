import threading
import time

from agent_pm.linear_orchestrator.linear import sessions
from orchestrator_test_helpers.unit.sessions.data import (
    CID,
    ISSUE,
    OTHER_ISSUE,
    SID,
    T1,
    T2,
    T3,
    comment,
    end,
    start,
    text,
)
from orchestrator_test_helpers.unit.sessions.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.sessions.late_fixtures import Late
from orchestrator_test_helpers.unit.sessions.repair_fixtures import Repair as RepairBase


class Repair1(RepairBase):
    def test_a_late_start_write_is_repaired_with_a_higher_seq(self):
        late = Late(self.fake)
        self.addCleanup(late.release.set)
        self.assertEqual(start(late, self.rec, timeout=0.1), "timed out after 0.1s")
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
        self.rec = self.record(started_at=T2)
        self.assertEqual(start(down, self.rec), "RuntimeError: down")
        self.assertEqual(end(down, self.rec, 0, ended_at=T3), "RuntimeError: down")
        self.fake = FakeLinear(comment(CID, text(f"done · #3 · {self.DONE}")))
        self.assertIsNone(self.repair())
        self.assertEqual(self.fake.calls, ["comment", "commentUpdate"])
        self.assertEqual(self.fake.only()["body"], text(f"done · #3 · {T2} → {T3} · exit 0"))

    def test_errors_return_a_string_and_write_nothing(self):
        theirs = f"LinearError: comment {CID} is not ours"
        body = f"Run {SID} · running · #1 · {T1}"
        for name, seed, fail, reason in (
            ("find fails", (), {"comment": RuntimeError("boom")}, "RuntimeError: boom"),
            ("another issue", (comment(CID, body, issue=OTHER_ISSUE),), {}, theirs),
            ("another author", (comment(CID, body, mine=False),), {}, theirs),
        ):
            with self.subTest(name):
                self.rec = self.record()
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
            self.repair(lambda _q, **_v: release.wait(30), limit=0.1), "timed out after 0.1s"
        )
        self.assertLess(time.monotonic() - t, 5)
