"""Ruff TID251 enforces unit import boundaries, with the migration inventory in guards.py."""

import json
import subprocess
import unittest
from pathlib import Path

from tests.case import REPO, Case
from tests.guards import UNIT_ISOLATION_ALLOWED

CONFIG = 'lint.flake8-tidy-imports.banned-api = {subprocess = {msg = "Unit tests cannot import subprocess"}, socket = {msg = "Unit tests cannot import socket"}}'


class UnitImports(Case):
    def test_unit_imports_obey_the_boundary(self):
        root = Path(REPO)
        files = sorted(
            str(path)
            for path in root.rglob("*.py")
            if "unit" in path.relative_to(root).parts
            and "tests" in path.relative_to(root).parts
            and str(path.relative_to(root)) not in UNIT_ISOLATION_ALLOWED
        )
        proc = subprocess.run(
            [
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--select",
                "TID251",
                "--config",
                CONFIG,
                "--output-format",
                "json",
                *files,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_the_rule_rejects_both_process_and_socket_imports(self):
        sample = Path(self.home, "sample.py")
        sample.write_text("import subprocess\nfrom socket import socket\n")
        proc = subprocess.run(
            [
                "ruff",
                "check",
                "--isolated",
                "--select",
                "TID251",
                "--config",
                CONFIG,
                "--output-format",
                "json",
                str(sample),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual([item["code"] for item in json.loads(proc.stdout)], ["TID251", "TID251"])


if __name__ == "__main__":
    unittest.main()
