"""The public session configuration reader owns the session local section."""

import os
from pathlib import Path

from agent_pm.session import read_config

from session.tests.case import Case


class Configuration(Case):
    def test_only_the_session_section_overlays_session_defaults(self):
        local = Path(self.home, "config.local.toml")
        local.write_text(
            "[session.hosts.tmux]\nstatus_line = true\n[delegate]\nunknown = 1\n[linear_orchestrator]\nunknown = 2\n"
        )
        found = read_config(local=str(local))
        self.assertTrue(found["hosts"]["tmux"]["status_line"])
        self.assertIn("adapters", found)
        self.assertNotIn("roles", found)

    def test_flat_local_keys_have_a_session_migration_hint(self):
        local = Path(self.home, "config.local.toml")
        local.write_text("[hosts.tmux]\nstatus_line = true\n")
        with self.assertRaisesRegex(ValueError, r"unknown key 'hosts'.*move it under \[session\]"):
            read_config(local=str(local))
        self.assertIn("hosts", read_config(local=os.devnull))
