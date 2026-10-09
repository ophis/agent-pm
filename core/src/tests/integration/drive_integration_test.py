"""drive.py run as a process (the headless runner) against fake_claude.py on PATH as `claude`."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(TESTS), TESTS]
import hermetic  # noqa: E402
import clients  # noqa: E402
import fake_claude  # noqa: E402
from drive_test import SID, outcome, progress  # noqa: E402

DRIVE = os.path.join(hermetic.CORE, "src", "drive.py")
DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class Integration(unittest.TestCase):
    ROLE = "dummy-tester"

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
        return [sys.executable, DRIVE, "--role", self.ROLE, "--input", "Hello.", "--out", self.out, "--workdir",
                self.work, "--sid", SID, *extra]

    def scene(self, steps, **scenario) -> None:
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario, "log": self.log}, f)

    def drive(self, steps, *extra, cwd=None, **scenario) -> subprocess.CompletedProcess:
        """drive.py's result, in `cwd` (default proj), the fake running scenario `steps` plus `scenario`'s keys."""
        self.scene(steps, **scenario)
        return subprocess.run(self.argv(*extra), cwd=cwd or self.proj, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def popen(self, *extra) -> subprocess.Popen:
        """drive.py in its own session and process group, killed with its agents when the test ends."""
        # A suite started in the background inherits SIGINT ignored; drive.py installs no handler of its own.
        proc = subprocess.Popen(self.argv(*extra), cwd=self.proj, env=self.env, stdin=subprocess.DEVNULL,
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

    def until(self, proc: subprocess.Popen, check, what: str) -> None:
        """Returns once check() holds; fails naming `what` once `proc` (popen's) ends first or 30 s pass."""
        deadline = time.monotonic() + 30
        while not check():
            if proc.poll() is not None or time.monotonic() > deadline:
                if proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                self.fail(f"drive.py never {what} (rc {proc.returncode}):\n{out}{err}")
            time.sleep(0.05)

    def events(self) -> list[dict]:
        """The run's record, <workdir>/run.jsonl, one dict per line."""
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def last(self, kind: str) -> dict:
        return [e for e in self.events() if e["kind"] == kind][-1]

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
        events = [{k: v for k, v in e.items() if k != "ts"} for e in self.events()]
        self.assertEqual(events, [
            {"kind": "input", "text": "Hello."},
            {"kind": "session", "sid": SID, "cwd": self.proj, "project": False, "transcript": self.transcript,
             "resume": f"cd {self.proj} && claude --resume {SID} --add-dir {self.work}"},
            progress("start", "echoing"), outcome(DONE),
            {"kind": "result", "outcome": {"status": "done", "title": "echo", "summary": "Hello.", "questions": [],
                                           "url": self.out, "files": []}},
            {"kind": "end", "sid": SID, "rc": 0}])
        self.assertEqual(sorted(os.listdir(self.work)), ["out.md", "run.jsonl"])
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
        self.assertEqual(self.last("result")["outcome"]["questions"], ["Which?"])

    def test_no_outcome(self):
        res = self.drive(["Thinking."])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("drive.py: the agent run returned no outcome", res.stderr)
        self.assertEqual([e["kind"] for e in self.events()][-2:], ["result", "end"])
        self.assertEqual(self.last("result")["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))

    def test_invalid_outcome_then_fixed(self):
        res = self.drive([outcome({k: v for k, v in DONE.items() if k != "deliverable"})])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("drive.py: invalid outcome: done with a local destination must carry the deliverable", res.stderr)
        self.assertIn("must carry the deliverable", self.last("result")["error"])
        self.assertFalse(os.path.exists(self.out))
        res = self.drive([outcome(DONE)], "--resume")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.out) as f:
            self.assertEqual(f.read(), "Hello.\n")
        self.assertEqual(self.last("result")["outcome"]["status"], "done")

    def test_nonzero_exit(self):
        res = self.drive([outcome(DONE)], exit=1)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertIn("drive.py: the client exited 1", res.stderr)
        self.assertEqual((self.last("result")["error"], self.last("end")["rc"]), ("the client exited 1", 1))
        self.assertFalse(os.path.exists(self.out))


class Stderr(Integration):
    def test_the_clients_stderr_lines_land_in_the_record(self):
        res = self.drive([progress("start", "first")])
        self.assertEqual(res.returncode, 1, res.stderr)
        os.unlink(self.transcript)
        res = self.drive([outcome(DONE)], "--resume")
        self.assertEqual(res.returncode, 3, res.stderr)
        said = f"No conversation found with session ID: {SID}"
        self.assertIn(said, res.stderr)
        stderr = [e for e in self.events() if e["kind"] == "stderr"]
        self.assertEqual([e["text"] for e in stderr], [said])
        self.assertEqual([e["kind"] for e in self.events()][-3:], ["stderr", "result", "end"])
        self.assertEqual((self.last("result")["error"], self.last("end")["rc"]), ("the client exited 1", 1))


class Resume(Integration):
    def test_resume_reuses_the_recorded_cwd_and_session(self):
        res = self.drive([progress("start", "first")])
        self.assertEqual(res.returncode, 1, res.stderr)
        res = self.drive([progress("round", "second"), outcome(DONE)], "--resume", "--input", "Use B.", cwd=self.root)
        self.assertEqual(res.returncode, 0, res.stderr)
        first, second = self.calls()
        self.assertEqual(second["cwd"], self.proj)
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], SID)
        self.assertNotIn("--session-id", second["argv"])
        self.assertTrue(prompt(second).startswith("Resumed agent run"))
        users = [line for line in self.transcribed() if line["type"] == "user"]
        self.assertEqual([u["message"]["content"] for u in users], [prompt(first), prompt(second)])
        self.assertTrue(prompt(second).endswith("\n# Input\n\nUse B.\n"))
        events = self.events()
        self.assertEqual([e["kind"] for e in events], ["input", "session", "progress", "result", "end",
                                                       "input", "session", "progress", "outcome", "result", "end"])
        self.assertEqual([e["text"] for e in events if e["kind"] == "input"], ["Hello.", "Use B."])
        self.assertEqual([e["cwd"] for e in events if e["kind"] == "session"], [self.proj, self.proj])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")


class Interrupt(Integration):
    def started(self) -> bool:
        try:
            return "start" in [e.get("name") for e in self.events() if e["kind"] == "progress"]
        except (OSError, ValueError):
            return False

    def test_interrupt_kills_a_hung_agent(self):
        self.scene([progress("start", "waiting")], hang=True)
        proc = self.popen()
        self.until(proc, self.started, "reported the start")
        (call,) = self.calls()
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=30)
        # Before communicate(): the fake shares drive's stderr pipe, which would wait for it to die by any path.
        with self.assertRaises(ProcessLookupError):
            os.kill(call["pid"], 0)
        out, err = proc.communicate()
        self.assertIn(proc.returncode, (-signal.SIGINT, 130), out + err)
        result, end = self.events()[-2:]
        self.assertEqual((result["kind"], result["error"]), ("result", "stopped: KeyboardInterrupt"))
        self.assertEqual((end["kind"], end["sid"], end["rc"]), ("end", SID, 1))


if __name__ == "__main__":
    unittest.main()
