"""An installed session and utils outside any checkout: copies of agent_pm/session and agent_pm/utils
in a temp site directory that has no .git in it or above it, the fake adapter's package copied in as
top-level module installed_client and its table appended to the copy's config.toml. A child
`python -I` with only that site on sys.path reads the copy's config through read_config() and runs
the fake client headless through Driver.start.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from session.tests import fake_adapter
from session.tests.case import REPO, Case
from utils.tests.sandbox import clean_env

TIMEOUT = 60  # seconds
SKIP = shutil.ignore_patterns("__pycache__")
MODULE = "installed_client"
# The child: argv[1] the site, argv[2] the workdir; prints the config read, the fake client's
# events and where agent_pm.session and agent_pm.utils were imported from.
CHILD = """import json, sys
sys.path.insert(0, sys.argv[1])
import agent_pm.session, agent_pm.utils
from agent_pm.session import Driver, Spec, read_config
config = read_config()
handle = Driver.from_config(config).start("fake", "headless", "hello", Spec(workdir=sys.argv[2]))
print(json.dumps({"config": config, "events": [type(e).__name__ for e in handle.events()],
                  "files": [agent_pm.session.__file__, agent_pm.utils.__file__]}))
"""


class InstalledIntegration(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.site = self.root / "site"
        for project in ("session", "utils"):
            shutil.copytree(
                Path(REPO, project, "src", "agent_pm", project),
                self.site / "agent_pm" / project,
                ignore=SKIP,
            )
        shutil.copytree(Path(fake_adapter.__file__).parent, self.site / MODULE, ignore=SKIP)
        scenario = self.root / "scenario.json"
        scenario.write_text(
            json.dumps(
                [
                    {"out": {"type": "session", "id": "native-1"}},
                    {"out": {"type": "message", "text": "hi"}},
                    {"out": {"type": "result", "text": "done"}},
                ]
            )
        )
        self.config = self.site / "agent_pm" / "session" / "config.toml"
        self.config.chmod(0o600)
        with self.config.open("a", encoding="utf-8") as f:
            f.write(
                f'\n[adapters.fake]\nimplementation = "{MODULE}:FakeAdapter"\n'
                f"scenario = {json.dumps(str(scenario))}\n"
                f"log = {json.dumps(str(self.root / 'log.jsonl'))}\n"
            )
        for name in ("home", "tmp"):
            (self.root / name).mkdir()
        self.work = self.root / "work"

    def test_the_copy_reads_its_packaged_config_and_runs_a_headless_session(self):
        self.assertEqual(
            [str(p) for p in (self.root, *self.root.parents) if (p / ".git").exists()], []
        )
        child = subprocess.run(
            [sys.executable, "-I", "-c", CHILD, str(self.site), str(self.work)],
            cwd=self.root,
            env=clean_env(
                {**os.environ, "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp")}
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        data = json.loads(child.stdout)
        with self.config.open("rb") as f:
            self.assertEqual(data["config"], tomllib.load(f))
        for file in data["files"]:
            self.assertTrue(file.startswith(str(self.site / "agent_pm") + os.sep), file)
        self.assertEqual(data["events"], ["Reply", "TurnEnd", "Exited"])


if __name__ == "__main__":
    unittest.main()
