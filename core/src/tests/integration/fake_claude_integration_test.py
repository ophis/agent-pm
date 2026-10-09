"""fake_claude.py run as a process, no tmux: interactive turns, hooks, print mode (its docstring: the format)."""
import glob
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

import fake_claude

FAKE = os.path.abspath(fake_claude.__file__)
SID = "11111111-2222-4333-8444-555555555555"
SESSION = ["--session-id", SID]
BASE = {"session_id", "transcript_path", "cwd", "hook_event_name"}


def wait(check, timeout: float = 10):
    """check()'s first truthy value; fails once `timeout` s pass."""
    until = time.monotonic() + timeout
    while not (value := check()):
        if time.monotonic() > until:
            raise AssertionError("timed out waiting")
        time.sleep(0.02)
    return value


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home, self.proj, self.bin = (os.path.join(self.root, d) for d in ("home", "proj", "bin"))
        for d in (self.home, self.proj, self.bin):
            os.mkdir(d)
        self.scenario, self.hooklog, self.log = (os.path.join(self.root, f) for f in ("scenario.json", "hooks.jsonl",
                                                                                     "log.jsonl"))
        self.env = {"PATH": fake_claude.install(self.bin), "HOME": self.home, "PYTHONUTF8": "1",
                    fake_claude.ENV: self.scenario}

    def scene(self, **scenario) -> None:
        with open(self.scenario, "w") as f:
            json.dump({"log": self.log, **scenario}, f)

    def settings(self, **entries) -> str:
        """--settings JSON: per event the given entries, else one command hook appending its stdin JSON to hooklog."""
        record = f"cat >> {shlex.quote(self.hooklog)}; echo >> {shlex.quote(self.hooklog)}"
        return json.dumps({"hooks": {e: v if v else [{"hooks": [{"type": "command", "command": record}]}]
                                     for e, v in entries.items()}})

    def run_fake(self, *args, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, FAKE, *args], cwd=self.proj, env=self.env, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def popen(self, *args) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, FAKE, *args], cwd=self.proj, env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    def transcripts(self) -> list[str]:
        return glob.glob(os.path.join(self.home, ".claude", "projects", "*", f"{SID}.jsonl"))

    def convo(self) -> list[tuple[str, str]]:
        """The transcript as (role, text) pairs."""
        (path,) = self.transcripts()
        with open(path) as f:
            lines = [json.loads(line) for line in f]
        return [(m["type"], m["message"]["content"] if m["type"] == "user" else m["message"]["content"][0]["text"])
                for m in lines]

    def fired(self) -> list[dict]:
        """The stdin JSON of each hook run so far, in order."""
        try:
            with open(self.hooklog) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def users(self) -> list[str]:
        return [text for role, text in self.convo() if role == "user"]


