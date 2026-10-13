"""delegate/cli.py --detach as a subprocess on a private tmux server (session/tests/live_tmux.py), a
control-mode client on the manager, the session config's ClaudeAdapter running
session/tests/integration/adapters/claude/fake_claude.py: the Driver session, the TUI beside the
caller or in no pane, the outcome line, a kill-session of the Driver session, an events path tmux
would misread. Ports main's tests/integration/core/drive_tui_integration_test.py
test_detach_from_the_manager. Not ported, as role entries are TASK-356: its
test_detach_events_default (the manager directory's events file needs roster) and main's
manager/tests/integration/crew_integration_test.py Roster role-run cases:
test_killed_role_run_gone_then_recovered, test_stop_a_role_run_by_its_tui_session,
test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume, the role parts of
test_stop_all_stops_what_mgr_opened_and_only_that and
test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker.
"""

import json
import os
import re
import signal
import subprocess
import tempfile
import unittest

from delegate.tests.case import PYTHONPATH, Case
from delegate.tests.integration.cli import ASKING, CLI, DONE, SID
from delegate.tests.replies import block, mark, running
from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude

MANAGER = "mgr"
ROLE = "dummy-tester"
TUI = f"{ROLE}-{SID[:8]}"
DRIVER = f"{TUI}-drive"
STAMP = "[0-9]{2}:[0-9]{2}:[0-9]{2}"
TURN_END = re.compile(rf"{STAMP} {TUI} done")
OUTCOME = re.compile(rf"{STAMP} {DRIVER} (outcome [a-z_]+)")
START = mark("start", "echoing")
WAIT = 60  # seconds: a run
# the second turn end's line can follow the outcome's: its hook marks after its record
DONE_LINES = (
    ["turn end", "turn end", "outcome done"],
    ["turn end", "outcome done", "turn end"],
)


class Detach(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.proj, self.work = (os.path.join(self.dir, d) for d in ("proj", "work"))
        for d in (self.proj, self.work):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")
        self.events, self.scenario = (
            os.path.join(self.dir, f) for f in ("events", "scenario.json")
        )

    def detach(self, *extra: str, steps, turns=(), inside=None, events=None):
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "turns": list(turns)}, f)
        env = self.server.env(
            PYTHONPATH=PYTHONPATH,
            CLAUDE_CONFIG_DIR=os.path.join(self.dir, "config"),
            **{fake_claude.ENV: self.scenario},
            **(self.server.inside(inside) if inside else {}),
        )
        argv = [*CLI, "--role", ROLE, "--input", "Hello.", "--out", self.out, "--workdir"]
        argv += [self.work, "--sid", SID, "--detach", "--events", events or self.events, *extra]
        return subprocess.run(
            argv,
            cwd=self.proj,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=WAIT,
        )

    def said(self, res: subprocess.CompletedProcess, *, tui: bool) -> None:
        want = [
            f"delegate: session {SID}\n",
            f"delegate: driver session {DRIVER}: tmux attach -t '={DRIVER}'\n",
            *([f"delegate: tui session {TUI}: tmux attach -t '={TUI}'\n"] if tui else []),
        ]
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
        self.assertTrue(res.stderr.endswith("".join(want)), res.stderr)

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave an exited pane's process unreaped until another child of
        # the server exits
        self.server.tmux("run-shell", "-b", "true")

    def live(self, name: str) -> bool:
        return self.server.tmux("has-session", "-t", f"={name}").returncode == 0

    def until(self, check, what: str):
        def poked():
            self.poke()
            return check()

        return live_tmux.wait(poked, WAIT, what=what)

    def ended(self, turn_ends: int = 0) -> list[str]:
        """The events file's lines, shaped, once they hold an outcome and turn_ends turn ends and
        the Driver session is gone.
        """

        def check():
            shaped = shape(live_tmux.events(self.events))
            return (
                any(s.startswith("outcome ") for s in shaped)
                and shaped.count("turn end") >= turn_ends
            )

        self.until(check, f"`{DRIVER} outcome` and {turn_ends} turn ends")
        self.until(lambda: not self.live(DRIVER), f"{DRIVER} gone")
        return shape(live_tmux.events(self.events))

    def records(self):
        try:
            with open(os.path.join(self.work, "run.jsonl")) as stream:
                return [json.loads(line) for line in stream]
        except FileNotFoundError:
            return []

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind: str) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()


