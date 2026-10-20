"""The Linear layer's own rules are checked without changing shipped source."""

import tomllib
from pathlib import Path

from tests.case import REPO
from tests.guard_checks import linear
from tests.guard_data.linear_orchestrator import FLOW_CONTRACT, ORCH_FILES, ORCH_PKG
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


ENGINE = f"{ORCH_FILES}engine/"
FLOW = f"{ORCH_FILES}flow/"


def contract(*modules, name=FLOW_CONTRACT, indirect=None):
    lines = [
        "[[tool.importlinter.contracts]]",
        f"name = {name!r}",
        'type = "forbidden"',
        f'source_modules = ["{ORCH_PKG}.flow"]',
        f"forbidden_modules = {[f'{ORCH_PKG}.engine.{m}' for m in modules]!r}",
    ]
    if indirect is not None:
        lines.append(f"allow_indirect_imports = {str(indirect).lower()}")
    return "\n".join(lines) + "\n"


class FlowBindingContract(Sample):
    BINDING = ("client", "issue", "issues", "ops", "related", "run_comments")

    def engine(self, config, **extra):
        files = {f"{ENGINE}{m}.py": "X = 1\n" for m in self.BINDING}
        files |= {f"{ENGINE}router.py": "X = 1\n", "pyproject.toml": config}
        return self.tree(files | {f"{ENGINE}{m}.py": t for m, t in extra.items()})

    def test_a_contract_forbidding_every_binding_module_passes(self):
        root = self.engine(contract(*self.BINDING))
        self.assertEqual(linear.flow_binding_contract(root), [])

    def test_issue_modules_and_query_text_modules_are_binding(self):
        root = self.engine(
            contract(*self.BINDING),
            issue_x="X = 1\n",
            adapter='Q = "query($i: String!) { issue(id: $i) { id } }"\n',
            words='"""Builds no query (or mutation) here."""\n',
        )
        self.assertEqual(
            linear.flow_binding_contract(root),
            [
                f"pyproject.toml: {FLOW_CONTRACT!r} must forbid {ORCH_PKG}.engine.{m}"
                for m in ("adapter", "issue_x")
            ],
        )

    def test_a_missing_module_contract_or_indirect_allowance_is_reported(self):
        missing = self.engine(contract(*self.BINDING[1:]))
        self.assertEqual(
            linear.flow_binding_contract(missing),
            [f"pyproject.toml: {FLOW_CONTRACT!r} must forbid {ORCH_PKG}.engine.client"],
        )
        none = self.engine(contract(*self.BINDING, name="other"))
        self.assertEqual(
            linear.flow_binding_contract(none),
            [f"pyproject.toml: no contract named {FLOW_CONTRACT!r}"],
        )
        indirect = self.engine(contract(*self.BINDING, indirect=True))
        self.assertEqual(
            linear.flow_binding_contract(indirect),
            [f"pyproject.toml: {FLOW_CONTRACT!r} must not allow indirect imports"],
        )

    def test_the_real_contract_lists_every_binding_module(self):
        self.assertEqual(linear.flow_binding_contract(REPO), [])
        contracts = tomllib.loads(Path(REPO, "pyproject.toml").read_text())["tool"]["importlinter"]
        found = next(c for c in contracts["contracts"] if c["name"] == FLOW_CONTRACT)
        self.assertEqual(found["source_modules"], [f"{ORCH_PKG}.flow"])


class FlowNamesNoLinear(Sample):
    def test_linear_concepts_and_query_text_are_reported(self):
        for line, match in (
            ("project: str", "project"),
            ("    email: str | None", "email"),
            ('"""else its state id."""', "state id"),
            ("# a label ID", "label ID"),
            ("# Linear's keys", "Linear"),
            ("# GraphQL text", "GraphQL"),
            ('Q = "query($i: String!) { x }"', "query($"),
            ('M = "mutation { x }"', "mutation {"),
        ):
            with self.subTest(line=line):
                root = self.tree({f"{FLOW}types.py": f"X = 1\n{line}\n"})
                self.assertEqual(
                    linear.flow_names_no_linear(root),
                    [f"{FLOW}types.py:2: flow names a Linear concept: {match}"],
                )

    def test_package_paths_toml_and_other_trees(self):
        root = self.tree(
            {
                f"{FLOW}workflow.py": 'REL = "linear-orchestrator/src/agent_pm/linear_orchestrator/x"\n',
                f"{FLOW}workflow.toml": "# [linear_orchestrator.roles] project\n",
                f"{ENGINE}ops.py": 'Q = "query { project { id } }"\n',
            }
        )
        self.assertEqual(
            linear.flow_names_no_linear(root),
            [f"{FLOW}workflow.toml:1: flow names a Linear concept: project"],
        )

    def test_real_flow_names_no_linear_concept(self):
        self.assertEqual(linear.flow_names_no_linear(REPO), [])
