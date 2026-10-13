import os
import tempfile

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.config.data import CORE_LOCAL, CORE_TABLES, CORE_TOP, write


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