def shape(lines: list[str]) -> list[str]:
    return [
        "turn end" if TURN_END.fullmatch(line) else m[1] if (m := OUTCOME.fullmatch(line)) else line
        for line in lines
    ]


class Tui(Detach):
    def test_from_the_manager_shown_in_its_grid_turn_ends_then_one_outcome_done(self):
        res = self.detach("--mode", "tui", steps=[START], turns=[[block(DONE)]], inside=MANAGER)
        early = shape(live_tmux.events(self.events))
        self.said(res, tui=True)
        self.assertNotIn("outcome done", early)
        self.assertTrue(self.live(DRIVER))
        self.until(
            lambda: live_tmux.assert_grid(self, self.server, MANAGER, [[TUI]]),
            f"the grid [[{TUI!r}]]",
        )
        self.assertIn(self.ended(turn_ends=2), DONE_LINES)
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertTrue(self.live(TUI))

    def test_outside_any_client_runs_shown_in_no_pane(self):
        res = self.detach("--mode", "tui", steps=[START], turns=[[block(DONE)]])
        self.said(res, tui=True)
        self.assertIn(self.ended(turn_ends=2), DONE_LINES)
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertTrue(self.live(TUI))
        clients = self.server.tmux("list-clients", "-t", f"={TUI}")
        self.assertEqual((clients.returncode, clients.stdout), (0, ""), clients.stderr)
        self.assertEqual(self.server.option(TUI, "@pane"), "")
        self.assertEqual(len(self.server.panes(f"={MANAGER}:")), 1)

    def test_kill_session_of_the_driver_session_stops_the_tui_one_outcome_error(self):
        res = self.detach("--mode", "tui", steps=[START])
        self.said(res, tui=True)
        self.until(lambda: "nudge" in self.kinds(), "the nudge")
        shown = self.server.tmux("display-message", "-p", "-t", f"={DRIVER}:", "#{pane_pid}")
        pid = int(shown.stdout)
        self.assertEqual(self.server.tmux("kill-session", "-t", f"={DRIVER}").returncode, 0)
        self.until(lambda: not self.live(TUI), f"{TUI} stopped")
        self.until(lambda: not running(pid), f"the driver form ({pid}) ended")
        outcomes = [s for s in shape(live_tmux.events(self.events)) if s.startswith("outcome")]
        self.assertEqual(outcomes, ["outcome error"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        rc = 128 + signal.SIGHUP
        self.assertEqual(
            (self.last("result")["error"], self.last("end")["rc"]),
            (f"stopped: Stopped: {rc}", rc),
        )

    def test_an_events_path_tmux_would_misread_detaches_then_the_driver_form_exits_2_outcome_error(
        self,
    ):
        events = os.path.join(self.dir, "ev#1")
        self.assertEqual(
            self.server.tmux("set-option", "-wg", "remain-on-exit", "on").returncode, 0
        )
        res = self.detach("--mode", "tui", steps=[START], events=events)
        self.said(res, tui=True)
        dead = ("display-message", "-p", "-t", f"={DRIVER}:", "#{pane_dead} #{pane_dead_status}")
        self.until(lambda: self.server.tmux(*dead).stdout == "1 2\n", f"{DRIVER} exited 2")
        self.assertEqual(shape(live_tmux.events(events)), ["outcome error"])
        pane = self.server.tmux("capture-pane", "-p", "-J", "-t", f"={DRIVER}:").stdout
        self.assertIn(f"delegate: events file {events}: tmux would misread it\n", pane)
        self.assertFalse(self.live(TUI))


class Headless(Detach):
    def test_done_one_outcome_line_the_driver_session_gone(self):
        res = self.detach(steps=[START, block(DONE)])
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome done"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")

    def test_needs_input_one_outcome_line_the_driver_session_gone(self):
        res = self.detach(steps=[block(ASKING)])
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome needs_input"])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])


if __name__ == "__main__":
    unittest.main()
