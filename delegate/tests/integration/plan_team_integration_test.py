"""Plan access checks for an owned team outside system temporary directories."""

import json
import os
import sys
import unittest
from unittest import mock

from agent_pm.delegate import composer, configure
from agent_pm.utils import temp_dirs, under

from delegate.tests.case import REPO
from delegate.tests.plans import TASKS, TASKS_PARAM, Base, write_team
from session.tests.fakes.host import FakeHost
from utils.tests.sandbox import owned_directory

TEAMS = os.path.join(REPO, "work")
IN_TEMP = under(os.path.realpath(TEAMS), temp_dirs()) is not None


@unittest.skipIf(IN_TEMP, f"{TEAMS} is in a temp dir, where check_team_dirs refuses a team dir")
class CustomTeam(Base):
    """A team dir outside temp dirs given as the run's team: check_team_dirs against what the run
    can write.
    """

    def setUp(self):
        super().setUp()
        self.team = owned_directory(self, TEAMS, prefix="team-")
        os.chmod(self.team, 0o755)
        write_team(self.team)
        self.tasks = os.path.join(self.team, "tasks")
        self.found = composer.team(composer.TEAM, [self.team])

    def read_unadded(self, role, task, reads, **kw):
        """
        `role` planned for the fake and the codex runtime: no added dir, `reads` its read dirs.
        """
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


class Configures(Base):
    def test_configures_driver_detaches_on_the_harness_host_not_the_default(self):
        fake = 'implementation = "session.tests.fakes.host:FakeHost"\n'
        self.local(
            f'[hosts]\ndefault = "spare"\n[hosts.spare]\n{fake}root = "~/spare"\n'
            f'[hosts.fake]\n{fake}[delegates]\nharness_host = "fake"\n'
        )
        driver = configure()
        driver.detach("d1", [sys.executable, "-I", "-c", "pass"], self.tmp, {}, opener="mgr")
        process = FakeHost("fake").attach("d1")
        self.addCleanup(process.close)
        self.assertEqual(process.display(), "mgr")
        self.assertTrue(process.wait_exit(10))
        self.assertEqual(FakeHost("spare", {"root": "~/spare"}).list(), [])
