"""drive.py resumes, run as a process as in drive_integration_test: an answer to needs_input, a run whose role picked its
task, a draft left at --out. A failure names its scenario step."""
import contextlib
import os
import signal
import sys
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(TESTS), TESTS]
import hermetic  # noqa: E402
import compose  # noqa: E402
from drive_integration_test import DONE, Integration, prompt  # noqa: E402
from drive_test import SID, outcome, progress  # noqa: E402

ASKING = {"status": "needs_input", "title": "echo", "summary": "Asking.", "questions": ["Which greeting?"]}
ANSWER = "Say hello."
DRAFT = "Draft.\n"


@contextlib.contextmanager
def step(n: int, name: str):
    """Names step `n` in a failure inside the block: `step <n> (<name>): ` before its message; any other error gets
    it as a note."""
    try:
        yield
    except AssertionError as e:
        e.args = (f"step {n} ({name}): {e}",)
        raise
    except Exception as e:
        e.add_note(f"step {n} ({name})")
        raise


def option(call: dict, name: str) -> str | None:
    """The value after `name` in a fake invocation's argv; None without `name`."""
    argv = call["argv"]
    return argv[argv.index(name) + 1] if name in argv else None


def bare(events: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "ts"} for e in events]


class NeedsInput(Integration):
    def test_the_answer_resumes_the_session(self):
        with step(1, "ask"):
            res = self.drive([progress("start", "echoing"), outcome(ASKING)])
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("status needs_input", res.stderr)
            self.assertEqual(self.last("result")["outcome"]["questions"], ASKING["questions"])
            self.assertFalse(os.path.exists(self.out), "a deliverable before the answer")
        with step(2, "resume with the answer"):
            res = self.drive([outcome(DONE)], "--resume", "--input", ANSWER)
            self.assertEqual(res.returncode, 0, res.stderr)
            events = self.events()
            self.assertEqual([e["kind"] for e in events], ["input", "session", "progress", "outcome", "result", "end",
                                                           "input", "session", "outcome", "result", "end"])
            self.assertEqual([e["text"] for e in events if e["kind"] == "input"], ["Hello.", ANSWER])
            self.assertEqual([(e["sid"], e["cwd"]) for e in events if e["kind"] == "session"], [(SID, self.proj)] * 2)
            first, second = self.calls()
            self.assertEqual((option(first, "--session-id"), option(second, "--resume"),
                              option(second, "--session-id")), (SID, SID, None))
            self.assertTrue(prompt(second).startswith(compose.RESUME), "the resume prompt")
            self.assertTrue(prompt(second).endswith(f"\n# Input\n\n{ANSWER}\n"), "the answer as the input")
            users = [line["message"]["content"] for line in self.transcribed() if line["type"] == "user"]
            self.assertEqual(users, [prompt(first), prompt(second)], "one session's transcript")
            self.assertEqual(bare(events[-2:]), [
                {"kind": "result", "outcome": {"status": "done", "title": "echo", "summary": "Hello.", "questions": [],
                                               "url": self.out, "files": []}},
                {"kind": "end", "sid": SID, "rc": 0}])
            with open(self.out) as f:
                self.assertEqual(f.read(), DONE["deliverable"])


class RolePicks(Integration):
    ROLE = "researcher"
    PICK = progress("start", "light-research: a quick, good-enough answer will do")
    REPORT = {"status": "done", "title": "Greetings", "summary": "web", "deliverable": "# Greetings\n"}

    def setUp(self):
        super().setUp()
        self.tasks = compose.index(hermetic.CORE, self.ROLE)

    def picks(self, text: str) -> None:
        """`text`'s guide has the run pick its task, naming none."""
        guide = text.split("\n# Principles\n", 1)[0]
        self.assertIn("**Your task**: pick it from your charter's Tasks section", guide)
        self.assertEqual([t for t in self.tasks if f"`{t}`" in guide], [], "tasks the guide names")

    def test_the_role_picks_and_a_resume_keeps_the_session(self):
        with open(os.path.join(hermetic.CORE, compose.TEXT, "roles", f"{self.ROLE}.md")) as f:
            index = compose.INDEX.search(f.read()).group(0).strip()
        with step(1, "pick"):
            res = self.drive([self.PICK])
            self.assertEqual(res.returncode, 1, res.stderr)
            (call,) = self.calls()
            text = prompt(call)
            self.assertIn(index, text, "the task index")
            for t in self.tasks:
                with open(os.path.join(hermetic.CORE, compose.TEXT, "tasks", f"{t}.md")) as f:
                    for line in f:
                        if len(line.strip()) >= 30 and not line.startswith("#"):
                            self.assertNotIn(line.strip(), text, f"a line of {t}'s steps")
            self.picks(text)
            self.assertIn(f"Progress (start): {self.PICK['text']}", res.stderr)
            self.assertEqual(bare([e for e in self.events() if e["kind"] == "progress"]), [self.PICK])
        with step(2, "resume"):
            res = self.drive([outcome(self.REPORT)], "--resume")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("missing progress mark", res.stderr)
            first, second = self.calls()
            self.assertEqual((option(second, "--resume"), option(second, "--session-id")), (SID, None))
            self.assertTrue(prompt(second).startswith(compose.RESUME), "the resume prompt")
            self.picks(prompt(second))
            events = self.events()
            self.assertEqual(bare([e for e in events if e["kind"] == "progress"]), [self.PICK])
            self.assertEqual([e["sid"] for e in events if e["kind"] == "session"], [SID, SID])
            self.assertEqual(self.last("result")["outcome"]["status"], "done")
            with open(self.out) as f:
                self.assertEqual(f.read(), self.REPORT["deliverable"])


class Draft(Integration):
    def drafted(self) -> bool:
        try:
            with open(self.out) as f:
                return f.read() == DRAFT
        except OSError:
            return False

    def test_a_resume_keeps_the_draft_until_the_deliverable_replaces_it(self):
        with step(1, "draft, hang"):
            self.scene([progress("start", "echoing"), {"kind": "write", "path": self.out, "text": DRAFT}], hang=True)
            proc = self.popen()
            self.until(proc, self.drafted, "saw the draft at --out")
        with step(2, "kill"):
            (call,) = self.calls()
            os.kill(call["pid"], signal.SIGKILL)
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 3, out + err)
            self.assertEqual(self.last("result")["error"], "the client exited -9")
            self.assertTrue(self.drafted(), "the draft after the kill")
        with step(3, "resume"):
            res = self.drive([{"kind": "read", "path": self.out}, outcome(DONE)], "--resume")
            self.assertEqual(res.returncode, 0, res.stderr)
            lines = self.transcribed()
            resumed = max(i for i, line in enumerate(lines) if line["type"] == "user")
            said = [line["message"]["content"][0]["text"] for line in lines[resumed + 1:]]
            self.assertEqual(said, [DRAFT], "--out as the resumed run read it first")
            self.assertEqual(self.last("result")["outcome"]["status"], "done")
            with open(self.out) as f:
                self.assertEqual(f.read(), DONE["deliverable"])


if __name__ == "__main__":
    unittest.main()
