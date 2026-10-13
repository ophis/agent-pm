from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine.types import LinearError
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.board_ids import TASK_GROUP
from orchestrator_test_helpers.unit.linear.data import LABEL1, LABEL2


class TaskGroupCheck(Case):
    LABELS = {"light-research": LABEL1, "deep-research": LABEL2}

    def cfg(self, labels=LABELS):
        return SimpleNamespace(task_label_group=TASK_GROUP, task_labels=labels)

    def gql(self, node=None, error=None):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if error:
                raise LinearError(error)
            return {"issueLabel": node}

        gql.calls = calls
        return gql

    def group(self, *ids):
        return {"isGroup": True, "children": {"nodes": [{"id": i} for i in ids]}}
