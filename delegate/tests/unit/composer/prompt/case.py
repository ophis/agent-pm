"""Shared fixtures for prompt behavior."""

import os
import tempfile
from dataclasses import replace

from agent_pm.delegate.composer import ConfigError, ConfigSource, RunOverrides, RunParams
from agent_pm.delegate.composer.prompt import (
    render,
)
from agent_pm.delegate.composer.run import resolve
from agent_pm.delegate.composer.teams import team

from delegate.tests.case import Case
from delegate.tests.unit.composer.teams import FILES, RUN_CONFIG

TEXTS = {
    "guide.md": (
        "# Guide\n\nYou are {{role}}. Task: {{task|pick one}}; files in "
        "`<tasks>`.\n\nOn conflict: [Principles](#principles) > [{{role}} "
        "rules](#{{role_anchor}}) > your task's.\n"
    ),
    "principles.md": "# Principles\n",
    "output/output.md": "# Output\n\nWrite `Output:`.\n",
    "output/destinations/local.md": "## Destination\n\nKeep it local.\n",
    "output/destinations/github.md": "## Destination\n\nPush to `<out-repo>` on `<out-branch>`.\n",
}


PARAMS = RunParams(input="Research X.", out="/w/out.md", workdir="/w")


METHOD_NAMES = ("deep-research", "ultracode")


class Fake(Case):
    """A team root, config and local file in a temp dir; a HOME of the test's own for team dirs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "team")
        self.config = os.path.join(self.tmp, "config.toml")
        self.local_file = os.path.join(self.tmp, "config.local.toml")
        self.write(FILES | TEXTS)
        with open(self.config, "w") as f:
            f.write(RUN_CONFIG)

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

    def run_of(self, role="writer", task=None, overlays=(), found=None):
        return resolve(
            role,
            task,
            team=self.found(found),
            source=ConfigSource(path=self.config, local=self.local_file),
            overrides=RunOverrides(values=overlays),
        )

    def found(self, given=None):
        return given if given is not None else team(self.root)

    def compose(self, role="writer", task=None, *, overlays=(), found=None, **params):
        run = self.run_of(role, task, overlays, found)
        return render(run, replace(PARAMS, **params), team=self.found(found)), run

    def fails(self, role="writer", task=None, overlays=(), found=None) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.compose(role, task, overlays=overlays, found=found)
        return str(cm.exception)
