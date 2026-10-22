"""Actual-client quota keys and tick-local probes at the Delegate boundary."""

import os
from datetime import UTC, datetime, timedelta

from agent_pm.orchestrator.engine import runs
from agent_pm.orchestrator.engine.gate import Gate
from orchestrator_tests.case import Case
from orchestrator_tests.gate_fixtures import quota_config, quota_provider

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
SHARED = ("first", "shared-client")


class DelegateQuotaKeys(Case):
    def test_tier_context_is_overlaid_and_not_shared_in_the_cache(self):
        provider = quota_provider("shared-client", 10)
        gate = self.gate(provider)
        for tier in (1, 2, 1):
            self.assertFalse(gate.blocked("engineer", "TASK-1", key=SHARED, tier=tier))
        self.assertEqual(provider.queried, ["engineer", "engineer"])
        self.assertEqual(
            [o[-1] for o in provider.overlays],
            [{"roles": {"engineer": {"tier": 1}}}, {"roles": {"engineer": {"tier": 2}}}],
        )

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
            self.gate(provider, client="overlay-client", core=core).blocked(
                "engineer", "TASK-1", key=("first", "overlay-client")
            )
        )
        self.assertEqual(
            (provider.queried, provider.overlays),
            (["engineer"], [(core, {"roles": {"engineer": {"client": "overlay-client"}}})]),
        )
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
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("first", "overlay-client")))
        self.assertEqual(provider.queried, ["engineer"])

    def test_roles_sharing_a_client_have_separate_probe_contexts(self):
        provider = quota_provider("shared-client", 95)
        gate = self.gate(provider)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=SHARED))
        self.assertTrue(gate.blocked("pm", "TASK-2", key=SHARED))
        self.assertTrue(gate.blocked("engineer", "TASK-3", key=SHARED))
        self.assertEqual(provider.queried, ["engineer", "pm"])

    def test_allowed_usage_is_not_cached_across_ticks(self):
        provider = quota_provider("shared-client", 10)
        for _ in range(2):
            gate = self.gate(provider)
            self.assertFalse(gate.blocked("engineer", "TASK-1", key=SHARED))
            self.assertFalse(gate.blocked("pm", "TASK-2", key=SHARED))
        self.assertEqual(provider.queried, ["engineer", "pm", "engineer", "pm"])

    def test_distinct_kinds_sharing_a_client_have_separate_probe_contexts(self):
        exhausted = quota_provider("shared-client", 95)
        available = quota_provider("shared-client", 10)
        gate = self.gate(exhausted)
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=SHARED))
        gate.cfg.delegate_kind = "second"
        gate.cfg.delegate_cls = available
        gate.cfg.delegates["second"] = available
        self.assertFalse(gate.blocked("engineer", "TASK-2", key=("second", "shared-client")))
        self.assertEqual((exhausted.queried, available.queried), (["engineer"], ["engineer"]))

    def test_recorded_client_is_probed_through_its_recorded_provider(self):
        provider = quota_provider("current-client", 95)
        saved = quota_provider("saved-client", 95)
        events = []
        gate = self.gate(
            provider, say=lambda event, *args, **fields: events.append((event, args, fields))
        )
        gate.cfg.delegates["saved-kind"] = saved
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-client")))
        self.assertEqual((provider.queried, saved.queried), ([], ["engineer"]))
        self.assertEqual(
            saved.overlays, [({}, {"roles": {"engineer": {"client": "saved-client"}}})]
        )
        self.assertEqual(events[0][0], "usage-skip")
        self.assertEqual(events[0][2]["client"], "saved-client")
        self.assertTrue(gate.blocked("engineer", "TASK-2", key=("first", "current-client")))
        self.assertEqual(provider.queried, ["engineer"])

    def test_missing_recorded_provider_blocks_without_probing_default(self):
        provider = quota_provider("current-client", 10)
        events = []
        gate = self.gate(
            provider, say=lambda event, *_args, **fields: events.append((event, fields))
        )
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("missing", "saved-client")))
        self.assertEqual(provider.queried, [])
        self.assertEqual(events[0][1]["reason"], "usage unknown")
