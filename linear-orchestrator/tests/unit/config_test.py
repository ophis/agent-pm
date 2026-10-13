"""orchestrator config.py, ported from the old config_test (less its clients layer and transcript cases) onto load()'s
frozen Config, plus the old attended name cases (TUI names)."""

import dataclasses
import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

import agent_pm.delegate.engine.config
from agent_pm import delegate
from agent_pm.delegate import composer
from agent_pm.linear_orchestrator.engine import config
from agent_pm.session import api
from agent_pm.session.adapters import registry
from agent_pm.utils import config_file
from agent_pm.utils.git import GUARD
from agent_pm.utils.paths import import_roots
from agent_pm.utils.shell import SHORT

from ..case import ORCH_ROOT, PY, Case, child_env
from .board_ids import HEADER, STATES, TASK_GROUP, TEAM, role

BASE = (
    HEADER
    + role("researcher", 'next = "pm"')
    + role("pm", 'next = "engineer"')
    + role("engineer")
    + role("solo")
)
PIPELINE = (
    HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer")
)
CORE_TOP = 'language = "Chinese"\n'
CORE_TABLES = """[roles.researcher.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Research/"

[roles.pm.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Product Design/"
"""
CORE_LOCAL = CORE_TOP + "\n" + CORE_TABLES

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
P1, P2 = "a0000000-0000-4000-8000-000000000031", "b0000000-0000-4000-8000-000000000032"
SLUG = "ophis/agent-pm"
HTTPS = "https://github.com/ophis/agent-pm.git"
STATES_LINE = HEADER[HEADER.index("states = ") : HEADER.index("task_label_group")]
ROOT_Q = shlex.quote(config.ROOT)
GATE = (
    f"env PYTHONPATH={shlex.quote(os.pathsep.join(import_roots(config.ROOT)))} "
    "python3 -P -m agent_pm.linear_orchestrator.engine.router --brake researcher"
)
LABEL = ("linear-orchestrator/config.toml", "core")
CORE = agent_pm.delegate.engine.config.CONFIG
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"


def missing(keys):
    return (
        f"required, unset: {keys}: set them in ~/.agent-pm/config.local.toml (linear-orchestrator/config.toml's "
        "`# local:` lines)"
    )


def old_key(key):
    return (
        f"core: linear-orchestrator/config.toml: [core.roles.researcher] has {key!r}: task-level config and default_task "
        "are gone; set run keys in [core.roles.researcher] (a run without a task picks one)"
    )


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class ConfigFile:
    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def local(self, text):
        """~/.agent-pm/config.local.toml: `text` amid CORE_LOCAL (the delegate's keys)."""
        write(
            os.path.join(self.agent_pm, "config.local.toml"), CORE_TOP + text + "\n" + CORE_TABLES
        )

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(path=path)

    def fails(self, message, text):
        with self.assertRaises(config.ConfigError) as cm:
            self.load(text)
        self.assertEqual(cm.exception.code, message)
        return cm.exception


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
                    "team, harness_key, task_label_group, states.todo, states.in_progress, states.in_review, "
                    "states.handoff, states.done, states.canceled"
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
                f"task_labels.light-research and task_labels.deep-research have the same label id {LABEL1}",
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
                msg = str(cm.exception.code)
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
            mock.patch.object(composer, "resolve", wraps=composer.resolve) as resolve,
            mock.patch.object(composer, "index", wraps=composer.index) as index,
        ):
            cfg = self.load(PIPELINE + "[core.roles.pm]\ntier = 1\n")
        overlay = {"roles": {"pm": {"tier": 1}}}
        roles = ["researcher", "pm", "engineer"]
        core = config_file.read_project("delegate")
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
            {r: composer.resolve(r, overlays=[overlay], label=LABEL) for r in roles},
        )

    def test_a_config_error_carries_the_log_dir_once_work_dir_is_valid(self):
        e = self.fails(
            "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'",
            'work_dir = "w"\n' + PIPELINE,
        )
        self.assertIsNone(e.logs_dir)
        self.assertIsInstance(e, SystemExit)
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
            "try:\n    config.load()\nexcept config.ConfigError as e:\n    print(json.dumps([e.msg, e.logs_dir]))"
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


