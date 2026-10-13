"""Runtime print_prompt through the Delegate's API, a driver built from the real config: per
built-in role a done run whose deliverable is the planned prompt, saved to out, no child process;
headless and resume refused; each role's destination and prompt the same under claude, codex and
print_prompt.
"""

import os
import tempfile

from agent_pm.delegate import Progress, Spec, composer

from delegate.tests.case import Case, committed

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


class PrintPromptCase(Case):
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
