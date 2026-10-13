from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import TASK_GROUP
from orchestrator_test_helpers.unit.linear.data import LABEL1, LABEL2, OTHER
from orchestrator_test_helpers.unit.linear.task_group_check_fixtures import (
    TaskGroupCheck as TaskGroupCheckBase,
)


class TaskGroupCheck1(TaskGroupCheckBase):
    def test_group_ok(self):
        for group, labels in ((self.group(LABEL1, LABEL2, OTHER), self.LABELS), (self.group(), {})):
            with self.subTest(labels=labels):
                gql = self.gql(group)
                self.assertIsNone(linear.task_group(gql, self.cfg(labels)))
                self.assertEqual(gql.calls, [(linear.Q_TASK_GROUP, {"i": TASK_GROUP})])
        self.assertIn(
            "issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } }",
            linear.Q_TASK_GROUP,
        )

    def test_fails(self):
        error = "linear api error: [{'message': 'Entity not found: IssueLabel'}]"
        not_labels = f"[task_labels] not labels of task_label_group {TASK_GROUP}: "
        cases = [
            (self.gql(self.group(LABEL1)), self.LABELS, not_labels + f"deep-research {LABEL2}"),
            (
                self.gql(self.group(OTHER)),
                {"deep-research": LABEL2, "light-research": LABEL1, "ok": OTHER},
                not_labels + f"deep-research {LABEL2}, light-research {LABEL1}",
            ),
            (
                self.gql(error=error),
                self.LABELS,
                f"task_label_group {TASK_GROUP} not found in Linear: {error}",
            ),
            (
                self.gql(
                    {"isGroup": False, "children": {"nodes": [{"id": LABEL1}, {"id": LABEL2}]}}
                ),
                self.LABELS,
                f"task_label_group {TASK_GROUP} is not a label group",
            ),
        ]
        for gql, labels, message in cases:
            with self.subTest(message):
                with self.assertRaises(LinearError) as cm:
                    linear.task_group(gql, self.cfg(labels))
                self.assertEqual(
                    str(str(cm.exception)), "linear-orchestrator/config.toml: " + message
                )
                self.assertEqual(len(gql.calls), 1)
