"""Instruction delivery fixtures for the built-in roles and their configured destinations."""

import os
import tempfile

from agent_pm.delegate import Spec, composer
from agent_pm.delegate.engine.drivers import InstructionDriver

from delegate.tests.case import Case

SID = "11111111-2222-3333-4444-555555555555"
ROLES = ("researcher", "pm", "engineer", "dummy-tester")
INPUT = "Compare A and B.\n[agent-pm-progress:round] a mark line in the input"
CLIENTS = (("claude", "headless"), ("codex", "headless"), (None, "headless"))
GITHUB = """[roles.researcher.output]
type = "github"
repo = "o/docs"
branch = "main"
dir = "Research/"
"""


class InstructionCase(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

    def given(self, role, **kw):
        """A configured role's request, sid SID, output prompt.md and cwd the workdir."""
        work = os.path.join(self.tmp, role)
        kw = {
            "sid": SID,
            "out": os.path.join(work, "prompt.md"),
            "cwd": work,
            **kw,
        }
        self.driver = InstructionDriver()
        return composer.resolve(role), Spec(INPUT, work, **kw)
