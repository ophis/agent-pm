
from orchestrator_test_helpers.unit import board_ids
from orchestrator_test_helpers.unit.writeback.base_fixtures import Base
from orchestrator_test_helpers.unit.writeback.data import SRC_UUID, STATES


class Bounce(Base):
    DONE = (STATES["done"], STATES["in_progress"])

    def src(self, team=board_ids.TEAM):
        return {"id": SRC_UUID, "team": {"id": team}}

    def text(self, reason, tail=""):
        return (
            f"Repo check failed: {reason}. To build it, move PRD-3 to Handoff again with a comment "
            f"`Repo: <owner>/<name>` naming the target repo.{tail}"
        )

    def question(self, reason):
        return (
            "Question: repo check failed: "
            + reason
            + ". Fix the description's `Repo:` line, then move this issue back to Todo."
        )
