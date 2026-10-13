"""delegate/engine/plan.py: main's drive_test Cwd cases (place); plan's refusals, Access, Spec, sid and prompt; main's team
dir checks (TeamRuns), for a custom team and for the configured team dirs, Codex's own writable dirs included, read
dirs (the tasks dir among them) never added nor counted."""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import ConfigError, Spec, composer, plan
from agent_pm.delegate.composer import RunConfig, RunParams
from agent_pm.delegate.composer.prompt import RESUME
from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.delegate.engine.plan import place
from agent_pm.delegate.event.reply import OUTCOME, PROGRESS
from agent_pm.session import Access, Session, api
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session.adapters import registry
from agent_pm.utils.paths import temp_dirs, under

from delegate.tests.case import REPO, Case, committed
from delegate.tests.unit.composer.team_test import POET, VERSE
from session.tests import fake_adapter

SID = "11111111-2222-3333-4444-555555555555"
TASKS = os.path.join(composer.TEAM, "tasks")
TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")
TEAM_FILES = {"roles/poet.md": POET, "tasks/verse.md": VERSE}
# A custom team dir must lie outside every temp dir (check_team_dirs): under the repo's gitignored work/.
TEAMS = os.path.join(REPO, "work", "new-team-dirs-tests")
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


class Base(Case):
    """A real temp dir for the workdir `work` (not made), a HOME of the test's own (self.agent_pm) and the registry's
    adapters: the core config's and the fake runtime."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.work = os.path.join(self.tmp, "work")
        self.out = os.path.join(self.work, "out.md")
        self.enterContext(
            mock.patch.dict(sys.modules, {"agent_pm.session.adapters.fake": fake_adapter})
        )
        self.local("")

    def local(self, text):
        """The local config `text`, the session configured with its adapters and the fake runtime."""
        local = os.path.join(self.agent_pm, "config.local.toml")
        with open(local, "w") as f:
            f.write(text)
        fake = {"implementation": "adapters.fake:FakeAdapter"}
        self.configure_session(committed(local) | {"fake": fake})

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
            got = plan(run, Spec(kw.pop("input"), workdir, task=task, team=team, **kw))
        return got, err.getvalue()

    def refused(self, *args, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.planned(*args, **kw)
        return str(cm.exception)


class ReplyTags(Base):
    def test_the_spec_carries_the_delegates_reply_protocol(self):
        got, _ = self.planned()
        self.assertEqual(got.session.reply_tags, (PROGRESS, OUTCOME))


class Configures(Base):
    def test_plan_never_configures_the_session(self):
        registry.reset()
        with mock.patch.object(Session, "configure") as configure:
            got, _ = self.planned()
        self.assertEqual(got.spec.runtime, "fake")
        configure.assert_not_called()
        with self.assertRaisesRegex(SessionConfigError, "not configured"):
            registry.configured()


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

    def test_empty_is_the_callers_current_directory(self):
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
        api.note(
            self.work,
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
                    (got.session.workdir, got.params.workdir, got.spec.out),
                    (self.work, self.work, self.out),
                )


class Refusals(Base):
    def test_each_refusal_names_its_cause_and_nothing_is_written(self):
        tasks = ", ".join(composer.index("researcher"))
        nobody = RunConfig(
            role="nope", tier=2, effort="high", runtime="fake", output={"type": "local"}
        )
        for kw, want in (
            ({"mode": "x"}, "mode must be one of headless, tui, in_process, got 'x'"),
            ({"name": "n"}, "name: tui mode only"),
            ({"events": "e"}, "events: tui mode only"),
            ({"mode": "in_process", "name": "n"}, "name: tui mode only"),
            ({"mode": "in_process", "events": "e"}, "events: tui mode only"),
            ({"resume": True}, "resume: needs a sid"),
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
    KEYS = {"read": ["{{team}}"], "commands": ["x --dir {{workdir}}/src *"]}

    def test_the_access_is_the_intents_with_the_gate_last_and_its_env(self):
        command, gate = f"x --dir {self.work}/src *", "python3 /u/usage.py *"
        env = {"PYTHONPATH": PYTHONPATH}
        for keys, commands, want in (
            (self.KEYS | {"gate": gate}, (command, gate), env),
            (self.KEYS | {"gate": ""}, (command,), env),
            (self.KEYS | {"commands": [], "gate": ""}, (), {}),
        ):
            with self.subTest(keys=keys):
                got, _ = self.planned(keys=keys)
                self.assertEqual(
                    got.session.access,
                    Access(
                        dirs=(),
                        commands=commands,
                        cwd=self.work,
                        project=False,
                        reads=(TASKS, composer.TEAM),
                        env=want,
                    ),
                )

    def test_the_cwd_unset_is_the_callers_and_the_workdir_an_added_dir(self):
        got, err = self.planned(keys=self.KEYS, cwd=None)
        access = got.session.access
        self.assertEqual(
            (access.cwd, access.project, access.dirs, access.reads),
            (os.getcwd(), False, (self.work,), (TASKS, composer.TEAM)),
        )
        self.assertEqual(err, notice(os.getcwd()))

    def test_the_spec_holds_the_runs_tier_and_effort_and_the_absolute_workdir(self):
        spec = self.planned(keys={"tier": 4, "effort": "low"})[0].session
        self.assertEqual(
            (spec.workdir, spec.tier, spec.effort, spec.sid), (self.work, 4, "low", None)
        )

    def test_a_new_run_gets_a_new_uuid4_sid_unless_given_one_and_a_resume_its_own(self):
        first, second = self.planned()[0], self.planned()[0]
        self.assertEqual((uuid.UUID(first.spec.sid).version, first.spec.resume), (4, False))
        self.assertNotEqual(first.spec.sid, second.spec.sid)
        for kw, resume in (({"sid": SID}, False), ({"sid": SID, "resume": True}, True)):
            with self.subTest(kw=kw):
                got = self.planned(**kw)[0]
                self.assertEqual(
                    (got.spec.sid, got.spec.resume, got.params.resume), (SID, resume, resume)
                )

    def test_a_resumes_prompt_starts_with_the_resume_text(self):
        new, resumed = self.planned()[0].prompt, self.planned(sid=SID, resume=True)[0].prompt
        self.assertFalse(new.startswith(RESUME))
        self.assertEqual(resumed, RESUME + new)

    def test_out_is_absolute_and_without_it_the_prompt_names_the_default_deliverable(self):
        got = self.planned()[0]
        self.assertEqual(got.spec.out, self.out)
        self.assertIn("`<Workdir>/out.md`", got.prompt)
        got = self.planned(out=None)[0]
        self.assertIsNone(got.spec.out)
        self.assertIn("`<Workdir>/tmp/deliverable.md`", got.prompt)
        self.assertEqual(got.params.deliverable, os.path.join(self.work, "tmp", "deliverable.md"))

    def test_the_plan_holds_the_run_with_its_task_and_its_params(self):
        got = self.planned()[0]
        self.assertEqual(
            (got.spec.runtime, got.spec.mode, got.run.task), ("fake", "headless", "light-research")
        )
        self.assertEqual((got.spec.name, got.spec.events), (None, None))
        self.assertEqual(
            got.params, RunParams(input="Research X.", out=self.out, workdir=self.work)
        )
        self.assertEqual(self.planned(task=None)[0].run.task, "")

    def test_without_a_runtime_the_runs_own(self):
        got = self.planned(runtime=None)[0]
        self.assertEqual(got.spec.runtime, got.run.runtime)

    def test_in_process_is_planned_as_headless(self):
        got = self.planned(mode="in_process", sid=SID)[0]
        self.assertEqual(got.spec.mode, "in_process")
        self.assertEqual(
            replace(got, spec=replace(got.spec, mode="headless")), self.planned(sid=SID)[0]
        )

    def test_tui_takes_name_and_events_unchecked_for_the_driver_to_check(self):
        for name, events in ((None, None), ("mine", "/x/events.log"), ("a b", "rel\n")):
            with self.subTest(name=name, events=events):
                got = self.planned(mode="tui", name=name, events=events)[0]
                self.assertEqual(
                    (got.spec.mode, got.spec.name, got.spec.events), ("tui", name, events)
                )
                self.assertFalse(os.path.exists(self.work))

    def test_plan_creates_and_writes_nothing(self):
        for kw in ({}, {"sid": SID, "resume": True}, {"out": None}):
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
        with mock.patch.object(api, "writable_dirs", return_value=()) as asked:
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
    """plan's Spec.flags/env: the selected runtime's [policy.<runtime>], the local overlay's replacing the core's."""

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
            with self.subTest(text=text), mock.patch.object(api, "start") as start:
                self.local(text)
                self.assertIn("policy", self.refused(runtime="claude"))
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
