"""Isolated Codex unit fixtures with recovery records and native session files."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm.session.adapters.base import Access, Spec
from agent_pm.session.adapters.codex import CodexAdapter

from session.tests.case import REPO, Case
from session.tests.selection import configured

NATIVE = "11111111-2222-3333-4444-555555555555"
CHILD = "aaaaaaaa-2222-3333-4444-555555555555"


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.work = self.root / "work"
        self.cwd = self.work / "project"
        self.extra = self.root / "extra"
        self.codex_home = self.home / ".codex"
        for path in (self.home, self.cwd, self.extra, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.enterContext(
            mock.patch.dict(
                os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home)}
            )
        )
        lexists = os.path.lexists
        self.enterContext(
            mock.patch(
                "os.path.lexists",
                side_effect=lambda path: (
                    os.path.commonpath((os.path.abspath(path), str(self.root))) == str(self.root)
                    and lexists(path)
                ),
            )
        )
        self.adapter = configured(
            CodexAdapter, {"tiers": {"1": "gpt-test"}, "efforts": {"low": "low"}}
        )
        self.common = self.adapter.common
        self.spec = Spec(
            workdir=str(self.work),
            sid="caller-id",
            tier=1,
            effort="low",
            access=Access(cwd=str(self.cwd), dirs=(str(self.extra),), commands=("git status",)),
        )

    def prepare(self):
        return self.common.prepare(self.spec, False)

    def rollout(self, native=NATIVE, *, cwd=None, parent=None, stamp=100):
        path = self.codex_home / "sessions" / "2026" / "10" / "11" / f"rollout-test-{native}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"id": native, "cwd": cwd or str(self.cwd), "source": "exec"}
        if parent:
            payload["parent_thread_id"] = parent
            payload["source"] = {
                "subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}
            }
        path.write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8"
        )
        os.utime(path, (stamp, stamp))
        return path

    def record(self):
        return next((self.home / ".agent-pm" / "core-new-sessions" / "codex").glob("*.json"))

    def edit_record(self, **fields):
        path = self.record()
        value = json.loads(path.read_text())
        value.update(fields)
        path.write_text(json.dumps(value), encoding="utf-8")
