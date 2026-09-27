"""Runs linear-research.sh with HOME in a temp dir: fake claude, tmux and date in $HOME/.local/bin
(first on the script's PATH) and a fake pick.py next to a copy of the script. --gate uses the real pick.py."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKES = {
    "claude": """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE/claude.calls"
if [[ " $* " == *" stream-json "* ]]; then cat "$FAKE/probe"; exit 0; fi
exit "${FAKE_CLAUDE_EXIT:-0}"
""",
    "tmux": """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE/tmux.calls"
case "$1" in
  has-session) [[ -n "${FAKE_TMUX_ACTIVE:-}" ]] ;;
  new-session) cd "$6" && bash -c "$7" ;;
esac
""",
    "date": """#!/bin/bash
if [[ "$1" == "+%H" ]]; then echo "$FAKE_HOUR"; else exec /bin/date "$@"; fi
""",
}

FAKE_PICK = """#!/usr/bin/env python3
import os, sys
open(os.path.join(os.environ["FAKE"], "pick.calls"), "a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "--gate":
    os.execv(sys.executable, [sys.executable, os.environ["REAL_PICK"]] + sys.argv[1:])
if sys.argv[1] == "--prune":
    sys.exit(int(os.environ.get("FAKE_PRUNE_EXIT", "0")))
out = os.environ.get("FAKE_PLAN" if sys.argv[1] == "--plan" else "FAKE_CLAIM", "")
if out:
    print(out)
"""

SID = "0f0f0f0f-1111-2222-3333-444444444444"


def probe(five, status="allowed", seven=0.1):
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": status, "utilization": five, "unifiedWindows": {
        "five_hour": {"utilization": five, "resetsAt": 1}, "seven_day": {"utilization": seven, "resetsAt": 2}}}}) + "\n"


class Dispatcher(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.fake = os.path.join(self.home, "fake")
        bindir = os.path.join(self.home, ".local", "bin")
        sdir = os.path.join(self.home, "scripts")
        for d in (self.fake, bindir, sdir):
            os.makedirs(d)
        for name, body in FAKES.items():
            self.write(os.path.join(bindir, name), body, 0o755)
        self.write(os.path.join(sdir, "pick.py"), FAKE_PICK, 0o755)
        self.script = shutil.copy(os.path.join(SCRIPTS, "linear-research.sh"), sdir)
        self.runs = os.path.join(self.home, "runs.log")
        self.env = {"HOME": self.home, "FAKE": self.fake, "REAL_PICK": os.path.join(SCRIPTS, "pick.py"), "FAKE_HOUR": "02"}
        self.set_probe(probe(0.1))

    def write(self, path, body, mode=0o644):
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, mode)

    def set_probe(self, text):
        self.write(os.path.join(self.fake, "probe"), text)

    def tick(self, *args, **env):
        r = subprocess.run(["/bin/bash", self.script, *args], env={**self.env, **env}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.stderr = r.stderr

    def calls(self, name):
        try:
            with open(os.path.join(self.fake, f"{name}.calls")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def runs_log(self):
        try:
            with open(self.runs) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def research_calls(self):
        return [c for c in self.calls("claude") if "stream-json" not in c]

    def plan_calls(self):
        return [c for c in self.calls("pick") if c.startswith("--plan")]

    def test_prune_before_plan(self):
        self.tick()
        self.assertEqual(self.calls("pick"), [f"--prune {self.runs}", f"--plan {self.runs}"])

    def test_prune_failure_continues(self):
        self.tick(FAKE_PRUNE_EXIT="1", FAKE_PLAN="new", FAKE_CLAIM="TASK-9 https://l/TASK-9")
        self.assertIn("skip: prune failed\n", self.runs_log())
        self.assertEqual(len(self.research_calls()), 1)

    def test_outside_hours(self):
        self.tick(FAKE_HOUR="12", FAKE_PLAN="new")
        self.assertIn("skip: outside hours", self.runs_log())
        self.assertEqual(self.calls("pick"), [])

    def test_hours_boundaries(self):
        for hour in ("01", "06"):
            self.tick(FAKE_HOUR=hour)
        for hour in ("00", "07", "23"):
            self.tick(FAKE_HOUR=hour)
        self.assertEqual(len(self.plan_calls()), 2)
        self.assertEqual(len(self.calls("pick")), 4)

    def test_now_skips_only_hours(self):
        self.tick("--now", FAKE_HOUR="12", FAKE_PLAN="new", FAKE_CLAIM="TASK-1 https://l/TASK-1")
        self.assertEqual(len(self.research_calls()), 1)
        self.tick("--now", FAKE_HOUR="12", FAKE_TMUX_ACTIVE="1")
        self.assertIn("skip: previous run still active", self.runs_log())

    def test_tmux_active(self):
        self.tick(FAKE_TMUX_ACTIVE="1", FAKE_PLAN="new")
        self.assertIn("skip: previous run still active", self.runs_log())
        self.assertEqual(self.calls("pick"), [])

    def test_nothing_to_do_skips_probe(self):
        self.tick()
        self.assertEqual(self.plan_calls(), [f"--plan {self.runs}"])
        self.assertEqual(self.calls("claude"), [])
        self.assertIn("skip: nothing to do", self.runs_log())

    def test_no_rate_limit_event(self):
        self.set_probe('{"type":"system"}\n')
        self.tick(FAKE_PLAN="new")
        self.assertIn("skip: new blocked by usage: no rate_limit_event", self.runs_log())
        self.assertEqual(self.research_calls(), [])

    def test_resume(self):
        self.set_probe(probe(0.7))
        self.tick(FAKE_PLAN=f"resume TASK-8 {SID} 2 https://l/TASK-8", FAKE_CLAUDE_EXIT="3")
        self.assertIn(f"resume TASK-8 session={SID} n=2\n", self.runs_log())
        self.assertIn(f"end TASK-8 session={SID} exit=3\n", self.runs_log())
        [call] = self.research_calls()
        self.assertIn(f"--resume {SID}", call)
        self.assertNotIn("--session-id", call)
        self.assertIn("Resumed run 2 for TASK-8 (https://l/TASK-8) after an interruption. "
                      f"Re-read {self.home}/stages/deep-research.md first (it may have changed since this session started) "
                      "and follow its resume rule.", call)
        self.assertIn(f"--model opus --effort xhigh --permission-mode auto --add-dir {self.home} --add-dir", call)
        self.assertNotIn("--claim", " ".join(self.calls("pick")))

    def test_resume_blocked(self):
        for text in (probe(0.9), probe(0.1, status="rejected"), probe(0.1, seven=1.0)):
            self.set_probe(text)
            self.tick(FAKE_PLAN=f"resume TASK-8 {SID} 1 https://l/TASK-8")
        self.assertEqual(self.runs_log().count("skip: resume blocked by usage"), 3)
        self.assertEqual(self.research_calls(), [])

    def test_new(self):
        self.tick(FAKE_PLAN="new", FAKE_CLAIM="TASK-9 https://l/TASK-9")
        self.assertEqual(self.calls("pick")[-1], f"--claim {self.runs}")
        log = self.runs_log()
        self.assertRegex(log, r"start TASK-9 session=(\S+) transcript=\S+/\.claude/projects/\S+-work/\1\.jsonl")
        sid = log.split("session=")[1].split()[0]
        self.assertIn(f"end TASK-9 session={sid} exit=0", log)
        [call] = self.research_calls()
        self.assertIn(f"--session-id {sid}", call)
        self.assertIn(f"Follow {self.home}/stages/deep-research.md to handle TASK-9 (https://l/TASK-9).", call)
        self.assertIn(f"new-session -d -s linear-research -c {self.home}/work", self.calls("tmux")[-1])

    def test_new_blocked(self):
        self.set_probe(probe(0.9))
        self.tick(FAKE_PLAN="new", FAKE_CLAIM="TASK-9 https://l/TASK-9")
        self.assertIn("skip: new blocked by usage", self.runs_log())
        self.assertNotIn("--claim", " ".join(self.calls("pick")))

    def test_new_queue_empty_at_claim(self):
        self.tick(FAKE_PLAN="new")
        self.assertIn("skip: queue empty", self.runs_log())
        self.assertEqual(self.research_calls(), [])

    def test_dry_run(self):
        self.tick("--dry-run", FAKE_HOUR="12", FAKE_TMUX_ACTIVE="1", FAKE_PLAN="new", FAKE_CLAIM="TASK-9 https://l/TASK-9")
        self.assertEqual(self.calls("pick")[0], f"--plan --dry-run {self.runs}")
        self.assertNotIn("--prune", " ".join(self.calls("pick")))
        self.assertNotIn("--claim", " ".join(self.calls("pick")))
        self.assertEqual(self.research_calls(), [])
        self.assertEqual(len(self.calls("claude")), 1)
        self.assertEqual(self.runs_log(), "")
        self.assertIn("usage: status=allowed five_hour=0.1 seven_day=0.1 (new allowed)", self.stderr)


if __name__ == "__main__":
    unittest.main()
