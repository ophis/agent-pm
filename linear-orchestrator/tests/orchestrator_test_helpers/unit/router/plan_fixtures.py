
from orchestrator_test_helpers.unit.router.base_fixtures import Base
from orchestrator_test_helpers.unit.router.data import CONFIG, HUMANS, STATES, USER, ago, issue
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear


class Plan(Base):
    def walk_fixture(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=2),
            issue("TASK-4", "In Progress", "researcher", priority=2),
            issue("TASK-3", "In Progress", "researcher", priority=3),
            issue("TASK-5", "In Progress", "researcher", priority=4),
            issue("TASK-6", "In Progress", "researcher", priority=4),
            issue("TASK-9", "Todo", "researcher", priority=1),
        )
        self.moved("TASK-1", 400)
        self.add("start", "TASK-1", "a", 399)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-6", "f", 50)
        for ident, sid in (("TASK-4", "d"), ("TASK-5", "e")):
            for m in (390, 380, 370, 360):
                self.add("start", ident, f"{sid}{m}", m)
        return fake

    def reset_fixture(self):
        self.write_config(HUMANS + CONFIG)
        self.moved("TASK-1", 700)
        self.add("start", "TASK-1", "old", 699)
        for m in (650, 600, 550):
            self.add("resume", "TASK-1", "old", m)
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.resumable("TASK-1", "new", 100)
        self.add("resume", "TASK-1", "new", 60)

    def capped(self, hist):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], {"TASK-1": hist}
        )
        for i, sid in enumerate("abcd"):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.touch("d", 200)
        return fake, self.plan(fake)
