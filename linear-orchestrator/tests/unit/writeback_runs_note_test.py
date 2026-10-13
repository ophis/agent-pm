import json
import os
import shutil
import tempfile
from datetime import datetime
from unittest import mock

from agent_pm.linear_orchestrator.engine import runs
from orchestrator_test_helpers.unit.writeback.data import ID, OTHER_SID, SID
from orchestrator_test_helpers.unit.writeback.runs_note_fixtures import RunsNote as RunsNoteBase


class RunsNote1(RunsNoteBase):
    def test_one_durable_json_line_per_note(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "logs", "runs.jsonl")
        at = "2026-10-11T18:00:00+08:00"
        with mock.patch.object(os, "fsync", wraps=os.fsync) as spy:
            runs.note(path, "limit", issue=ID, sid=SID, runtime="claude", resets_at=None)
            runs.note(path, "limit", issue=ID, sid=OTHER_SID, runtime="claude", resets_at=at)
        self.assertEqual(spy.call_count, 4)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.endswith("\n"))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            [list(r) for r in rows], [["ts", "kind", "issue", "sid", "runtime", "resets_at"]] * 2
        )
        for r in rows:
            ts = datetime.fromisoformat(r["ts"])
            self.assertIsNotNone(ts.utcoffset())
            self.assertEqual(r["ts"], ts.isoformat(timespec="seconds"))
        self.assertEqual(
            [{k: v for k, v in r.items() if k != "ts"} for r in rows],
            [
                {"kind": "limit", "issue": ID, "sid": SID, "runtime": "claude", "resets_at": None},
                {
                    "kind": "limit",
                    "issue": ID,
                    "sid": OTHER_SID,
                    "runtime": "claude",
                    "resets_at": at,
                },
            ],
        )