class Interactive(Case):
    def test_turns_from_stdin(self):
        self.scene(steps=["First."], turns=[["Second."]], exit=3)
        res = self.run_fake("hello", *SESSION, stdin="line1\nline2\n")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(res.stdout, "First.\nSecond.\n")
        self.assertEqual(self.convo(), [("user", "hello"), ("assistant", "First."), ("user", "line1"),
                                        ("assistant", "Second."), ("user", "line2")])

    def test_last_line_without_newline_is_a_line(self):
        self.scene()
        self.assertEqual(self.run_fake("hello", *SESSION, stdin="a\nb").returncode, 0)
        self.assertEqual(self.users(), ["hello", "a", "b"])

    def test_no_prompt_waits_for_a_line(self):
        self.scene(steps=["Hi."], turns=[["Again."]], exit=2)
        proc = self.popen(*SESSION)
        wait(lambda: os.path.isfile(self.log))
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.transcripts(), [])
        out, err = proc.communicate("go\nmore\n", timeout=30)
        self.assertEqual((proc.returncode, out), (2, "Hi.\nAgain.\n"), err)
        self.assertEqual(self.convo(), [("user", "go"), ("assistant", "Hi."), ("user", "more"),
                                        ("assistant", "Again.")])

    def test_eof_before_any_turn(self):
        self.scene(exit=3)
        res = self.run_fake(*SESSION)
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(self.transcripts(), [])

    def test_resume_needs_the_transcript(self):
        self.scene()
        res = self.run_fake("--resume", SID)
        self.assertEqual(res.returncode, 1)
        self.assertIn("No conversation found with session ID: " + SID, res.stderr)

    def test_resume_without_a_prompt_appends(self):
        self.scene()
        self.assertEqual(self.run_fake("hello", *SESSION).returncode, 0)
        res = self.run_fake("--resume", SID, stdin="again\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.users(), ["hello", "again"])

    def test_output_is_plain_even_for_stream_json(self):
        self.scene(steps=["Hi."])
        res = self.run_fake("hello", *SESSION, "--output-format", "stream-json", "--verbose")
        self.assertEqual((res.returncode, res.stdout), (0, "Hi.\n"), res.stderr)

    def test_hang_is_ignored(self):
        self.scene(hang=True)
        self.assertEqual(self.run_fake("hello", *SESSION).returncode, 0)


class Reports(Case):
    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.root, "report.py")
        self.calls = os.path.join(self.root, "calls.jsonl")
        with open(self.stub, "w") as f:
            f.write(f"import json, sys\nwith open({self.calls!r}, 'a') as f:\n"
                    f"    f.write(json.dumps(sys.argv[1:]) + '\\n')\n")

    def test_command_from_the_first_prompt_naming_one(self):
        self.scene(turns=[[{"kind": "progress", "name": "n", "text": "t"}]] * 2)
        res = self.run_fake("no command here", *SESSION, stdin=f"`python3 {self.stub} --to one`\n"
                            f"`python3 {self.stub} --to two`\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.calls) as f:
            self.assertEqual([json.loads(line) for line in f], [["--to", "one", "progress", "n", "t"]] * 2)

    def test_report_step_without_a_command_fails(self):
        self.scene(turns=[[{"kind": "progress", "name": "n", "text": "t"}]])
        res = self.run_fake("no command here", *SESSION, stdin="nor here\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("report command", res.stderr)


class Hooks(Case):
    def test_payloads_and_order(self):
        self.scene(steps=[{"kind": "hook", "event": "Mark"}], turns=[["Next."]])
        res = self.run_fake("hi", *SESSION, "--settings", self.settings(UserPromptSubmit=[], Mark=[], Stop=[]),
                            stdin="next\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        fired = self.fired()
        self.assertEqual([h["hook_event_name"] for h in fired], ["UserPromptSubmit", "Mark", "Stop",
                                                                 "UserPromptSubmit", "Stop"])
        (path,) = self.transcripts()
        for h in fired:
            self.assertEqual({k: h[k] for k in BASE - {"hook_event_name"}},
                             {"session_id": SID, "transcript_path": path, "cwd": self.proj})
        submit, mark, stop = fired[:3]
        self.assertEqual(set(mark), BASE)
        self.assertEqual((submit["prompt"], fired[3]["prompt"]), ("hi", "next"))
        self.assertEqual(set(submit), BASE | {"prompt"})
        self.assertIs(stop["stop_hook_active"], False)
        self.assertEqual(set(stop), BASE | {"stop_hook_active"})

    def test_matchers_and_malformed_entries(self):
        def tag(name: str) -> dict:
            return {"type": "command", "command": f"echo {name} >> {shlex.quote(self.hooklog)}"}

        entries = [{"matcher": "^x$", "hooks": [tag("skipped")]}, {"matcher": None, "hooks": [tag("null")]}, "text",
                   {"matcher": "*"}, {"hooks": "no list"},
                   {"hooks": ["text", {"type": "command"}, {**tag("http"), "type": "http"},
                              {**tag("five"), "command": 5}]},
                   {"matcher": "*", "hooks": [tag("star")]}, {"matcher": "", "hooks": [tag("empty")]},
                   {"hooks": [tag("absent")]}, {"hooks": [tag("a"), tag("b")]}]
        self.scene()
        settings = json.dumps({"hooks": {"Stop": entries, "UserPromptSubmit": "not a list"}})
        res = self.run_fake("hi", *SESSION, "--settings", settings)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.hooklog) as f:
            self.assertEqual(f.read().split(), ["star", "empty", "absent", "a", "b"])

    def test_failing_hooks_do_not_stop_the_fake(self):
        marker = shlex.quote(self.hooklog)
        commands = ["exit 1", "cat >/dev/null; kill -9 $$", "no-such-command-anywhere", f"echo after >> {marker}"]
        settings = json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c}
                                                             for c in commands]}]}})
        self.scene(steps=["Hi."])
        res = self.run_fake("hi", *SESSION, "--settings", settings, stdin="again\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.hooklog) as f:
            self.assertEqual(f.read().split(), ["after", "after"])
        self.assertEqual(self.users(), ["hi", "again"])

    def test_cwd_env_stdout_and_stderr(self):
        command = 'echo "$PWD $MARK" >> ' + shlex.quote(self.hooklog) + "; echo to-stdout; echo to-stderr >&2"
        self.env["MARK"] = "m"
        self.scene()
        res = self.run_fake("hi", *SESSION, "--settings", json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}))
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(self.hooklog) as f:
            self.assertEqual(f.read(), f"{self.proj} m\n")
        self.assertNotIn("to-stdout", res.stdout)
        self.assertIn("to-stderr", res.stderr)

    def test_settings_without_usable_hooks(self):
        self.scene(steps=[{"kind": "hook", "event": "Stop"}])
        for settings in ('{"hooks": "x"}', "{}"):
            res = self.run_fake("hi", *SESSION, "--settings", settings)
            self.assertEqual(res.returncode, 0, res.stderr)


