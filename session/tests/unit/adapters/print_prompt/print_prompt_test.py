"""agent_pm/session/adapters/print_prompt/: PrintPromptAdapter's factory and keys;
PrintPromptInProcess's check, its run's replies and the prompt file it writes, with every
process spawn patched to fail."""

import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from agent_pm.session.adapters.print_prompt import PrintPromptAdapter
from agent_pm.session.adapters.print_prompt.in_process import PrintPromptInProcess
from agent_pm.session.engine.types import ConfigError, Reply, SessionStatus, Spec, TurnEnd
from agent_pm.utils import fenced

from session.tests.case import SPAWNS, Case

PROGRESS, OUTCOME = "agent-pm-progress", "agent-pm-outcome"
START = "[agent-pm-progress:start] the composed prompt is handed to the caller"
SUMMARY = "The prompt is composed and handed to the caller, who runs the task."
PROMPT = (
    "# Task\n[agent-pm-progress:start] the step's own mark\n"
    '```agent-pm-outcome\n{"status": "failed"}\n```\n'
    "naïve – 中文 ✓\r\n  end"
)


def outcome_block(text: str) -> str:
    return next(body for info, body in fenced(text) if info == OUTCOME)


class Factory(Case):
    def test_in_process_only_a_fresh_run_each_call(self):
        adapter = PrintPromptAdapter({})
        self.assertEqual((adapter.headless(), adapter.tui()), (None, None))
        first, second = adapter.in_process(), adapter.in_process()
        self.assertIsInstance(first, PrintPromptInProcess)
        self.assertIsNot(first, second)
        self.assertIs(first.adapter, adapter)

    def test_no_keys_no_runtime_note_and_the_base_defaults(self):
        adapter = PrintPromptAdapter({})
        self.assertEqual((adapter.keys, adapter.runtime_note), (frozenset(), ""))
        self.assertIsNone(adapter.usage_probe())
        self.assertEqual(adapter.session_status("s", "/w"), SessionStatus("unknown", None, ""))

    def test_a_destination_key_is_a_config_error(self):
        for key in ("roles", "output"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as caught:
                    PrintPromptAdapter({key: {}})
                self.assertEqual(
                    str(caught.exception), f"unknown key {key!r} in PrintPromptAdapter's config"
                )


class Run(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workdir = os.path.join(tmp.name, "work")
        self.spec = Spec(workdir=self.workdir, reply_tags=(PROGRESS, OUTCOME))
        self.session = PrintPromptAdapter({}).in_process()

    def ran(self, prompt=PROMPT) -> list:
        """The run's events, each of SPAWNS failing meanwhile and never called; cwd and environ
        unchanged."""
        cwd, environ = os.getcwd(), dict(os.environ)
        with contextlib.ExitStack() as stack:
            spawns = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            found = list(self.session.run(prompt, self.spec, False))
        for spawn in spawns:
            spawn.assert_not_called()
        self.assertEqual((os.getcwd(), dict(os.environ)), (cwd, environ))
        return found

    def written(self, found) -> bytes:
        name = json.loads(outcome_block(found[1].text))["deliverable_file"]
        with open(os.path.join(self.workdir, name), "rb") as f:
            return f.read()

    def test_check_refuses_resume_only(self):
        self.assertIsNone(self.session.check(PROMPT, self.spec, False))
        with self.assertRaises(ConfigError) as caught:
            self.session.check(PROMPT, self.spec, True)
        self.assertEqual(str(caught.exception), "print_prompt has no session to resume")

    def test_check_refuses_a_spec_without_the_reply_protocol(self):
        with self.assertRaises(ConfigError) as caught:
            self.session.check(PROMPT, Spec(workdir=self.workdir), False)
        self.assertEqual(
            str(caught.exception),
            "print_prompt needs the caller's reply protocol (Spec.reply_tags)",
        )

    def test_the_replies_use_the_tags_the_spec_names(self):
        self.spec = Spec(workdir=self.workdir, reply_tags=("p-x", "o-x"))
        found = self.ran()
        self.assertEqual(found[0], Reply("[p-x:start] the composed prompt is handed to the caller"))
        self.assertTrue(found[1].text.startswith("```o-x\n"), found[1].text)

    def test_replies_the_start_mark_then_the_outcome_block_naming_the_new_file(self):
        found = self.ran()
        (name,) = os.listdir(os.path.join(self.workdir, "tmp"))
        self.assertRegex(name, r"\Aprompt-[^/]+\.md\Z")
        data = {"status": "done", "title": "Composed prompt", "summary": SUMMARY}
        block = "```agent-pm-outcome\n" + json.dumps(data | {"deliverable_file": f"tmp/{name}"})
        block += "\n```"
        self.assertEqual(found, [Reply(START), Reply(block), TurnEnd(block, 0)])
        self.assertEqual(
            json.loads(outcome_block(found[2].reply))["deliverable_file"], f"tmp/{name}"
        )

    def test_the_file_holds_the_prompt_byte_exact(self):
        self.assertEqual(self.written(self.ran()), PROMPT.encode())

    def test_each_run_writes_a_new_file(self):
        first, second = self.ran("a"), self.ran("b")
        self.assertNotEqual(first[1], second[1])
        self.assertEqual((self.written(first), self.written(second)), (b"a", b"b"))
        self.assertEqual(len(os.listdir(os.path.join(self.workdir, "tmp"))), 2)

    def test_a_failed_file_create_raises(self):
        with mock.patch("tempfile.mkstemp", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                list(self.session.run(PROMPT, self.spec, False))


if __name__ == "__main__":
    unittest.main()
