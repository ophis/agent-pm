
from orchestrator_test_helpers.unit.run.case_fixtures import Case
from orchestrator_test_helpers.unit.run_fixtures import ID


class Tui(Case):
    MINE = [f"engineer-{ID}-aaaaaaaa", f"engineer-{ID}-bbbbbbbb"]

    OTHER = [
        f"engineer-{ID}-aaaaaaaa-drive",
        f"pm-{ID}-cccccccc",
        "engineer-TASK-70-dddddddd",
        f"agent-pm-engineer-{ID}",
    ]
