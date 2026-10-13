"""Runtime print_prompt through the Delegate's API, a driver built from the real config: per
built-in role a done run whose deliverable is the planned prompt, saved to out, no child process;
headless and resume refused; each role's destination and prompt the same under claude, codex and
print_prompt.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr

from agent_pm.delegate import Begun, ConfigError, Finished, Progress, Spec, composer, plan, run
from agent_pm.delegate.event.reply import marks

from delegate.tests.case import Case, committed
from delegate.tests.unit.runs import no_child

SID = "11111111-2222-3333-4444-555555555555"
ROLES = ("researcher", "pm", "engineer", "dummy-tester")
INPUT = "Compare A and B.\n[agent-pm-progress:round] a mark line in the input"
STARTED = Progress("start", "the composed prompt is handed to the caller")
RUNTIMES = (("claude", "headless"), ("codex", "headless"), ("print_prompt", "in_process"))
GITHUB = """[roles.researcher.output]
type = "github"
repo = "o/docs"
branch = "main"
dir = "Research/"
"""


class PrintPrompt(Case):
    """A HOME of the test's own; per role, workdir <tmp>/<role> (not made), out its prompt.md."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.configure_session(committed())

    def given(self, role, **kw):
        """(RunConfig, Spec) for run() and plan(): print_prompt in_process, sid SID, the cwd the
        workdir, unless `kw`.
        """
        work = os.path.join(self.tmp, role)
        kw = {
            "runtime": "print_prompt",
            "mode": "in_process",
            "sid": SID,
            "out": os.path.join(work, "prompt.md"),
            "cwd": work,
            **kw,
        }
        return composer.resolve(role), Spec(INPUT, work, **kw)

    def test_each_role_is_done_its_deliverable_the_planned_prompt_saved_to_out(self):
        for role in ROLES:
            with self.subTest(role=role):
                args = self.given(role)
                want, events = plan(*args, driver=self.driver).prompt, []
                with no_child(), redirect_stderr(io.StringIO()):
                    r = run(*args, on_event=events.append, driver=self.driver)
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.outcome.deliverable), (0, "done", want)
                )
                with open(args[1].out) as f:
                    self.assertEqual(f.read(), want)
                self.assertEqual(marks(want), [("round", "a mark line in the input")])
                self.assertEqual(events, [Begun(SID), STARTED, Finished(r)])

    def test_headless_is_refused_and_nothing_starts(self):
        args = self.given("dummy-tester", mode="headless")
        with (
            no_child(),
            self.assertRaisesRegex(
                ConfigError, "runtime 'print_prompt' does not support headless mode"
            ),
        ):
            run(*args, driver=self.driver)
        self.assertFalse(os.path.exists(args[1].workdir))

    def test_a_resume_is_refused(self):
        args = self.given("dummy-tester", resume=True)
        with (
            no_child(),
            self.assertRaisesRegex(ConfigError, "print_prompt has no session to resume"),
        ):
            run(*args, driver=self.driver)

    def test_each_roles_destination_and_prompt_are_the_same_under_any_runtime(self):
        for local in ("", GITHUB):
            with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
                f.write(local)
            for role in ROLES:
                with self.subTest(github=bool(local), role=role):
                    first, *others = (
                        plan(*args, driver=self.driver)
                        for args in (
                            self.given(role, runtime=runtime, mode=mode)
                            for runtime, mode in RUNTIMES
                        )
                    )
                    for p in others:
                        self.assertEqual((p.run.output, p.prompt), (first.run.output, first.prompt))
        self.assertEqual(composer.resolve("researcher").output["type"], "github")


if __name__ == "__main__":
    unittest.main()
