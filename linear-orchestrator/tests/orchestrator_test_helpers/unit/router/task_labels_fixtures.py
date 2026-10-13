
from orchestrator_test_helpers.unit.router.base_fixtures import Base
from orchestrator_test_helpers.unit.router.data import CONFIG, HUMANS, TASK_LABELS


class TaskLabels(Base):
    ORPHAN = (
        'Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). '
        "Fix the label or the assignee, then move the issue back to Todo."
    )

    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
