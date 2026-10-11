"""A new core client works from its module and config table, without changing production dispatch."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CORE = Path(__file__).resolve().parents[3]


class ExtensionClientIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.core = self.root / "core"
        shutil.copytree(CORE, self.core)
        self.home = self.root / "home"
        self.home.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(self.core / "src")}
        (self.core / "src" / "clients" / "extension.py").write_text(
            "from .base import Client, Launch\n"
            "class CustomAdapter(Client):\n"
            "    keys = frozenset({'banner'})\n"
            "    def handover(self):\n"
            "        return self.config['banner']\n"
            "    def launch(self, prompt, run, *, params, access):\n"
            "        return Launch(argv=['extension-runner', '--tier', str(run.tier), '--effort', run.effort, prompt],\n"
            "                      cwd=access.cwd)\n"
        )

    def config(self, text):
        with (self.core / "config.toml").open("a") as f:
            f.write("\n" + text)

    def drive(self, client):
        return subprocess.run(
            [sys.executable, str(self.core / "src" / "drive.py"), "--client", client,
             "--role", "dummy-tester", "--task", "echo", "--input", "Hello.",
             "--out", str(self.work / "out.md"), "--workdir", str(self.work), "--dry-run"],
            cwd=self.work, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)

    def test_extension_module_and_config_support_dry_run_capability_catalog_and_resolution(self):
        self.config('[clients.extension]\nimplementation = "CustomAdapter"\ntier = 4\nbanner = "Extension client handover."\n')
        result = self.drive("extension")
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(result.stdout)
        self.assertEqual(launch["argv"][:5], ["extension-runner", "--tier", "4", "--effort", "low"])
        self.assertIn("Extension client handover.", launch["argv"][5])
        self.assertIn("# Input\n\nHello.", launch["argv"][5])

        script = ("import json, sys, clients, compose; "
                  "root = sys.argv[1]; client = clients.get('extension', root); "
                  "print(json.dumps({'handover': client.handover(), "
                  "'tier': compose.resolve(root, client, 'dummy-tester', 'echo').tier, "
                  "'catalog': clients.catalog('extension', root)}))")
        api = subprocess.run([sys.executable, "-c", script, str(self.core)], cwd=self.work, env=self.env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(api.returncode, 0, api.stderr)
        data = json.loads(api.stdout)
        self.assertEqual(data["handover"], "Extension client handover.")
        self.assertEqual(data["tier"], 4)
        self.assertEqual(list(data["catalog"]), ["researcher", "pm", "engineer", "dummy-tester"])
        self.assertIn("echo", data["catalog"]["dummy-tester"])

    def test_configured_client_without_module_fails_before_launch(self):
        self.config('[clients.absent]\nimplementation = "AbsentClient"\n')
        result = self.drive("absent")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("client 'absent' has no module clients.absent", result.stderr)

    def test_module_without_config_table_fails_before_launch(self):
        result = self.drive("extension")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("no [clients.extension] table", result.stderr)


if __name__ == "__main__":
    unittest.main()