ROLES = {
    "researcher": (
        "researcher@agents.test",
        "linear-api-key-researcher",
        ("deep-research", "light-research"),
        1,
        "pm",
    ),
    "pm": ("pm@agents.test", "linear-api-key-pm", ("product-design",), 1, "engineer"),
    "engineer": (
        "engineer@agents.test",
        "linear-api-key-engineer",
        ("build", "light-build"),
        1,
        None,
    ),
}


def fields(roles):
    return {r: (x.account, x.key, x.tasks, x.max_runs, x.next) for r, x in roles.items()}


class Runnable(ConfigFile, Case):
    def runs(self, text=PIPELINE):
        return self.load(text).roles

    def test_roles_in_pipeline_order_tasks_from_the_index(self):
        roles = self.runs()
        self.assertEqual(fields(roles), ROLES)
        self.assertEqual(list(roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            {r: x.tasks for r, x in roles.items()}, {r: tuple(composer.index(r)) for r in roles}
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
                    f"task_labels.{task} is not a task of a role in linear-orchestrator/config.toml",
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
        with open(agent_pm.delegate.engine.config.CONFIG) as f:
            dirs = re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read())
        self.assertTrue(dirs)
        self.assertEqual(set(dirs), {config.CLONES[0]})
        with open(os.path.join(composer.TEAM, "output", "destinations", "github.md")) as f:
            self.assertIn(f"`<Workdir>/{config.CLONES[1]}`", f.read())
        self.assertEqual(
            agent_pm.delegate.engine.config.CONFIG,
            os.path.join(config.ROOT, "delegate", "config.toml"),
        )


CORE_TOML = """tier = 2
effort = "high"
runtime = "claude"
output = { type = "local" }

[roles.researcher]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Research/" }

[roles.pm]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }

[roles.engineer]
output = { type = "pull-request" }
"""
DESIGN = 'output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }'
DIRS = {"researcher": "Research/", "pm": "Designs/"}


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
        index = composer.index
        with mock.patch.object(
            composer,
            "index",
            side_effect=lambda r, cfg, **kw: (
                {"deep-research": ""} if r == "researcher" else index(r, cfg=cfg, **kw)
            ),
        ) as fake:
            self.assertEqual(self.load(PIPELINE).roles["researcher"].tasks, ("deep-research",))
        core = config_file.read_project("delegate", path=self.core_path)
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
        with mock.patch.object(config_file, "open", create=True, side_effect=open) as opened:
            cfg = config.load(self.root, path=path, local=local, core=self.core_path)
        projects = {
            os.path.join(config_file.ROOT, p, "config.toml")
            for level in config_file.LEVELS
            for p in level
        }
        self.assertEqual(
            {c.args[0] for c in opened.call_args_list} - projects, {path, local, self.core_path}
        )
        engineer = cfg.roles["engineer"]
        self.assertEqual((engineer.max_runs, engineer.run.language), (2, "French"))

    def test_load_reads_each_config_once_passing_the_read_table_on(self):
        path, local = (os.path.join(self.dir, n) for n in ("config.toml", "config.local.toml"))
        write(path, PIPELINE)
        write(local, "")
        with (
            mock.patch.object(config_file, "open", create=True, side_effect=open) as opened,
            mock.patch.object(config_file, "read_project", wraps=config_file.read_project) as read,
        ):
            config.load(self.root, path=path, local=local, core=self.core_path)
        paths = [c.args[0] for c in opened.call_args_list]
        session = os.path.join(config_file.ROOT, "session", "config.toml")
        self.assertEqual(
            [paths.count(p) for p in (path, self.core_path, session)], [1, 1, 3], paths
        )
        self.assertEqual(
            sorted(c.args[0] for c in read.call_args_list),
            ["delegate", "linear-orchestrator", "session"],
        )

    def test_an_unknown_harness_host_or_delegates_key_stops_the_caller(self):
        for text, want in (
            ('[delegates]\nharness_host = "nope"\n', "[delegates] harness_host: want one of tmux"),
            ("[delegates]\nkind = 1\n", "[delegates] kind: the delegate takes only harness_host"),
        ):
            with self.subTest(text):
                self.core(CORE_TOML + text)
                registry.reset()
                self.fails(f"core: {want}", PIPELINE)
                with self.assertRaisesRegex(api.ConfigError, "not configured"):
                    registry.configured()

    def test_an_index_error_stops_the_caller(self):
        error = composer.ConfigError("roles/researcher.md: lists 'ghost' without tasks/ghost.md")
        with mock.patch.object(composer, "index", side_effect=error):
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
        self.refused_unconfigured()
        self.load(PIPELINE)
        self.assertEqual(registry.configured(), ["a", "claude", "codex", "print_prompt"])
        self.assertEqual(registry.tui(), registry.TuiSettings(True, 3, "open-close"))

    def test_a_later_delegate_plan_keeps_the_settings_load_configured(self):
        path, local = (os.path.join(self.dir, n) for n in ("config.toml", "config.local.toml"))
        write(path, PIPELINE)
        write(local, "[hosts.tmux]\nstatus_line = true\n")
        pm = config.load(self.root, path=path, local=local, core=self.core_path).roles["pm"]
        work = os.path.join(self.dir, "work")
        delegate.plan(pm.run, delegate.Spec("x", work, runtime="claude", cwd=work))
        self.assertTrue(registry.tui().status_line)

    def test_a_bad_session_config_stops_the_caller(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), '[hosts]\ndefault = "nope"\n')
        self.fails("session: [hosts] default: want one of tmux", PIPELINE)
        self.refused_unconfigured()

    def refused_unconfigured(self):
        with self.assertRaisesRegex(api.ConfigError, "not configured"):
            registry.configured()

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


