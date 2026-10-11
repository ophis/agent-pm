"""A new runtime is one package under src/adapters/ plus one [adapters.<name>] table: a temp copy of new/core, the fake
adapter's package copied into it as src/adapters/fake/ and its table appended to the copy's config.toml, nothing else
changed, runs a headless session through driver.api.start in an isolated child Python that sees only the copy."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from new.tests import CORE, fake_adapter

TIMEOUT = 60   # seconds
# The child: argv[1] goes first on sys.path; it starts runtime fake headless in workdir argv[2] and prints its events.
CHILD = """import dataclasses, json, sys
sys.path.insert(0, sys.argv[1])
from driver import api
handle = api.start("fake", "headless", "hello", api.Spec(workdir=sys.argv[2]))
events = [[type(e).__name__, dataclasses.asdict(e)] for e in handle.events()]
print(json.dumps({"sid": handle.sid, "events": events,
                  "files": [api.__file__, sys.modules["adapters.fake"].__file__]}))
"""


class ExtensionAdapterIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        core = os.path.join(self.root, "core")
        skip = shutil.ignore_patterns("__pycache__")
        shutil.copytree(CORE, core, ignore=skip)
        self.src = os.path.join(core, "src")
        shutil.copytree(os.path.dirname(fake_adapter.__file__), os.path.join(self.src, "adapters", "fake"),
                        ignore=skip)
        self.home = os.path.join(self.root, "home")
        os.mkdir(self.home)
        self.work = os.path.join(self.root, "work")
        scenario = os.path.join(self.root, "scenario.json")
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump([{"out": {"type": "session", "id": "native-1"}}, {"out": {"type": "message", "text": "hi"}},
                       {"out": {"type": "result", "text": "done"}}], f)
        with open(os.path.join(core, "config.toml"), "a", encoding="utf-8") as f:
            f.write('\n[adapters.fake]\nimplementation = "adapters.fake:FakeAdapter"\n'
                    f"scenario = {json.dumps(scenario)}\nlog = {json.dumps(os.path.join(self.root, 'log.jsonl'))}\n")

    def test_a_copied_adapter_package_and_its_table_run_a_headless_session(self):
        child = subprocess.run([sys.executable, "-I", "-c", CHILD, self.src, self.work], cwd=self.root,
                               env={**os.environ, "HOME": self.home}, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=TIMEOUT)
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        for path in data["files"]:
            self.assertTrue(path.startswith(self.src + os.sep), path)
        self.assertEqual([name for name, _ in data["events"]], ["SessionBound", "Reply", "TurnEnd", "Exited"])
        self.assertEqual(data["events"][-1], ["Exited", {"returncode": 0}])
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as f:
            written = [json.loads(line) for line in f]
        self.assertEqual([(e["kind"], e["sid"]) for e in written],
                         [("session", data["sid"]), ("client_session", data["sid"])])


if __name__ == "__main__":
    unittest.main()
