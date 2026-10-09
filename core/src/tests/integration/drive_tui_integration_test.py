"""drive.py's tui runner run as a process against a private tmux server, fake_claude.py as `claude`: the Stop hook's
stop line, the nudge, the outcome; --detach from the manager, its driver session and outcome event."""
import functools
import json
import os
import re
import subprocess
import sys
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(TESTS), TESTS]
import hermetic  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402
import fake_claude  # noqa: E402
import live_tmux  # noqa: E402
from drive_integration_test import DONE  # noqa: E402
from drive_test import SID, outcome, progress  # noqa: E402

DRIVE = os.path.join(hermetic.CORE, "src", "drive.py")
MANAGER = "mgr"
TUI = f"dummy-tester-{SID[:8]}"
DRIVER = f"{TUI}-drive"
STAMP = "[0-9]{2}:[0-9]{2}:[0-9]{2}"
TURN_END = re.compile(rf"{STAMP} {TUI} done")
OUTCOME = re.compile(rf"{STAMP} {DRIVER} outcome done")
TIMEOUT = 60   # seconds: a drive.py process; the detached driver's outcome line


class TuiRun(unittest.TestCase):
    """As drive_integration_test's Integration (HOME, proj as cwd, work, out, the fake's log, its transcript), on a
    private tmux server with mgr started and attached; one events file. The fake's scenario: turn 1 reports progress
    start, turn 2 (the nudge's) the outcome."""

    def setUp(self):
        self.server = live_tmux.Server(self, os.path.dirname(hermetic.home(self)))
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        root = self.server.root
        self.proj, self.work = (os.path.join(root, d) for d in ("proj", "work"))
        for d in (self.proj, self.work):
            os.mkdir(d)
        self.out, self.events = os.path.join(self.work, "out.md"), os.path.join(root, "events")
        self.scenario, self.log = os.path.join(root, "scenario.json"), os.path.join(root, "log.jsonl")
        with open(self.scenario, "w") as f:
            json.dump({"steps": [progress("start", "echoing")], "turns": [[outcome(DONE)]], "log": self.log}, f)
        self.transcript = clients.claude.transcript(self.proj, SID,
                                                    projects=os.path.join(self.server.home, ".claude", "projects"))

    def drive(self, *extra: str, inside: str | None = None) -> subprocess.CompletedProcess:
        """drive.py --runner tui in proj, env Server.env() plus the scenario and, with `inside`, that session's pane."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}), **{fake_claude.ENV: self.scenario})
        argv = [sys.executable, DRIVE, "--role", "dummy-tester", "--runner", "tui", "--events", self.events, "--input",
                "Hello.", "--out", self.out, "--workdir", self.work, "--sid", SID, *extra]
        return subprocess.run(argv, cwd=self.proj, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=TIMEOUT)

    def record(self) -> dict:
        with open(os.path.join(self.work, drive.RECORD)) as f:
            return json.load(f)

    def calls(self) -> list[dict]:
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def jsonl(self, path: str) -> list[dict]:
        """A JSON-lines file's objects, blank lines skipped."""
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def until(self, enough, what: str, timeout: float = live_tmux.TIMEOUT) -> list[str]:
        """The events file's lines once enough(lines) holds."""
        def check():
            lines = live_tmux.events(self.events)
            return lines if enough(lines) else None

        return live_tmux.wait(check, timeout, what=f"{what} in {self.events}")

    def test_stop_hook_nudge_outcome(self):
        res = self.drive()
        self.assertEqual(res.returncode, 0, res.stderr)
        for text in ("drive.py: status done", f"tmux attach -t '={TUI}'"):
            self.assertIn(text, res.stderr)
        shown = self.server.tmux("display-message", "-p", "-t", f"={TUI}:", "#{session_name} #{pane_dead}")
        self.assertEqual(shown.stdout, f"{TUI} 0\n", shown.stderr)
        self.assertIn({"kind": "stop"}, self.jsonl(os.path.join(self.work, compose.CHANNEL)))
        (call,) = self.calls()
        users = [line["message"]["content"] for line in self.jsonl(self.transcript) if line["type"] == "user"]
        self.assertEqual(users, [call["argv"][0], drive.NUDGE])
        # drive can exit on the outcome before turn 2's Stop hooks run
        lines = self.until(lambda lines: len(lines) >= 2, f"two `{TUI} done` lines")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(TURN_END.fullmatch(line) for line in lines), lines)
        self.assertEqual(self.record()["outcome"]["status"], "done")
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])

    def test_detach_from_the_manager(self):
        res = self.drive("--detach", inside=MANAGER)
        early = live_tmux.events(self.events)
        self.assertEqual(res.returncode, 0, res.stderr)
        # at once: the driver sleeps drive.POLL and tui_claude.PAUSE at least before its outcome line
        self.assertFalse([line for line in early if OUTCOME.fullmatch(line)], early)
        self.assertIn(f"drive.py: driver session {DRIVER}: tmux attach -t '={DRIVER}'", res.stderr)
        lines = self.until(lambda lines: any(OUTCOME.fullmatch(line) for line in lines), f"`{DRIVER} outcome done`",
                           TIMEOUT)
        at = next(i for i, line in enumerate(lines) if OUTCOME.fullmatch(line))
        self.assertTrue(lines[:at] and all(TURN_END.fullmatch(line) for line in lines[:at]), lines)
        check = functools.partial(live_tmux.assert_grid, self, self.server, MANAGER, [[TUI]])
        live_tmux.wait(check, what=f"the grid [[{TUI!r}]]")
        self.assertEqual(self.record()["outcome"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
