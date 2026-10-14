"""delegate/cli.py run --detach as a subprocess on a private tmux server (session/tests/live_tmux.py),
a control-mode client on the manager, the session config's ClaudeAdapter running
session/tests/integration/adapters/claude/fake_claude.py: the one Record line whose sessions are live
on return, the harness session, the TUI beside the caller or in no pane, the harness's outcome and
exited lines, a kill-session of the harness session, an events path tmux would misread, the events
file and sid a launcher gives in $AGENT_PM_EVENTS and $AGENT_PM_SID, a needs_input run answered by
`run --resume --sid --input --detach` (refused while the run lives) whose outcome a
`resume WORKDIR` then leaves alone. Ports main's
tests/integration/core/drive_tui_integration_test.py
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

from agent_pm.utils import Record

from delegate.tests.case import PYTHONPATH, Case
from delegate.tests.integration.cli import ASKING, DONE, RUN, SID
from delegate.tests.replies import block, mark, running
from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude

MANAGER = "mgr"
ROLE = "dummy-tester"
TUI = f"{ROLE}-{SID[:8]}"
HARNESS = f"{TUI}-drive"
STAMP = "[0-9]{2}:[0-9]{2}:[0-9]{2}"
TURN_END = re.compile(rf"{STAMP} {TUI} done")
HARNESS_LINE = re.compile(rf"{STAMP} {HARNESS} ((?:outcome [a-z_]+)|(?:exited -?[0-9]+))")
START = mark("start", "echoing")
WAIT = 60  # seconds: a run
ANSWER = "Use option B."
RESUME = (*RUN[:-1], "resume")


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

    def detach(
        self,
        *extra: str,
        steps,
        turns=(),
        inside=None,
        events=None,
        launcher=False,
        text="Hello.",
        **scenario,
    ):
        """launcher: the events file and sid come as $AGENT_PM_EVENTS and $AGENT_PM_SID, as
        agent-pm-crew run gives them.
        """
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "turns": list(turns), **scenario}, f)
        self.events_used = events or self.events
        given = {"AGENT_PM_EVENTS": self.events_used, "AGENT_PM_SID": SID}
        argv = [*RUN, "--role", ROLE, "--input", text, "--out", self.out, "--workdir"]
        argv += [self.work, "--detach", *extra]
        argv += [] if launcher else ["--sid", SID, "--events", self.events_used]
        return self.cli(argv, inside=inside, given=given if launcher else {})

    def cli(self, argv, *, inside=None, given=None) -> subprocess.CompletedProcess:
        env = self.server.env(
            PYTHONPATH=PYTHONPATH,
            CLAUDE_CONFIG_DIR=os.path.join(self.dir, "config"),
            **{fake_claude.ENV: self.scenario},
            **(self.server.inside(inside) if inside else {}),
            **(given or {}),
        )
        return subprocess.run(
            argv,
            cwd=self.proj,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=WAIT,
        )

    def said(self, res: subprocess.CompletedProcess, *, tui: bool) -> Record:
        """The one Record line, every session it lists live when the command returned."""
        live = {name: self.live(name) for name in (HARNESS, TUI)}
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 1, res.stdout)
        record = Record.parse(res.stdout)
        sessions = (HARNESS, TUI) if tui else (HARNESS,)
        self.assertEqual(
            (record.kind, record.name, record.sessions, record.sid, record.workdir),
            ("role", TUI, sessions, SID, self.work),
        )
        self.assertEqual(
            (record.ctl, record.resume),
            (("agent-pm-delegate",), ("agent-pm-delegate", "resume", self.work)),
        )
        self.assertTrue(all(live[name] for name in sessions), live)
        self.assertIn(f"delegate: session {SID}\n", res.stderr)
        return record

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
        """The events file's lines, shaped, once they hold the harness's exited line and turn_ends
        turn ends and the harness session is gone.
        """

        def check():
            shaped = shape(live_tmux.events(self.events_used))
            return (
                any(s.startswith("exited ") for s in shaped)
                and shaped.count("turn end") >= turn_ends
            )

        self.until(check, f"`{HARNESS} exited` and {turn_ends} turn ends")
        self.until(lambda: not self.live(HARNESS), f"{HARNESS} gone")
        return shape(live_tmux.events(self.events_used))

    def harness_lines(self, shaped: list[str]) -> list[str]:
        return [line for line in shaped if line != "turn end"]

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
        "turn end"
        if TURN_END.fullmatch(line)
        else m[1]
        if (m := HARNESS_LINE.fullmatch(line))
        else line
        for line in lines
    ]


class Tui(Detach):
    def test_from_the_manager_shown_in_its_grid_turn_ends_then_one_outcome_done(self):
        res = self.detach("--mode", "tui", steps=[START], turns=[[block(DONE)]], inside=MANAGER)
        early = shape(live_tmux.events(self.events))
        record = self.said(res, tui=True)
        self.assertNotIn("outcome done", early)
        self.until(
            lambda: live_tmux.assert_grid(self, self.server, MANAGER, [[TUI]]),
            f"the grid [[{TUI!r}]]",
        )
        shaped = self.ended(turn_ends=2)
        self.assertEqual(shaped.count("turn end"), 2)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertTrue(self.live(TUI))
        self.assertTrue(record.display.startswith(MANAGER), record.display)

    def test_outside_any_client_runs_shown_in_no_pane(self):
        res = self.detach("--mode", "tui", steps=[START], turns=[[block(DONE)]])
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertTrue(self.live(TUI))
        clients = self.server.tmux("list-clients", "-t", f"={TUI}")
        self.assertEqual((clients.returncode, clients.stdout), (0, ""), clients.stderr)
        self.assertEqual(self.server.option(TUI, "@pane"), "")
        self.assertEqual(len(self.server.panes(f"={MANAGER}:")), 1)

    def test_kill_session_of_the_harness_stops_the_tui_one_outcome_error(self):
        res = self.detach("--mode", "tui", steps=[START])
        self.said(res, tui=True)
        self.until(lambda: "nudge" in self.kinds(), "the nudge")
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        pid = int(shown.stdout)
        self.assertEqual(self.server.tmux("kill-session", "-t", f"={HARNESS}").returncode, 0)
        self.until(lambda: not self.live(TUI), f"{TUI} stopped")
        self.until(lambda: not running(pid), f"the harness ({pid}) ended")
        outcomes = [s for s in shape(live_tmux.events(self.events)) if s.startswith("outcome")]
        self.assertEqual(outcomes, ["outcome error"])
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        rc = 128 + signal.SIGHUP
        self.assertEqual(
            (self.last("result")["error"], self.last("end")["rc"]),
            (f"stopped: Stopped: {rc}", rc),
        )

    def test_the_launchers_events_file_gets_the_turn_ends_and_one_outcome(self):
        res = self.detach("--mode", "tui", steps=[START], turns=[[block(DONE)]], launcher=True)
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(shaped.count("turn end"), 2)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])

    def test_an_events_path_tmux_would_misread_exits_2_starting_nothing(self):
        events = os.path.join(self.dir, "ev#1")
        res = self.detach("--mode", "tui", steps=[START], events=events)
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(f"delegate: events file {events}: tmux would misread it\n", res.stderr)
        self.assertFalse(self.live(HARNESS) or self.live(TUI))
        self.assertFalse(os.path.exists(events))
        self.assertEqual(os.listdir(self.work), [])


class Headless(Detach):
    def test_sigterm_immediately_after_detach_exits_stopped_with_one_outcome(self):
        res = self.detach(steps=[START])
        self.said(res, tui=False)
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        os.kill(int(shown.stdout), signal.SIGTERM)
        self.assertEqual(self.ended(), ["outcome error", "exited 143"])

    def test_done_one_outcome_line_the_harness_session_gone(self):
        res = self.detach(steps=[START, block(DONE)])
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")

    def test_the_launchers_events_file_gets_one_outcome_line(self):
        res = self.detach(steps=[START, block(DONE)], launcher=True)
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome done", "exited 0"])

    def test_needs_input_one_outcome_line_the_harness_session_gone(self):
        res = self.detach(steps=[block(ASKING)])
        self.said(res, tui=False)
        self.assertEqual(self.ended(), ["outcome needs_input", "exited 0"])
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])


class Answer(Detach):
    def setUp(self):
        super().setUp()
        self.second = os.path.join(self.dir, "answer-events")

    def answer(self, *extra: str, steps, turns=()) -> subprocess.CompletedProcess:
        return self.detach(
            *extra, "--resume", steps=steps, turns=turns, events=self.second, text=ANSWER
        )

    def answered(self, record: Record, *, turn_ends: int) -> None:
        """record's run resumed SID with ANSWER to done; a resume then starts nothing."""
        shaped = self.ended(turn_ends=turn_ends)
        self.assertEqual(self.harness_lines(shaped), ["outcome done", "exited 0"])
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        self.assertEqual(self.attempt(), 2)
        records = self.records()
        self.assertEqual([e["text"] for e in records if e["kind"] == "input"], ["Hello.", ANSWER])
        self.assertEqual(
            {(e["sid"], e["cwd"]) for e in records if e["kind"] == "session"}, {(SID, self.proj)}
        )
        again = self.cli([*RESUME, self.work])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(Record.parse(again.stdout), record)
        self.assertFalse(self.live(HARNESS))
        self.assertEqual(shape(live_tmux.events(self.second)), shaped)

    def attempt(self) -> int:
        with open(os.path.join(self.work, "delegate.json")) as f:
            return json.load(f)["attempt"]

    def test_headless_needs_input_answered_by_a_detached_resume(self):
        self.said(self.detach(steps=[block(ASKING)]), tui=False)
        self.assertEqual(self.ended(), ["outcome needs_input", "exited 0"])
        res = self.answer(steps=[START, block(DONE)])
        self.answered(self.said(res, tui=False), turn_ends=0)

    def test_tui_needs_input_answered_by_a_detached_resume_closing_its_open_tui(self):
        res = self.detach("--mode", "tui", steps=[START], turns=[[block(ASKING)]])
        self.said(res, tui=True)
        shaped = self.ended(turn_ends=2)
        self.assertEqual(self.harness_lines(shaped), ["outcome needs_input", "exited 0"])
        self.assertTrue(self.live(TUI))
        res = self.answer("--mode", "tui", steps=[START], turns=[[block(DONE)]])
        self.answered(self.said(res, tui=True), turn_ends=2)

    def test_an_answer_while_the_run_lives_exits_2_starting_nothing(self):
        self.said(self.detach(steps=[START], hang=True), tui=False)
        self.until(lambda: "progress" in self.kinds(), "the client's start mark")
        res = self.answer(steps=[START, block(DONE)])
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(f"delegate: {self.work}: delegate is still alive\n", res.stderr)
        self.assertTrue(self.live(HARNESS))
        self.assertEqual(self.attempt(), 1)
        shown = self.server.tmux("display-message", "-p", "-t", f"={HARNESS}:", "#{pane_pid}")
        self.assertEqual(self.server.tmux("kill-session", "-t", f"={HARNESS}").returncode, 0)
        self.until(lambda: not running(int(shown.stdout)), "the harness ended")


if __name__ == "__main__":
    unittest.main()
