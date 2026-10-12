"""delegate/plan.py: main's drive_test Cwd cases (place); plan's refusals, Access, Spec, sid and prompt; main's team
dir checks (TeamRuns), for a custom team and for the configured team dirs."""

from new.tests import NEW, hermetic
from new.tests.unit.composer.team_test import POET, VERSE

import io
import json
import os
import re
import shutil
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

import composer
from composer import RunConfig, RunParams
from composer.base import SRC
from composer.prompt import RESUME
from delegate import ConfigError, plan
from delegate.plan import place
from driver import api
from utils.paths import temp_dirs, under

SID = "11111111-2222-3333-4444-555555555555"
TASKS = os.path.join(composer.TEAM, "tasks")
METHODS = os.path.join(composer.TEAM, "methods")
TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")
TEAM_FILES = {"roles/poet.md": POET, "tasks/verse.md": VERSE}
# A custom team dir must lie outside every temp dir (check_team_dirs): under the repo's gitignored work/.
TEAMS = os.path.join(os.path.dirname(NEW), "work", "new-team-dirs-tests")
IN_TEMP = under(os.path.realpath(TEAMS), temp_dirs()) is not None


def notice(cwd):
    return f"delegate: cwd {cwd} is not in trusted_dirs: its project settings are off\n"


def write_team(top, files=TEAM_FILES):
    for rel, text in files.items():
        path = os.path.join(top, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)


