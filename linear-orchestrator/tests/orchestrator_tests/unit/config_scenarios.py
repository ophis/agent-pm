import os
import tempfile

from agent_pm.linear_orchestrator.engine import config
from orchestrator_tests.case import Case
from orchestrator_tests.unit.config_data import CORE_LOCAL, CORE_TABLES, CORE_TOP, HTTPS, git, write


class ConfigFile:
    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def local(self, text):
        """~/.agent-pm/config.local.toml: `text` amid CORE_LOCAL (the delegate's keys)."""
        write(
            os.path.join(self.agent_pm, "config.local.toml"), CORE_TOP + text + "\n" + CORE_TABLES
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

class Clones(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.dir = os.path.realpath(self.dir)

    def clone(self, name="clone", origin=HTTPS):
        path = os.path.join(self.dir, name)
        git("init", "-q", path)
        if origin:
            git("-C", path, "remote", "add", "origin", origin)
        return path

    def bad(self):
        dotgit_file = os.path.join(self.dir, "file")
        os.makedirs(dotgit_file)
        write(os.path.join(dotgit_file, ".git"), "gitdir: /elsewhere\n")
        linked = os.path.join(self.dir, "linked")
        os.makedirs(linked)
        os.symlink(os.path.join(self.clone("real"), ".git"), os.path.join(linked, ".git"))
        missing = os.path.join(self.dir, "missing")
        no_origin = self.clone("no-origin", origin=None)
        gitlab = self.clone("gitlab", "https://gitlab.com/ophis/agent-pm.git")
        other = self.clone("other", "https://github.com/ophis/other.git")
        return [
            ("missing", missing, f"{missing} does not exist"),
            (".git a file", dotgit_file, f"{dotgit_file} is not a git clone"),
            (".git a symlink", linked, f"{linked} is not a git clone"),
            ("no origin", no_origin, f"{no_origin}: no origin URL naming host/owner/name"),
            (
                "non-github host",
                gitlab,
                "origin is gitlab.com/ophis/agent-pm, not github.com/ophis/agent-pm",
            ),
            (
                "other owner/name",
                other,
                "origin is github.com/ophis/other, not github.com/ophis/agent-pm",
            ),
        ]
