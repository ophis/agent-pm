"""Planning behavior."""

import io
import json
import os
import uuid
from contextlib import redirect_stderr
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import ConfigError, composer
from agent_pm.delegate.composer import RunConfig, RunParams
from agent_pm.delegate.composer.prompt import RESUME
from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.delegate.engine.plan import place
from agent_pm.delegate.event.reply import OUTCOME, PROGRESS
from agent_pm.session import Access, note

from delegate.tests.unit.engine.plan.case import TASKS, notice
from delegate.tests.unit.engine.plans import SID, Base


class ReplyTags(Base):
    def test_the_spec_carries_the_delegates_reply_protocol(self):
        got, _ = self.planned()
        self.assertEqual(got.session.reply_tags, (PROGRESS, OUTCOME))


class Configures(Base):
    def test_plan_keeps_the_callers_driver(self):
        got, _ = self.planned()
        self.assertEqual(got.spec.runtime, "fake")
        self.assertIs(got.driver, self.driver)


class Cwd(Base):
    """place: the run's cwd and whether its project settings load."""

    def setUp(self):
        super().setUp()
        self.trusted, self.other = (os.path.join(self.tmp, d) for d in ("trusted", "other"))
        for d in (self.trusted, self.other):
            os.makedirs(d)

    def placed(self, *trusted, cwd=None, key="", resume=None):
        """(place's (cwd, project), stderr) for researcher with run key cwd `key` and trusted_dirs
        `trusted`.
        """
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
        note(
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
