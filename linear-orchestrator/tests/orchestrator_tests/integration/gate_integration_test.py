"""Recorded quota limits gate recovery using the saved Delegate kind and runtime."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine import runs
from agent_pm.linear_orchestrator.engine.gate import Gate
from orchestrator_tests.case import Case
from orchestrator_tests.gate_fixtures import quota_provider

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


class RecordedQuotaKeys(Case):
    def test_saved_kind_and_runtime_limit_blocks_drifted_resume_without_probing(self):
        provider = quota_provider("current-runtime", 10)
        cfg = SimpleNamespace(
            delegate_kind="current-kind",
            delegate_cls=provider,
            delegates={"current-kind": provider},
        )
        path = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            path,
            issue="TASK-1",
            sid="saved-sid",
            runtime="saved-runtime",
            delegate_kind="saved-kind",
            resets_at=(NOW + timedelta(hours=1)).isoformat(),
        )
        events = []
        gate = Gate(
            path,
            NOW,
            lambda event, *args, **fields: events.append((event, args, fields)),
            cfg=cfg,
            dry=False,
        )
        self.assertTrue(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-runtime")))
        self.assertEqual((provider.queried, gate.seen), ([], {}))
        self.assertEqual(
            events,
            [
                (
                    "usage-skip",
                    ("TASK-1",),
                    {
                        "once": True,
                        "runtime": "saved-runtime",
                        "reason": "limit",
                        "until": (NOW + timedelta(hours=1)).isoformat(),
                    },
                )
            ],
        )
        self.assertFalse(gate.blocked("engineer", "TASK-2"))
        self.assertEqual(provider.queried, ["engineer"])

    def test_limit_in_one_kind_does_not_block_another_kind_with_the_same_runtime(self):
        provider = quota_provider("shared-runtime", 10)
        cfg = SimpleNamespace(
            delegate_kind="current-kind",
            delegate_cls=provider,
            delegates={"current-kind": provider},
        )
        path = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            path,
            issue="TASK-1",
            sid="saved-sid",
            runtime="shared-runtime",
            delegate_kind="other-kind",
            resets_at=(NOW + timedelta(hours=1)).isoformat(),
        )
        gate = Gate(path, NOW, cfg=cfg, dry=False)
        self.assertFalse(gate.blocked("engineer", "TASK-2"))
        self.assertEqual(provider.queried, ["engineer"])

    def test_expired_saved_limit_releases_drift_without_caching_usage(self):
        provider = quota_provider("current-runtime", 95)
        cfg = SimpleNamespace(
            delegate_kind="current-kind",
            delegate_cls=provider,
            delegates={"current-kind": provider},
        )
        path = os.path.join(self.home, "runs.jsonl")
        runs.note_limit(
            path,
            issue="TASK-1",
            sid="saved-sid",
            runtime="saved-runtime",
            delegate_kind="saved-kind",
            resets_at=(NOW - timedelta(seconds=1)).isoformat(),
        )
        gate = Gate(path, NOW, cfg=cfg, dry=False)
        self.assertFalse(gate.blocked("engineer", "TASK-1", key=("saved-kind", "saved-runtime")))
        self.assertEqual((provider.queried, gate.seen), ([], {}))
