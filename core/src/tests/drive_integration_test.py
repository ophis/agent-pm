"""drive.py run as a process (the headless runner) against fake_claude.py on PATH as `claude`."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
import clients  # noqa: E402
import fake_claude  # noqa: E402
from drive_test import SID, outcome, progress  # noqa: E402

DRIVE = os.path.join(hermetic.CORE, "src", "drive.py")
DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class Integration(unittest.TestCase):
    def setUp(self):
        hermetic.home(self)
        home = os.environ["HOME"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.proj, self.work, bin_dir = (os.path.join(self.root, d) for d in ("proj", "work", "bin"))
        for d in (self.proj, self.work, bin_dir):
            os.mkdir(d)
        self.out = os.path.join(self.work, "out.md")
        self.scenario, self.log = os.path.join(self.root, "scenario.json"), os.path.join(self.root, "log.jsonl")
        self.env = {"PATH": fake_claude.install(bin_dir), "HOME": home, "PYTHONUTF8": "1",
                    fake_claude.ENV: self.scenario}
        self.transcript = clients.claude.transcript(self.proj, SID, projects=os.path.join(home, ".claude", "projects"))

    def argv(self, *extra) -> list[str]:
        return [sys.executable, DRIVE, "--role", "dummy-tester", "--input", "Hello.", "--out", self.out, "--workdir",
                self.work, "--sid", SID, *extra]

    def scene(self, steps, **scenario) -> None:
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.log}, f)

    def drive(self, steps, *extra, cwd=None, **scenario) -> subprocess.CompletedProcess:
        """drive.py's result, in `cwd` (default proj), the fake running scenario `steps` plus `scenario`'s keys."""
        self.scene(steps, **scenario)
        return subprocess.run(self.argv(*extra), cwd=cwd or self.proj, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def record(self) -> dict:
        with open(os.path.join(self.work, "run.json")) as f:
            return json.load(f)

    def calls(self) -> list[dict]:
        """The fake's invocations, from its log."""
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def transcribed(self) -> list[dict]:
        """The transcript's lines."""
        with open(self.transcript) as f:
            return [json.loads(line) for line in f]


