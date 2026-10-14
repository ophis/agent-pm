"""composer/run.py: resolve (ported from the old composer's Resolve, PublicResolution, OldKeys and
Validate tests, less the client layer and the prompt), the opaque runtime key, run params,
trusted_dirs, bind and the access intent (the old driver's access tests, less the report command).
"""

import inspect
import os
import tempfile
from dataclasses import fields, replace
from unittest import mock

from agent_pm.delegate import composer
from agent_pm.delegate.composer import ConfigError, RunConfig, RunParams
from agent_pm.delegate.composer.run import (
    RUN_KEYS,
    access,
    bind,
    check_old_keys,
    resolve,
    trusted_dirs,
)
from agent_pm.delegate.composer.teams import role_tables, team
from agent_pm.delegate.composer.types import Intent
from agent_pm.session import Effort
from agent_pm.utils import LOCAL, read_project

from delegate.tests.case import SRC, Case
from delegate.tests.unit.composer.prompts import (
    composed,
    task_text,
)
from delegate.tests.unit.composer.shipped import (
    RealConfig,
    catalog,
    role_tasks,
    roles,
)
from delegate.tests.unit.composer.teams import EDITOR, FILES, POET, RUN_CONFIG, VERSE
from utils.tests.case import local_section

TIER = "tier must be an integer 1–4"
RUNTIME = "runtime must be a non-empty one-line name, got "


