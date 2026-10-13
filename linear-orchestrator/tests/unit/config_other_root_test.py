import builtins
import os
import shlex
from unittest import mock

from agent_pm import delegate, utils
from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.case import REPO, ROOTS
from orchestrator_test_helpers.unit.board_ids import HEADER, role
from orchestrator_test_helpers.unit.config.data import (
    CORE_TOML,
    DESIGN,
    DIRS,
    PIPELINE,
    old_key,
    write,
)
from orchestrator_test_helpers.unit.config.other_root_fixtures import OtherRoot as OtherRootBase


class OtherRoot1(OtherRootBase):
    def test_tasks_are_the_core_configs_index(self):
        index = delegate.task_index
        with mock.patch.object(
            delegate,
            "task_index",
            side_effect=lambda r, cfg, **kw: (
                {"deep-research": ""} if r == "researcher" else index(r, cfg=cfg, **kw)
            ),
        ) as fake:
            self.assertEqual(self.load(PIPELINE).roles["researcher"].tasks, ("deep-research",))
        core = utils.read_project("delegate", path=self.core_path)
        self.assertEqual(
            fake.call_args_list,
            [
                mock.call(r, config=self.core_path, local=config.LOCAL, cfg=core)
                for r in ("researcher", "pm", "engineer")
            ],
        )

    def test_reads_only_the_paths_passed_and_the_projects_config_files(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), "[[[ not toml")
        path, local = (os.path.join(self.dir, n) for n in ("config.toml", "config.local.toml"))
        write(path, PIPELINE)
        write(local, 'language = "French"\n' + role("engineer", "max_runs = 2"))
        with mock.patch.object(builtins, "open", side_effect=open) as opened:
            cfg = config.load(self.root, path=path, local=local, core=self.core_path)
        projects = {os.path.join(os.path.dirname(root), "config.toml") for root in ROOTS}
        self.assertEqual(
            {c.args[0] for c in opened.call_args_list if str(c.args[0]).endswith(".toml")}
            - projects,
            {path, local, self.core_path},
        )
        engineer = cfg.roles["engineer"]
        self.assertEqual((engineer.max_runs, engineer.run.language), (2, "French"))

    def test_load_reads_each_config_once_passing_the_read_table_on(self):
        path, local = (os.path.join(self.dir, n) for n in ("config.toml", "config.local.toml"))
        write(path, PIPELINE)
        write(local, "")
        with (
            mock.patch.object(builtins, "open", side_effect=open) as opened,
            mock.patch.object(utils, "read_project", wraps=utils.read_project) as read,
        ):
            config.load(self.root, path=path, local=local, core=self.core_path)
        paths = [c.args[0] for c in opened.call_args_list]
        session = os.path.join(REPO, "session", "config.toml")
        self.assertEqual(
            [paths.count(p) for p in (path, self.core_path, session)], [1, 1, 3], paths
        )
        self.assertEqual(
            sorted(c.args[0] for c in read.call_args_list),
            ["delegate", "linear-orchestrator"],
        )

    def test_an_unknown_harness_host_or_delegates_key_stops_the_caller(self):
        for text, want in (
            ('[delegates]\nharness_host = "nope"\n', "[delegates] harness_host: want one of tmux"),
            ("[delegates]\nkind = 1\n", "[delegates] kind: the delegate takes only harness_host"),
        ):
            with self.subTest(text):
                self.core(CORE_TOML + text)
                self.fails(f"delegate config: {want}", PIPELINE)

    def test_an_index_error_stops_the_caller(self):
        error = delegate.ComposerConfigError(
            "roles/researcher.md: lists 'ghost' without tasks/ghost.md"
        )
        with mock.patch.object(delegate, "task_index", side_effect=error):
            self.fails("core: roles/researcher.md: lists 'ghost' without tasks/ghost.md", PIPELINE)

    def test_overlay_fills_root(self):
        cfg = self.load(
            PIPELINE + '[core]\ntier = 3\ncommands = ["ls {{root}}/a", "true"]\n'
            '[core.roles.researcher]\ngate = "python3 {{root}}/x --y {{root}}"\n[core.roles.pm]\nlanguage = "French"\n'
        )
        q = shlex.quote(self.root)
        self.assertTrue(q.startswith("'"), q)
        run = cfg.roles["researcher"].run
        self.assertEqual(
            (run.tier, run.commands, run.gate), (3, [f"ls {q}/a", "true"], f"python3 {q}/x --y {q}")
        )
        run = cfg.roles["pm"].run
        self.assertEqual((run.gate, run.language), ("", "French"))

    def test_a_trusted_dir_holding_the_work_dir_stops_the_caller(self):
        work = os.path.join(os.path.realpath(os.path.dirname(self.root)), "state", "agent-pm")
        os.makedirs(work)
        text = f'work_dir = "{work}"\n' + PIPELINE
        for entry in (work, os.path.dirname(work)):
            with self.subTest(entry):
                self.core(f'trusted_dirs = ["{entry}"]\n' + CORE_TOML)
                self.fails(f"core: trusted_dirs: {entry} is or contains work_dir {work}", text)
        self.core(f'trusted_dirs = ["{os.path.join(work, "x")}", "{self.dir}/other"]\n' + CORE_TOML)
        self.assertEqual(self.load(text).work_dir, work)

    def test_load_configures_the_session_from_the_session_config(self):
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            '[adapters.a]\nimplementation = "adapters.a:AAdapter"\n[hosts.tmux]\nstatus_line = true\n',
        )
        cfg = self.load(PIPELINE)
        self.assertEqual(cfg.driver.registry.configured(), ["a", "claude", "codex", "print_prompt"])
        self.assertEqual(cfg.driver.registry.tui().status_line, True)

    def test_a_later_delegate_plan_keeps_the_settings_load_configured(self):
        path, local = (os.path.join(self.dir, n) for n in ("config.toml", "config.local.toml"))
        write(path, PIPELINE)
        write(local, "[hosts.tmux]\nstatus_line = true\n")
        cfg = config.load(self.root, path=path, local=local, core=self.core_path)
        pm = cfg.roles["pm"]
        work = os.path.join(self.dir, "work")
        delegate.plan(
            pm.run, delegate.Spec("x", work, runtime="claude", cwd=work), driver=cfg.driver
        )
        self.assertTrue(cfg.driver.registry.tui().status_line)

    def test_a_bad_session_config_stops_the_caller(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), '[hosts]\ndefault = "nope"\n')
        self.fails("session config: [hosts] default: want one of tmux", PIPELINE)

    def test_no_core_table(self):
        self.assertEqual(self.load(PIPELINE).roles["researcher"].run.gate, "")

    def test_overlay_unknown_keys(self):
        unknown = "core: linear-orchestrator/config.toml: unknown key "
        cases = [
            ("[core]\nfoo = 1\n", unknown + "'foo' in [core]"),
            ('[core]\nusers = ["octocat"]\n', unknown + "'users' in [core]"),
            ('[core]\ntrusted_dirs = ["~/x"]\n', unknown + "'trusted_dirs' in [core]"),
            ("[core]\nworkers_per_column = 2\n", unknown + "'workers_per_column' in [core]"),
            ('[core]\ngrid_retile = "off"\n', unknown + "'grid_retile' in [core]"),
            ('[core]\nteam_dirs = ["~/.agent-pm/team"]\n', unknown + "'team_dirs' in [core]"),
            ("[core.clients.claude]\nflags = []\n", unknown + "'clients' in [core]"),
            ("[core.roles.researcher]\nfoo = 1\n", unknown + "'foo' in [core.roles.researcher]"),
        ]
        cases += [
            (
                '[core.roles.researcher]\ndefault_task = "light-research"\nfoo = 1\n',
                old_key("default_task"),
            ),
            ('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n', old_key("tasks")),
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails(message, PIPELINE + text)

    def test_resolution_rejects_unknown_overlay_roles_and_output_keys(self):
        core = "core: linear-orchestrator/config.toml: "
        for text, message in (
            ("[core.roles.reseacher]\ntier = 2\n", "unknown role 'reseacher' in [core.roles]"),
            ('[core.output]\ntype = "local"\ntypo = 1\n', "unknown key 'typo' in [core.output]"),
            (
                '[core.roles.researcher.output]\ntype = "local"\ntypo = 1\n',
                "unknown key 'typo' in [core.roles.researcher.output]",
            ),
        ):
            with self.subTest(message):
                self.fails(core + message, PIPELINE + text)

    def test_core_errors(self):
        cases = [
            (
                'trusted_dirs = ["rel/dir"]\n' + CORE_TOML,
                PIPELINE,
                "core: trusted_dirs: 'rel/dir' is not an absolute or expandable ~ path",
            ),
            (
                CORE_TOML,
                PIPELINE + '[core]\ncwd = "rel"\n',
                "core: cwd must be a printable absolute or ~ path, got 'rel'",
            ),
            (
                CORE_TOML.replace("[roles.engineer]\n", "[roles.engineer]\ntier = 9\n"),
                PIPELINE,
                "core: tier must be an integer 1–4, got 9",
            ),
            (
                CORE_TOML,
                PIPELINE + '[core.roles.researcher]\ngate = "echo `id`"\n',
                "core: gate must be one line of shell command without backticks",
            ),
        ]
        for core, text, message in cases:
            with self.subTest(message):
                self.core(core)
                self.fails(message, text)

    def test_docs(self):
        self.assertEqual(self.load(PIPELINE).docs, config.Docs("acme/notes", "trunk", DIRS))
        self.assertEqual(
            self.load(HEADER + role("pm", 'next = "engineer"') + role("engineer")).docs,
            config.Docs("acme/notes", "trunk", {"pm": "Designs/"}),
        )
        self.core(CORE_TOML.replace(DESIGN, DESIGN.replace(" }", ', host = "github.com" }')))
        self.assertEqual(self.load(PIPELINE).docs, config.Docs("acme/notes", "trunk", DIRS))

    def test_docs_mismatch(self):
        message = (
            "core: document roles must publish to one github.com repo and branch (their [output] in "
            "~/.agent-pm/config.local.toml)"
        )
        for design in (
            DESIGN.replace("acme/notes", "acme/other"),
            DESIGN.replace('"trunk"', '"main"'),
            DESIGN.replace(" }", ', host = "ghe.example.com" }'),
        ):
            with self.subTest(design):
                self.core(CORE_TOML.replace(DESIGN, design))
                self.fails(message, PIPELINE)
        self.core(CORE_TOML)
        self.fails(message, HEADER + role("engineer"))
