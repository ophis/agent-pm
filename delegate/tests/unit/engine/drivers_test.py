"""Driver start takes one spec; instruction delivery creates no runtime or event stream."""

import os
import unittest
from dataclasses import fields

from agent_pm.delegate import ConfigError, Spec, composer, plan, run
from agent_pm.delegate.engine.drivers import AgentDriver, Driver, InstructionDriver
from agent_pm.delegate.engine.types import Instruction
from agent_pm.session import Session

from delegate.tests.case import Case
from delegate.tests.fakes.session import SessionClass, StartSpec
from delegate.tests.unit.runs import no_child


class Drivers(Case):
    def test_driver_is_abstract_and_has_no_abstract_constructor(self):
        self.assertEqual(Driver.__abstractmethods__, frozenset({"start"}))
        with self.assertRaises(TypeError):
            Driver()

    def test_agent_start_forwards_the_exact_spec_and_placement_to_its_session_class(self):
        session_cls = SessionClass.fresh()
        driver = AgentDriver(session_cls)
        spec = StartSpec(
            workdir=self.home,
            runtime="fake",
            mode="headless",
            prompt="the prompt",
            sid="11111111-2222-3333-4444-555555555555",
            resume=True,
            name="mine",
            events=os.path.join(self.home, "events"),
            flags=("--caller-flag",),
            env={"CALLER": "kept"},
        )
        self.assertNotIn("idle_timeout", {field.name for field in fields(spec)})
        for timeout in (None, 30):
            with self.subTest(idle_timeout=timeout):
                session = driver.start(spec, host="other", display="opaque", idle_timeout=timeout)
                self.assertIsInstance(session, Session)
                self.assertEqual(session_cls.started[-1], (spec, "other", "opaque", timeout))
                self.assertIs(session_cls.started[-1][0], spec)
                self.assertEqual(
                    (session.runtime, session.mode, session.sid, session.name),
                    ("fake", "headless", spec.sid, "mine"),
                )

    def test_instruction_start_saves_exact_utf8_bytes_without_process_or_thread(self):
        out = os.path.join(self.home, "work", "prompt.md")
        prompt = "héllo\r\n[agent-pm-progress:start] input, not an event\n\x1b"
        spec = StartSpec(workdir=self.home, runtime="fake", mode="headless", prompt=prompt)
        with no_child():
            instruction = InstructionDriver(out).start(spec)
        self.assertEqual(instruction, Instruction(out))
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), prompt.encode("utf-8"))
        self.assertFalse(os.path.exists(os.path.join(self.home, "run.jsonl")))

    def test_instruction_resume_and_detach_are_refused_before_any_write(self):
        out = os.path.join(self.home, "work", "prompt.md")
        driver = InstructionDriver(out)
        spec = StartSpec(
            workdir=self.home, runtime="fake", mode="headless", prompt="the prompt", resume=True
        )
        with self.assertRaisesRegex(ConfigError, "instruction has no session to resume"):
            driver.start(spec)
        with self.assertRaisesRegex(ConfigError, "instruction cannot detach"):
            driver.detach()
        self.assertFalse(os.path.exists(os.path.dirname(out)))

    def test_instruction_rejects_session_controls_before_any_write(self):
        out = os.path.join(self.home, "work", "prompt.md")
        driver = InstructionDriver(out)
        spec = StartSpec(workdir=self.home, runtime="fake", mode="headless", prompt="the prompt")
        for keywords, message in (
            ({"host": "other"}, "instruction has no host or display"),
            ({"display": "opaque"}, "instruction has no host or display"),
            ({"idle_timeout": 30}, "instruction has no idle timeout"),
        ):
            with self.subTest(keywords=keywords), no_child():
                with self.assertRaises(ConfigError) as caught:
                    driver.start(spec, **keywords)
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(os.path.exists(os.path.dirname(out)))

    def test_instruction_run_bypasses_attempts_replies_and_callbacks(self):
        work = os.path.join(self.home, "work")
        out = os.path.join(work, "prompt.md")
        spec = Spec("[agent-pm-progress:start] source text", work, out=out, cwd=work)
        config = composer.resolve("dummy-tester")
        driver = InstructionDriver(out)
        expected = plan(config, spec, driver=driver).prompt
        events = []
        with no_child():
            result = run(config, spec, driver=driver, on_event=events.append)
        self.assertEqual((result.returncode, result.outcome, result.instruction), (0, None, out))
        self.assertEqual(events, [])
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), expected.encode("utf-8"))
        self.assertFalse(os.path.exists(os.path.join(work, "run.jsonl")))


if __name__ == "__main__":
    unittest.main()
