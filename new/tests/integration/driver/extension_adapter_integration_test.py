"""A new runtime is one package under src/agent_pm/adapters/ plus one [adapters.<name>] table: a temp copy of new/core,
the fake adapter's package copied into it as src/agent_pm/adapters/fake/ and its table appended to the copy's
config.toml, nothing else changed, runs a headless session through driver.api.start in a child Python whose
PYTHONPATH is only the copy's src/. A table without its package, or a package without its table, fails there before
anything starts."""

from new.tests import fake_adapter
from new.tests.case import CORE, PY, Case, child_env

import json
import os
import shutil
import subprocess
import tempfile
import unittest

TIMEOUT = 60  # seconds
SKIP = shutil.ignore_patterns("__pycache__")
# The child: starts runtime argv[2] headless in workdir argv[1] and prints its events; a ConfigError exits 1, its text
# on stderr.
CHILD = """import dataclasses, json, sys
from agent_pm.driver import api
try:
    handle = api.start(sys.argv[2], "headless", "hello", api.Spec(workdir=sys.argv[1]))
except api.ConfigError as e:
    sys.exit(f"ConfigError: {e}")
events = [[type(e).__name__, dataclasses.asdict(e)] for e in handle.events()]
print(json.dumps({"sid": handle.sid, "events": events,
                  "files": [api.__file__, sys.modules["agent_pm.adapters.fake"].__file__]}))
"""


class ExtensionAdapterIntegration(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        core = os.path.join(self.root, "core")
        shutil.copytree(CORE, core, ignore=SKIP)
        self.src = os.path.join(core, "src")
        self.config = os.path.join(core, "config.toml")
        self.package("fake")
        self.home = os.path.join(self.root, "home")
        os.mkdir(self.home)
        self.work = os.path.join(self.root, "work")
        scenario = os.path.join(self.root, "scenario.json")
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"out": {"type": "session", "id": "native-1"}},
                    {"out": {"type": "message", "text": "hi"}},
                    {"out": {"type": "result", "text": "done"}},
                ],
                f,
            )
        self.table(
            '[adapters.fake]\nimplementation = "adapters.fake:FakeAdapter"\n'
            f"scenario = {json.dumps(scenario)}\nlog = {json.dumps(os.path.join(self.root, 'log.jsonl'))}\n"
        )

    def package(self, name: str) -> None:
        """The fake adapter's package copied in as src/agent_pm/adapters/<name>/."""
        shutil.copytree(
            os.path.dirname(fake_adapter.__file__),
            os.path.join(self.src, "agent_pm", "adapters", name),
            ignore=SKIP,
        )

    def table(self, text: str) -> None:
        with open(self.config, "a", encoding="utf-8") as f:
            f.write("\n" + text)

    def child(self, runtime: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*PY, "-c", CHILD, self.work, runtime],
            cwd=self.root,
            env={**child_env({**os.environ, "HOME": self.home}), "PYTHONPATH": self.src},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def refused(self, runtime: str, error: str) -> None:
        """The child starting `runtime` exits 1 on ConfigError `error`, before any run record or process."""
        child = self.child(runtime)
        self.assertEqual((child.returncode, child.stdout), (1, ""), child.stderr)
        self.assertIn(f"ConfigError: {error}", child.stderr)
        self.assertFalse(os.path.exists(self.work))

    def test_a_copied_adapter_package_and_its_table_run_a_headless_session(self):
        child = self.child("fake")
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        for path in data["files"]:
            self.assertTrue(path.startswith(self.src + os.sep), path)
        self.assertEqual(
            [name for name, _ in data["events"]], ["SessionBound", "Reply", "TurnEnd", "Exited"]
        )
        self.assertEqual(data["events"][-1], ["Exited", {"returncode": 0}])
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as f:
            written = [json.loads(line) for line in f]
        self.assertEqual(
            [(e["kind"], e["sid"]) for e in written],
            [("session", data["sid"]), ("client_session", data["sid"])],
        )

    def test_a_table_without_its_package_fails_before_launch(self):
        self.table('[adapters.absent]\nimplementation = "adapters.absent:AbsentAdapter"\n')
        self.refused("absent", "adapter 'absent' has no package adapters.absent")

    def test_a_package_without_its_table_fails_before_launch(self):
        self.package("untabled")
        self.refused("untabled", "no [adapters.untabled] table")


if __name__ == "__main__":
    unittest.main()
