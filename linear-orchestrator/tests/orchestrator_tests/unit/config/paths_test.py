import json
import os
import subprocess
import tempfile
from unittest import mock

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import temp_dirs
from orchestrator_tests.case import ORCH_ROOT, PY, Case, child_env
from orchestrator_tests.unit.config.data import (
    BASE,
    CORE_LOCAL,
    ID,
    IMPORT_ALONE,
    PIPELINE,
    SID,
    STEPS,
    A,
    B,
    git,
    write,
)
from orchestrator_tests.unit.config.fixtures import ConfigFile


class TeamDirectoryIsolation(ConfigFile, Case):
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


class WritableDirectories(Case):
    def test_work_then_the_temp_dirs(self):
        self.assertEqual(config.writable("/w"), ("/w", *temp_dirs()))
        with self.assertRaises(TypeError):
            config.writable()


class RepositoryNames(Case):
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


class WorkDirectories(ConfigFile, Case):
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


class ExecutablePath(Case):
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


class ShellExecution(Case):
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


class TuiSessionNames(Case):
    def test_prefix_of_a_role_and_issue(self):
        for role_name in ("engineer", "dummy-tester", "pm2"):
            with self.subTest(role=role_name):
                self.assertEqual(config.tui_prefix(role_name, ID), f"{role_name}-{ID}-")
        self.assertEqual(config.session("pm", ID), f"agent-pm-pm-{ID}")

    def test_a_prefix_outside_the_contract(self):
        for role_name, ident in (
            ("Engineer", ID),
            ("eng_x", ID),
            ("", ID),
            ("2pm", ID),
            ("engineer", "task-12"),
            ("engineer", "TASK-12 "),
        ):
            with (
                self.subTest(role=role_name, ident=ident),
                self.assertRaisesRegex(ValueError, "TUI session prefix"),
            ):
                config.tui_prefix(role_name, ident)
        with self.assertRaises(ValueError) as cm:
            config.tui_prefix("Engineer", ID)
        self.assertEqual(
            str(cm.exception),
            r"TUI session prefix 'Engineer-TASK-12': want ([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)",
        )

    def test_the_name_round_trips(self):
        self.assertEqual(config.TUI.pattern, r"([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)-[0-9a-f]{8}")
        self.assertEqual(config.tui_name("engineer", ID, SID), A)
        self.assertEqual(
            config.tui_issue(config.tui_name("dummy-tester", "T-1", SID)), ("dummy-tester", "T-1")
        )

    def test_found_by_name_only(self):
        found = {
            A: ("engineer", ID),
            B: ("dummy-tester", ID),
            "pm-TASK-12-cccccccc": ("pm", ID),
            "engineer-TASK-1-aaaaaaaa": ("engineer", "TASK-1"),
            "pm-T-1-aaaaaaaa": ("pm", "T-1"),
        }
        others = [
            "Engineer-TASK-12-bbbbbbbb",
            "agent-pm-engineer-TASK-12",
            "agent-pm-engineer-TASK-12345678",
            "engineer-0b6f2c1e",
            "engineer-TASK-12-0B6F2C1E",
            "engineer-TASK-12-0b6f2c1",
            "engineer-task-12-0b6f2c1e",
            "mine",
            "pm-T-1-aaaaaaaa-drive",
            "agent-pm-pm-T-1",
        ]
        for name, want in [*found.items(), *((o, None) for o in others)]:
            with self.subTest(name):
                self.assertEqual(config.tui_issue(name), want)
