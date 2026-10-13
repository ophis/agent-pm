import builtins
import os
import re
import shlex
from unittest import mock

from agent_pm import delegate, utils
from agent_pm.linear_orchestrator.engine import config
from orchestrator_tests.case import REPO, ROOTS, Case
from orchestrator_tests.unit.board_ids import HEADER, STATES, TEAM, role
from orchestrator_tests.unit.config_data import (
    BASE,
    CORE_LOCAL,
    CORE_TOML,
    DESIGN,
    DIRS,
    LABEL1,
    LABEL2,
    P1,
    P2,
    PIPELINE,
    ROLES,
    fields,
    old_key,
    write,
)
from orchestrator_tests.unit.config_scenarios import ConfigFile


class Runnable(ConfigFile, Case):
    def runs(self, text=PIPELINE):
        return self.load(text).roles

    def test_roles_in_pipeline_order_tasks_from_the_index(self):
        roles = self.runs()
        self.assertEqual(fields(roles), ROLES)
        self.assertEqual(list(roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            {r: x.tasks for r, x in roles.items()},
            {r: tuple(delegate.task_index(r)) for r in roles},
        )
        roles = self.runs(HEADER + role("engineer") + role("researcher", "max_runs = 3"))
        self.assertEqual(
            [(r, x.max_runs) for r, x in roles.items()], [("engineer", 1), ("researcher", 3)]
        )
        text = PIPELINE + f'[task_labels]\nlight-research = "{LABEL1}"\nbuild = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])

    def test_tasks_by_role(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        self.assertEqual(
            {r: (x.kind, x.prefix, x.files) for r, x in config.TASKS.items()},
            {
                "researcher": ("research", "", False),
                "pm": ("design", "PRD", False),
                "engineer": ("build", "ENG", True),
            },
        )

    def test_rejected(self):
        cases = [
            (PIPELINE + role("ghost"), "role 'ghost' is not in delegate/config.toml"),
            (PIPELINE + role("dummy-tester"), "role 'dummy-tester' has no entry in config.TASKS"),
            (
                HEADER + role("researcher", 'next = "pm"'),
                "next of 'researcher' names undefined role 'pm'",
            ),
            (
                HEADER + role("researcher") + role("pm", 'next = "researcher"'),
                "next of 'pm' is role 'researcher', whose config.TASKS entry has no prefix",
            ),
            (
                PIPELINE.replace("linear-api-key-pm", "linear-api-key-researcher"),
                "[roles.pm] key 'linear-api-key-researcher' is also [roles.researcher]'s",
            ),
            (
                PIPELINE.replace("linear-api-key-engineer", "linear-api-key"),
                "[roles.engineer] key 'linear-api-key' is harness_key",
            ),
            *[
                (
                    text + f'[task_labels]\n{task} = "{LABEL1}"\n',
                    (
                        f"task_labels.{task}"
                        f" is not a task of a role in linear-orchestrator/config.toml"
                    ),
                )
                for task, text in (
                    ("echo", PIPELINE),
                    ("ghost", PIPELINE),
                    ("product-design", HEADER + role("researcher")),
                )
            ],
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails("linear-orchestrator/config.toml: " + message, text)

    def test_role_for(self):
        roles = self.runs()
        for email in ("researcher@agents.test", "RESEARCHER@Agents.Test"):
            self.assertEqual(config.role_for(roles, email), "researcher")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(config.role_for(roles, email))

    def test_clones_match_core(self):
        self.assertEqual(config.CLONES, ("src", "publish"))
        with open(delegate.CONFIG) as f:
            dirs = re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read())
        self.assertTrue(dirs)
        self.assertEqual(set(dirs), {config.CLONES[0]})
        with open(
            os.path.join(
                REPO,
                "delegate",
                "src",
                "agent_pm",
                "delegate",
                "team",
                "output",
                "destinations",
                "github.md",
            )
        ) as f:
            self.assertIn(f"`<Workdir>/{config.CLONES[1]}`", f.read())
        self.assertEqual(
            delegate.CONFIG,
            os.path.join(config.ROOT, "delegate", "config.toml"),
        )


class OtherRoot(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.agent_pm, "config.local.toml"))
        self.root = os.path.join(self.dir, "my root")
        self.dir = os.path.join(self.root, "linear-orchestrator")
        os.makedirs(self.dir)
        os.makedirs(os.path.join(self.root, "delegate"))
        self.core_path = os.path.join(self.root, "delegate", "config.toml")
        self.core(CORE_TOML)

    def core(self, text):
        write(self.core_path, text)

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(self.root, path=path, core=self.core_path)

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
            PIPELINE
            + (
                '[core]\ntier = 3\ncommands = ["ls {{root}}/a", "true"]\n'
                '[core.roles.researcher]\ngate = "python3 {{root}}/x --y {{root}}"\n'
                '[core.roles.pm]\nlanguage = "French"\n'
            )
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
            (
                '[adapters.a]\nimplementation = "adapters.a:AAdapter"\n[hosts.tmux]\n'
                "status_line = true\n"
            ),
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
            "core: document roles must publish to one github.com repo and branch "
            "(their [output] in ~/.agent-pm/config.local.toml)"
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


class Local(ConfigFile, Case):
    def test_local_on_top(self):
        self.local(
            HEADER
            + 'human_members = ["b@x.com"]\n'
            + role("researcher", "max_runs = 2")
            + role("pm")
            + f'[project_repos]\n"{P2}" = "ophis/y"\n'
        )
        cfg = self.load(
            f'human_members = ["a@x.com", "c@x.com"]\n[roles.researcher]\nnext = '
            f'"pm"\nmax_runs = 3\n[project_repos]\n"{P1}'
            f'" = "ophis/x"\n[core.roles.pm]\ntier = 1\n'
        )
        self.assertEqual((cfg.team, cfg.states, cfg.human_members), (TEAM, STATES, ("b@x.com",)))
        self.assertEqual(
            fields(cfg.roles),
            {
                "researcher": (
                    "researcher@agents.test",
                    "linear-api-key-researcher",
                    ROLES["researcher"][2],
                    2,
                    "pm",
                ),
                "pm": ("pm@agents.test", "linear-api-key-pm", ROLES["pm"][2], 1, None),
            },
        )
        self.assertEqual(cfg.project_repos, {P1: "ophis/x", P2: "ophis/y"})
        self.assertEqual(cfg.roles["pm"].run.tier, 1)

    def test_local_alone_and_no_local(self):
        self.local(PIPELINE)
        self.assertEqual(self.load("").roles, self.load(PIPELINE).roles)
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.assertEqual(self.load(PIPELINE).team, TEAM)

    def test_an_empty_local_role_table_adds_no_role(self):
        self.local(PIPELINE)
        want = set(self.load("").roles)
        self.local(PIPELINE + "[roles.ghost]\n")
        self.assertEqual(set(self.load("").roles), want)

    def test_old_task_keys_in_the_local_overlay_stop_runnable(self):
        self.local('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n')
        self.fails(old_key("tasks"), PIPELINE)

    def test_a_bad_local_value_is_rejected(self):
        self.local(f'team = "{TEAM}"\nstates = {{ done = "Done" }}\n')
        self.fails(
            (
                "linear-orchestrator/config.toml: states.done must be a Linear workflow"
                " state id (UUID): 'Done'"
            ),
            BASE,
        )
