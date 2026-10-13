"""Access behavior."""

import os
import tempfile
from dataclasses import fields

from agent_pm.delegate import composer
from agent_pm.delegate.composer import (
    ConfigError,
    ConfigSource,
    RunConfig,
    RunLocation,
    RunParams,
)
from agent_pm.delegate.composer.run import (
    access,
    bind,
    resolve,
    trusted_dirs,
)
from agent_pm.delegate.composer.teams import role_tables, team
from agent_pm.delegate.composer.types import Intent
from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.utils import LOCAL

from delegate.tests.case import Case
from delegate.tests.unit.composer.run.case import Fake
from delegate.tests.unit.composer.teams import POET, VERSE


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
            "resolve": lambda: resolve("writer", team=found, source=ConfigSource(path=c, local=lf)),
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
        self.assertEqual(self.resolved().effort, "low")
        self.assertEqual(self.resolved(local=os.devnull).effort, "medium")

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
                **kw,
            }
        )
        return access(
            run,
            self.work,
            RunLocation(cwd=self.work if cwd is None else cwd, project=project, repo_dir=repo_dir),
            team=found or team(self.root),
        )

    def test_dirs_reads_writes_commands_filled_the_gate_apart_verbatim_and_the_session_env(self):
        gate, home = "python3 /u/usage.py {{workdir}} *", os.path.expanduser("~/w")
        env = {"PYTHONPATH": PYTHONPATH}
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
