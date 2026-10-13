"""fake protocol integration test behavior."""

import json
import os
import shlex
import subprocess
import sys
import time

from agent_pm.session.adapters.claude import ClaudeAdapter

from session.tests import live_tmux
from session.tests.integration.adapters.claude.fake_fixtures import (
    ARGS,
    FAKE,
    RESUME,
    SESSION,
    SID,
    Case,
    gone,
)


class BadScenario(Case):
    def test_a_bad_step_or_value_exits_1_before_the_session(self):
        tui = [
            {"kind": "hook", "event": "Stop"},
            {"kind": "dialog", "text": "x"},
            {"kind": "limit", "text": "x"},
            {"kind": "sleep", "seconds": 0},
        ]
        rows = [
            *({"steps": [step]} for step in tui),
            {"steps": "x"},
            {"steps": [1]},
            {"steps": [{"kind": "nope"}]},
            {"steps": [{"kind": "tool"}]},
            {"steps": [{"kind": "tool", "name": 7}]},
            {"steps": [{"kind": "tool", "name": "R", "x": 1}]},
            {"steps": [{"kind": "limit"}]},
            {"steps": [{"kind": "limit", "resets_at": "soon"}]},
            {"steps": [{"kind": "limit", "resets_at": True}]},
            {"steps": [{"kind": "write", "path": "a"}]},
            {"steps": [{"kind": "write", "path": 1, "text": "x"}]},
            {"steps": [{"kind": "write", "path": "", "text": "x"}]},
            {"steps": [{"kind": "write", "path": "a", "text": None}]},
            {"resume": "x"},
            {"resume": [1]},
            {"turns": "x"},
            {"turns": ["x"]},
            {"background": -1},
            {"background": True},
            {"rate_limit": []},
            {"exit": "0"},
            {"exit": True},
            {"hang": 1},
            {"log": 5},
            {"gate": 5},
        ]
        for scenario in rows:
            with self.subTest(scenario=scenario):
                self.scene(**scenario)
                res = self.run_fake(*ARGS)
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                self.assertIn("fake_claude.py: bad scenario", res.stderr)
                self.assertFalse(os.path.exists(self.config))

    def test_a_bad_interactive_step_exits_1_before_the_session(self):
        for scenario in (
            {"turns": [[1]]},
            {"turns": [["ok"], [{"kind": "nope"}]]},
            {"turns": [[{"kind": "hook"}]]},
            {"steps": [{"kind": "hook", "event": ""}]},
            {"steps": [{"kind": "hook", "event": 7}]},
            {"steps": [{"kind": ["hook"], "event": "Stop"}]},
            {"steps": [{"kind": "limit", "resets_at": None}]},
            {"steps": [{"kind": "limit", "text": 5}]},
            {"steps": [{"kind": "dialog"}]},
            {"steps": [{"kind": "dialog", "text": "x", "y": 1}]},
            {"steps": [{"kind": "sleep", "seconds": -1}]},
            {"steps": [{"kind": "sleep", "seconds": "1"}]},
            {"steps": [{"kind": "sleep", "seconds": True}]},
            {"steps": [{"kind": "sleep"}]},
            {"steps": [{"kind": "write", "path": "a", "text": "x"}]},
        ):
            with self.subTest(scenario=scenario):
                self.scene(**scenario)
                res = self.run_fake(*SESSION, "--", "hi")
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                self.assertIn("fake_claude.py: bad scenario", res.stderr)
                self.assertFalse(os.path.exists(self.config))


