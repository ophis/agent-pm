"""A role run through `agent-pm-crew run -- agent-pm-delegate run … --detach` with `pty` the
[hosts] default (FR-47, FR-48 356-16), on the real commands (manager/tests/live_crew.py): crew run
records it, attach neither shows it nor fails (its status's attach is null), its --display is kept
opaque, and crew resume brings back both sessions once they are gone.
"""

import json
import os

from agent_pm.manager import State

from delegate.tests.replies import mark
from manager.tests.live_crew import MANAGER, ROLE, LiveCrew

START = mark("start", "echoing")
DISPLAY = f"{MANAGER}/below"


class PtyCrew(LiveCrew):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w", encoding="utf-8") as f:
            f.write('[session.hosts]\ndefault = "pty"\n')

    def on_pty(self, name: str) -> bool:
        if not os.path.isdir(os.path.join(self.agent_pm, "pty", name)):
            return False
        (line,) = self.ok(self.session("status", name)).stdout.splitlines()
        return json.loads(line)["alive"]

    def test_a_role_run_on_the_pty_host_runs_attaches_without_show_and_resumes(self):
        calls = self.spy("agent-pm-delegate")
        record = self.record(self.role("r", "--display", DISPLAY, steps=[START]))
        harness, tui = record.sessions
        self.addCleanup(lambda: [self.session("close", name) for name in record.sessions])
        self.assertEqual((tui, record.display), (f"{ROLE}-{record.sid[:8]}", DISPLAY))
        entry = self.roster()["r"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, record))
        self.wait_event(tui, "done")
        self.assertTrue(self.on_pty(harness) and self.on_pty(tui))
        self.assertFalse(self.live(harness) or self.live(tui))
        self.ok(self.crew("attach"))
        with open(calls, encoding="utf-8") as f:
            self.assertEqual([c for c in f.read().splitlines() if c.startswith("show")], [])
        (status,) = map(json.loads, self.ok(self.crew("status", "r")).stdout.splitlines())
        self.assertEqual((status["alive"], status["attach"]), (True, None))
        self.assertEqual(self.roster()["r"].state, State.LIVE)

        for name in (harness, tui):
            self.session("close", name)
        self.until(lambda: not self.on_pty(harness) and not self.on_pty(tui), "both gone")
        out = self.ok(self.crew("attach")).stdout.splitlines()
        self.assertIn(f"resume: agent-pm-crew resume --manager {MANAGER} r", out)
        self.assertEqual(self.roster()["r"].state, State.GONE)
        resumed = self.record(self.crew("resume", "r", **self.scene("r", [START])))
        self.assertEqual(
            (resumed.sid, resumed.sessions, resumed.display),
            (record.sid, record.sessions, DISPLAY),
        )
        self.until(lambda: self.on_pty(harness) and self.on_pty(tui), "both back on the pty")
        entry = self.roster()["r"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, resumed))
