"""The standalone macOS replay uses the real reader's logic and only stdlib imports."""

import ast
import sys
from pathlib import Path

from manager.tests.case import REPO, Case


class ReproFollow(Case):
    def test_reader_snapshots_match_source(self):
        root = Path(REPO)
        standalone = ast.parse((root / "manager/repro_follow.py").read_text())
        sources = {
            "utils/src/agent_pm/utils/files.py": ["read_regular", "_read_fd", "file_lock"],
            "utils/src/agent_pm/utils/text.py": ["printable"],
            "manager/src/agent_pm/manager/engine/events.py": [
                "_identity",
                "Events.identity",
                "Events.read",
                "Events.following",
                "Events.count",
                "Events._due",
                "Events.due",
            ],
            "manager/src/agent_pm/manager/engine/roster.py": [
                "Roster._cursor",
                "Roster.watch_events",
                "Roster.event_identity",
                "Roster.next_event",
            ],
            "manager/src/agent_pm/manager/engine/crew.py": ["Crew.next_event"],
            "manager/src/agent_pm/manager/cli.py": ["_next_event"],
        }

        def function(tree, name):
            parts = name.split(".")
            nodes = tree.body
            if len(parts) == 2:
                nodes = next(
                    node
                    for node in nodes
                    if isinstance(node, ast.ClassDef) and node.name == parts[0]
                ).body
            return next(
                node
                for node in nodes
                if isinstance(node, ast.FunctionDef) and node.name == parts[-1]
            )

        for file, names in sources.items():
            source = ast.parse((root / file).read_text())
            for name in names:
                with self.subTest(file=file, name=name):
                    self.assertEqual(
                        ast.dump(function(standalone, name)), ast.dump(function(source, name))
                    )

    def test_standalone_imports_are_only_standard_library(self):
        tree = ast.parse(Path(REPO, "manager/repro_follow.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                names = [node.module]
            else:
                continue
            for name in names:
                self.assertIn(name.split(".")[0], sys.stdlib_module_names)
