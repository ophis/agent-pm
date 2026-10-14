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
            ("engine.hosted_session", "adapters.codex"),
            ("engine.hosted_session", "runtimes.tui"),
            ("engine.hosted_session", "hosts.tmux.transport"),
            ("engine.hosted_session", "engine.records"),
            ("engine.hosted_session", "engine.base"),
            ("engine.records", "engine.types"),
            ("__init__", "engine.records"),
            ("runtimes.tui", "hosts.tmux.transport"),
            ("runtimes.headless", "adapters.base"),
            ("runtimes.base", "adapters.registry"),
            ("adapters.codex", "engine.types"),
            ("adapters.registry", "hosts.base"),
            ("engine.base", "hosts.base"),
            ("hosts.base", "engine.types"),
            ("hosts.tmux.host", "hosts.base"),
            ("hosts.tmux.host", "engine.types"),
            ("__init__", "engine.base"),
            ("extension", "adapters.base"),
            ("extension", "hosts.base"),
            ("extension", "engine.types"),
        )
        for source, target in pairs:
            with self.subTest(source=source, target=target):
                self.assertFalse(
                    self.rejected("agent_pm.session." + source, "agent_pm.session." + target)
                )

    def test_lower_units_cannot_reach_the_session_class_wrappers_or_hosts(self):
        for source, target in (
            ("adapters.codex", "hosts.tmux.transport"),
            ("hosts.base", "hosts.tmux.transport"),
            ("hosts.tmux.host", "adapters.registry"),
            ("adapters.base", "runtimes.tui"),
            ("runtimes.tui", "engine.hosted_session"),
            ("runtimes.tui", "engine.base"),
            ("runtimes.tui", "engine.records"),
            ("engine.base", "engine.hosted_session"),
            ("engine.base", "adapters.registry"),
            ("__init__", "engine.hosted_session"),
            ("__init__", "hosts.tmux.transport"),
            ("engine.types", "adapters.base"),
            ("__init__", "extension"),
            ("adapters.base", "extension"),
            ("engine.hosted_session", "extension"),
            ("extension", "runtimes.base"),
            ("extension", "engine.hosted_session"),
            ("extension", "hosts.tmux.host"),
        ):
            with self.subTest(source=source, target=target):
                self.assertTrue(
                    self.rejected("agent_pm.session." + source, "agent_pm.session." + target)
                )

    def test_shared_modules_cannot_reach_future_engine_modules(self):
        for source in ("config", "engine.config", "event.reply", "team.tools.repo"):
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
        self.assertTrue(self.rejected("agent_pm.delegate.config", "agent_pm.delegate.event"))
        self.assertTrue(self.rejected("agent_pm.delegate.config", "agent_pm.session"))
        self.assertFalse(
            self.rejected("agent_pm.delegate.engine.config", "agent_pm.delegate.config")
        )

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
