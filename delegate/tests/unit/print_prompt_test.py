"""Runtime print_prompt through the Delegate's API, the real config's registry: per built-in role a done run whose
deliverable is the planned prompt, saved to out, no child process; headless and resume refused; each role's
destination and prompt the same under claude, codex and print_prompt."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr

from agent_pm.delegate import Begun, ConfigError, Finished, Progress, composer, plan, run
from agent_pm.delegate.event.reply import marks

from delegate.tests.case import Case
from delegate.tests.unit.runner_test import no_child

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

    def given(self, role, **kw):
        """(args, kw) for run() and plan(): print_prompt in_process, sid SID, the cwd the workdir, unless `kw`."""
        work = os.path.join(self.tmp, role)
        kw = {
            "runtime": "print_prompt",
            "mode": "in_process",
            "sid": SID,
            "out": os.path.join(work, "prompt.md"),
            "cwd": work,
            **kw,
        }
        return (composer.resolve(role), None, INPUT, work), kw

    def test_each_role_is_done_its_deliverable_the_planned_prompt_saved_to_out(self):
        for role in ROLES:
            with self.subTest(role=role):
                args, kw = self.given(role)
                want, events = plan(*args, **kw).prompt, []
                with no_child(), redirect_stderr(io.StringIO()):
                    r = run(*args, on_event=events.append, **kw)
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.outcome.deliverable), (0, "done", want)
                )
                with open(kw["out"]) as f:
                    self.assertEqual(f.read(), want)
                self.assertEqual(marks(want), [("round", "a mark line in the input")])
                self.assertEqual(events, [Begun(SID), STARTED, Finished(r)])

    def test_headless_is_refused_and_nothing_starts(self):
        args, kw = self.given("dummy-tester", mode="headless")
        with (
            no_child(),
            self.assertRaisesRegex(
                ConfigError, "runtime 'print_prompt' does not support headless mode"
            ),
        ):
            run(*args, **kw)
        self.assertFalse(os.path.exists(args[3]))

    def test_a_resume_is_refused(self):
        args, kw = self.given("dummy-tester", resume=SID)
        with (
            no_child(),
            self.assertRaisesRegex(ConfigError, "print_prompt has no session to resume"),
        ):
            run(*args, **kw)

    def test_each_roles_destination_and_prompt_are_the_same_under_any_runtime(self):
        for local in ("", GITHUB):
            with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
                f.write(local)
            for role in ROLES:
                with self.subTest(github=bool(local), role=role):
                    first, *others = (
                        plan(*args, **kw)
                        for args, kw in (
                            self.given(role, runtime=runtime, mode=mode)
                            for runtime, mode in RUNTIMES
                        )
                    )
                    for p in others:
                        self.assertEqual((p.run.output, p.prompt), (first.run.output, first.prompt))
        self.assertEqual(composer.resolve("researcher").output["type"], "github")


if __name__ == "__main__":
    unittest.main()
