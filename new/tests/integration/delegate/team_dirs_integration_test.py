"""A local `team_dirs` read by delegate/cli.py as a subprocess over the fake `claude`: a dry run, --list, a run in
headless mode of the team dir's role. Main's tests/integration/core/team_dirs_integration_test.py, the Delegate's
part."""

from new.tests.integration.composer.team_dirs_integration_test import IN_TEMP, TEAMS, make_team
from new.tests.integration.delegate.cli_integration_test import CLI, Integration, mark
from new.tests.unit.delegate.runner_test import block

import json
import os
import sys
import unittest

DONE = {"status": "done", "title": "note", "summary": "A note.", "deliverable": "A note.\n"}


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
        self.assertIn(self.tasks, plan["dirs"])
        self.assertEqual(os.listdir(self.work), [])
        self.assertEqual(self.calls(), [])

    def test_list(self):
        res = self.drive([], argv=[sys.executable, CLI, "--list"])
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
        self.assertIn(self.tasks, call["argv"])
        self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))
        self.assertEqual(self.read(self.out), DONE["deliverable"])


if __name__ == "__main__":
    unittest.main()
