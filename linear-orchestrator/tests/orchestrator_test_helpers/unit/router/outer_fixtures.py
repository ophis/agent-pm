import os
import subprocess

from agent_pm.utils import GUARD
from orchestrator_test_helpers.unit.router.data import OUTER
from orchestrator_test_helpers.unit.router.outer_base_fixtures import OuterBase


class Outer(OuterBase):
    """FR-45, FR-47's resume check: the outer reads the issue, checks its repo, renders its input and starts run.py in
    a detached Driver session."""

    def local_clone(self):
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (
            ["init", "-q", path],
            ["-C", path, "remote", "add", "origin", "https://github.com/ophis/agent-pm.git"],
        ):
            subprocess.run(["git", *GUARD, *argv], check=True)
        self.write_config(OUTER + f'[local_clones]\n"Ophis/Agent-PM" = "{path}"\n')
        return path
