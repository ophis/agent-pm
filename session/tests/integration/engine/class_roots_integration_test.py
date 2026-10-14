"""Session.class_roots through the Session ABC in a child Python: the import root of each class the
session loads by name, a class installed through a .pth file included.
"""

import json
import os
import subprocess
import tempfile

from session.tests.case import PY, REPO, SRC, Case, child_env

MODULES = {
    "pth_runtime": "from session.tests.fake_adapter import FakeAdapter\n\n\n"
    "class PthAdapter(FakeAdapter):\n    pass\n",
    "pth_host": "from session.tests.fakes.host import FakeHost\n\n\n"
    "class PthHost(FakeHost):\n    pass\n",
}
CHILD = """import json, site, sys
site.addsitedir(sys.argv[1])
from agent_pm.session import Host, Session
Session.configure(
    adapters={"pth": {"implementation": "pth_runtime:PthAdapter"}},
    hosts=Host.parse_hosts({
        "tmux": {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"},
        "fake": {"implementation": "session.tests.fakes.host:FakeHost"},
        "pth": {"implementation": "pth_host:PthHost"},
        "default": "tmux",
    }),
)
print(json.dumps(Session.class_roots("pth")))
"""


class ClassRootsIntegration(Case):
    def test_a_pth_installed_class_gives_the_directory_its_pth_file_names(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        site, installed = os.path.join(root, "site"), os.path.join(root, "installed")
        os.mkdir(site)
        os.mkdir(installed)
        with open(os.path.join(site, "extension.pth"), "w", encoding="utf-8") as f:
            f.write(f"{installed}\n")
        for name, text in MODULES.items():
            with open(os.path.join(installed, f"{name}.py"), "w", encoding="utf-8") as f:
                f.write(text)
        env = child_env(None, REPO)
        self.assertNotIn(installed, env["PYTHONPATH"].split(os.pathsep))
        child = subprocess.run(
            [*PY, "-c", CHILD, site], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), [installed, SRC["session"], REPO])
