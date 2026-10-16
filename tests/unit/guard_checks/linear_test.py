"""Linear's typed comparisons are checked without changing shipped source."""

from tests.case import REPO
from tests.guard_checks import linear
from tests.guard_data.linear_orchestrator import ORCH_FILES
from tests.guard_samples import Sample


class EnumComparisons(Sample):
    def test_string_comparisons_in_either_order_and_membership_are_rejected(self):
        for expression in (
            'run.resumable == "yes"',
            '"unknown" != run.resumable',
            'view.status().state == "gone"',
            '"working" is view.state',
            'outcome.status != "needs_input"',
            '"done" == result.outcome.status',
            '"yes" == run.resumable == Resumable.YES',
            'Resumable.NO != run.resumable != "no"',
            'run.state in ("working", "gone")',
            'outcome.status not in {"done", "failed"}',
            'run.resumable in ["yes", Resumable.NO]',
            'run.state in {"gone": 1}',
        ):
            with self.subTest(expression=expression):
                root = self.tree({f"{ORCH_FILES}nested/example.py": expression + "\n"})
                found = linear.linear_enum_comparisons(root)
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].startswith(f"{ORCH_FILES}nested/example.py:1:"))
                self.assertIn("enum members", found[0])

    def test_enum_none_numeric_and_noncomparison_uses_are_legal(self):
        expressions = (
            "run.resumable == Resumable.YES",
            "run.resumable in (Resumable.YES, Resumable.NO)",
            "view.state is None",
            "outcome.status == OutcomeStatus.DONE",
            "view.state == StatusState.UNKNOWN",
            "response.status == 404",
            'cfg.states["done"] == issue["state"]["id"]',
            'reason == "usage"',
            'log("state", run.state)',
            'text = "run.state == gone"',
            "outcome.status in {}",
            'outcome.status in {None: "done"}',
            'obj.other == "done"',
        )
        root = self.tree({f"{ORCH_FILES}example.py": "\n".join(expressions)})
        self.assertEqual(linear.linear_enum_comparisons(root), [])

    def test_only_linear_source_is_scanned(self):
        root = self.tree(
            {
                "manager/src/agent_pm/manager/example.py": 'entry.state == "live"\n',
                "linear-orchestrator/tests/example.py": 'run.state == "working"\n',
            }
        )
        self.assertEqual(linear.linear_enum_comparisons(root), [])

    def test_real_linear_source_uses_enums(self):
        self.assertEqual(linear.linear_enum_comparisons(REPO), [])