class PrintMode(Case):
    def test_valid_steps(self):
        self.scene(
            steps=[
                "Hi.",
                {"kind": "tool", "name": "Read"},
                {"kind": "limit", "resets_at": None},
                {"kind": "limit", "resets_at": 1.5},
            ],
            turns=[[{"kind": "dialog", "text": "x"}]],
        )
        res = self.run_fake(*ARGS, "--settings", self.settings("UserPromptSubmit", "Stop"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            [json.loads(line)["type"] for line in res.stdout.splitlines()],
            [
                "system",
                "assistant",
                "assistant",
                "rate_limit_event",
                "assistant",
                "rate_limit_event",
                "assistant",
                "result",
            ],
        )
        self.assertEqual(self.fired(), [])

    def test_a_write_step_writes_its_file_under_the_cwd_then_the_next_step_runs(self):
        self.scene(steps=[{"kind": "write", "path": "sub/out.md", "text": "# Out\n"}, "After."])
        res = self.run_fake(*ARGS)
        self.assertEqual(self.result(res), "After.")
        self.assertEqual(
            [json.loads(line)["type"] for line in res.stdout.splitlines()],
            ["system", "assistant", "result"],
        )
        with open(os.path.join(self.proj, "sub", "out.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Out\n")

    def test_resume_steps_replace_steps_only_on_a_resume(self):
        self.scene(steps=["First."], resume=["Again."])
        self.assertEqual(self.result(self.run_fake(*ARGS)), "First.")
        self.assertEqual(self.result(self.run_fake(*RESUME)), "Again.")
        self.scene(steps=["First."])
        self.assertEqual(self.result(self.run_fake(*RESUME)), "First.")


class Interactive(Case):
    def test_turns_from_stdin(self):
        self.scene(steps=["First."], turns=[["Second."]], exit=3)
        res = self.run_fake(*SESSION, "--", "hello", stdin="line1\nline2\n")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(res.stdout, "First.\nSecond.\n")
        self.assertEqual(
            self.convo(),
            [
                ("user", "hello"),
                ("assistant", "First."),
                ("user", "line1"),
                ("assistant", "Second."),
                ("user", "line2"),
            ],
        )

    def test_last_line_without_newline_is_a_line(self):
        self.scene()
        self.assertEqual(self.run_fake("hello", *SESSION, stdin="a\nb").returncode, 0)
        self.assertEqual(self.users(), ["hello", "a", "b"])

    def test_no_prompt_waits_for_a_line(self):
        self.scene(steps=["Hi."], turns=[["Again."]], exit=2)
        proc = self.popen(*SESSION)
        live_tmux.wait(self.logged, what="the fake's log line")
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.transcripts(), [])
        out, err = proc.communicate("go\nmore\n", timeout=30)
        self.assertEqual((proc.returncode, out), (2, "Hi.\nAgain.\n"), err)
        self.assertEqual(
            self.convo(),
            [("user", "go"), ("assistant", "Hi."), ("user", "more"), ("assistant", "Again.")],
        )

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

    def test_hang_and_rate_limit_are_print_mode_only(self):
        self.scene(hang=True, rate_limit={"status": "allowed"})
        res = self.run_fake("hello", *SESSION)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_output_format_is_print_mode_only(self):
        self.scene()
        res = self.run_fake(*SESSION, "--output-format", "stream-json", "--verbose", "--", "hello")
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage: claude", res.stderr)

    def test_orphaned_it_ends(self):
        self.scene()
        q = shlex.quote
        parent = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                f"exec 3<&0; {q(sys.executable)} -I {q(FAKE)} --session-id {SID} "
                "<&3 >/dev/null 2>&1 & wait",
            ],
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.PIPE,
        )
        self.addCleanup(self.reap, parent)
        (line,) = live_tmux.wait(self.logged, what="the fake's log line")
        time.sleep(0.5)
        self.assertFalse(gone(line["pid"]))
        parent.kill()
        parent.wait()
        live_tmux.wait(lambda: gone(line["pid"]), what="the orphaned fake to end")


class Gate(Case):
    def test_a_session_waits_for_its_gate_after_its_log_line(self):
        gate = os.path.join(self.root, "gate")
        self.scene(steps=["Gated."], gate=gate)
        for n, (args, users) in enumerate(((ARGS, []), (RESUME, ["hi"])), start=1):
            with self.subTest(args[2]):
                proc = self.popen(*args)
                live_tmux.wait(lambda n=n: len(self.logged()) == n, what=f"the fake's log line {n}")
                time.sleep(0.5)
                self.assertIsNone(proc.poll())
                self.assertEqual(self.users() if self.transcripts() else [], users)
                with open(gate, "w"):
                    pass
                out, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(json.loads(out.splitlines()[-1])["result"], "Gated.")
                self.assertEqual(self.users(), [*users, "hi"])
                os.remove(gate)

    def test_the_usage_probe_never_waits(self):
        gate = os.path.join(self.root, "gate")
        self.scene(steps=["OK"], gate=gate)
        self.assertEqual(
            self.result(self.run_fake(*ClaudeAdapter({}).usage_probe().argv[1:])), "OK"
        )
        self.assertFalse(os.path.exists(gate))
