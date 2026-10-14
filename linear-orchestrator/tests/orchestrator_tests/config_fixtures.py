"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

import os
import tempfile

from agent_pm.delegate import Delegate
from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import load_class
from orchestrator_tests.unit.board_ids import HEADER, role

from utils.tests.case import local_section

HARNESS_DELEGATE = load_class("agent_pm.delegate.engine.harness_delegate:HarnessDelegate", Delegate)


PIPELINE = (
    HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer")
)


BASE = (
    HEADER
    + role("researcher", 'next = "pm"')
    + role("pm", 'next = "engineer"')
    + role("engineer")
    + role("solo")
)

CORE_TOP = '[delegate]\nlanguage = "Chinese"\n'


CORE_TABLES = """[delegate.roles.researcher.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Research/"

[delegate.roles.pm.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Product Design/"
"""


CORE_LOCAL = CORE_TOP + "\n" + CORE_TABLES


SLUG = "ophis/agent-pm"


HTTPS = "https://github.com/ophis/agent-pm.git"


SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


STEPS = (
    "\n## Steps\n\n1. **Report progress:**\n   [agent-pm-progress:start] the topic\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)


IMPORT_ALONE = """import json, os, sys
opened = []
sys.addaudithook(lambda event, args: event == "open" and opened.append(str(args[0])))
from agent_pm.linear_orchestrator.engine import config
import agent_pm.session
home, front = os.environ["HOME"], os.environ["AGENT_PM_PATH"]
os.environ.update(HOME="/later", AGENT_PM_PATH="/later/bin")
print(json.dumps({
    "read": [p for p in opened if p.endswith(".toml") or p.startswith(home)],
    "kept": sorted(k for k, v in vars(config).items() if isinstance(v, str) and (home in v or front in v)),
    "path": config.path(),
}))
"""


class ConfigFile:
    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def local(self, text):
        """~/.agent-pm/config.local.toml: `text` amid CORE_LOCAL (the delegate's keys)."""
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            CORE_LOCAL + "\n" + local_section(text, "linear_orchestrator"),
        )

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(path=path)

    def fails(self, message, text):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(text)
        self.assertEqual(str(cm.exception), message)
        return cm.exception
