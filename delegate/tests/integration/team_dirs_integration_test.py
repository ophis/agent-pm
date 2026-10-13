"""A local `team_dirs` read by delegate/cli.py as a subprocess over the fake `claude`: a dry run, --list, a run in
headless mode of the team dir's role, its tasks dir a read rule, never added; over fake_codex: a run and its resume
reading the tasks dir unadded, a read dir holding the team dir run unadded, a write dir or CODEX_HOME overlapping it
refused. Main's tests/integration/core/team_dirs_integration_test.py, the Delegate's part."""

import json
import os
import unittest

from delegate.tests.integration import codex_integration_test
from delegate.tests.integration.cli_integration_test import CLI, Integration, mark
from delegate.tests.integration.composer.team_dirs_integration_test import (
    CORE_TASKS,
    IN_TEMP,
    TEAMS,
    make_team,
)
from delegate.tests.unit.runner_test import block

DONE = {"status": "done", "title": "note", "summary": "A note.", "deliverable": "A note.\n"}


def added(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


@unittest.skipIf(IN_TEMP, f"{TEAMS} is in a temp dir, where check_team_dirs refuses a team dir")
class TeamDirs(Integration):
    """Role `writer` of a team dir (the composer test's TEAM) as the local config's `team_dirs`."""

    ROLE = "writer"

    def setUp(self):
        super().setUp()
        self.team = make_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        with open(os.path.join(self.env["HOME"], ".agent-pm", "core-new.local.toml"), "w") as f:
            f.write(f"team_dirs = {json.dumps([self.team])}\n")

    def test_a_dry_run(self):
        res = self.drive([], "--task", "note", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        plan = json.loads(res.stdout)
        self.assertIn(f"`{self.tasks}`", plan["prompt"])
        self.assertEqual((plan["dirs"], plan["reads"]), ([self.work], [self.tasks]))
        self.assertEqual(os.listdir(self.work), [])
        self.assertEqual(self.calls(), [])

    def test_list(self):
        res = self.drive([], argv=[*CLI, "--list"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            f"\nwriter  local  {os.path.join(self.team, 'roles', 'writer.md')}\n"
            f"  note: a short note; always; light.  [{os.path.join(self.tasks, 'note.md')}]\n",
            res.stdout,
        )

    def test_a_run_in_headless_mode_reads_its_task_from_its_tasks_dir(self):
        res = self.drive([mark("start", "noting"), block(DONE)], "--task", "note")
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): noting", "delegate: status done"):
            self.assertIn(line, res.stderr)
        (call,) = self.calls()
        self.assertIn(f"`{self.tasks}`", call["argv"][-1])
        self.assertIn(f"Read(/{self.tasks}/**)", call["argv"])
        self.assertEqual(added(call["argv"]), [self.work])
        self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))
        self.assertEqual(self.read(self.out), DONE["deliverable"])


@unittest.skipIf(IN_TEMP, f"{TEAMS} is in a temp dir, where check_team_dirs refuses a team dir")
class CodexTeamDirs(codex_integration_test.Integration):
    """--runtime codex over fake_codex, role `writer` of a team dir (the composer test's TEAM) as the local config's
    `team_dirs`."""

    ROLE, TASK = "writer", "note"

    def setUp(self):
        super().setUp()
        self.team = make_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        os.makedirs(os.path.join(self.home, ".agent-pm"), exist_ok=True)
        self.toml = os.path.join(self.home, ".agent-pm", "core-new.local.toml")
        with open(self.toml, "w") as f:
            f.write(f"team_dirs = {json.dumps([self.team])}\n")

    def role(self, key: str) -> None:
        with open(self.toml, "a") as f:
            f.write(f"[roles.writer]\n{key} = {json.dumps([self.team])}\n")

    def test_a_codex_run_and_its_resume_read_its_tasks_dir_unadded(self):
        for extra, resume_id in (((), None), (("--resume",), codex_integration_test.NATIVE)):
            with self.subTest(extra=extra):
                res = self.drive(
                    codex_integration_test.said(mark("start", "noting"), block(DONE)), *extra
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                call = self.calls()[-1]
                self.assertEqual(call["resume_id"], resume_id)
                dirs = added(call["argv"])
                self.assertNotIn(self.tasks, map(os.path.realpath, dirs))
                self.assertNotIn(CORE_TASKS, dirs)
                self.assertIn(f"`{self.tasks}`", call["prompt"])
                self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))
                self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_a_read_dir_holding_the_team_dir_runs_unadded(self):
        self.role("read")
        res = self.drive(codex_integration_test.said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        self.assertFalse({self.team, self.tasks} & set(map(os.path.realpath, added(call["argv"]))))

    def test_a_write_dir_holding_the_team_dir_is_refused(self):
        self.role("write")
        res = self.drive([])
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn(
            f"delegate: team_dirs: {self.team} overlaps write dir {self.team} {self.team}: "
            "the agent run can write there\n",
            res.stderr,
        )
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.work), [])

    def test_one_overlapping_codex_home_exits_2_before_codex_starts(self):
        for home in (os.path.join(self.team, "codex"), os.path.dirname(self.team)):
            with self.subTest(home=home):
                self.env["CODEX_HOME"] = home
                res = self.drive([])
                self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
                self.assertIn(
                    f"delegate: team_dirs: {self.team} overlaps a dir codex can write {home}: "
                    "the agent run can write there\n",
                    res.stderr,
                )
                self.assertEqual(self.calls(), [])
                self.assertEqual(os.listdir(self.work), [])


if __name__ == "__main__":
    unittest.main()
