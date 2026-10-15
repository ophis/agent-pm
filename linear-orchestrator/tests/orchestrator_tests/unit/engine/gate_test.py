"""Quota keys, configuration drift and tick-local probes at the Delegate boundary."""

import os
from datetime import UTC, datetime, timedelta

from agent_pm.linear_orchestrator.engine import runs
from agent_pm.linear_orchestrator.engine.gate import Gate
from orchestrator_tests.case import Case
from orchestrator_tests.gate_fixtures import quota_config, quota_provider

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


class DelegateQuotaKeys(Case):
    def gate(self, provider, *, kind="first", say=None, client=None, core=None, log=None):
        cfg = quota_config(
            provider, kind, core=core, clients=None if client is None else {"engineer": client}
        )
        path = log or os.path.join(self.home, "missing-runs.jsonl")
        return Gate(path, NOW, say, cfg=cfg, dry=False)

    def test_the_configured_overlay_client_keys_limits_and_reaches_the_probe(self):
        core = {"roles": {"engineer": {"client": "overlay-client"}}}
        provider = quota_provider("catalog-client", 10)
        self.assertFalse(
            self.gate(provider, client="overlay-client", core=core).blocked("engineer", "TASK-1")
        )
        self.assertEqual((provider.queried, provider.overlays), (["engineer"], [(core,)]))
        log = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            log,
            issue="TASK-0",
            sid="s",
            client="overlay-client",
            resets_at=(NOW + timedelta(hours=1)).isoformat(),
            delegate_kind="first",
            attempt=1,
        )
        gate = self.gate(provider, client="overlay-client", core=core, log=log)
        self.assertTrue(gate.blocked("engineer", "TASK-1"))
        self.assertEqual(provider.queried, ["engineer"])

    def test_roles_sharing_a_client_probe_once_even_when_blocked(self):
        provider = quota_provider("shared-client", 95)
        gate = self.gate(provider)
        self.assertTrue(gate.blocked("engineer", "TASK-1"))
        self.assertTrue(gate.blocked("pm", "TASK-2"))
        self.assertTrue(gate.blocked("engineer", "TASK-3"))
        self.assertEqual(provider.queried, ["engineer"])

    def test_allowed_usage_is_not_cached_across_ticks(self):
        provider = quota_provider("shared-client", 10)
        for _ in range(2):
            gate = self.gate(provider)
            self.assertFalse(gate.blocked("engineer", "TASK-1"))
            self.assertFalse(gate.blocked("pm", "TASK-2"))
        self.assertEqual(provider.queried, ["engineer", "engineer"])

    def test_distinct_kinds_sharing_a_client_keep_separate_probe_results(self):
        exhausted = quota_provider("shared-client", 95)
        available = quota_provider("shared-client", 10)
        gate = self.gate(exhausted)
        self.assertTrue(gate.blocked("engineer", "TASK-1"))
        gate.cfg.delegate_kind = "second"
        gate.cfg.delegate_cls = available
        gate.cfg.delegates["second"] = available
        self.assertFalse(gate.blocked("engineer", "TASK-2"))
        self.assertEqual((exhausted.queried, available.queried), (["engineer"], ["engineer"]))

    def test_drifted_resume_skips_probe_and_does_not_cache_a_reading(self):
        provider = quota_provider("current-client", 95)
        events = []
        gate = self.gate(
            provider, say=lambda event, *args, **fields: events.append((event, args, fields))
        )
        self.assertFalse(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-client")))
        self.assertEqual((provider.queried, gate.seen), ([], {}))
        self.assertEqual(
            events,
            [
                (
                    "usage-drift",
                    ("TASK-1",),
                    {
                        "client": "saved-client",
                        "delegate_kind": "saved-kind",
                        "current_client": "current-client",
                        "current_delegate_kind": "first",
                    },
                )
            ],
        )
        self.assertTrue(gate.blocked("engineer", "TASK-2"))
        self.assertEqual(provider.queried, ["engineer"])
