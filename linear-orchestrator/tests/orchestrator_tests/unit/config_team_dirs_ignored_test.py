import json
import os
import subprocess
import tempfile
import tomllib
from unittest import mock

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import GUARD, SHORT, temp_dirs
from orchestrator_tests.case import ORCH_ROOT, PY, Case, child_env
from orchestrator_tests.unit.board_ids import HEADER, TEAM, role
from orchestrator_tests.unit.config_data import (
    BASE,
    CORE_LOCAL,
    CORE_TABLES,
    CORE_TOP,
    DEPLOYMENT,
    HTTPS,
    IMPORT_ALONE,
    PIPELINE,
    ROLES,
    SID,
    SLUG,
    STEPS,
    fields,
    git,
    missing,
    write,
)
from orchestrator_tests.unit.config_scenarios import Clones, ConfigFile
from orchestrator_tests.unit.config_support import GATE


class TeamDirsIgnored(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.team = os.path.realpath(os.path.join(self.agent_pm, "team"))
        writer = (
            "# Writer\n\nYou write.\n\n## Tasks\n\nPick the task below that fits "
            "the input; unsure → `one-pager` (`<tasks>/one-pager.md`).\n\n- "
            "`one-pager` (`<tasks>/one-pager.md`): a brief; always; light.\n"
        )
        for rel, text in (
            ("tasks/one-pager.md", "# One-Pager\n" + STEPS),
            ("roles/writer.md", writer),
        ):
            path = os.path.join(self.team, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write(path, text)
        self.work = os.path.join(os.path.dirname(self.agent_pm), "work")

    def seen(self):
        pm = self.load(PIPELINE).roles["pm"]
        return pm, delegate.plan(
            pm.run,
            delegate.Spec(
                "x",
                self.work,
                runtime="claude",
                sid=SID,
                out=os.path.join(self.work, "out.md"),
                cwd=self.work,
            ),
        )

    def test_team_dirs_change_no_orchestrator_run(self):
        before = self.seen()
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f"team_dirs = {json.dumps([self.team])}\n" + CORE_LOCAL + "[roles.writer]\ntier = 3\n",
        )
        after = self.seen()
        self.assertEqual(after, before)
        self.assertNotIn("one-pager", after[1].prompt)
        self.assertFalse(os.path.exists(self.work))


class RealConfig(Case):
    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.local = os.path.join(self.agent_pm, "config.local.toml")

    def load(self, local=None):
        if local is not None:
            write(self.local, CORE_TOP + local + "\n" + CORE_TABLES)
        return config.load()

    def test_committed_config_holds_no_deployment_values(self):
        with open(config.CONFIG, "rb") as f:
            committed = tomllib.load(f)
        self.assertEqual(
            {k: committed[k] for k in DEPLOYMENT},
            {
                **dict.fromkeys(("team", "harness_key", "task_label_group"), ""),
                "states": dict.fromkeys(config.STATES, ""),
                "human_members": [],
                **dict.fromkeys(("task_labels", "project_repos", "local_clones"), {}),
                "work_dir": "~/.agent-pm",
            },
        )
        self.assertEqual(
            {r: {k: p[k] for k in ("account", "key")} for r, p in committed["roles"].items()},
            dict.fromkeys(("researcher", "pm", "engineer"), {"account": "", "key": ""}),
        )

    def test_without_local_names_what_is_missing(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load()
        self.assertEqual(
            str(cm.exception),
            "linear-orchestrator/config.toml: "
            + missing(
                "team, harness_key, task_label_group, states.todo, states.in_progress, "
                "states.in_review, states.handoff, states.done, states.canceled, "
                "roles.researcher.account, roles.pm.account, roles.engineer.account, "
                "roles.researcher.key, roles.pm.key, roles.engineer.key"
            ),
        )

    def test_real_config(self):
        cfg = self.load(HEADER + role("researcher") + role("pm") + role("engineer"))
        self.assertEqual(
            {r: (*v, cfg.roles[r].require_instructions) for r, v in fields(cfg.roles).items()},
            {
                "researcher": (*ROLES["researcher"][:3], 3, "pm", True),
                "pm": (*ROLES["pm"][:3], 3, "engineer", False),
                "engineer": (*ROLES["engineer"][:3], 3, None, True),
            },
        )
        self.assertEqual(list(cfg.roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            cfg.docs,
            config.Docs(
                "ophis/private_docs", "main", {"researcher": "Research/", "pm": "Product Design/"}
            ),
        )
        self.assertEqual(
            (cfg.team, cfg.task_labels, cfg.project_repos, cfg.local_clones), (TEAM, {}, {}, {})
        )
        self.assertEqual(cfg.stages, {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertEqual(
            {r: (x.run.gate, x.run.task, x.run.language) for r, x in cfg.roles.items()},
            {
                "researcher": (GATE, "", "Chinese"),
                "pm": ("", "", "Chinese"),
                "engineer": ("", "", "Chinese"),
            },
        )

    def test_local_lines_uncommented_set_every_required_key(self):
        with open(config.CONFIG) as f:
            local = "".join(
                line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
            )
        with self.assertRaises(config.ConfigError) as cm:
            self.load(local)
        zero = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(
            str(cm.exception),
            f"linear-orchestrator/config.toml: project_repos.{zero} must map a Linear project id "
            "(UUID) to <owner>/<name>: '<owner>/<name>'",
        )


class LocalClones(Clones):
    def test_entries_stored_as_realpaths(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        real = os.path.join(home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.dir, "link"))
        git("init", "-q", real)
        git("-C", real, "remote", "add", "origin", HTTPS)
        cfg = self.load(
            f'work_dir = "{self.dir}/w"\n'
            + PIPELINE
            + f'[local_clones]\n"{SLUG}" = "{self.dir}/link"\n'
        )
        self.assertEqual(cfg.local_clones, {SLUG: real})


class CloneError(Clones):
    def test_matching_origin(self):
        for name, origin, slug in (
            ("https", HTTPS, SLUG),
            ("scp", "git@github.com:ophis/agent-pm.git", SLUG),
            ("bare", "https://github.com/ophis/agent-pm", SLUG),
            ("case", HTTPS, "OPHIS/Agent-PM"),
            ("origin case", "https://GitHub.com/Ophis/AGENT-PM.git", SLUG),
        ):
            with self.subTest(name):
                self.assertIsNone(config.clone_error(self.clone(name, origin), slug))

    def test_reasons(self):
        bad = self.bad() + [
            (repr(path), path, f"{path!r} has a non-printable character")
            for path in ("/x\ny", "/x\x85y", "/x\u2028y")
        ]
        for name, path, reason in bad:
            with self.subTest(name):
                self.assertEqual(config.clone_error(path, SLUG), reason)

    def test_run_failures_are_reasons(self):
        clone = self.clone()
        for error in (
            OSError("boom"),
            subprocess.TimeoutExpired(["git"], 10),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ):

            def run(argv, timeout):
                raise error

            with self.subTest(type(error).__name__):
                self.assertEqual(config.clone_error(clone, SLUG, run=run), f"git: {error}")

    def test_git_runs_guarded(self):
        clone, calls = self.clone(), []

        def run(argv, timeout):
            calls.append((argv, timeout))
            return subprocess.CompletedProcess(argv, 0, HTTPS + "\n", "")

        self.assertIsNone(config.clone_error(clone, SLUG, run=run))
        self.assertEqual(
            calls, [(["git", *GUARD, "-C", clone, "remote", "get-url", "origin"], SHORT)]
        )


class LocalClonesRunnable(Clones):
    def runs(self, key, path):
        return self.load(PIPELINE + f'[local_clones]\n"{key}" = {json.dumps(path)}\n').roles

    def test_each_entry_is_checked_and_a_bad_one_names_its_key(self):
        self.assertEqual(list(self.runs(SLUG, self.clone())), ["researcher", "pm", "engineer"])
        for name, path, reason in self.bad():
            with self.subTest(name):
                self.fails(
                    f"linear-orchestrator/config.toml: local_clones.{SLUG}: {reason}",
                    PIPELINE + f'[local_clones]\n"{SLUG}" = {json.dumps(path)}\n',
                )

    def test_no_entries_runs_no_git(self):
        with mock.patch.object(subprocess, "run") as clone_error:
            self.assertEqual(list(self.load(PIPELINE).roles), ["researcher", "pm", "engineer"])
        clone_error.assert_not_called()


class Writable(Case):
    def test_work_then_the_temp_dirs(self):
        self.assertEqual(config.writable("/w"), ("/w", *temp_dirs()))
        with self.assertRaises(TypeError):
            config.writable()


class RepoSlug(Case):
    def test_slug(self):
        cases = {
            "ophis/agent-pm": ("ophis", "agent-pm"),
            "ophis/.github": ("ophis", ".github"),
            "-a/b": None,
            "a/-b": None,
            "a/..": None,
            "a/.": None,
            "a b/c": None,
            "ophis": None,
            "https://github.com/ophis/x": None,
            "": None,
            None: None,
            42: None,
        }
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(config.repo_slug(value), want)


class WorkDir(ConfigFile, Case):
    def test_unset_is_in_home(self):
        work = os.path.realpath(self.agent_pm)
        cfg = self.load(PIPELINE)
        self.assertEqual(
            (cfg.work_dir, cfg.runs_dir, cfg.logs_dir, cfg.runs_log, cfg.run_dir("TASK-9")),
            (
                work,
                work + "/work",
                work + "/logs",
                work + "/logs/runs.jsonl",
                work + "/work/TASK-9",
            ),
        )
        home = os.path.dirname(work)
        self.fails(
            "linear-orchestrator/config.toml: work_dir must neither lie in nor contain "
            f"local_clones.ophis/x {home}: '~/.agent-pm'",
            PIPELINE + '[local_clones]\n"ophis/x" = "~"\n',
        )

    def test_set(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        self.assertEqual(
            self.load('work_dir = "~/d"\n' + PIPELINE).work_dir, os.path.join(home, "d")
        )
        for name in ("d-c", "c"):
            path = os.path.join(home, name)
            git("init", "-q", path)
            git(
                "-C",
                path,
                "remote",
                "add",
                "origin",
                f"https://github.com/ophis/{'x' if name == 'd-c' else 'y'}.git",
            )
        cfg = self.load(
            f'work_dir = "{home}/d"\n'
            + PIPELINE
            + f'[local_clones]\n"ophis/x" = "{home}/d-c"\n"ophis/y" = "{home}/c"\n'
        )
        self.assertEqual(cfg.work_dir, f"{home}/d")

    def test_refused(self):
        real = os.path.realpath(config.ROOT)
        d = os.path.realpath(self.dir)
        link, clink = os.path.join(d, "link"), os.path.join(d, "clink")
        os.symlink(real, link)
        os.makedirs(os.path.join(d, "c"))
        os.symlink(os.path.join(d, "c"), clink)
        repo = f"work_dir must neither lie in nor contain the repo {real}: "

        def clone(path):
            return f'[local_clones]\n"ophis/x" = "{path}"\n'

        def in_clone(path):
            return f"work_dir must neither lie in nor contain local_clones.ophis/x {path}: "

        cases = [
            (f'work_dir = "{real}"\n', "", f"{repo}{real!r}"),
            (f'work_dir = "{real}/w"\n', "", f"{repo}{real + '/w'!r}"),
            (f'work_dir = "{link}/w"\n', "", f"{repo}{link + '/w'!r}"),
            (f'work_dir = "{os.path.dirname(real)}"\n', "", f"{repo}{os.path.dirname(real)!r}"),
            (f'work_dir = "{d}/c"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{d + '/c'!r}"),
            (f'work_dir = "{clink}"\n', clone(f"{d}/c/x"), f"{in_clone(d + '/c/x')}{clink!r}"),
            (f'work_dir = "{clink}/w"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{clink + '/w'!r}"),
            ('work_dir = "w"\n', "", "work_dir must be a printable absolute or ~ path: 'w'"),
            ("work_dir = 5\n", "", "work_dir must be a printable absolute or ~ path: 5"),
        ]
        for text, tail, msg in cases:
            with self.subTest(text=text, tail=tail):
                self.fails(f"linear-orchestrator/config.toml: {msg}", text + BASE + tail)


class PathSeam(Case):
    def path(self, home, env):
        """config.path() with HOME `home` and AGENT_PM_PATH as in `env` (unset when absent)."""
        rest = {k: v for k, v in os.environ.items() if k != "AGENT_PM_PATH"}
        with mock.patch.dict(os.environ, {**rest, "HOME": home, **env}, clear=True):
            return config.path()

    def test_agent_pm_path_goes_in_front_empty_or_unset_is_the_default(self):
        for home in (self.home, os.path.join(self.home, "other")):
            default = f"/opt/homebrew/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
            for env, front in (
                ({"AGENT_PM_PATH": "/fakes/bin:/more"}, "/fakes/bin:/more:"),
                ({}, ""),
                ({"AGENT_PM_PATH": ""}, ""),
            ):
                with self.subTest(home=home, env=env):
                    self.assertEqual(self.path(home, env), front + default)

    def test_importing_reads_no_config_home_or_environment(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "AGENT_PM_PATH": "/before/bin"}
            res = subprocess.run(
                [*PY, "-c", IMPORT_ALONE],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                env=child_env(env, ORCH_ROOT),
            )
            self.assertEqual((res.returncode, res.stderr), (0, ""))
            self.assertEqual(
                json.loads(res.stdout),
                {
                    "read": [],
                    "kept": [],
                    "path": (
                        "/later/bin:/opt/homebrew/bin:/later/.local/bin:/usr/local/bin:/usr/"
                        "bin:/bin"
                    ),
                },
            )
            self.assertEqual(os.listdir(home), [])


class Shell(Case):
    def test_sh_run(self):
        with (
            mock.patch.object(subprocess, "run", return_value="res") as run,
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "KEEP": "1"}),
        ):
            self.assertEqual(config.sh_run(["git", "status"], 5), "res")
            env = {**os.environ, "PATH": config.path()}
        run.assert_called_once_with(
            ["git", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self.assertEqual(env["KEEP"], "1")
