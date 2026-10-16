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

    def test_unit_contracts_keep_allowed_edges_and_reject_the_rest(self):
        session = (
            *(
                (source, target, False)
                for source, target in (
                    ("engine.hosted_session", "clients.codex"),
                    ("engine.hosted_session", "runtimes.tui"),
                    ("engine.hosted_session", "hosts.tmux.transport"),
                    ("engine.hosted_session", "engine.records"),
                    ("engine.hosted_session", "engine.base"),
                    ("engine.records", "engine.types"),
                    ("__init__", "engine.records"),
                    ("runtimes.tui", "hosts.tmux.transport"),
                    ("runtimes.headless", "clients.base"),
                    ("runtimes.base", "clients.registry"),
                    ("clients.codex", "engine.types"),
                    ("clients.registry", "hosts.base"),
                    ("engine.base", "hosts.base"),
                    ("hosts.base", "engine.types"),
                    ("hosts.tmux.host", "hosts.base"),
                    ("hosts.tmux.host", "engine.types"),
                    ("__init__", "engine.base"),
                    ("extension", "clients.base"),
                    ("extension", "hosts.base"),
                    ("extension", "engine.types"),
                )
            ),
            *(
                (source, target, True)
                for source, target in (
                    ("clients.codex", "hosts.tmux.transport"),
                    ("hosts.base", "hosts.tmux.transport"),
                    ("hosts.tmux.host", "clients.registry"),
                    ("clients.base", "runtimes.tui"),
                    ("runtimes.tui", "engine.hosted_session"),
                    ("runtimes.tui", "engine.base"),
                    ("runtimes.tui", "engine.records"),
                    ("engine.base", "engine.hosted_session"),
                    ("engine.base", "clients.registry"),
                    ("__init__", "engine.hosted_session"),
                    ("__init__", "hosts.tmux.transport"),
                    ("engine.types", "clients.base"),
                    ("__init__", "extension"),
                    ("clients.base", "extension"),
                    ("engine.hosted_session", "extension"),
                    ("extension", "runtimes.base"),
                    ("extension", "engine.hosted_session"),
                    ("extension", "hosts.tmux.host"),
                )
            ),
        )
        shared = ("config", "engine.config", "event.reply", "team.tools.repo")
        delegate = (
            *((source, "engine.future", True) for source in shared),
            ("event.reply", "engine.config", False),
            ("team.tools.repo", "engine.config", False),
            ("engine.config", "event", True),
            ("config", "event", True),
            ("engine.config", "config", False),
            ("engine.plan", "team.tools.repo", True),
            *(
                (source, "team.tools.helper", False)
                for source in ("team.tools", "team.tools.repo", "team.tools.nested.caller")
            ),
        )
        manager = (
            ("engine.crew", "engine.roster", False),
            ("engine.holder", "engine.roster", False),
            ("engine.roster", "engine.events", False),
            ("engine.crew", "engine.types", False),
            ("cli", "engine.crew", False),
            ("engine.roster", "engine.crew", True),
            ("engine.roster", "engine.holder", True),
            ("cli", "engine.roster", True),
        )
        cases = (
            *((f"agent_pm.session.{s}", f"agent_pm.session.{t}", r) for s, t, r in session),
            *((f"agent_pm.delegate.{s}", f"agent_pm.delegate.{t}", r) for s, t, r in delegate),
            *((f"agent_pm.delegate.{source}", "agent_pm.utils", False) for source in shared),
            ("agent_pm.delegate.config", "agent_pm.session", True),
            (
                "delegate.tests.unit.team.tools.repo_test",
                "agent_pm.delegate.team.tools.repo",
                False,
            ),
            *((f"agent_pm.manager.{s}", f"agent_pm.manager.{t}", r) for s, t, r in manager),
            ("agent_pm.manager.engine.holder", "agent_pm.session", True),
            ("agent_pm.manager.engine.crew", "agent_pm.session", True),
            ("agent_pm.manager.engine.roster", "agent_pm.delegate", True),
        )
        for source, target, rejected in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(self.rejected(source, target), rejected)