DEPLOYMENT = {
    "team",
    "states",
    "human_members",
    "harness_key",
    "task_label_group",
    "task_labels",
    "project_repos",
    "local_clones",
    "work_dir",
}


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
            'human_members = ["a@x.com", "c@x.com"]\n[roles.researcher]\nnext = "pm"\nmax_runs = 3\n'
            f'[project_repos]\n"{P1}" = "ophis/x"\n[core.roles.pm]\ntier = 1\n'
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
            "linear-orchestrator/config.toml: states.done must be a Linear workflow state id (UUID): 'Done'",
            BASE,
        )


STEPS = (
    "\n## Steps\n\n1. **Report progress:**\n   [agent-pm-progress:start] the topic\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)


class TeamDirsIgnored(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.team = os.path.realpath(os.path.join(self.agent_pm, "team"))
        writer = (
            "# Writer\n\nYou write.\n\n## Tasks\n\nPick the task below that fits the input; unsure → `one-pager` "
            "(`<tasks>/one-pager.md`).\n\n- `one-pager` (`<tasks>/one-pager.md`): a brief; always; light.\n"
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
        self.assertIn(
            "writer", composer.catalog(composer.team(composer.TEAM, composer.team_dirs()))
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
            cm.exception.code,
            "linear-orchestrator/config.toml: "
            + missing(
                "team, harness_key, task_label_group, states.todo, states.in_progress, states.in_review, states.handoff, "
                "states.done, states.canceled, roles.researcher.account, roles.pm.account, roles.engineer.account, "
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
            cm.exception.code,
            f"linear-orchestrator/config.toml: project_repos.{zero} must map a Linear project id "
            "(UUID) to <owner>/<name>: '<owner>/<name>'",
        )


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


class Clones(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.dir = os.path.realpath(self.dir)

    def clone(self, name="clone", origin=HTTPS):
        path = os.path.join(self.dir, name)
        git("init", "-q", path)
        if origin:
            git("-C", path, "remote", "add", "origin", origin)
        return path

    def bad(self):
        dotgit_file = os.path.join(self.dir, "file")
        os.makedirs(dotgit_file)
        write(os.path.join(dotgit_file, ".git"), "gitdir: /elsewhere\n")
        linked = os.path.join(self.dir, "linked")
        os.makedirs(linked)
        os.symlink(os.path.join(self.clone("real"), ".git"), os.path.join(linked, ".git"))
        missing = os.path.join(self.dir, "missing")
        no_origin = self.clone("no-origin", origin=None)
        gitlab = self.clone("gitlab", "https://gitlab.com/ophis/agent-pm.git")
        other = self.clone("other", "https://github.com/ophis/other.git")
        return [
            ("missing", missing, f"{missing} does not exist"),
            (".git a file", dotgit_file, f"{dotgit_file} is not a git clone"),
            (".git a symlink", linked, f"{linked} is not a git clone"),
            ("no origin", no_origin, f"{no_origin}: no origin URL naming host/owner/name"),
            (
                "non-github host",
                gitlab,
                "origin is gitlab.com/ophis/agent-pm, not github.com/ophis/agent-pm",
            ),
            (
                "other owner/name",
                other,
                "origin is github.com/ophis/other, not github.com/ophis/agent-pm",
            ),
        ]


class LocalClones(Clones):
    def test_entries_stored_as_realpaths(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        real = os.path.join(home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.dir, "link"))
        with mock.patch.object(config, "clone_error", return_value=None) as clone_error:
            cfg = self.load(
                f'work_dir = "{self.dir}/w"\n'
                + PIPELINE
                + f'[local_clones]\n"{SLUG}" = "{self.dir}/link"\n"ophis/x" = "~/real"\n"ophis/y" = "~"\n'
            )
        self.assertEqual(cfg.local_clones, {SLUG: real, "ophis/x": real, "ophis/y": home})
        self.assertEqual(
            clone_error.call_args_list,
            [mock.call(real, SLUG), mock.call(real, "ophis/x"), mock.call(home, "ophis/y")],
        )


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
        with mock.patch.object(config, "clone_error") as clone_error:
            self.assertEqual(list(self.load(PIPELINE).roles), ["researcher", "pm", "engineer"])
        clone_error.assert_not_called()


class Writable(Case):
    def test_work_then_the_temp_dirs(self):
        with mock.patch.object(config, "temp_dirs", return_value=("/t1", "/t2")):
            self.assertEqual(config.writable("/w"), ("/w", "/t1", "/t2"))
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
        with mock.patch.object(config, "clone_error", return_value=None):
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


IMPORT_ALONE = """import json, os, sys
opened = []
sys.addaudithook(lambda event, args: event == "open" and opened.append(str(args[0])))
from agent_pm.linear_orchestrator.engine import config
from agent_pm.session import api
from agent_pm.session.adapters import registry
home, front = os.environ["HOME"], os.environ["AGENT_PM_PATH"]
os.environ.update(HOME="/later", AGENT_PM_PATH="/later/bin")
print(json.dumps({
    "read": [p for p in opened if p.endswith(".toml") or p.startswith(home)],
    "kept": sorted(k for k, v in vars(config).items() if isinstance(v, str) and (home in v or front in v)),
    "path": config.path(),
}))
"""


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
                    "path": "/later/bin:/opt/homebrew/bin:/later/.local/bin:/usr/local/bin:/usr/bin:/bin",
                },
            )
            self.assertEqual(os.listdir(home), [])


class Shell(Case):
    def test_sh_run(self):
        with (
            mock.patch.object(config.subprocess, "run", return_value="res") as run,
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


ID = "TASK-12"
A, B = "engineer-TASK-12-0b6f2c1e", "dummy-tester-TASK-12-7c1d9e2f"


class TuiNames(Case):
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


if __name__ == "__main__":
    unittest.main()
