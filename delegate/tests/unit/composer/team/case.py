"""Shared fixtures for team behavior."""

import os
import tempfile

from agent_pm.delegate.composer import ConfigError
from agent_pm.delegate.composer.teams import (
    index,
    team,
)
from agent_pm.utils import read_project

from delegate.tests.case import Case
from delegate.tests.unit.composer.teams import FILES, STEPS

CONFIG = """\
tier = 2
effort = "high"

[output]
type = "local"

[roles.writer]
effort = "medium"

[roles.editor]
tier = 1
"""


BUILT_IN = {
    "writer": {
        "short-note": "a note; most times; light.",
        "long-note": "a long note; when asked; heavy.",
    },
    "editor": {"long-note": "a long note; always; heavy."},
}


TINY = "# Tiny Note\n" + STEPS


TINY_LINE = "- `tiny-note` (`<tasks>/tiny-note.md`): a tiny note; rarely; light.\n"


FRONT = '---\ncommands: ["rm *"]\n---\n'


class Fake(Case):
    """A team root, config and local file in a temp dir; a HOME of the test's own for team dirs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(tmp.name, "team")
        self.config = os.path.join(tmp.name, "config.toml")
        self.local_file = os.path.join(tmp.name, "config.local.toml")
        self.write(self.root, FILES)
        self.configure(CONFIG)
        os.chmod(self.agent_pm, 0o755)

    @staticmethod
    def write(top, files):
        for rel, text in files.items():
            path = os.path.join(top, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def configure(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def local(self, text):
        with open(self.local_file, "w") as f:
            f.write(text)

    def cfg(self):
        return read_project("delegate", path=self.config, local=self.local_file)

    def tasks_of(self, role, found=None):
        """`role`'s index in `found`, else in a fresh scan of the team root, with the temp config."""
        return index(
            role, found if found is not None else team(self.root), self.config, self.local_file
        )

    def refused(self, call, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            call(*args, **kw)
        return str(cm.exception)

    def team_dir(self, files, name="team"):
        """Writes {rel: text} under ~/.agent-pm/<name> (files 0o644, dirs 0o755) and returns its
        real path.
        """
        top = os.path.realpath(os.path.join(self.agent_pm, name))
        for rel, text in files.items():
            path = os.path.join(top, rel)
            for d in (top, os.path.dirname(path)):
                os.makedirs(d, exist_ok=True)
                os.chmod(d, 0o755)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
        return top

    def with_dir(self, files, name="team"):
        """The Team of the team root and one team dir of `files`."""
        return team(self.root, (self.team_dir(files, name),))

    def path(self, rel, name="team"):
        return os.path.join(os.path.realpath(os.path.join(self.agent_pm, name)), rel)