class PrintMode(Case):
    ARGS = ["-p", "hi", *SESSION, "--output-format", "stream-json", "--verbose"]

    def test_stop_runs_once_before_the_result(self):
        self.scene(steps=["Hi.", {"kind": "hook", "event": "Mark"}], turns=[["Unused."]])
        record = f"cat >> {shlex.quote(self.hooklog)}; echo >> {shlex.quote(self.hooklog)}"
        settings = json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": record}]}],
            "Mark": [{"hooks": [{"type": "command", "command": record}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "sleep 0.3; " + record}]}]}})
        proc = self.popen(*self.ARGS, "--settings", settings)
        proc.stdin.close()
        seen = []
        for line in proc.stdout:
            seen.append(json.loads(line))
            if seen[-1]["type"] == "result":
                self.assertEqual([h["hook_event_name"] for h in self.fired()], ["UserPromptSubmit", "Mark", "Stop"])
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual([m["type"] for m in seen], ["system", "assistant", "result"])
        self.assertEqual([h["hook_event_name"] for h in self.fired()], ["UserPromptSubmit", "Mark", "Stop"])

    def test_hang_runs_no_stop(self):
        self.scene(steps=["Hi.", {"kind": "hook", "event": "Mark"}], hang=True)
        settings = self.settings(Mark=[], Stop=[])
        proc = self.popen(*self.ARGS, "--settings", settings)
        wait(lambda: self.fired())
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual([h["hook_event_name"] for h in self.fired()], ["Mark"])