class Fake(Case):
    """A team root, config and local file in a temp dir; a HOME of the test's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "team")
        self.config = os.path.join(self.tmp, "config.toml")
        self.local_file = os.path.join(self.tmp, "config.local.toml")
        self.write(FILES)
        self.configure(RUN_CONFIG)

    def write(self, files, top=None):
        for rel, text in files.items():
            path = os.path.join(top or self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def configure(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def local(self, text):
        with open(self.local_file, "w") as f:
            f.write(local_section(text, "delegate"))

    def team_dir(self, files):
        """The Team of the team root and one team dir of `files`, <tmp>/mine, and that dir."""
        top = os.path.join(self.tmp, "mine")
        self.write(files, top)
        return team(self.root, (top,)), top

    def resolved(self, role="writer", task=None, found=None, **kw):
        config = kw.pop("config", self.config)
        local = kw.pop("local", self.local_file)
        return resolve(role, task, team=found or team(self.root), config=config, local=local, **kw)

    def refused(self, role="writer", task=None, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.resolved(role, task, **kw)
        return str(cm.exception)


class Params(Case):
    def test_its_fields(self):
        self.assertEqual([f.name for f in fields(RunParams)], ["input", "out", "workdir", "resume"])

    def test_the_deliverable_is_out_under_the_real_workdir_else_tmp_deliverable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        wd, elsewhere = (os.path.join(root, d) for d in ("wd", "elsewhere"))
        os.makedirs(os.path.join(wd, "sub"))
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(wd, "link"))
        os.symlink(wd, os.path.join(root, "via"))
        tmp = os.path.join(wd, "tmp", "deliverable.md")
        for out, want in (
            (f"{wd}/out.md", f"{wd}/out.md"),
            (f"{wd}/sub/../out.md", f"{wd}/out.md"),
            (f"{elsewhere}/out.md", tmp),
            (f"{wd}/../elsewhere/out.md", tmp),
            (f"{wd}/link/out.md", tmp),
            (f"{root}/via/out.md", f"{root}/via/out.md"),
        ):
            with self.subTest(out=out):
                self.assertEqual(RunParams(input="x", out=out, workdir=wd).deliverable, want)


class TrustedDirs(Fake):
    def test_unset_none_else_the_local_files_real_paths(self):
        self.assertEqual(trusted_dirs(self.config, self.local_file), frozenset())
        self.local('trusted_dirs = ["~/data", "/x/../y"]\n')
        self.assertEqual(
            trusted_dirs(self.config, self.local_file),
            frozenset({os.path.realpath(os.path.expanduser("~/data")), os.path.realpath("/y")}),
        )

    def test_a_malformed_value(self):
        for value, msg in (
            ('"/a"', "trusted_dirs: want a list of local paths"),
            ('["a/b"]', "trusted_dirs: 'a/b' is not an absolute or expandable ~ path"),
        ):
            with self.subTest(value=value):
                self.local(f"trusted_dirs = {value}\n")
                with self.assertRaises(ConfigError) as cm:
                    trusted_dirs(self.config, self.local_file)
                self.assertEqual(str(cm.exception), msg)


class BadRead(Fake):
    def test_a_config_that_cannot_be_read_is_a_config_error_from_each_reader(self):
        found, c, lf = team(self.root), self.config, self.local_file
        calls = {
            "resolve": lambda: resolve("writer", team=found, config=c, local=lf),
            "index": lambda: composer.index("nobody", found, c, lf),
            "catalog": lambda: composer.catalog(found, c, lf),
            "role_tables": lambda: role_tables(c, lf),
            "trusted_dirs": lambda: trusted_dirs(c, lf),
        }
        for text, want in (("bogus = 1\n", "unknown key 'bogus'"), ("x = [\n", "Invalid")):
            self.local(text)
            for name, call in calls.items():
                with (
                    self.subTest(name, text=text),
                    self.assertRaisesRegex(ConfigError, f"^config: .*{want}"),
                ):
                    call()


class ExplicitPaths(Fake):
    """resolve and trusted_dirs read only the config and local passed, never the HOME one (malformed
    here).
    """

    def setUp(self):
        super().setUp()
        with open(os.path.expanduser(LOCAL), "w") as f:
            f.write("malformed = [\n")

    def test_resolve(self):
        self.local('[roles.writer]\neffort = "low"\n')
        self.assertIs(self.resolved().effort, Effort.LOW)
        self.assertIs(self.resolved(local=os.devnull).effort, Effort.MEDIUM)

    def test_trusted_dirs(self):
        self.local('trusted_dirs = ["/x/../y"]\n')
        self.assertEqual(
            trusted_dirs(self.config, self.local_file), frozenset({os.path.realpath("/y")})
        )
        self.assertEqual(trusted_dirs(self.config, os.devnull), frozenset())


class Bind(Case):
    def test_repo_is_the_repo_dir_else_dropped_and_any_other_entry_a_path(self):
        self.assertEqual(bind("repo", "/r/x/.."), "/r")
        self.assertIsNone(bind("repo", None))
        self.assertEqual(bind("~/d", None), os.path.expanduser("~/d"))
        self.assertEqual(bind("/a/./b", "/r"), "/a/b")


class Access(Fake):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.tmp, "work")
        self.repo = os.path.join(self.tmp, "repo")
        self.tasks = os.path.join(self.root, "tasks")

    def intent(self, cwd=None, *, project=False, repo_dir=None, found=None, **kw):
        run = RunConfig(
            **{
                "role": "writer",
                "tier": 2,
                "effort": "high",
                "runtime": "a",
                "output": {"type": "local"},
                "idle_timeout": 60,
                **kw,
            }
        )
        return access(
            run,
            self.work,
            cwd=self.work if cwd is None else cwd,
            project=project,
            repo_dir=repo_dir,
            team=found or team(self.root),
        )

    def test_dirs_reads_writes_commands_filled_the_gate_apart_verbatim_and_the_session_env(self):
        gate, home = "python3 /u/usage.py {{workdir}} *", os.path.expanduser("~/w")
        env = {
            "PYTHONPATH": os.pathsep.join(SRC[name] for name in ("delegate", "session", "utils"))
        }
        out = os.path.join(self.root, "out")
        for name, cfg, repo_dir, dirs, reads, writes, commands, want, session in (
            (
                "gate",
                {"commands": ["python3 -P -m x *"], "gate": gate},
                None,
                [],
                [self.tasks],
                [],
                ["python3 -P -m x *"],
                gate,
                env,
            ),
            ("no gate", {"commands": ["x"]}, None, [], [self.tasks], [], ["x"], "", env),
            ("a gate alone", {"gate": gate}, None, [], [self.tasks], [], [], gate, env),
            (
                "repo entry binds to the repo dir",
                {"write": ["repo"], "commands": ["x --dir {{workdir}}/src *"]},
                self.repo,
                [self.repo],
                [self.tasks],
                [self.repo],
                [f"x --dir {self.work}/src *"],
                "",
                env,
            ),
            (
                "team fills commands",
                {"commands": ["python3 -P {{team}}/tools/x.py --dir {{workdir}}/src *"]},
                None,
                [],
                [self.tasks],
                [],
                [f"python3 -P {self.root}/tools/x.py --dir {self.work}/src *"],
                "",
                env,
            ),
            (
                "no repo dir drops the repo entry",
                {"read": ["repo"], "write": ["repo", "~/w", "~/w"]},
                None,
                [home],
                [self.tasks],
                [home],
                [],
                "",
                {},
            ),
            (
                "read dirs after the tasks dir, deduped",
                {"read": ["{{team}}", "repo", self.tasks, "{{team}}"]},
                self.repo,
                [],
                [self.tasks, self.root, self.repo],
                [],
                [],
                "",
                {},
            ),
            (
                "team fills read and write, deduped, write wins",
                {"read": ["{{team}}", self.tasks], "write": ["{{team}}/out", "{{team}}"]},
                None,
                [out, self.root],
                [self.tasks],
                [out, self.root],
                [],
                "",
                {},
            ),
            (
                "a tasks dir to write",
                {"write": [self.tasks]},
                None,
                [self.tasks],
                [],
                [self.tasks],
                [],
                "",
                {},
            ),
        ):
            with self.subTest(name):
                self.assertEqual(
                    self.intent(repo_dir=repo_dir, **cfg),
                    Intent(
                        dirs=tuple(dirs),
                        reads=tuple(reads),
                        writes=tuple(writes),
                        commands=tuple(commands),
                        gate=want,
                        env=session,
                        cwd=self.work,
                        project=False,
                    ),
                )

    def test_a_dir_to_read_and_write_is_only_in_dirs_also_through_a_symlink(self):
        shared, link = os.path.join(self.tmp, "shared"), os.path.join(self.tmp, "link")
        tasks_link = os.path.join(self.tmp, "tasks-link")
        os.makedirs(shared)
        os.symlink(shared, link)
        os.symlink(self.tasks, tasks_link)
        for read, write, dirs, reads in (
            ([shared], [shared], (shared,), (self.tasks,)),
            ([link], [shared], (shared,), (self.tasks,)),
            ([shared], [link], (link,), (self.tasks,)),
            ([], [tasks_link], (tasks_link,), ()),
            ([link, shared], [], (), (self.tasks, link)),
        ):
            with self.subTest(read=read, write=write):
                got = self.intent(read=read, write=write)
                self.assertEqual((got.dirs, got.reads, got.writes), (dirs, reads, tuple(write)))

    def test_the_workdir_comes_first_only_when_the_cwd_is_another_dir_and_then_is_no_read_dir(self):
        other, link = os.path.join(self.tmp, "other"), os.path.join(self.tmp, "link")
        for d in (self.work, other):
            os.makedirs(d)
        os.symlink(self.work, link)
        for cwd, dirs, reads in (
            (self.work, (self.repo,), (self.tasks, link)),
            (link, (self.repo,), (self.tasks, link)),
            (other, (self.work, self.repo), (self.tasks,)),
        ):
            with self.subTest(cwd=cwd):
                got = self.intent(cwd=cwd, project=True, read=[link], write=[self.repo])
                self.assertEqual(
                    (got.dirs, got.reads, got.cwd, got.project), (dirs, reads, cwd, True)
                )

    def test_a_cwd_that_is_not_an_absolute_path(self):
        for cwd in ("", "work"):
            with self.subTest(cwd=cwd):
                with self.assertRaises(ConfigError) as cm:
                    self.intent(cwd=cwd)
                self.assertEqual(str(cm.exception), f"cwd must be an absolute path, got {cwd!r}")

    def test_a_team_dir_roles_tasks_dir_is_read_not_added(self):
        found, top = self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        got = self.intent(found=found, role="poet")
        self.assertEqual((got.dirs, got.reads), ((), (os.path.join(top, "tasks"),)))

    def test_an_unknown_placeholder(self):
        for key in ("read", "write", "commands"):
            for name in ("nope", "scripts"):
                with self.subTest(key=key, name=name), self.assertRaises(ConfigError) as cm:
                    self.intent(**{key: [f"{{{{{name}}}}}"]})
                self.assertEqual(str(cm.exception), f"unfilled placeholder {{{{{name}}}}} in {key}")


class Resolve(Fake):
    def test_role_overrides_global_output_is_replaced_whole_and_lists_default_empty(self):
        for role, tier, effort, output in (
            ("editor", 1, "high", {"type": "github", "repo": "o/docs", "branch": "main"}),
            ("writer", 2, "medium", {"type": "local"}),
        ):
            with self.subTest(role=role):
                run = self.resolved(role)
                self.assertEqual((run.tier, run.effort, run.output), (tier, effort, output))
        run = self.resolved("editor")
        self.assertEqual((run.read, run.write, run.commands, run.templates), ([], [], [], []))
        self.assertEqual(run.role_title, "Editor")

    def test_a_task_is_the_named_one_else_empty(self):
        self.assertEqual(self.resolved().task, "")
        self.assertEqual(self.resolved(task="long-note").task, "long-note")
        self.assertEqual(self.resolved("editor", "long-note").task, "long-note")

    def test_later_overlays_replace_run_keys_at_any_level(self):
        overlay = {"effort": "low", "roles": {"editor": {"tier": 3, "output": {"type": "local"}}}}
        run = self.resolved("editor", overlays=[overlay])
        self.assertEqual((run.tier, run.effort, run.output), (3, "low", {"type": "local"}))
        self.assertIs(run.effort, Effort.LOW)
        self.assertEqual(self.resolved("editor", overlays=[overlay, {"tier": 4}]).tier, 4)

    def test_gate_defaults_to_empty_and_an_overlays_or_the_configs_gate_is_its_value_verbatim(self):
        self.assertEqual(self.resolved().gate, "")
        gate = "python3 /u/usage.py --below 80 *"
        self.assertEqual(self.resolved(overlays=[{"roles": {"writer": {"gate": gate}}}]).gate, gate)
        self.configure(
            RUN_CONFIG.replace('effort = "medium"', 'effort = "medium"\ngate = "make gate"')
        )
        self.assertEqual(self.resolved().gate, "make gate")

    def test_language_at_any_level(self):
        self.assertEqual(self.resolved("editor").language, "")
        self.configure(
            RUN_CONFIG.replace('effort = "high"', 'effort = "high"\nlanguage = "French"')
        )
        self.assertEqual(self.resolved("editor").language, "French")
        overlay = {"roles": {"editor": {"language": "German"}}}
        self.assertEqual(self.resolved("editor", overlays=[overlay]).language, "German")
        self.assertEqual(self.resolved("editor", overlays=[{"language": ""}]).language, "")

    def test_the_local_file_goes_on_top(self):
        self.local(
            'language = "French"\ncwd = "/srv"\n'
            '[roles.editor]\ntier = 3\n[roles.editor.output]\nbranch = "trunk"\n'
            '[roles.writer]\nread = ["/data"]\n'
        )
        run = self.resolved("editor")
        self.assertEqual(
            (run.language, run.cwd, run.tier, run.effort),
            ("French", "/srv", 3, "high"),
        )
        self.assertEqual(run.output, {"type": "github", "repo": "o/docs", "branch": "trunk"})
        self.assertEqual(self.resolved().read, ["/data"])


class Overlays(Fake):
    def test_the_config_with_the_local_file_then_each_overlay_in_order(self):
        self.local('language = "French"\n[roles.writer]\neffort = "max"\n')
        run = self.resolved(
            "writer",
            "long-note",
            overlays=(
                {"tier": 4, "effort": "high", "output": {"type": "orchestrator"}},
                {"roles": {"writer": {"tier": 1, "output": {"type": "local"}}}},
            ),
        )
        self.assertEqual(
            (run.tier, run.effort, run.language, run.task, run.output),
            (1, "high", "French", "long-note", {"type": "local"}),
        )
        run = self.resolved(overlays=({"output": {"type": "orchestrator"}},))
        self.assertEqual((run.effort, run.output), ("max", {"type": "orchestrator"}))
        self.assertEqual(self.resolved(overlays=({},)), self.resolved())

    def test_an_overlay_rejects_unknown_and_legacy_keys(self):
        bad = (
            ({"flags": []}, "flags"),
            ({"tasks": {}}, "tasks"),
            ({"output": {"type": "local", "typo": "value"}}, "typo"),
            ({"roles": {"writer": {"default_task": "short-note"}}}, "default_task"),
            ({"roles": {"writer": {"tasks": {}}}}, "tasks"),
            ({"roles": {"writer": {"allow": []}}}, "allow"),
            ({"roles": {"writer": {"output": {"type": "local", "typo": "value"}}}}, "typo"),
            ({"roles": {"stranger": {"tier": 3}}}, "stranger"),
        )
        for overlay, message in bad:
            with self.subTest(overlay=overlay), self.assertRaisesRegex(ConfigError, message):
                self.resolved(overlays=(overlay,))

    def test_malformed_overlay_tables_fail_clearly(self):
        for overlay in (
            None,
            [],
            {"roles": []},
            {"roles": {"writer": 2}},
            {"output": []},
            {"roles": {"writer": {"output": []}}},
        ):
            with self.subTest(overlay=overlay), self.assertRaises(ConfigError):
                self.resolved(overlays=(overlay,))

    def test_an_unknown_role(self):
        self.assertEqual(self.refused("stranger"), "unknown role 'stranger'")

    def test_non_string_overlay_key_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            self.resolved(overlays=({1: "bad", "typo": "bad"},))

    def test_overlay_errors_name_the_label_source_and_table(self):
        bad = (
            ({"foo": 1}, "unknown key 'foo' in [{t}]"),
            ({"roles": {"stranger": {"tier": 3}}}, "unknown role 'stranger' in [{t}.roles]"),
            ({"roles": {"writer": {"foo": 1}}}, "unknown key 'foo' in [{t}.roles.writer]"),
            (
                {"roles": {"writer": {"output": {"type": "local", "typo": 1}}}},
                "unknown key 'typo' in [{t}.roles.writer.output]",
            ),
            ({"output": {"type": "local", "typo": 1}}, "unknown key 'typo' in [{t}.output]"),
            (
                {"roles": {"writer": {"tasks": {}}}},
                (
                    "[{t}.roles.writer] has 'tasks': task-level config and "
                    "default_task are gone; set run keys in [{t}.roles.writer] (a run without a task picks one)"
                ),
            ),
        )
        labels = (
            ({}, "run overlay", "overlay"),
            ({"label": ("my.toml", "core")}, "my.toml", "core"),
        )
        for (overlay, message), (kwargs, source, table) in (
            (b, label) for b in bad for label in labels
        ):
            with self.subTest(overlay=overlay, table=table):
                self.assertEqual(
                    self.refused(overlays=(overlay,), **kwargs),
                    f"{source}: {message.format(t=table)}",
                )


class OldKeys(Fake):
    def test_task_tables_or_default_task_in_any_role_table(self):
        for local in (
            '[roles.writer]\ndefault_task = "short-note"\n',
            "[roles.writer.tasks.short-note]\ntier = 3\n",
            '[roles.writer]\ndefault_task = "x"\nmodel = "y"\n',
        ):
            for role in ("writer", "editor"):
                with self.subTest(local=local, role=role):
                    self.local(local)
                    msg = self.refused(role)
                    self.assertIn("[roles.writer]", msg)
                    self.assertNotIn("unknown key", msg)

    def test_the_check_names_its_layers_table(self):
        for table in ({"default_task": "x"}, {"tasks": {}}, {"tier": 1, "tasks": {"t": {}}}):
            with self.subTest(table=table), self.assertRaises(ConfigError) as cm:
                check_old_keys(table, "researcher", "core.")
            self.assertIn("[core.roles.researcher]", str(cm.exception))
        check_old_keys({"tier": 1, "output": {}}, "researcher")


class Validate(Fake):
    def test_an_invalid_config_fails_whatever_the_overlays(self):
        for old, new, msg, role, overlays in (
            ("tier = 1", "tier = 9", TIER, "editor", [{"tier": 2}]),
            ("tier = 1", "tier = 5", TIER, "editor", ()),
            ('effort = "medium"', 'effort = "ultra"', "effort must be one of", "writer", ()),
        ):
            with self.subTest(new=new):
                self.configure(RUN_CONFIG.replace(old, new))
                self.assertIn(msg, self.refused(role, overlays=overlays))

    def test_the_local_file_is_checked_with_the_config(self):
        self.local('[roles.editor]\nmodel = "opus"\n')
        self.assertEqual(
            self.refused("editor"),
            "unknown key 'model' in roles.editor",
        )

    def test_config_errors_come_before_a_missing_charter_or_task_file(self):
        self.configure(RUN_CONFIG.replace("tier = 1", "tier = 9"))
        os.remove(os.path.join(self.root, "tasks", "long-note.md"))
        os.remove(os.path.join(self.root, "roles", "editor.md"))
        self.assertIn(TIER, self.refused("editor"))

    def test_a_configured_role_without_its_charter(self):
        os.remove(os.path.join(self.root, "roles", "editor.md"))
        self.assertEqual(
            self.refused("editor"), "roles.editor: no charter roles/editor.md in core or team_dirs"
        )

    def test_an_invalid_overlay_value(self):
        self.assertLessEqual({"language", "runtime"}, RUN_KEYS)
        gate, language = (
            "gate must be one line of shell command without backticks",
            "language must be one line of text",
        )
        for overlay, msg in (
            ({"tier": 9}, TIER),
            ({"gate": "a\nb"}, gate),
            ({"gate": "echo `id`"}, gate),
            ({"gate": 7}, gate),
            ({"language": "a\nb"}, language),
            ({"language": 7}, language),
        ):
            with self.subTest(overlay=overlay):
                self.assertIn(msg, self.refused("editor", overlays=[overlay]))

    def test_replace_is_checked(self):
        run = self.resolved("editor")
        for change in ({"effort": "ultra"}, {"gate": "a\nb"}, {"runtime": ""}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                replace(run, **change)

    def test_unknown_role_or_task_or_another_roles(self):
        for role, task, msg in (
            ("nobody", None, "unknown role 'nobody'"),
            (
                "writer",
                "essay",
                "task 'essay' is not one of writer's tasks (short-note, long-note)",
            ),
            ("editor", "short-note", "task 'short-note' is not one of editor's tasks (long-note)"),
        ):
            with self.subTest(role=role, task=task):
                self.assertEqual(self.refused(role, task), msg)


class OwnedKeys(Fake):
    def test_another_layers_global_keys_are_left_alone(self):
        self.configure(
            "surprise = 1\n"
            + RUN_CONFIG
            + '\n[adapters.x]\nimplementation = "agent_pm.session.adapters.x:XAdapter"\nmodel = "m"\n'
        )
        self.assertEqual(self.resolved().runtime, "a")

    def test_owned_tables_and_overlays_stay_strict(self):
        for local, overlay, msg in (
            (
                '[roles.writer]\nmodel = "m"\n',
                {},
                "unknown key 'model' in roles.writer",
            ),
            ('[roles.writer]\nteam_dirs = ["/x"]\n', {}, "unknown key 'team_dirs' in roles.writer"),
            (
                "[output]\ntypo = 1\n",
                {},
                "unknown key 'typo' in output",
            ),
            (
                "[roles.editor.output]\ntypo = 1\n",
                {},
                "unknown key 'typo' in roles.editor.output",
            ),
            ('output = "local"\n', {}, "output must be a table"),
            ('[roles.writer]\noutput = "local"\n', {}, "roles.writer.output must be a table"),
            ("", {"model": "m"}, "run overlay: unknown key 'model' in [overlay]"),
            (
                "",
                {"roles": {"writer": {"model": "m"}}},
                "run overlay: unknown key 'model' in [overlay.roles.writer]",
            ),
        ):
            with self.subTest(msg=msg):
                self.local(local)
                self.assertEqual(self.refused(overlays=(overlay,)), msg)

    def test_show_is_an_unknown_key_at_every_level(self):
        self.assertNotIn("show", RUN_KEYS)
        self.assertNotIn("show", [f.name for f in fields(RunConfig)])
        for local, overlay, msg in (
            (
                'show = "none"\n',
                {},
                f"config: {self.local_file}: unknown key 'show' in [delegate]",
            ),
            (
                '[roles.writer]\nshow = "none"\n',
                {},
                "unknown key 'show' in roles.writer",
            ),
            ("", {"show": ""}, "run overlay: unknown key 'show' in [overlay]"),
            (
                "",
                {"roles": {"writer": {"show": "none"}}},
                "run overlay: unknown key 'show' in [overlay.roles.writer]",
            ),
        ):
            with self.subTest(msg=msg):
                self.local(local)
                self.assertEqual(self.refused(overlays=(overlay,)), msg)


class Runtime(Fake):
    def setUp(self):
        super().setUp()
        for role in ("pm", "researcher"):
            self.write({f"roles/{role}.md": EDITOR.replace("# Editor", f"# {role}")})

    def test_the_configs_value_and_a_role_overlay_per_role(self):
        overlay = {"roles": {"pm": {"runtime": "x"}}}
        self.assertEqual(
            [self.resolved(role, overlays=(overlay,)).runtime for role in ("pm", "researcher")],
            ["x", "a"],
        )

    def test_the_configs_role_table_per_role(self):
        self.configure(RUN_CONFIG + '\n[roles.pm]\nruntime = "x"\n')
        self.assertEqual([self.resolved(role).runtime for role in ("pm", "researcher")], ["x", "a"])

    def test_an_overlays_global_value_beats_the_configs_role_value(self):
        self.configure(RUN_CONFIG.replace('effort = "medium"', 'effort = "medium"\nruntime = "w"'))
        self.assertEqual(self.resolved().runtime, "w")
        self.assertEqual(self.resolved(overlays=({"runtime": "x"},)).runtime, "x")

    def test_missing_or_not_a_one_line_name_after_any_layer(self):
        unset = RUN_CONFIG.replace('runtime = "a"\n', "")
        for config, overlays, got in (
            (unset, (), "''"),
            (RUN_CONFIG.replace('"a"', '""'), (), "''"),
            (RUN_CONFIG.replace('"a"', '"  "'), (), "'  '"),
            (RUN_CONFIG.replace('"a"', '"a\\nb"'), (), "'a\\nb'"),
            (RUN_CONFIG.replace('"a"', "3"), (), "3"),
            (RUN_CONFIG, ({"runtime": ""},), "''"),
            (RUN_CONFIG, ({"roles": {"writer": {"runtime": ["x"]}}},), "['x']"),
        ):
            with self.subTest(got=got, overlays=overlays):
                self.configure(config)
                self.assertEqual(self.refused(overlays=overlays), RUNTIME + got)
        self.local('runtime = "l"\n')
        self.assertEqual(self.resolved().runtime, "l")

    def test_resolve_takes_no_runtime_client_or_trust(self):
        self.assertEqual(
            list(inspect.signature(resolve).parameters),
            ["role", "task", "overlays", "label", "team", "config", "local", "cfg"],
        )

    def test_a_read_config_given_is_used_reading_no_file(self):
        cfg = read_project("delegate")
        want = (resolve("pm"), trusted_dirs(), composer.index("pm"))
        original = open

        def opened(path, *args, **kwargs):
            if str(path).endswith(".toml"):
                raise OSError("read")
            return original(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=opened):
            got = (
                resolve("pm", cfg=cfg),
                trusted_dirs(cfg=cfg),
                composer.index("pm", cfg=cfg),
            )
        self.assertEqual(got, want)


class TeamRoles(Fake):
    def test_a_team_dirs_role_without_a_table_takes_the_global_keys_and_an_overlay(self):
        found, _ = self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        run = self.resolved(
            "poet", "verse", found, overlays=({"roles": {"poet": {"effort": "low"}}},)
        )
        self.assertEqual(
            (run.role, run.task, run.tier, run.effort, run.output, run.templates, run.role_title),
            ("poet", "verse", 2, "low", {"type": "local"}, [], "Poet"),
        )
        self.assertEqual(self.refused("poet"), "unknown role 'poet'")
        self.assertEqual(
            self.refused(task="verse", found=found),
            "task 'verse' is not one of writer's tasks (short-note, long-note)",
        )

    def test_a_misnamed_file_a_run_names_or_a_role_table_without_a_charter(self):
        found, top = self.team_dir({"roles/Poet.md": POET, "tasks/Verse.md": VERSE})
        self.assertEqual(
            self.refused("Poet", found=found), f"{top}/roles/Poet.md: name 'Poet': want [a-z0-9-]+"
        )
        self.assertEqual(
            self.refused(task="Verse", found=found),
            f"{top}/tasks/Verse.md: name 'Verse': want [a-z0-9-]+",
        )
        self.local("[roles.ghost]\ntier = 3\n")
        self.assertEqual(
            self.refused("ghost"), "roles.ghost: no charter roles/ghost.md in core or team_dirs"
        )


class Real(RealConfig):
    def test_a_local_files_other_layers_keys_are_left_alone(self):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as stream:
            stream.write(
                '[delegate]\nusers = ["octocat"]\ntrusted_dirs = ["~/data-repo"]\nlanguage = "French"\n[session.hosts.tmux]\nworkers_per_column = 2\ngrid_retile = "off"\n[linear_orchestrator]\nteam = "T"\n[linear_orchestrator.roles.pm]\naccount = "pm@x"\n'
            )
        for role, task in role_tasks():
            run = composer.resolve(role, task)
            self.assertEqual((run.task, run.language), (task, "French"))

    def test_a_task_from_another_roles_index_is_an_error(self):
        with self.assertRaisesRegex(
            composer.ConfigError, "task 'deep-research' is not one of engineer's tasks"
        ):
            composer.resolve("engineer", "deep-research")

    def test_pm_engineer_and_researcher_runs(self):
        researcher = (
            (
                "[Researcher rules](#researcher) > your task file's rules",
                "# Template: `templates/research-report.md`",
                "`ophis/private_docs`",
            ),
            lambda run: (run.tier, run.effort, run.read),
            (2, "high", ["{{team}}"]),
        )
        for role, task, literals, got, want in (
            (
                "pm",
                None,
                ("[PM rules](#pm) > your task file's rules", "# Template: `templates/prd.md`"),
                lambda run: (run.output["dir"], run.read, run.write),
                ("Product Design/", [], []),
            ),
            (
                "engineer",
                None,
                ("[Engineer rules](#engineer) > your task file's rules", "gh pr create"),
                lambda run: (run.effort, run.write, run.output),
                ("xhigh", [], {"type": "pull-request"}),
            ),
            *(
                ("researcher", task, *researcher)
                for task in (None, "deep-research", "light-research")
            ),
        ):
            with self.subTest(role=role, task=task):
                prompt, run = composed(role, task)
                for literal in literals:
                    self.assertIn(literal, prompt)
                self.assertEqual(got(run), want)

    def test_a_run_reads_the_team_root_when_its_text_names_a_method(self):
        named = []
        for role in roles():
            with open(os.path.join(composer.TEAM, "roles", f"{role}.md")) as f:
                text = f.read() + "".join(task_text(t) for t in catalog()[role])
            if "<team>/methods/" in text:
                named.append(role)
                self.assertIn("{{team}}", composer.resolve(role).read, role)
        self.assertEqual(named, ["researcher"])
