"""delegate/engine: commands parse their arguments in cli.py, never in an engine module (AC34)."""

import ast
import os

from agent_pm.delegate import engine

from delegate.tests.case import Case


class EngineImports(Case):
    def test_no_engine_module_imports_argparse(self):
        root = os.path.dirname(engine.__file__)
        found = []
        for name in sorted(n for n in os.listdir(root) if n.endswith(".py")):
            with open(os.path.join(root, name), encoding="utf-8") as file:
                tree = ast.parse(file.read(), name)
            found += [
                f"{name}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                and any(a.name == "argparse" for a in node.names)
                or isinstance(node, ast.ImportFrom)
                and node.module == "argparse"
            ]
        self.assertEqual(found, [])