class NewRun(Integration):
    def test_done(self):
        res = self.drive([progress("start", "echoing"), "Echoing.", outcome(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in (f"session {SID}", "Progress (start): echoing", "Echoing.", "status done"):
            self.assertIn(line, res.stderr)
        with open(self.out) as f:
            self.assertEqual(f.read(), "Hello.\n")
        rec = self.record()
        (entry,) = rec["sessions"]
        self.assertEqual([type(entry.pop(k)) for k in ("started", "ended")], [str, str])
        self.assertEqual(entry, {"sid": SID, "cwd": self.proj, "project": False, "transcript": self.transcript,
                                 "resume": f"cd {self.proj} && claude --resume {SID} --add-dir {self.work}"})
        self.assertEqual([{k: p[k] for k in ("name", "text")} for p in rec["progress"]],
                         [{"name": "start", "text": "echoing"}])
        self.assertEqual(rec["outcome"], {**DONE, "url": self.out, "questions": [], "files": []})
        self.assertTrue(os.path.isfile(self.transcript))
        (call,) = self.calls()
        argv = call["argv"]
        self.assertIn("-p", argv)
        self.assertEqual((argv[argv.index("--session-id") + 1], argv[argv.index("--output-format") + 1]),
                         (SID, "stream-json"))


def prompt(call: dict) -> str:
    """The prompt of a fake invocation: the argument after `-p`."""
    return call["argv"][call["argv"].index("-p") + 1]


class Outcomes(Integration):
    def test_needs_input(self):
        asking = {"status": "needs_input", "title": "echo", "summary": "Asking.", "questions": ["Which?"]}
        res = self.drive([outcome(asking)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("status needs_input", res.stderr)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.record()["outcome"]["questions"], ["Which?"])

    def test_no_outcome(self):
        res = self.drive(["Thinking."])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("drive.py: the agent run returned no outcome", res.stderr)
        rec = self.record()
        self.assertIsNone(rec["outcome"])
        self.assertIsInstance(rec["sessions"][0]["ended"], str)
        self.assertFalse(os.path.exists(self.out))

    def test_invalid_outcome_then_fixed(self):
        res = self.drive([outcome({k: v for k, v in DONE.items() if k != "deliverable"})])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("drive.py: invalid outcome: done with a local destination must carry the deliverable", res.stderr)
        self.assertIsNone(self.record()["outcome"])
        self.assertFalse(os.path.exists(self.out))
        res = self.drive([outcome(DONE)], "--resume")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.out) as f:
            self.assertEqual(f.read(), "Hello.\n")
        self.assertEqual(self.record()["outcome"]["status"], "done")

    def test_nonzero_exit(self):
        res = self.drive([outcome(DONE)], exit=1)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("drive.py: the client exited 1", res.stderr)
        self.assertIsNone(self.record()["outcome"])
        self.assertFalse(os.path.exists(self.out))


class Resume(Integration):
    def test_resume_reuses_the_recorded_cwd_and_session(self):
        res = self.drive([progress("start", "first")])
        self.assertEqual(res.returncode, 1, res.stderr)
        # Stamps have one-second resolution: a sentinel tells a kept `started` from a fresh one.
        rec = self.record()
        rec["sessions"][0]["started"] = started = "2000-01-01T00:00:00+0000"
        with open(os.path.join(self.work, "run.json"), "w") as f:
            json.dump(rec, f)
        res = self.drive([progress("round", "second"), outcome(DONE)], "--resume", cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        first, second = self.calls()
        self.assertEqual(second["cwd"], self.proj)
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], SID)
        self.assertNotIn("--session-id", second["argv"])
        self.assertTrue(prompt(second).startswith("Resumed agent run"))
        users = [line for line in self.transcribed() if line["type"] == "user"]
        self.assertEqual([u["message"]["content"] for u in users], [prompt(first), prompt(second)])
        rec = self.record()
        (entry,) = rec["sessions"]
        self.assertEqual(entry["started"], started)
        self.assertEqual([p["name"] for p in rec["progress"]], ["start", "round"])
        self.assertEqual(rec["outcome"]["status"], "done")


class Interrupt(Integration):
    def popen(self) -> subprocess.Popen:
        """drive.py in its own session and process group, killed with its agents when the test ends."""
        # A suite started in the background inherits SIGINT ignored; drive.py installs no handler of its own.
        proc = subprocess.Popen(self.argv(), cwd=self.proj, env=self.env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc: subprocess.Popen) -> None:
        try:
            pids = [call["pid"] for call in self.calls()]
        except (OSError, ValueError):
            pids = []
        for kill, target in [(os.killpg, proc.pid)] + [(os.kill, pid) for pid in pids]:
            try:
                kill(target, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()

    def started(self) -> bool:
        try:
            return "start" in [p["name"] for p in self.record()["progress"]]
        except (OSError, ValueError):
            return False

    def test_interrupt_kills_a_hung_agent(self):
        self.scene([progress("start", "waiting")], hang=True)
        proc = self.popen()
        deadline = time.monotonic() + 30
        while not self.started():
            if proc.poll() is not None or time.monotonic() > deadline:
                if proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                self.fail(f"drive.py never reported the start (rc {proc.returncode}):\n{out}{err}")
            time.sleep(0.05)
        (call,) = self.calls()
        os.kill(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn(proc.returncode, (-signal.SIGINT, 130), out + err)
        with self.assertRaises(ProcessLookupError):
            os.kill(call["pid"], 0)
        rec = self.record()
        self.assertIsInstance(rec["sessions"][0]["ended"], str)
        self.assertIsNone(rec["outcome"])


if __name__ == "__main__":
    unittest.main()
