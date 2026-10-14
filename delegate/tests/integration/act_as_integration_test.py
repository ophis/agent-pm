"""The approved skill command in an isolated Python environment."""

import os
import subprocess

from agent_pm.delegate import composer
from agent_pm.delegate.cli import listing
from agent_pm.delegate.composer import Composer, RunParams

from delegate.tests.case import Case
from delegate.tests.skills.act_as import argv, command
from utils.tests.sandbox import clean_env, python_path

ROLES = ["researcher", "pm", "engineer", "dummy-tester"]


class ActAs(Case):
    def test_step_1s_command_lists_the_team(self):
        env = clean_env()
        env["PATH"] = python_path(os.path.join(self.home, "bin"))
        res = subprocess.run(
            argv(command(1)),
            cwd=self.home,
            env={**env, "HOME": self.home},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        want = listing(Composer())
        self.assertEqual((res.returncode, res.stdout), (0, want), res.stderr)
        self.assertEqual([r.split()[0] for r in want.splitlines() if r[:1] != " "], ROLES)

    def test_step_3_delivers_the_instruction_file(self):
        workdir = os.path.join(self.home, "work")
        out = os.path.join(workdir, "prompt.md")
        run = (
            command(3)
            .replace("<role>", "dummy-tester")
            .replace("[--task <task>]", "--task echo")
            .replace("<workdir>", workdir)
            .replace("<Input>", "Hello.")
        )
        env = clean_env()
        env["PATH"] = python_path(os.path.join(self.home, "bin"))
        result = subprocess.run(
            argv(run),
            cwd=self.home,
            env={**env, "HOME": self.home},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (0, "", f"delegate: instruction delivered: {out}\n"),
        )
        expected = composer.render(
            composer.resolve("dummy-tester", "echo"),
            RunParams(input="Hello.", out=out, workdir=workdir),
        )
        with open(out, "rb") as stream:
            self.assertEqual(stream.read(), expected.encode("utf-8"))
