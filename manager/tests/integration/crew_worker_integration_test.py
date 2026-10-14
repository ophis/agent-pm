"""Workers through `agent-pm-crew run -- agent-pm-session start` (FR-17, FR-20) on the real
commands (manager/tests/live_crew.py): the worker's Record and entry, a gone worker's crew resume,
a live one's no-op, and main's crew_integration_test.py dialog waits (C:429, C:528) on this entry.
"""

import glob
import json
import os

from agent_pm.manager import State

from manager.tests.live_crew import MANAGER, LiveCrew, has_pair, logged
from session.tests.integration.adapters.codex import fake_tui

PERMISSION = {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"}


class WorkerTest(LiveCrew):
    def test_a_worker_runs_through_crew_run_and_a_gone_one_comes_back_by_crew_resume(self):
        record = self.record(self.worker("w1", note="first"))
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.ctl, record.resume),
            ("worker", "w1", ("w1",), ("agent-pm-session",), ("agent-pm-session", "resume", "w1")),
        )
        self.assertEqual(record.display, MANAGER)
        entry = self.roster()["w1"]
        self.assertEqual(
            (entry.state, entry.record, entry.note, entry.sid),
            (State.LIVE, record, "first", record.sid),
        )
        (begun,) = logged(self.log("w1"))
        self.assertEqual(begun["cwd"], self.cwd)
        self.assertEqual(begun["argv"][-1], "hi")
        self.assertTrue(has_pair(begun["argv"], "--session-id", record.sid), begun["argv"])
        self.wait_event("w1", "done")
        self.assertEqual(
            glob.glob(os.path.join(self.agent_pm, "managers", "**", "worker.json")), []
        )
        self.assertTrue(
            os.path.isfile(os.path.join(self.agent_pm, "workers", record.sid, "worker.json"))
        )

        self.assertEqual(self.ok(self.crew("resume", "w1")).stdout, record.to_line() + "\n")
        self.assertEqual(len(logged(self.log("w1"))), 1)

        self.kill("w1")
        out = self.ok(self.crew("attach")).stdout.splitlines()
        self.assertIn("resume: agent-pm-crew resume --manager mgr w1", out)
        self.assertEqual(self.roster()["w1"].state, State.GONE)
        self.assertTrue(self.gone("w1"))
        resumed = self.record(self.crew("resume", "w1", **self.scene("w1")))
        self.assertEqual((resumed.name, resumed.sid, resumed.display), ("w1", record.sid, MANAGER))
        self.assertTrue(self.live("w1"))
        self.assertTrue(has_pair(logged(self.log("w1"))[-1]["argv"], "--resume", record.sid))
        self.assertEqual(self.roster()["w1"].state, State.LIVE)


class DialogTest(LiveCrew):
    def test_a_permission_hook_at_start_is_said_on_crew_runs_stderr(self):
        steps = [PERMISSION, {"kind": "dialog", "text": "Allow this edit?"}, "Edited."]
        res = self.worker("w1", steps=steps)
        self.assertEqual(self.record(res).name, "w1")
        self.assertIn("session w1: waits on a dialog: Edit", res.stderr.splitlines())
        blocked = [line for line in self.wait_event("w1", "blocked") if " w1 blocked" in line]
        self.assertEqual(
            self.ok(self.crew("next-event", "--after", "0", "--gen", "0")).stdout,
            f"1 {blocked[0]}\n",
        )
        self.assertIn("Allow this edit?", self.ok(self.session("read", "w1")).stdout)

    def test_the_codex_trust_dialog_at_start_is_said_on_crew_runs_stderr(self):
        fake_tui.install(self.server.bin)
        never, scenario = (os.path.join(self.root, n) for n in ("never", "w1.codex.json"))
        steps = [{"kind": "screen", "text": "Trust this folder?"}, {"kind": "pause", "file": never}]
        with open(scenario, "w", encoding="utf-8") as f:
            json.dump({"tui_turns": [steps]}, f)
        codex = {"FAKE_SCENARIO": scenario, "FAKE_LOG": os.path.join(self.root, "w1.codex.jsonl")}
        res = self.worker("w1", client="codex", **codex)
        self.assertEqual(self.record(res).detail, "codex")
        self.assertIn("session w1: waits on a dialog: Trust this folder?", res.stderr.splitlines())
        self.wait_event("w1", "blocked")
        self.assertEqual(self.server.option("w1", "@state"), "blocked")
