import dataclasses
import json
import os
import subprocess
from unittest import mock

from agent_pm import delegate, utils
from agent_pm.linear_orchestrator.engine import config
from orchestrator_tests.case import ORCH_ROOT, PY, Case, child_env
from orchestrator_tests.unit.board_ids import HEADER, STATES, TASK_GROUP, TEAM, role
from orchestrator_tests.unit.config_data import (
    BASE,
    CORE,
    LABEL,
    LABEL1,
    LABEL2,
    P1,
    P2,
    PIPELINE,
    SLUG,
    STATES_LINE,
    missing,
    write,
)
from orchestrator_tests.unit.config_scenarios import ConfigFile


class Config(ConfigFile, Case):
    def test_tests_never_read_this_machines_local_file(self):
        self.assertEqual(
            os.path.expanduser(config.LOCAL),
            os.path.join(self.agent_pm, "config.local.toml"),
        )

    def test_stage_order(self):
        self.assertEqual(self.load(PIPELINE).stages, {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertEqual(
            self.load(
                HEADER + role("researcher") + role("pm", 'next = "engineer"') + role("engineer")
            ).stages,
            {"researcher": 0, "pm": 0, "engineer": 1},
        )

    def test_rejected(self):
        body = BASE[BASE.index("[roles") :]
        group = "task_label_group must be a Linear label group id (UUID): "
        label = BASE + f'[task_labels]\nlight-research = "{LABEL1}"\n'
        upper = "ABCDEF00-0000-4000-8000-000000000021"
        solo = role("solo")
        x = BASE + "[roles.x]\n"
        cases = [
            (
                BASE + role("a", 'next = "b"') + role("b", 'next = "a"'),
                "the next chain from 'a' has a cycle",
            ),
            (
                HEADER
                + 'docs = { repo = "acme/notes", clone = "/nonexistent/notes", branch = "trunk" }\n'
                + body,
                "unknown keys: docs",
            ),
            (HEADER + '[projects.p]\nnext = "q"\n' + body, "unknown keys: projects"),
            (
                BASE + '[tasks.x]\nmodel = "opus"\n[docs]\nrepo = "acme/notes"\n',
                "unknown keys: docs, tasks",
            ),
            (
                BASE.replace(solo, role("solo", 'role = "x"', 'task = "y"')),
                "[roles.solo] has unknown keys: role, task",
            ),
            (
                BASE.replace(solo, role("solo", 'tasks = ["x"]', "read_only = []")),
                "[roles.solo] has unknown keys: read_only, tasks",
            ),
            (x + 'key = "k-x"\n', missing("roles.x.account")),
            (x + 'account = ""\nkey = "k-x"\n', missing("roles.x.account")),
            (
                x + 'account = 5\nkey = "k-x"\n',
                "[roles.x] account must be the role's Linear email: 5",
            ),
            (x + 'account = "x@agents.test"\n', missing("roles.x.key")),
            (
                BASE + "[roles.x]\n[roles.y]\nkey = 'k-y'\n",
                missing("roles.x.account, roles.y.account, roles.x.key"),
            ),
            (x + 'account = "x@agents.test"\nkey = ""\n', missing("roles.x.key")),
            (
                x + 'account = "x@agents.test"\nkey = ["k"]\n',
                "[roles.x] key must be a Keychain service name: ['k']",
            ),
            *[
                (
                    BASE + role("x", f"max_runs = {value}"),
                    f"[roles.x] max_runs must be a whole number >= 1: {shown}",
                )
                for shown, value in {
                    "0": "0",
                    "-1": "-1",
                    "'2'": '"2"',
                    "True": "true",
                    "1.5": "1.5",
                }.items()
            ],
            (BASE.replace('harness_key = "linear-api-key"\n', ""), missing("harness_key")),
            (
                BASE.replace('harness_key = "linear-api-key"', "harness_key = 1"),
                "harness_key must be a Keychain service name: 1",
            ),
            (
                BASE.replace(TEAM, "Frank's Agents"),
                'team must be a Linear team id (UUID): "Frank\'s Agents"',
            ),
            (BASE.replace(f'"{TEAM}"', "5"), "team must be a Linear team id (UUID): 5"),
            (BASE.replace(f'team = "{TEAM}"\n', ""), missing("team")),
            (
                body,
                missing(
                    "team, harness_key, task_label_group, states.todo, states.in_progress, "
                    "states.in_review, states.handoff, states.done, states.canceled"
                ),
            ),
            (
                BASE.replace(STATES_LINE, ""),
                missing(
                    "states.todo, states.in_progress, states.in_review, states.handoff, "
                    "states.done, states.canceled"
                ),
            ),
            (
                BASE.replace(STATES_LINE, 'states = "x"\n'),
                missing(
                    "states.todo, states.in_progress, states.in_review, "
                    "states.handoff, states.done, states.canceled"
                ),
            ),
            (BASE.replace(f', canceled = "{STATES["canceled"]}"', ""), missing("states.canceled")),
            (BASE.replace(" }\n", ', backlog = "x" }\n', 1), "[states] has unknown keys: backlog"),
            (
                BASE.replace(STATES["done"], "Done"),
                "states.done must be a Linear workflow state id (UUID): 'Done'",
            ),
            (BASE.replace(f'task_label_group = "{TASK_GROUP}"\n', ""), missing("task_label_group")),
            (BASE.replace(TASK_GROUP, "not-a-uuid"), group + "'not-a-uuid'"),
            (
                BASE.replace(TASK_GROUP, "ABCDEF00-0000-4000-8000-000000000002"),
                group + "'ABCDEF00-0000-4000-8000-000000000002'",
            ),
            (BASE.replace(f'"{TASK_GROUP}"', "5"), group + "5"),
            *[
                (
                    HEADER + f"task_labels = {value}\n" + body,
                    'task_labels must be a table of <task> = "<Linear label id>"',
                )
                for value in ('"x"', "[1]", "5")
            ],
            *[
                (
                    label + f"deep-research = {value}\n",
                    f"task_labels.deep-research must be a Linear label id (UUID): {shown}",
                )
                for shown, value in {
                    "'Light Research'": '"Light Research"',
                    repr(upper): f'"{upper}"',
                    "5": "5",
                    "['x']": '["x"]',
                    "''": '""',
                    "True": "true",
                }.items()
            ],
            (
                label + f'other = "{LABEL2}"\ndeep-research = "{LABEL1}"\n',
                (
                    f"task_labels.light-research and task_labels.deep-research have the same"
                    f" label id {LABEL1}"
                ),
            ),
            ('logs_dir = "/x"\n' + BASE, "unknown keys: logs_dir"),
            (
                HEADER + 'project_repos = "x"\n' + body,
                'project_repos must be a table of "<Linear project id>" = "<owner>/<name>"',
            ),
            *[
                (
                    HEADER + f"local_clones = {value}\n" + body,
                    'local_clones must be a table of "<owner>/<name>" = "<path>"',
                )
                for value in ('"/x"', '["/x"]', "5")
            ],
            (
                BASE + f'[local_clones]\n"{SLUG}" = "/x"\n"Ophis/Agent-PM" = "/y"\n',
                f"local_clones.{SLUG} and local_clones.Ophis/Agent-PM are the same repo",
            ),
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                self.fails("linear-orchestrator/config.toml: " + message, text)

    def test_bad_table_entry_rejected(self):
        cases = [
            ("project_repos", key, value)
            for key, value in (
                ("not-a-uuid", "ophis/x"),
                (P1.upper(), "ophis/x"),
                (P1, "https://github.com/ophis/x"),
                (P1, "ophis/."),
                (P1, "ophis/.."),
                (P1, ""),
                (P1, 42),
                (P1, "ophis"),
            )
        ]
        cases += [
            ("local_clones", key, value)
            for key, value in (
                ("agent-pm", "/x"),
                ("https://github.com/ophis/agent-pm", "/x"),
                ("ophis/..", "/x"),
                (SLUG, 42),
                (SLUG, True),
                (SLUG, ["/x"]),
                (SLUG, ""),
                (SLUG, "x/y"),
                (SLUG, "./x"),
                (SLUG, " /x"),
                (SLUG, "/x\ny"),
                (SLUG, "/x\x85y"),
                (SLUG, "/x\u2028y"),
                (SLUG, "/x\u2029y"),
                (SLUG, "/x\ty"),
                (SLUG, "/x\x00y"),
            )
        ]
        for table, key, value in cases:
            with self.subTest(table=table, key=key, value=value):
                with self.assertRaises(config.ConfigError) as cm:
                    self.load(BASE + f'[{table}]\n"{key}" = {json.dumps(value)}\n')
                msg = str(str(cm.exception))
                self.assertTrue(
                    msg.startswith(f"linear-orchestrator/config.toml: {table}.{key} "), msg
                )
                self.assertIn(repr(value), msg)

    def test_loads(self):
        cfg = self.load(PIPELINE)
        self.assertEqual(
            (cfg.team, cfg.states, cfg.harness_key, cfg.task_label_group),
            (TEAM, STATES, "linear-api-key", TASK_GROUP),
        )
        pm = cfg.roles["pm"]
        self.assertEqual(
            (pm.account, pm.key, pm.next, pm.max_runs, pm.require_instructions),
            ("pm@agents.test", "linear-api-key-pm", "engineer", 1, True),
        )
        self.assertEqual(
            (cfg.task_labels, cfg.project_repos, cfg.local_clones, cfg.human_members),
            ({}, {}, {}, ()),
        )
        cfg = self.load(
            PIPELINE.replace(
                role("engineer"), role("engineer", "max_runs = 3", "require_instructions = false")
            )
            + "[core.roles.pm]\ntier = 1\n"
        )
        self.assertEqual(
            (cfg.roles["engineer"].max_runs, cfg.roles["engineer"].require_instructions), (3, False)
        )
        self.assertEqual((cfg.roles["pm"].run.tier, cfg.roles["researcher"].run.tier), (1, 2))
        cfg = self.load(
            PIPELINE + f'[task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n'
        )
        self.assertEqual(cfg.task_labels, {"light-research": LABEL1, "deep-research": LABEL2})
        cfg = self.load(PIPELINE + f'[project_repos]\n"{P1}" = "ophis/x"\n"{P2}" = "ophis/x"\n')
        self.assertEqual(cfg.project_repos, {P1: "ophis/x", P2: "ophis/x"})

    def test_frozen(self):
        cfg = self.load(
            'human_members = ["a@x.com"]\n' + PIPELINE + f'[task_labels]\nbuild = "{LABEL1}"\n'
        )
        for obj, attr in (
            (cfg, "team"),
            (cfg, "roles"),
            (cfg, "work_dir"),
            (cfg.roles["pm"], "max_runs"),
            (cfg.docs, "repo"),
        ):
            with self.subTest(attr), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, attr, None)
        for name in ("states", "roles", "task_labels", "project_repos", "local_clones", "stages"):
            with self.subTest(name), self.assertRaises(TypeError):
                getattr(cfg, name)["x"] = 1
        with self.assertRaises(TypeError):
            cfg.docs.dirs["x"] = "y"
        self.assertEqual(
            (cfg.human_members, cfg.roles["pm"].tasks), (("a@x.com",), ("product-design",))
        )

    def test_one_resolve_and_one_index_per_role(self):
        with (
            mock.patch.object(delegate, "resolve_run", wraps=delegate.resolve_run) as resolve,
            mock.patch.object(delegate, "task_index", wraps=delegate.task_index) as index,
        ):
            cfg = self.load(PIPELINE + "[core.roles.pm]\ntier = 1\n")
        overlay = {"roles": {"pm": {"tier": 1}}}
        roles = ["researcher", "pm", "engineer"]
        core = utils.read_project("delegate")
        self.assertEqual(
            resolve.call_args_list,
            [
                mock.call(
                    r,
                    None,
                    overlays=[overlay],
                    label=LABEL,
                    config=CORE,
                    local=config.LOCAL,
                    cfg=core,
                )
                for r in roles
            ],
        )
        self.assertEqual(
            index.call_args_list,
            [mock.call(r, config=CORE, local=config.LOCAL, cfg=core) for r in roles],
        )
        self.assertEqual(
            {r: x.run for r, x in cfg.roles.items()},
            {r: delegate.resolve_run(r, overlays=[overlay], label=LABEL) for r in roles},
        )

    def test_a_config_error_carries_the_log_dir_once_work_dir_is_valid(self):
        e = self.fails(
            "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'",
            'work_dir = "w"\n' + PIPELINE,
        )
        self.assertIsNone(e.logs_dir)
        self.assertIsInstance(e, config.ConfigError)
        logs = os.path.join(os.path.realpath(self.agent_pm), "logs")
        for message, text in (
            (
                "linear-orchestrator/config.toml: " + missing("team"),
                PIPELINE.replace(f'team = "{TEAM}"\n', ""),
            ),
            (
                "linear-orchestrator/config.toml: role 'ghost' is not in delegate/config.toml",
                PIPELINE + role("ghost"),
            ),
        ):
            with self.subTest(message):
                e = self.fails(message, text)
                self.assertEqual((e.msg, e.logs_dir), (message, logs))

    def test_importing_reads_no_config_file(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), "[[[ not toml")
        code = (
            "import json\nfrom agent_pm.linear_orchestrator.engine import config\n"
            "try:\n    config.load()\nexcept config.ConfigError as e:\n    "
            "print(json.dumps([e.msg, e.logs_dir]))"
        )
        res = subprocess.run(
            [*PY, "-c", code],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            env=child_env(None, ORCH_ROOT),
        )
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        msg, logs = json.loads(res.stdout)
        self.assertTrue(msg.startswith("linear-orchestrator/config.toml: "), msg)
        self.assertIsNone(logs)
