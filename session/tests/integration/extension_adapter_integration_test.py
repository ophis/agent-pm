"""A new runtime is one importable module plus one [adapters.<name>] table naming it as
module.path:Class: a temp copy of session and utils, the fake adapter's package copied in outside
them as module extension_runtime (a name unrelated to the runtime's) and its table appended to the
copy's session/config.toml, nothing else changed, runs a headless session through Driver.start in a
child Python whose PYTHONPATH is only the copy's src/ dirs and that module's dir. A table without its
module, or a module without its table, fails there before anything starts.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from session.tests import fake_adapter
from session.tests.case import PY, REPO, Case, child_env

TIMEOUT = 60  # seconds
SKIP = shutil.ignore_patterns("__pycache__")
PROJECTS = ("session", "utils")
MODULE = "extension_runtime"
# The child: configures the session from the copy's session config, starts runtime argv[2] headless
# in workdir argv[1] and prints its events; a ConfigError exits 1, its text on stderr.
CHILD = """import dataclasses, json, sys
from agent_pm.session import Driver, ConfigError, Spec
from agent_pm.session.engine import hosted
from agent_pm.utils import read_project
try:
    driver = Driver.from_config(read_project("session"))
    handle = driver.start(sys.argv[2], "headless", "hello", Spec(workdir=sys.argv[1]))
except ConfigError as e:
    sys.exit(f"ConfigError: {e}")
events = [[type(e).__name__, dataclasses.asdict(e)] for e in handle.events()]
print(json.dumps({"sid": handle.sid, "events": events,
                  "files": [hosted.__file__, sys.modules["extension_runtime"].__file__]}))
"""


class ExtensionAdapterIntegration(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.src = os.path.join(self.root, "session", "src")
        self.extensions = os.path.join(self.root, "extensions")
        self.package(MODULE)
        self.package("untabled")
        for project in PROJECTS:
            shutil.copytree(
                os.path.join(REPO, project),
                os.path.join(self.root, project),
                ignore=SKIP,
                dirs_exist_ok=True,
            )
        self.srcs = os.pathsep.join(
            (*(os.path.join(self.root, p, "src") for p in PROJECTS), self.extensions)
        )
        self.config = os.path.join(self.root, "session", "config.toml")
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
            f'[adapters.fake]\nimplementation = "{MODULE}:FakeAdapter"\n'
            f"scenario = {json.dumps(scenario)}\nlog = {json.dumps(os.path.join(self.root, 'log.jsonl'))}\n"
        )

    def package(self, name: str) -> None:
        """The fake adapter's package copied in as top-level package <name>, outside session."""
        shutil.copytree(
            os.path.dirname(fake_adapter.__file__),
            os.path.join(self.extensions, name),
            ignore=SKIP,
        )

    def table(self, text: str) -> None:
        with open(self.config, "a", encoding="utf-8") as f:
            f.write("\n" + text)

    def child(self, runtime: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*PY, "-c", CHILD, self.work, runtime],
            cwd=self.root,
            env={**child_env({**os.environ, "HOME": self.home}), "PYTHONPATH": self.srcs},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def refused(self, runtime: str, error: str) -> None:
        """The child starting `runtime` exits 1 on ConfigError `error`, before any run record or
        process.
        """
        child = self.child(runtime)
        self.assertEqual((child.returncode, child.stdout), (1, ""), child.stderr)
        self.assertIn(f"ConfigError: {error}", child.stderr)
        self.assertFalse(os.path.exists(self.work))

    def test_a_copied_adapter_package_and_its_table_run_a_headless_session(self):
        child = self.child("fake")
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        hosted, module = data["files"]
        self.assertTrue(hosted.startswith(self.src + os.sep), hosted)
        self.assertTrue(module.startswith(os.path.join(self.extensions, MODULE) + os.sep), module)
        self.assertEqual([name for name, _ in data["events"]], ["Reply", "TurnEnd", "Exited"])
        self.assertEqual(data["events"][-1], ["Exited", {"returncode": 0}])
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as f:
            written = [json.loads(line) for line in f]
        self.assertEqual(
            [(e["kind"], e["sid"]) for e in written],
            [("session", data["sid"]), ("client_session", data["sid"])],
        )

    def test_a_table_without_its_module_fails_before_launch(self):
        self.table('[adapters.absent]\nimplementation = "absent_runtime:AbsentAdapter"\n')
        self.refused(
            "absent",
            "[adapters.absent].implementation: absent_runtime:AbsentAdapter: cannot import "
            "absent_runtime: No module named 'absent_runtime'",
        )

    def test_a_module_without_its_table_fails_before_launch(self):
        self.assertTrue(os.path.isdir(os.path.join(self.extensions, "untabled")))
        self.refused("untabled", "no [adapters.untabled] table")


if __name__ == "__main__":
    unittest.main()