class Gate(Case):
    def setUp(self):
        super().setUp()
        self.gate = os.path.join(self.root, "gate")

    def parked(self, *args) -> subprocess.Popen:
        """A fake started with the scenario, once it has logged and had time to run a turn."""
        proc = self.popen(*args)
        wait(lambda: os.path.isfile(self.log))
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.transcripts(), [])
        return proc

    def open_gate(self) -> None:
        open(self.gate, "w").close()

    def test_interactive_parks_until_the_file_appears(self):
        self.scene(steps=["Hi."], gate=self.gate)
        proc = self.parked("hello", *SESSION)
        self.open_gate()
        out, err = proc.communicate("", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Hi.\n"), err)
        self.assertEqual(self.convo(), [("user", "hello"), ("assistant", "Hi.")])

    def test_print_mode_parks_before_anything_is_said(self):
        self.scene(steps=["Hi."], gate=self.gate)
        proc = self.parked("-p", "hello", *SESSION, "--output-format", "stream-json", "--verbose")
        self.open_gate()
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual([json.loads(line)["type"] for line in out.splitlines()], ["system", "assistant", "result"])

    def test_no_hook_or_report_before_the_gate(self):
        stub, calls = os.path.join(self.root, "report.py"), os.path.join(self.root, "calls.jsonl")
        with open(stub, "w") as f:
            f.write(f"open({calls!r}, 'a').close()\n")
        self.scene(steps=[{"kind": "progress", "name": "n", "text": "t"}], gate=self.gate)
        proc = self.parked(f"`python3 {stub} --to x`", *SESSION, "--settings", self.settings(UserPromptSubmit=[]))
        self.assertEqual((self.fired(), os.path.exists(calls)), ([], False))
        self.open_gate()
        proc.communicate("", timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(self.fired()), 1)
        self.assertTrue(os.path.exists(calls))

    def test_an_existing_file_does_not_hold(self):
        self.open_gate()
        self.scene(gate=self.gate)
        self.assertEqual(self.run_fake("hello", *SESSION).returncode, 0)
        self.assertEqual(self.users(), ["hello"])

    def test_no_gate_or_a_null_one_does_not_hold(self):
        for extra in ({}, {"gate": None}):
            with self.subTest(extra=extra):
                self.scene(**extra)
                self.assertEqual(self.run_fake("hello", *SESSION).returncode, 0)

    def test_a_resume_without_its_transcript_fails_without_waiting(self):
        self.scene(gate=self.gate)
        res = self.run_fake("--resume", SID)
        self.assertEqual(res.returncode, 1)
        self.assertIn("No conversation found", res.stderr)

    def test_orphaned_at_the_gate_exits_1(self):
        err = os.path.join(self.root, "err.txt")
        self.scene(gate=self.gate)
        q = shlex.quote
        parent = subprocess.Popen(["sh", "-c", f"{q(sys.executable)} {q(FAKE)} hello --session-id {SID} "
                                               f"</dev/null >/dev/null 2>{q(err)} & wait"], cwd=self.proj, env=self.env)
        self.addCleanup(lambda: (parent.kill(), parent.wait()))
        wait(lambda: os.path.isfile(self.log))
        parent.kill()

        def said():
            with open(err) as f:
                return f.read()

        self.assertIn("fake_claude.py: gate", wait(said))
        self.assertEqual(self.transcripts(), [])


class BadScenario(Case):
    def test_bad_turns_and_hook_steps_exit_1(self):
        hook = {"kind": "hook", "event": "Stop"}
        for scenario in ({"turns": "x"}, {"turns": ["x"]}, {"turns": [[1]]}, {"turns": [["ok"], [{"kind": "nope"}]]},
                         {"turns": [[{"kind": "hook"}]]}, {"steps": [{**hook, "extra": 1}]},
                         {"steps": [{"kind": "hook", "event": ""}]}, {"steps": [{"kind": "hook", "event": 7}]},
                         {"steps": [{"kind": ["hook"], "event": "Stop"}]}):
            with self.subTest(scenario=scenario):
                self.scene(**scenario)
                res = self.run_fake("hi", *SESSION)
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn("fake_claude.py:", res.stderr)
                self.assertNotIn("unknown scenario key", res.stderr)
                self.assertEqual(self.transcripts(), [])

    def test_a_gate_that_is_no_string_exits_1(self):
        for gate in (5, True, ["x"], {}):
            with self.subTest(gate=gate):
                self.scene(gate=gate)
                res = self.run_fake("hi", *SESSION)
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn("fake_claude.py: gate", res.stderr)
                self.assertEqual(self.transcripts(), [])

    def test_valid_hook_steps(self):
        self.scene(steps=[{"kind": "hook", "event": "Stop"}], turns=[[], [{"kind": "hook", "event": "Mark"}]])
        self.assertEqual(self.run_fake("hi", *SESSION).returncode, 0)


if __name__ == "__main__":
    unittest.main()
