"""Team policy behavior."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from agent_pm.delegate import composer

from delegate.tests.unit.engine.plan.case import IN_TEMP, TASKS, TASKS_PARAM, TEAMS, write_team
from delegate.tests.unit.engine.plans import SID, Base


class ConfiguredTeamDirs(Base):
    """No team given: the built-in team, and the local `team_dirs` (a team dir in a temp dir)
    checked only for overlap.
    """

    def setUp(self):
        super().setUp()
        self.team = os.path.join(self.tmp, "team")
        write_team(self.team)
        self.local(f"team_dirs = {json.dumps([self.team])}\n")

    def test_one_overlapping_what_the_run_can_write_is_refused(self):
        inside = os.path.join(self.team, "w")
        for kw, what in (
            ({"workdir": inside}, f"the workdir {inside}"),
            ({"cwd": self.tmp}, f"the cwd {self.tmp}"),
            ({"keys": {"write": [inside]}}, f"write dir {inside} {inside}"),
        ):
            with self.subTest(what=what):
                self.assertEqual(
                    self.refused(**kw),
                    f"team_dirs: {self.team} overlaps {what}: the agent run can write there",
                )

    def test_a_read_dir_overlapping_one_is_read_unadded(self):
        for read in (self.tmp, os.path.join(self.team, "tasks")):
            with self.subTest(read=read):
                access = self.planned(keys={"read": [read]})[0].session.access
                self.assertEqual((access.dirs, access.reads), ((), (TASKS, read)))

    def test_one_overlapping_codex_home_is_refused(self):
        for home in (os.path.join(self.team, "codex"), self.tmp):
            with self.subTest(home=home), mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                self.assertEqual(
                    self.refused(runtime="codex"),
                    f"team_dirs: {self.team} overlaps a dir codex can write {home}: the agent "
                    "run can write there",
                )

    def test_the_runtimes_dirs_are_asked_with_the_runs_spec_only_when_there_are_team_dirs(self):
        with mock.patch.object(self.driver, "writable_dirs", return_value=()) as asked:
            got = self.planned(sid=SID, resume=True)[0]
            asked.assert_called_once_with("fake", got.session, SID)
            asked.reset_mock()
            self.local("")
            self.planned(runtime="codex")
            asked.assert_not_called()

    def test_a_symlink_in_one_is_checked_by_its_real_path(self):
        tasks, moved = os.path.join(self.team, "tasks"), os.path.join(self.work, "team-tasks")
        os.makedirs(self.work)
        os.rename(tasks, moved)
        os.symlink(moved, tasks)
        self.assertEqual(
            self.refused(),
            f"team_dirs: {tasks} ({moved}) overlaps the workdir {self.work}: the agent "
            "run can write there",
        )

    def test_a_bad_or_unset_team_dirs_changes_nothing(self):
        base = self.planned(sid=SID)[0]
        for text in ("team_dirs = []\n", 'team_dirs = "x"\n', ""):
            with self.subTest(text=text):
                self.local(text)
                self.assertEqual(self.planned(sid=SID)[0], base)

    def test_given_as_the_runs_team_it_is_refused_in_a_temp_dir(self):
        found = composer.team(composer.TEAM, [self.team])
        self.assertTrue(
            self.refused("poet", "verse", team=found).startswith(
                f"team_dirs: {self.team} is in temp dir "
            )
        )


@unittest.skipIf(IN_TEMP, f"{TEAMS} is in a temp dir, where check_team_dirs refuses a team dir")
class CustomTeam(Base):
    """A team dir outside temp dirs given as the run's team: check_team_dirs against what the run
    can write.
    """

    def setUp(self):
        super().setUp()
        os.makedirs(TEAMS, mode=0o700, exist_ok=True)
        os.chmod(TEAMS, 0o700)
        self.team = os.path.realpath(tempfile.mkdtemp(dir=TEAMS, prefix="team-"))
        self.addCleanup(shutil.rmtree, self.team, ignore_errors=True)
        os.chmod(self.team, 0o755)
        write_team(self.team)
        self.tasks = os.path.join(self.team, "tasks")
        self.found = composer.team(composer.TEAM, [self.team])

    def read_unadded(self, role, task, reads, **kw):
        """`role` planned for the fake and the codex runtime: no added dir, `reads` its read dirs."""
        home = os.path.join(self.tmp, "codex")
        for runtime in ("fake", "codex"):
            with (
                self.subTest(role=role, runtime=runtime),
                mock.patch.dict(os.environ, {"CODEX_HOME": home}),
            ):
                got, _ = self.planned(role, task, team=self.found, runtime=runtime, **kw)
                self.assertEqual((got.session.access.dirs, got.session.access.reads), ((), reads))
        return got

    def test_the_roles_own_tasks_dir_is_a_read_dir_never_added(self):
        got = self.read_unadded("poet", "verse", (self.tasks,))
        self.assertEqual(TASKS_PARAM.search(got.prompt)[1], self.tasks)

    def test_a_read_dir_holding_it_or_its_tasks_dir_is_read_unadded(self):
        self.read_unadded("poet", "verse", (self.tasks, self.team), keys={"read": [self.team]})
        self.read_unadded(
            "researcher", "light-research", (TASKS, self.tasks), keys={"read": [self.tasks]}
        )

    def test_one_overlapping_what_the_run_can_write_is_refused(self):
        inside, top = os.path.join(self.team, "w"), os.path.dirname(self.team)
        for kw, what in (
            ({"workdir": inside}, f"the workdir {inside}"),
            ({"cwd": top}, f"the cwd {top}"),
            ({"keys": {"write": [inside]}}, f"write dir {inside} {inside}"),
            ({"keys": {"write": [self.tasks]}}, f"write dir {self.tasks} {self.tasks}"),
        ):
            with self.subTest(what=what):
                self.assertEqual(
                    self.refused("poet", "verse", team=self.found, **kw),
                    f"team_dirs: {self.team} overlaps {what}: the agent run can write there",
                )

    def test_one_overlapping_a_dir_codex_can_write_is_refused(self):
        inside, top = os.path.join(self.team, "codex"), os.path.dirname(self.team)
        tmpdir = f"[adapters.codex.env]\nTMPDIR = {json.dumps(top)}\n"
        for home, local, what in (
            (inside, "", inside),
            (self.tasks, "", self.tasks),
            (top, "", top),
            (os.path.join(self.tmp, "codex"), tmpdir, top),
        ):
            with self.subTest(home=home, local=local):
                self.local(local)
                with mock.patch.dict(os.environ, {"CODEX_HOME": home}):
                    self.assertEqual(
                        self.refused("poet", "verse", team=self.found, runtime="codex"),
                        f"team_dirs: {self.team} overlaps a dir codex can write {what}: the "
                        "agent run can write there",
                    )


class Policy(Base):
    """plan's Spec.flags/env: the selected runtime's [policy.<runtime>], the local overlay's
    replacing the core's.
    """

    SHIPPED = {
        "claude": (
            (
                "--permission-mode",
                "auto",
                "--strict-mcp-config",
                "--settings",
                '{"enabledPlugins": {"agent-pm@agent-pm": false}}',
            ),
            {},
        ),
        "codex": (
            (
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            {},
        ),
        "fake": ((), {}),
    }

    def test_the_default_config_gives_each_runtime_its_shipped_policy(self):
        for runtime, want in self.SHIPPED.items():
            with self.subTest(runtime=runtime):
                got, _ = self.planned(runtime=runtime)
                self.assertEqual((got.session.flags, dict(got.session.env)), want)

    def test_a_local_policy_replaces_the_shipped_one(self):
        self.local('[policy.codex]\nflags = ["-s", "workspace-write"]\nenv = { K = "v" }\n')
        got, _ = self.planned(runtime="codex")
        self.assertEqual(
            (got.session.flags, dict(got.session.env)), (("-s", "workspace-write"), {"K": "v"})
        )

    def test_an_invalid_policy_is_a_config_error_before_any_launch(self):
        for text in (
            'policy = "x"\n',
            '[policy]\nclaude = "x"\n',
            '[policy.claude]\nflags = "--x"\n',
            "[policy.claude]\nflags = [1]\n",
            '[policy.claude]\nflags = ["a\\u0000"]\n',
            "[policy.claude]\nenv = { K = 1 }\n",
            '[policy.claude]\nunset = ["K"]\n',
        ):
            with self.subTest(text=text), mock.patch.object(self.driver, "start") as start:
                self.local(text)
                self.assertIn("policy", self.refused(runtime="claude"))
                start.assert_not_called()