class Base(unittest.TestCase):
    """A real temp dir for the workdir `work` (not made) and a HOME of the test's own (self.agent_pm)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.agent_pm = hermetic.home(self)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
            f.write(text)

    def planned(
        self, role="researcher", task="light-research", *, keys=None, team=None, run=None, **kw
    ):
        """(Plan, stderr) of `run`, else `role` resolved with run keys `keys`: input "Research X.", runtime "fake",
        headless, out self.out, workdir self.work, the cwd the workdir, unless `kw` says otherwise."""
        kw = {"runtime": "fake", "mode": "headless", "out": self.out, "input": "Research X.", **kw}
        workdir = kw.pop("workdir", self.work)
        kw.setdefault("cwd", workdir)
        run = run or replace(composer.resolve(role, team=team), **(keys or {}))
        err = io.StringIO()
        with redirect_stderr(err):
            got = plan(run, task, kw.pop("input"), workdir, team=team, **kw)
        return got, err.getvalue()

    def refused(self, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.planned(*args, **kw)
        return str(cm.exception)


class Cwd(Base):
    """place: the run's cwd and whether its project settings load."""

    def setUp(self):
        super().setUp()
        self.trusted, self.other = (os.path.join(self.tmp, d) for d in ("trusted", "other"))
        for d in (self.trusted, self.other):
            os.makedirs(d)

    def placed(self, *trusted, cwd=None, key="", resume=None):
        """(place's (cwd, project), stderr) for researcher with run key cwd `key` and trusted_dirs `trusted`."""
        self.local(f"trusted_dirs = {json.dumps(list(trusted))}\n")
        err = io.StringIO()
        with redirect_stderr(err):
            got = place(
                replace(composer.resolve("researcher"), cwd=key),
                self.work,
                resume_sid=resume,
                cwd=cwd,
            )
        return got, err.getvalue()

    def write_record(self, *entries):
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "run.jsonl"), "w") as f:
            f.writelines(json.dumps({"ts": "t", "kind": "session", **e}) + "\n" for e in entries)

    def test_unset_is_the_callers_current_directory(self):
        here = os.getcwd()
        self.assertEqual(self.placed(), ((here, False), notice(here)))

    def test_the_run_key_wins_over_the_callers_cwd_and_expands_tilde(self):
        with mock.patch.dict(os.environ, {"HOME": self.tmp}):
            self.assertEqual(self.placed(cwd=self.trusted, key="~/other")[0], (self.other, False))

    def test_a_trusted_cwd_loads_project_settings_without_a_notice(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.trusted, link)
        for entry, cwd in (
            (self.trusted, self.trusted),
            (link, self.trusted),
            (self.tmp, self.trusted),
            (self.trusted, link),
        ):
            with self.subTest(entry=entry, cwd=cwd):
                self.assertEqual(self.placed(entry, cwd=cwd), ((cwd, True), ""))

    def test_an_untrusted_cwd_gets_a_notice(self):
        self.assertEqual(
            self.placed(self.trusted, cwd=self.other), ((self.other, False), notice(self.other))
        )

    def test_the_workdir_as_cwd_has_no_notice(self):
        self.assertEqual(self.placed(cwd=self.work), ((self.work, False), ""))

    def test_a_trusted_cwd_in_the_workdir_is_refused(self):
        sub = os.path.join(self.work, "sub")
        os.makedirs(sub)
        for cwd in (self.work, sub):
            with self.subTest(cwd=cwd), self.assertRaises(ConfigError) as cm:
                self.placed(self.tmp, cwd=cwd)
            self.assertEqual(
                str(cm.exception),
                f"cwd {cwd} is in trusted_dirs and in the workdir: project settings "
                "must not load from it",
            )
        self.assertEqual(self.placed(cwd=sub)[0], (sub, False))

    def test_a_cwd_that_is_no_directory_or_a_bad_trusted_dirs_is_refused(self):
        nope, file = os.path.join(self.tmp, "nope"), os.path.join(self.tmp, "file")
        open(file, "w").close()
        for trusted, key, want in (
            ((), nope, f"cwd {nope} is not a directory"),
            ((), file, f"cwd {file} is not a directory"),
            (("rel/dir",), "", "trusted_dirs: 'rel/dir' is not an absolute or expandable ~ path"),
        ):
            with self.subTest(want=want), self.assertRaises(ConfigError) as cm:
                self.placed(*trusted, cwd=self.other, key=key)
            self.assertEqual(str(cm.exception), want)

    def test_a_resume_runs_in_the_cwd_and_project_of_its_session_record(self):
        os.makedirs(self.work)
        api.records.note(
            api.records.path(self.work),
            "session",
            sid=SID,
            cwd=self.trusted,
            project=True,
            transcript="",
            resume="",
        )
        self.assertEqual(
            self.placed(self.trusted, cwd=self.other, key=self.work, resume=SID),
            ((self.trusted, True), ""),
        )
        self.write_record({"sid": SID, "cwd": self.trusted, "project": False})
        self.assertEqual(self.placed(self.trusted, resume=SID)[0], (self.trusted, False))

    def test_a_resume_whose_cwd_is_no_longer_trusted_is_refused(self):
        self.write_record({"sid": SID, "cwd": self.trusted, "project": True})
        with self.assertRaises(ConfigError) as cm:
            self.placed(resume=SID)
        self.assertEqual(str(cm.exception), f"cwd {self.trusted} is no longer in trusted_dirs")

    def test_a_resume_without_its_session_record_runs_in_the_workdir(self):
        self.assertEqual(
            self.placed(self.trusted, cwd=self.trusted, resume=SID), ((self.work, False), "")
        )
        for entries in (
            (),
            ({"sid": "other", "cwd": self.trusted, "project": True},),
            ({"sid": SID, "cwd": "rel", "project": True},),
            ({"sid": SID, "cwd": self.trusted, "project": 1},),
        ):
            with self.subTest(entries=entries):
                self.write_record(*entries)
                self.assertEqual(
                    self.placed(self.trusted, cwd=self.trusted, resume=SID),
                    ((self.work, False), ""),
                )

    def test_prompt_paths_stay_absolute_whatever_the_cwd_and_an_input_naming_a_file_is_text(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, "input.md"), "w") as f:
            f.write("Research X.")
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        self.local(f"trusted_dirs = {json.dumps([self.trusted])}\n")
        for cwd in (self.trusted, self.other):
            with self.subTest(cwd=cwd):
                got, _ = self.planned(
                    cwd=cwd, input="work/input.md", workdir="work", out="work/out.md"
                )
                self.assertIn(f"\n- `<Workdir>`: `{self.work}`\n", got.prompt)
                self.assertTrue(got.prompt.endswith("\n# Input\n\nwork/input.md\n"))
                self.assertEqual(
                    (got.spec.workdir, got.params.workdir, got.out),
                    (self.work, self.work, self.out),
                )


