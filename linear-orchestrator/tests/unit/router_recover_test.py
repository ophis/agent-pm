import os

from agent_pm.linear_orchestrator.engine import router
from agent_pm.session import ConfigError, DriverError, SessionStatus
from orchestrator_test_helpers.unit.router.data import CONFIG, OVERLAY, ago, issue
from orchestrator_test_helpers.unit.router.recover_fixtures import Recover as RecoverBase


class Recover1(RecoverBase):
    def test_a_session_active_within_live_is_not_resumed(self):
        for minutes, out in (
            (10, ""),
            (29, ""),
            (31, "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"),
        ):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
                self.resumable("TASK-1", "a", 60)
                self.touch("a", minutes)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual((fake.mutations, self.said()), ([], []))

    def test_not_resumable_goes_to_todo_after_live(self):
        for minutes, out, state in ((20, "", "In Progress"), (40, "new", "Todo")):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
                self.moved("TASK-1", minutes + 1)
                self.add("start", "TASK-1", "a", minutes)
                self.api.statuses[self.sid("a")] = SessionStatus("no", None, "")
                self.assertEqual(self.plan(fake), out)
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t.get("comments")), (state, [router.INTERRUPTED] if out else None)
                )
                self.assertEqual(
                    self.said(),
                    ["recover TASK-1 to=todo reason=not resumable sid=a"] if out else [],
                )

    def test_an_unknown_session_changes_nothing_and_is_said_once_a_day(self):
        statuses = (
            SessionStatus("unknown", None, ""),
            DriverError("boom"),
            ConfigError("no runtime a"),
            OSError("gone"),
        )
        for st in statuses:
            with self.subTest(st=st):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
                self.resumable("TASK-1", "a", 300)
                self.api.statuses[self.sid("a")] = st
                for again, said in (
                    (False, ["recover-skip TASK-1 sid=a reason=session unknown"]),
                    (True, []),
                ):
                    self.assertEqual(self.plan(fake, again=again), "")
                    self.assertEqual(self.said(), said)
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations), ("In Progress", [])
                )
                with open(self.runs) as f:
                    self.assertEqual(f.read(), "".join(f"{line}\n" for line in self.lines))

    def test_an_unknown_session_is_not_settled_without_the_wait(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
        self.resumable("TASK-1", "a", 300)
        self.api.statuses[self.sid("a")] = DriverError("boom")

        def use(b):
            (found,) = b.issues("in_progress")
            return b.settle(found, b.current_sid(found), wait=False)

        self.assertFalse(self.board(fake, use))
        self.assertEqual(self.said(), ["recover-skip TASK-1 sid=a reason=session unknown"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Progress", []))

    def test_one_status_read_per_sid_with_its_recorded_runtime_at_the_workdir(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.write_config(CONFIG.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )
        self.assertEqual(
            self.api.info, [("a", self.sid("a"), os.path.join(self.work, "work", "TASK-1"))]
        )
