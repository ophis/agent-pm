"""Shared fixtures for run behavior."""

import os
import tempfile

from agent_pm.delegate.composer import (
    ConfigError,
    ConfigSource,
    RunOverrides,
)
from agent_pm.delegate.composer.run import (
    resolve,
)
from agent_pm.delegate.composer.teams import team

from delegate.tests.case import Case
from delegate.tests.unit.composer.teams import FILES, RUN_CONFIG

TIER = "tier must be an integer 1–4"


RUNTIME = "runtime must be a non-empty one-line name, got "


class Fake(Case):
    """A team root, config and local file in a temp dir; a HOME of the test's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "team")
        self.config = os.path.join(self.tmp, "config.toml")
        self.local_file = os.path.join(self.tmp, "config.local.toml")
        self.write(FILES)
        self.configure(RUN_CONFIG)

    def write(self, files, top=None):
        for rel, text in files.items():
            path = os.path.join(top or self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def configure(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def local(self, text):
        with open(self.local_file, "w") as f:
            f.write(text)

    def team_dir(self, files):
        """The Team of the team root and one team dir of `files`, <tmp>/mine, and that dir."""
        top = os.path.join(self.tmp, "mine")
        self.write(files, top)
        return team(self.root, (top,)), top

    def resolved(self, role="writer", task=None, found=None, **kw):
        source = ConfigSource(
            path=kw.pop("config", self.config),
            local=kw.pop("local", self.local_file),
            table=kw.pop("cfg", None),
        )
        label = kw.pop("label", ("run overlay", "overlay"))
        overrides = RunOverrides(values=kw.pop("overlays", ()), source=label[0], table=label[1])
        return resolve(
            role, task, team=found or team(self.root), source=source, overrides=overrides, **kw
        )

    def refused(self, role="writer", task=None, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.resolved(role, task, **kw)
        return str(cm.exception)