class Refusals(Base):
    def test_each_refusal_names_its_cause_and_nothing_is_written(self):
        tasks = ", ".join(composer.index("researcher"))
        nobody = RunConfig(
            role="nope", tier=2, effort="high", runtime="fake", output={"type": "local"}
        )
        for kw, want in (
            ({"mode": "x"}, "mode must be headless or tui, got 'x'"),
            ({"mode": "tui"}, "tui mode is not available yet"),
            ({"name": "n"}, "name: tui mode only"),
            ({"events": "e"}, "events: tui mode only"),
            ({"sid": "a", "resume": "b"}, "sid 'a' differs from resume 'b'"),
            ({"run": nobody}, "unknown role 'nope'"),
            ({"task": "nope"}, f"task 'nope' is not one of researcher's tasks ({tasks})"),
        ):
            with self.subTest(want=want):
                self.assertEqual(self.refused(**kw), want)
                self.assertFalse(os.path.exists(self.work))

    def test_a_composer_or_driver_error_is_the_delegates_with_its_message(self):
        self.assertEqual(
            self.refused(keys={"templates": ["nope"]}), "missing file templates/nope.md"
        )
        self.assertIn(
            "workdir must be a printable absolute path, got ",
            self.refused(workdir=self.work + "\n", cwd=self.tmp),
        )


class Planned(Base):
    KEYS = {"read": ["{{methods}}"], "commands": ["{{scripts}}/x --dir {{workdir}}/src *"]}

    def test_the_access_is_the_intents_with_the_gate_last(self):
        command, gate = f"{SRC}/x --dir {self.work}/src *", "python3 /u/usage.py *"
        for value, commands in ((gate, (command, gate)), ("", (command,))):
            with self.subTest(gate=value):
                got, _ = self.planned(keys=self.KEYS | {"gate": value})
                self.assertEqual(
                    got.spec.access,
                    api.Access(
                        dirs=(TASKS, METHODS), commands=commands, cwd=self.work, project=False
                    ),
                )

    def test_the_cwd_unset_is_the_callers_and_the_workdir_an_added_dir(self):
        got, err = self.planned(keys=self.KEYS, cwd=None)
        access = got.spec.access
        self.assertEqual(
            (access.cwd, access.project, access.dirs),
            (os.getcwd(), False, (self.work, TASKS, METHODS)),
        )
        self.assertEqual(err, notice(os.getcwd()))

    def test_the_spec_holds_the_runs_tier_and_effort_and_the_absolute_workdir(self):
        spec = self.planned(keys={"tier": 4, "effort": "low"})[0].spec
        self.assertEqual(
            (spec.workdir, spec.tier, spec.effort, spec.sid), (self.work, 4, "low", None)
        )

    def test_a_new_run_gets_a_new_uuid4_sid_unless_given_one_and_a_resume_its_own(self):
        first, second = self.planned()[0], self.planned()[0]
        self.assertEqual((uuid.UUID(first.sid).version, first.resume), (4, False))
        self.assertNotEqual(first.sid, second.sid)
        for kw, resume in (
            ({"sid": SID}, False),
            ({"resume": SID}, True),
            ({"sid": SID, "resume": SID}, True),
        ):
            with self.subTest(kw=kw):
                got = self.planned(**kw)[0]
                self.assertEqual((got.sid, got.resume, got.params.resume), (SID, resume, resume))

    def test_a_resumes_prompt_starts_with_the_resume_text(self):
        new, resumed = self.planned()[0].prompt, self.planned(resume=SID)[0].prompt
        self.assertFalse(new.startswith(RESUME))
        self.assertEqual(resumed, RESUME + new)

    def test_out_is_absolute_and_without_it_the_prompt_names_the_default_deliverable(self):
        got = self.planned()[0]
        self.assertEqual(got.out, self.out)
        self.assertIn("`<Workdir>/out.md`", got.prompt)
        got = self.planned(out=None)[0]
        self.assertIsNone(got.out)
        self.assertIn("`<Workdir>/tmp/deliverable.md`", got.prompt)
        self.assertEqual(got.params.deliverable, os.path.join(self.work, "tmp", "deliverable.md"))

    def test_the_plan_holds_the_run_with_its_task_and_its_params(self):
        got = self.planned()[0]
        self.assertEqual(
            (got.runtime, got.mode, got.run.task), ("fake", "headless", "light-research")
        )
        self.assertEqual(
            got.params, RunParams(input="Research X.", out=self.out, workdir=self.work)
        )
        self.assertEqual(self.planned(task=None)[0].run.task, "")

    def test_plan_creates_and_writes_nothing(self):
        for kw in ({}, {"resume": SID}, {"out": None}):
            with self.subTest(kw=kw):
                self.planned(**kw)
                self.assertFalse(os.path.exists(self.work))
        os.makedirs(self.work)
        self.planned()
        self.assertEqual(os.listdir(self.work), [])


