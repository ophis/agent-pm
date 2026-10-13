"""Import Linter reports forbidden graph edges and their source locations."""

import tomllib
from pathlib import Path

import grimp

from tests.case import REPO, Case
from tests.import_contracts import UnitsContract


class Units(Case):
    def test_contract_reports_real_graph_edges_and_line_numbers(self):
        source, target = "agent_pm.session.adapters.codex", "agent_pm.session.hosts.tables"
        graph = grimp.ImportGraph()
        for module in (source, target):
            graph.add_module(module)
        graph.add_import(
            importer=source, imported=target, line_number=7, line_contents="import " + target
        )
        config = tomllib.loads(Path(REPO, "pyproject.toml").read_text())
        policy = next(
            c for c in config["tool"]["importlinter"]["contracts"] if c["type"] == "units"
        )
        check = UnitsContract(policy["name"], {}, policy).check(graph, verbose=False)
        self.assertFalse(check.kept)
        self.assertEqual(
            [(e["importer"], e["imported"], e["line_number"]) for e in check.metadata["broken"]],
            [(source, target, 7)],
        )
