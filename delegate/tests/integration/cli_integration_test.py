"""delegate/cli.py as a subprocess over session/tests/integration/adapters/claude/fake_claude.py (on
PATH as `claude`), HOME and CLAUDE_CONFIG_DIR temp: a run's exit code, stderr, out and run.jsonl;
its fix resume, a resume, a draft kept through a kill and resume, an interrupt, a usage limit,
config errors. Main's tests/integration/core/drive_integration_test.py and
resume_integration_test.py, the Delegate's part.
"""

import json
import os
import signal
import unittest
from datetime import UTC, datetime
from unittest import mock

from agent_pm.delegate.composer.prompt import RESUME
from agent_pm.delegate.engine.runner import FIX
from agent_pm.session import Driver, read_config

from delegate.tests.integration.cli import (
    ASKING,
    DONE,
    RESETS,
    RUN,
    SID,
    UNSAVED,
    UNSAVED_ERROR,
    WAIT,
    Integration,
)
from delegate.tests.replies import bare, block, mark, option, prompt, running

ANSWER = "Say hello."
DRAFT = "Draft.\n"


class NewRun(Integration):
    def test_done(self):
        res = self.drive([mark("start", "echoing"), "Echoing.", block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        for line in (
            f"delegate: session {SID}",
            "Progress (start): echoing",
            "Echoing.",
            "delegate: status done",
        ):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(
            self.kinds(), ["session", "input", "client_session", "progress", "result", "end"]
        )
        session = self.records()[0]
        self.assertEqual(
            (session["sid"], session["cwd"], session["project"], session["transcript"]),
            (SID, self.proj, False, self.transcript),
        )
        self.assertEqual(
            [*map(bare, self.records()[1:])],
            [
                {"kind": "input", "text": "Hello."},
                {"kind": "client_session", "sid": SID, "client_id": SID},
                {"kind": "progress", "name": "start", "text": "echoing"},
                {
                    "kind": "result",
                    "outcome": {
                        "status": "done",
                        "title": "echo",
                        "summary": "Hello.",
                        "questions": [],
                        "url": self.out,
                        "files": [],
                    },
                },
                {"kind": "end", "sid": SID, "rc": 0},
            ],
        )
        self.assertEqual(
            set(os.listdir(self.work)),
            {"out.md", "run.jsonl", "delegate.json"},
        )
        self.assertTrue(os.path.isfile(self.transcript))
        (call,) = self.calls()
        self.assertEqual(
            (call["cwd"], option(call, "--session-id"), option(call, "--output-format")),
            (self.proj, SID, "stream-json"),
        )
        self.assertIsNone(option(call, "--resume"))

    def test_the_default_policy_reaches_the_argv_as_before_its_move(self):
        res = self.drive([block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        argv = call["argv"]
        start = argv.index("--permission-mode")
        self.assertEqual(
            argv[start : start + 5],
            [
                "--permission-mode",
                "auto",
                "--strict-mcp-config",
                "--settings",
                '{"enabledPlugins": {"agent-pm@agent-pm": false}}',
            ],
        )
        for flag in ("--permission-mode", "--strict-mcp-config", "--settings"):
            self.assertEqual(argv.count(flag), 1, flag)

    def test_a_deliverable_file_the_agent_wrote_is_saved_to_out(self):
        report = os.path.join(self.work, "report.md")
        res = self.drive(
            [
                mark("start", "echoing"),
                {"kind": "write", "path": report, "text": "Written.\n"},
                block({**UNSAVED, "deliverable_file": report}),
            ]
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.read(self.out), "Written.\n")
        self.assertEqual(self.last("result")["outcome"]["url"], self.out)


class Outcomes(Integration):
    def test_needs_input(self):
        res = self.drive([block(ASKING)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("delegate: status needs_input", res.stderr)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])

    def test_no_outcome(self):
        res = self.drive(["Thinking."])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("delegate: the agent run returned no outcome", res.stderr)
        self.assertEqual(self.kinds()[-2:], ["result", "end"])
        self.assertEqual(self.last("result")["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(len(self.calls()), 1)

    def test_invalid_outcome_then_fixed_by_the_automatic_resume(self):
        res = self.drive([mark("start", "echoing"), block(UNSAVED)], resume=[block(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once", res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        first, second = self.calls()
        self.assertEqual(
            (
                option(first, "--session-id"),
                option(second, "--resume"),
                option(second, "--session-id"),
            ),
            (SID, SID, None),
        )
        self.assertEqual(prompt(second), FIX.format(error=UNSAVED_ERROR))
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "progress",
                "invalid",
                "session",
                "client_session",
                "result",
                "end",
            ],
        )
        self.assertEqual(self.last("invalid")["error"], UNSAVED_ERROR)
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(res.stderr.count(f"delegate: session {SID}"), 1)

    def test_invalid_outcome_twice(self):
        res = self.drive([block(UNSAVED)])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}\n", res.stderr)
        self.assertEqual(self.last("result")["error"], f"invalid outcome: {UNSAVED_ERROR}")
        self.assertEqual([option(call, "--resume") for call in self.calls()], [None, SID])
        self.assertFalse(os.path.exists(self.out))

    def test_nonzero_exit_preserves_report_for_recovery(self):
        res = self.drive([mark("start", "echoing"), block(DONE)], exit=7)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("delegate: status done", res.stderr)
        self.assertEqual(self.read(self.out), DONE["deliverable"])
        saved = self.last("result")
        self.assertEqual(
            (
                saved["outcome"]["status"],
                saved["outcome"]["url"],
                saved["error"],
                self.last("end")["rc"],
            ),
            ("done", self.out, "the client exited 7", 7),
        )
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.kinds()[-2:], ["result", "end"])

    def test_nonzero_exit_without_valid_report_still_fails(self):
        for steps in (["Thinking."], [block({**DONE, "status": "maybe"})]):
            with self.subTest(steps=steps):
                before = len(self.calls())
                res = self.drive(steps, exit=7)
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn("delegate: the client exited 7", res.stderr)
                self.assertEqual(
                    (self.last("result")["error"], self.last("end")["rc"]),
                    ("the client exited 7", 7),
                )
                self.assertNotIn("outcome", self.last("result"))
                self.assertFalse(os.path.exists(self.out))
                self.assertEqual(len(self.calls()), before + 1)


class Stderr(Integration):
    def test_the_clients_stderr_lines_land_in_the_record(self):
        res = self.drive([mark("start", "first")])
        self.assertEqual(res.returncode, 1, res.stderr)
        os.unlink(self.transcript)
        res = self.drive([block(DONE)], "--resume")
        self.assertEqual(res.returncode, 1, res.stderr)
        said = f"No conversation found with session ID: {SID}"
        self.assertIn(said, res.stderr)
        self.assertEqual([e["text"] for e in self.records() if e["kind"] == "stderr"], [said])
        self.assertEqual(self.kinds()[-3:], ["stderr", "result", "end"])
        self.assertEqual(
            (self.last("result")["error"], self.last("end")["rc"]), ("the client exited 1", 1)
        )


class Resume(Integration):
    def test_resume_reuses_the_recorded_cwd_and_session(self):
        res = self.drive([mark("start", "first")])
        self.assertEqual(res.returncode, 1, res.stderr)
        res = self.drive(
            [mark("round", "second"), block(DONE)], "--resume", "--input", "Use B.", cwd=self.root
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        first, second = self.calls()
        self.assertEqual(second["cwd"], self.proj)
        self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (SID, None))
        self.assertTrue(prompt(second).startswith(RESUME))
        self.assertTrue(prompt(second).endswith("\n# Input\n\nUse B.\n"))
        self.assertEqual(self.users(), [prompt(first), prompt(second)])
        self.assertEqual(
            self.kinds(), ["session", "input", "client_session", "progress", "result", "end"] * 2
        )
        records = self.records()
        self.assertEqual([e["text"] for e in records if e["kind"] == "input"], ["Hello.", "Use B."])
        self.assertEqual(
            [e["cwd"] for e in records if e["kind"] == "session"], [self.proj, self.proj]
        )
        self.assertEqual(self.last("result")["outcome"]["status"], "done")


class NeedsInput(Integration):
    def test_the_answer_resumes_the_session(self):
        res = self.drive([mark("start", "echoing"), block(ASKING)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("delegate: status needs_input", res.stderr)
        self.assertFalse(os.path.exists(self.out), "a deliverable before the answer")
        res = self.drive([block(DONE)], "--resume", "--input", ANSWER)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(os.path.join(self.work, "delegate.json")) as f:
            self.assertEqual(json.load(f)["attempt"], 2)
        records = self.records()
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "progress",
                "result",
                "end",
                "session",
                "input",
                "client_session",
                "result",
                "end",
            ],
        )
        self.assertEqual([e["text"] for e in records if e["kind"] == "input"], ["Hello.", ANSWER])
        self.assertEqual(
            [(e["sid"], e["cwd"]) for e in records if e["kind"] == "session"],
            [(SID, self.proj)] * 2,
        )
        first, second = self.calls()
        self.assertEqual(
            (
                option(first, "--session-id"),
                option(second, "--resume"),
                option(second, "--session-id"),
            ),
            (SID, SID, None),
        )
        self.assertTrue(prompt(second).startswith(RESUME), "the resume prompt")
        self.assertTrue(
            prompt(second).endswith(f"\n# Input\n\n{ANSWER}\n"), "the answer as the input"
        )
        self.assertEqual(self.users(), [prompt(first), prompt(second)], "one session's transcript")
        self.assertEqual(
            [*map(bare, records[-2:])],
            [
                {
                    "kind": "result",
                    "outcome": {
                        "status": "done",
                        "title": "echo",
                        "summary": "Hello.",
                        "questions": [],
                        "url": self.out,
                        "files": [],
                    },
                },
                {"kind": "end", "sid": SID, "rc": 0},
            ],
        )
        self.assertEqual(self.read(self.out), DONE["deliverable"])


class RolePicks(Integration):
    ROLE = "researcher"
    PICK = mark("start", "light-research: a quick, good-enough answer will do")
    REPORT = {
        "status": "done",
        "title": "Greetings",
        "summary": "web",
        "deliverable": "# Greetings\n",
    }

    def test_the_role_picks_and_a_resume_keeps_the_session(self):
        picked = {
            "kind": "progress",
            "name": "start",
            "text": "light-research: a quick, good-enough answer will do",
        }
        res = self.drive([self.PICK])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(f"Progress (start): {picked['text']}", res.stderr)
        self.assertIn(
            "**Your task**: pick it from your charter's Tasks section", prompt(self.calls()[0])
        )
        self.assertEqual([bare(e) for e in self.records() if e["kind"] == "progress"], [picked])
        res = self.drive([block(self.REPORT)], "--resume")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("missing progress mark", res.stderr)
        self.assertNotIn("missing", self.kinds())
        first, second = self.calls()
        self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (SID, None))
        self.assertTrue(prompt(second).startswith(RESUME), "the resume prompt")
        self.assertEqual([bare(e) for e in self.records() if e["kind"] == "progress"], [picked])
        self.assertEqual([e["sid"] for e in self.records() if e["kind"] == "session"], [SID, SID])
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(self.read(self.out), self.REPORT["deliverable"])


class UsageLimit(Integration):
    def test_a_limit_ends_the_run_and_its_session_stays_resumable(self):
        res = self.drive(
            [mark("start", "echoing"), {"kind": "limit", "resets_at": RESETS}, block(DONE)]
        )
        self.assertEqual(res.returncode, 1, res.stderr)
        resets = datetime.fromtimestamp(RESETS, UTC).isoformat(timespec="seconds")
        self.assertIn(f"delegate: usage limit; resets at {resets}\n", res.stderr)
        self.assertNotIn("status done", res.stderr)
        self.assertEqual(
            self.kinds(),
            ["session", "input", "client_session", "progress", "limit", "result", "end"],
        )
        self.assertEqual(self.last("limit")["resets_at"], RESETS)
        self.assertEqual(
            [bare(self.last("result"))],
            [{"kind": "result", "error": f"usage limit; resets at {resets}"}],
        )
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(len(self.calls()), 1)
        self.driver = Driver.from_config(read_config())
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.config}):
            self.assertEqual(
                self.driver.session_status("claude", SID, self.proj, workdir=self.work).resumable,
                "yes",
            )


class ConfigErrors(Integration):
    def test_a_config_error_exits_2_and_starts_nothing(self):
        cli = list(RUN)
        cases = {
            "unknown role": (
                [
                    *cli,
                    "--role",
                    "nobody",
                    "--input",
                    "x",
                    "--out",
                    self.out,
                    "--workdir",
                    self.work,
                ],
                "'nobody'",
            ),
            "task off the index": (self.argv("--task", "nope"), "task 'nope' is not one of"),
            "missing options": (
                [*cli, "--role", self.ROLE, "--input", "x", "--workdir", self.work],
                "--input, --out and --workdir are required",
            ),
            "a client without in_process": (
                self.argv("--client", "codex", "--mode", "in_process"),
                "client 'codex' does not support in_process mode",
            ),
        }
        for name, (argv, said) in cases.items():
            with self.subTest(name):
                res = self.drive([block(DONE)], argv=argv)
                self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
                self.assertTrue(res.stderr.startswith("delegate: "), res.stderr)
                self.assertIn(said, res.stderr)
                self.assertEqual(self.calls(), [])
                self.assertEqual(os.listdir(self.work), [])


class Draft(Integration):
    def holds(self, text: str) -> bool:
        try:
            return self.read(self.out) == text
        except OSError:
            return False

    def inputs(self) -> int:
        try:
            return self.kinds().count("input")
        except ValueError:  # a line half appended
            return 0

    def test_a_resume_keeps_the_draft_until_the_deliverable_replaces_it(self):
        self.scene(
            [mark("start", "echoing"), {"kind": "write", "path": self.out, "text": DRAFT}],
            hang=True,
        )
        proc = self.popen()
        self.until(proc, lambda: self.holds(DRAFT), "saw the draft at --out")
        (call,) = self.calls()
        os.kill(call["pid"], signal.SIGKILL)
        out, err = proc.communicate(timeout=WAIT)
        self.assertEqual(proc.returncode, 1, out + err)
        self.assertEqual(self.last("result")["error"], "the client exited -9")
        self.assertTrue(self.holds(DRAFT), "the draft after the kill")
        gate = os.path.join(self.root, "gate")
        self.scene([block(DONE)], gate=gate)
        proc = self.popen("--resume")
        self.until(proc, lambda: self.inputs() == 2, "recorded the resume's input")
        self.assertTrue(self.holds(DRAFT), "the draft as the resumed client starts")
        open(gate, "w").close()
        out, err = proc.communicate(timeout=WAIT)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertEqual(option(self.calls()[1], "--resume"), SID)
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(self.read(self.out), DONE["deliverable"])


class Interrupt(Integration):
    def started(self) -> bool:
        try:
            return "start" in [e.get("name") for e in self.records() if e["kind"] == "progress"]
        except (OSError, ValueError):
            return False

    def test_interrupt_kills_a_hung_agent(self):
        self.scene([mark("start", "waiting")], hang=True)
        proc = self.popen()
        self.until(proc, self.started, "reported the start")
        (call,) = self.calls()
        os.kill(proc.pid, signal.SIGINT)
        proc.wait(timeout=WAIT)
        # Before communicate(): the fake shares the CLI's stderr pipe, which would wait for it to
        # die by any path.
        self.assertFalse(running(call["pid"]))
        out, err = proc.communicate()
        self.assertIn(proc.returncode, (-signal.SIGINT, 130), out + err)
        result, end = map(bare, self.records()[-2:])
        self.assertEqual(result, {"kind": "result", "error": "stopped: KeyboardInterrupt"})
        self.assertEqual(end, {"kind": "end", "sid": SID, "rc": 1})


if __name__ == "__main__":
    unittest.main()