class ConfiguredTeamDirs(Base):
    """No team given: the built-in team, and the local `team_dirs` (a team dir in a temp dir) checked only for overlap."""

    def setUp(self):
        super().setUp()
        self.team = os.path.join(self.tmp, "team")
        write_team(self.team)
        self.local(f"team_dirs = {json.dumps([self.team])}\n")

    def test_one_overlapping_what_the_run_can_write_is_refused(self):
        inside, tasks = os.path.join(self.team, "w"), os.path.join(self.team, "tasks")
        for kw, what in (
            ({"workdir": inside}, f"the workdir {inside}"),
            ({"cwd": self.tmp}, f"the cwd {self.tmp}"),
            ({"keys": {"write": [inside]}}, f"write dir {inside} {inside}"),
            ({"keys": {"read": [self.tmp]}}, f"a dir the run adds {self.tmp}"),
            ({"keys": {"read": [tasks]}}, f"a dir the run adds {tasks}"),
        ):
            with self.subTest(what=what):
                self.assertEqual(
                    self.refused(**kw),
                    f"team_dirs: {self.team} overlaps {what}: the agent run can write there",
                )

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
    """A team dir outside temp dirs given as the run's team: check_team_dirs against what the run can write."""

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

    def test_the_roles_own_tasks_dir_is_an_added_dir_exempt_from_the_check(self):
        got, _ = self.planned("poet", "verse", team=self.found)
        self.assertIn(self.tasks, got.spec.access.dirs)
        self.assertEqual(TASKS_PARAM.search(got.prompt)[1], self.tasks)

    def test_one_overlapping_what_the_run_can_write_is_refused(self):
        inside, top = os.path.join(self.team, "w"), os.path.dirname(self.team)
        for role, kw, what in (
            ("poet", {"workdir": inside}, f"the workdir {inside}"),
            ("poet", {"cwd": top}, f"the cwd {top}"),
            ("poet", {"keys": {"write": [inside]}}, f"write dir {inside} {inside}"),
            ("poet", {"keys": {"read": [self.team]}}, f"a dir the run adds {self.team}"),
            ("researcher", {"keys": {"read": [self.tasks]}}, f"a dir the run adds {self.tasks}"),
        ):
            with self.subTest(role=role, what=what):
                task = "verse" if role == "poet" else "light-research"
                self.assertEqual(
                    self.refused(role, task, team=self.found, **kw),
                    f"team_dirs: {self.team} overlaps {what}: the agent run can write there",
                )


if __name__ == "__main__":
    unittest.main()
