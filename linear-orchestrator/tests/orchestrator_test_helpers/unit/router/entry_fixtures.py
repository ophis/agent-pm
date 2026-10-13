
from orchestrator_test_helpers.unit.router.claim_linear_fixtures import ClaimLinear
from orchestrator_test_helpers.unit.router.data import issue
from orchestrator_test_helpers.unit.router.outer_base_fixtures import OuterBase
from orchestrator_test_helpers.unit.run_fixtures import ID, PROJECT, USER_NOTE, node


class Entry(OuterBase):
    """router.main end to end through its own outer (the old AttendedEntry's headless parts)."""

    def setUp(self):
        super().setUp()
        self.todo = issue(ID, "Todo", "engineer")
        self.todo["project"] = {"id": PROJECT, "name": "Agent PM"}
        self.fake = ClaimLinear(self.todo, node(comments=[USER_NOTE]))

    def entry(self, *argv, live=(), tty=False):
        outer = {"run": self.run_, "keychain": self.keychain}
        return self.tick(self.fake, *argv, live=live, again=True, tty=tty, outer=outer)
