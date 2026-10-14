"""Nested unit contracts preserve main's allowed edges and reject future-module escapes."""

import tomllib
from pathlib import Path

from tests.case import REPO, Case
from tests.import_policy import violation


class Units(Case):
    def setUp(self):
        config = tomllib.loads(Path(REPO, "pyproject.toml").read_text())
        self.policies = [
            c for c in config["tool"]["importlinter"]["contracts"] if c["type"] == "units"
        ]

    def rejected(self, source, target):
        return any(violation(source, target, policy) for policy in self.policies)

    def test_main_session_edges_are_kept(self):
        pairs = (
            ("engine.api", "adapters.codex"),
            ("engine.api", "modes.tui"),
            ("engine.api", "hosts.tmux.tmux_host"),
            ("engine.api", "engine.records"),
            ("modes.tui", "hosts.tmux.tmux_host"),
            ("modes.headless", "adapters.base"),
            ("adapters.codex", "engine.types"),
            ("adapters.registry", "hosts.tables"),
            ("engine.base", "adapters.registry"),
            ("engine.base", "hosts.tables"),
            ("hosts.base", "engine.types"),
            ("__init__", "engine.base"),
        )
        for source, target in pairs:
            with self.subTest(source=source, target=target):
                self.assertFalse(
                    self.rejected("agent_pm.session." + source, "agent_pm.session." + target)
                )

    def test_lower_units_cannot_reach_api_or_hosts(self):
        for source, target in (
            ("adapters.codex", "hosts.tables"),
            ("adapters.base", "modes.tui"),
            ("modes.tui", "engine.api"),
            ("engine.types", "adapters.base"),
        ):
            with self.subTest(source=source, target=target):
                self.assertTrue(
                    self.rejected("agent_pm.session." + source, "agent_pm.session." + target)
                )

    def test_shared_modules_cannot_reach_future_engine_modules(self):
        for source in ("engine.config", "event.reply", "team.tools.repo"):
            with self.subTest(source=source):
                self.assertTrue(
                    self.rejected("agent_pm.delegate." + source, "agent_pm.delegate.engine.future")
                )
                self.assertFalse(self.rejected("agent_pm.delegate." + source, "agent_pm.utils"))
        self.assertFalse(
            self.rejected("agent_pm.delegate.event.reply", "agent_pm.delegate.engine.config")
        )
        self.assertFalse(
            self.rejected("agent_pm.delegate.team.tools.repo", "agent_pm.delegate.engine.config")
        )
        self.assertTrue(self.rejected("agent_pm.delegate.engine.config", "agent_pm.delegate.event"))

    def test_tools_are_not_imported_by_production_modules(self):
        self.assertTrue(
            self.rejected("agent_pm.delegate.engine.plan", "agent_pm.delegate.team.tools.repo")
        )
        self.assertFalse(
            self.rejected(
                "delegate.tests.unit.team.tools.repo_test", "agent_pm.delegate.team.tools.repo"
            )
        )

    def test_tools_can_import_modules_inside_the_same_unit(self):
        for source in ("team.tools", "team.tools.repo", "team.tools.nested.caller"):
            with self.subTest(source=source):
                self.assertFalse(
                    self.rejected(
                        "agent_pm.delegate." + source, "agent_pm.delegate.team.tools.helper"
                    )
                )

    def test_manager_units_keep_commands_out_of_bookkeeping(self):
        for source, target in (
            ("engine.crew", "engine.roster"),
            ("engine.holder", "engine.roster"),
            ("engine.roster", "engine.events"),
            ("engine.crew", "engine.types"),
            ("cli", "engine.crew"),
        ):
            self.assertFalse(
                self.rejected("agent_pm.manager." + source, "agent_pm.manager." + target)
            )
        for source, target in (
            ("engine.roster", "engine.crew"),
            ("engine.roster", "engine.holder"),
            ("engine.holder", "agent_pm.session"),
            ("cli", "engine.roster"),
            ("engine.crew", "agent_pm.session"),
            ("engine.roster", "agent_pm.delegate"),
        ):
            imported = target if target.startswith("agent_pm.") else "agent_pm.manager." + target
            self.assertTrue(self.rejected("agent_pm.manager." + source, imported))
