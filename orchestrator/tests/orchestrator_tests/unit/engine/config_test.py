"""Configuration loading, validation, frozen values and driver names."""

import builtins
import dataclasses
import io
import json
import os
import plistlib
import tempfile
import tomllib
from importlib.resources import files
from pathlib import Path
from unittest import mock

from agent_pm import delegate, utils
from agent_pm.orchestrator.engine import config
from agent_pm.orchestrator.engine.flow import workflow
from agent_pm.orchestrator.engine.types import State
from agent_pm.utils import temp_dirs
from orchestrator_tests.case import REPO, ROOTS, Case
from orchestrator_tests.config_fixtures import (
    BASE,
    CORE_LOCAL,
    HARNESS_DELEGATE,
    PIPELINE,
    SID,
    SLUG,
    ConfigFile,
    write,
)
from orchestrator_tests.process_fixtures import DEVNULL, CompletedProcess
from orchestrator_tests.unit.board_ids import HEADER, STATES, TASK_GROUP, TEAM, role
from orchestrator_tests.unit.config_fixtures import GATE

from delegate.tests.fakes.delegate import FakeDelegate
from utils.tests.case import local_section

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
AGENT_GROUP = "00000000-0000-4000-8000-000000000023"

P1, P2 = "a0000000-0000-4000-8000-000000000031", "b0000000-0000-4000-8000-000000000032"


STATES_LINE = HEADER[HEADER.index("linear.states = ") : HEADER.index("linear.task_label_group")]


def missing(keys):
    return (
        f"required, unset: {keys}: set them in ~/.agent-pm/config.local.toml (orchestrator/config.toml's "
        "`# local:` lines)"
    )


def old_key(key):
    return (
        f"core: run overlay: [overlay.roles.researcher] has {key!r}: task-level config and default_task "
        "are gone; set run keys in [overlay.roles.researcher] (a run without a task picks one)"
    )


ROLES = {
    "researcher": (
        "researcher@agents.test",
        "linear-api-key-researcher",
        ("deep-research", "research", "light-research"),
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


CORE_TOML = """tier = 2
effort = "high"
client = "claude"
idle_timeout = 60
show = ""
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


class CatalogDelegate(FakeDelegate):
    ROLES = {
        "researcher": {"catalog-task": "Research"},
        "pm": {"design-task": "Design"},
        "engineer": {"build-task": "Build"},
    }
    calls = []
    trusted = frozenset()

    @classmethod
    def roles(cls):
        cls.calls.append(("roles",))
        return super().roles()

    @classmethod
    def trusted_dirs(cls):
        cls.calls.append(("trusted_dirs",))
        return cls.trusted

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        cls.calls.append(("resolve", role, overlays))
        return delegate.RunConfig(
            role=role,
            task=task or "",
            tier=2,
            effort="high",
            client="catalog",
            output={"type": "github", "repo": "acme/notes", "branch": "trunk", "dir": f"{role}/"},
            idle_timeout=60,
            show="",
        )


LINEAR_DEFAULTS = {
    **dict.fromkeys(("team", "harness_key", "task_label_group", "agent_label_group"), ""),
    "states": dict.fromkeys(State, ""),
    "human_members": [],
    **dict.fromkeys(("task_labels", "agent_labels", "project_repos"), {}),
    "roles": dict.fromkeys(("researcher", "pm", "engineer"), {"account": "", "key": ""}),
}


ID = "TASK-12"

A = "engineer-TASK-12-0b6f2c1e"


class ConfigurationLoading(ConfigFile, Case):
    def test_stage_order(self):
        self.assertEqual(self.load(PIPELINE).stages, {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertEqual(
            self.load(
                HEADER + role("researcher") + role("pm", 'next = "engineer"') + role("engineer")
            ).stages,
            {"researcher": 0, "pm": 0, "engineer": 1},
        )

    def test_rejected(self):
        body = BASE[BASE.index("[linear.roles") :]
        group = "linear.task_label_group must be a Linear label group id (UUID): "
        label = BASE + f'[linear.task_labels]\nlight-research = "{LABEL1}"\n'
        upper = "ABCDEF00-0000-4000-8000-000000000021"
        solo = role("solo")
        x = BASE + "[roles.x]\n[linear.roles.x]\n"
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
            (BASE + "[roles.x]\n", missing("linear.roles.x.account, linear.roles.x.key")),
            (x + 'key = "k-x"\n', missing("linear.roles.x.account")),
            (x + 'account = ""\nkey = "k-x"\n', missing("linear.roles.x.account")),
            (
                x + 'account = 5\nkey = "k-x"\n',
                "[linear.roles.x] account must be the role's Linear email: 5",
            ),
            (x + 'account = "x@agents.test"\n', missing("linear.roles.x.key")),
            (
                BASE + "[roles.x]\n[roles.y]\n[linear.roles.y]\nkey = 'k-y'\n",
                missing("linear.roles.y.account, linear.roles.x.account, linear.roles.x.key"),
            ),
            (x + 'account = "x@agents.test"\nkey = ""\n', missing("linear.roles.x.key")),
            (
                x + 'account = "x@agents.test"\nkey = ["k"]\n',
                "[linear.roles.x] key must be a Keychain service name: ['k']",
            ),
            (
                x + 'account = "x@agents.test"\nkey = "k-x"\nmax_runs = 2\n',
                "[linear.roles.x] has unknown keys: max_runs",
            ),
            (
                BASE + '[linear.roles.ghost]\naccount = "g@agents.test"\nkey = "k-g"\n',
                "[linear.roles.ghost] names no [roles.ghost]",
            ),
            (
                BASE.replace(
                    "[roles.solo]\n", '[roles.solo]\naccount = "s@agents.test"\nkey = "k"\n'
                ),
                "[roles.solo] has unknown keys: account, key",
            ),
            ("linear.typo = 1\nlinear.other = 2\n" + BASE, "unknown keys in [linear]: other, typo"),
            (HEADER + "linear.roles = 5\n[roles.x]\n", "[linear.roles] must be a table"),
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
            (
                BASE.replace('linear.harness_key = "linear-api-key"\n', ""),
                missing("linear.harness_key"),
            ),
            (
                BASE.replace('harness_key = "linear-api-key"', "harness_key = 1"),
                "linear.harness_key must be a Keychain service name: 1",
            ),
            (
                BASE.replace(TEAM, "Frank's Agents"),
                'linear.team must be a Linear team id (UUID): "Frank\'s Agents"',
            ),
            (BASE.replace(f'"{TEAM}"', "5"), "linear.team must be a Linear team id (UUID): 5"),
            (BASE.replace(f'linear.team = "{TEAM}"\n', ""), missing("linear.team")),
            (
                body,
                missing(
                    "linear.team, linear.harness_key, linear.task_label_group, linear.states.todo, "
                    "linear.states.in_progress, linear.states.in_review, linear.states.handoff, "
                    "linear.states.done, linear.states.canceled"
                ),
            ),
            (
                BASE.replace(STATES_LINE, ""),
                missing(
                    "linear.states.todo, linear.states.in_progress, linear.states.in_review, "
                    "linear.states.handoff, linear.states.done, linear.states.canceled"
                ),
            ),
            (
                BASE.replace(STATES_LINE, 'linear.states = "x"\n'),
                missing(
                    "linear.states.todo, linear.states.in_progress, linear.states.in_review, "
                    "linear.states.handoff, linear.states.done, linear.states.canceled"
                ),
            ),
            (
                BASE.replace(f', canceled = "{STATES["canceled"]}"', ""),
                missing("linear.states.canceled"),
            ),
            (
                BASE.replace(" }\n", ', backlog = "x" }\n', 1),
                "[linear.states] has unknown keys: backlog",
            ),
            (
                BASE.replace(STATES["done"], "Done"),
                "linear.states.done must be a Linear workflow state id (UUID): 'Done'",
            ),
            (
                BASE.replace(f'linear.task_label_group = "{TASK_GROUP}"\n', ""),
                missing("linear.task_label_group"),
            ),
            (BASE.replace(TASK_GROUP, "not-a-uuid"), group + "'not-a-uuid'"),
            (
                BASE.replace(TASK_GROUP, "ABCDEF00-0000-4000-8000-000000000002"),
                group + "'ABCDEF00-0000-4000-8000-000000000002'",
            ),
            (BASE.replace(f'"{TASK_GROUP}"', "5"), group + "5"),
            *[
                (
                    HEADER + f"linear.task_labels = {value}\n" + body,
                    'linear.task_labels must be a table of <task> = "<Linear label id>"',
                )
                for value in ('"x"', "[1]", "5")
            ],
            *[
                (
                    label + f"deep-research = {value}\n",
                    f"linear.task_labels.deep-research must be a Linear label id (UUID): {shown}",
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
                    f"linear.task_labels.light-research and linear.task_labels.deep-research have the "
                    f"same label id {LABEL1}"
                ),
            ),
            ('logs_dir = "/x"\n' + BASE, "unknown keys: logs_dir"),
            (
                HEADER + 'linear.project_repos = "x"\n' + body,
                'linear.project_repos must be a table of "<Linear project id>" = "<owner>/<name>"',
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
                "[linear.roles.pm] key 'linear-api-key-researcher' is also [linear.roles.researcher]'s",
            ),
            (
                PIPELINE.replace("linear-api-key-engineer", "linear-api-key"),
                "[linear.roles.engineer] key 'linear-api-key' is linear.harness_key",
            ),
            *[
                (
                    text + f'[linear.task_labels]\n{task} = "{LABEL1}"\n',
                    (
                        f"linear.task_labels.{task} is not a task of a role in orchestrator/config.toml"
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
            with self.subTest(message, text=text):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_bad_table_entry_rejected(self):
        cases = [
            ("linear.project_repos", key, value)
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
                self.assertTrue(msg.startswith(f"orchestrator/config.toml: {table}.{key} "), msg)
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
            PIPELINE
            + f'[linear.task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n'
        )
        self.assertEqual(cfg.task_labels, {"light-research": LABEL1, "deep-research": LABEL2})
        cfg = self.load(
            PIPELINE + f'[linear.project_repos]\n"{P1}" = "ophis/x"\n"{P2}" = "ophis/x"\n'
        )
        self.assertEqual(cfg.project_repos, {P1: "ophis/x", P2: "ophis/x"})

    def test_task_tiers(self):
        self.assertEqual(self.load(PIPELINE).task_tiers, {})
        self.assertEqual(self.load(PIPELINE + "[task_tiers]\n").task_tiers, {})
        cfg = self.load(PIPELINE + "[task_tiers]\nlight-build = 3\nbuild = 1\n")
        self.assertEqual(cfg.task_tiers, {"light-build": 3, "build": 1})

    def test_task_tiers_rejected(self):
        cases = [
            (
                "task_tiers = 3\n" + PIPELINE,
                "task_tiers must be a table of <task> = <tier 1-4>",
            ),
            *[
                (
                    PIPELINE + f"[task_tiers]\nlight-build = {value}\n",
                    f"task_tiers.light-build must be a tier, a whole number 1-4: {shown}",
                )
                for value, shown in (
                    ("0", "0"),
                    ("5", "5"),
                    ("true", "True"),
                    ('"3"', "'3'"),
                    ("2.0", "2.0"),
                )
            ],
            *[
                (
                    text + f"[task_tiers]\n{task} = 3\n",
                    f"task_tiers.{task} is not a task of a role in orchestrator/config.toml",
                )
                for task, text in (("ghost", PIPELINE), ("build", HEADER + role("researcher")))
            ],
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_client_max_runs(self):
        for text, limits in (
            (PIPELINE, {}),
            (PIPELINE + "[client_max_runs]\n", {}),
            (
                PIPELINE + "[client_max_runs]\nopencode = 1\nghost = 2\n",
                {"opencode": 1, "ghost": 2},
            ),
        ):
            with self.subTest(limits=limits):
                cfg = self.load(text)
                self.assertEqual(cfg.client_max_runs, limits)
                with self.assertRaises(TypeError):
                    cfg.client_max_runs["new"] = 1
        self.local("[client_max_runs]\nopencode = 1\n")
        self.assertEqual(self.load(PIPELINE).client_max_runs, {"opencode": 1})

    def test_client_max_runs_rejected(self):
        cases = [
            (
                "client_max_runs = 1\n" + PIPELINE,
                "client_max_runs must be a table of <client> = <whole number >= 1>",
            ),
            *[
                (
                    PIPELINE + f"[client_max_runs]\nopencode = {value}\n",
                    f"[client_max_runs] opencode must be a whole number >= 1: {shown}",
                )
                for value, shown in (
                    ("0", "0"),
                    ("-1", "-1"),
                    ('"2"', "'2'"),
                    ("true", "True"),
                    ("1.5", "1.5"),
                )
            ],
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_agent_labels(self):
        for top, table, group, labels in (
            ("", "", "", {}),
            ('linear.agent_label_group = ""\n', "[linear.agent_labels]\n", "", {}),
            (
                f'linear.agent_label_group = "{AGENT_GROUP}"\n',
                f'[linear.agent_labels]\nfuture-client = "{LABEL1}"\nother = "{LABEL2}"\n',
                AGENT_GROUP,
                {"future-client": LABEL1, "other": LABEL2},
            ),
        ):
            with self.subTest(group=group, labels=labels):
                cfg = self.load(top + PIPELINE + table)
                self.assertEqual((cfg.agent_label_group, cfg.agent_labels), (group, labels))
                with self.assertRaises(TypeError):
                    cfg.agent_labels["new"] = LABEL1

    def test_agent_labels_rejected(self):
        top = f'linear.agent_label_group = "{AGENT_GROUP}"\n'
        table = f'[linear.agent_labels]\nfirst = "{LABEL1}"\n'
        paired = (
            "linear.agent_label_group and [linear.agent_labels] must both be set or both be empty"
        )
        cases = [
            (top + PIPELINE, paired),
            (
                top + PIPELINE + f'[linear.agent_labels]\n"" = "{LABEL1}"\n',
                "[linear.agent_labels] client names must not be empty",
            ),
            (PIPELINE + table, paired),
            (top + PIPELINE + "[linear.agent_labels]\n", paired),
            ('linear.agent_label_group = ""\n' + PIPELINE + table, paired),
            *[
                (
                    f"linear.agent_label_group = {value}\n" + PIPELINE + table,
                    f"linear.agent_label_group must be a Linear label group id (UUID) or empty: {shown}",
                )
                for value, shown in (("5", "5"), ("false", "False"), ('"Agent"', "'Agent'"))
            ],
            *[
                (
                    top + f"linear.agent_labels = {value}\n" + PIPELINE,
                    'linear.agent_labels must be a table of <client> = "<Linear label id>"',
                )
                for value in ('"bad"', "[]", "false")
            ],
            *[
                (
                    top + PIPELINE + f"[linear.agent_labels]\nfirst = {value}\n",
                    f"linear.agent_labels.first must be a Linear label id (UUID): {shown}",
                )
                for value, shown in (
                    ('""', "''"),
                    ('"not-a-uuid"', "'not-a-uuid'"),
                    (
                        '"ABCDEF00-0000-4000-8000-000000000021"',
                        "'ABCDEF00-0000-4000-8000-000000000021'",
                    ),
                    ("5", "5"),
                    ("false", "False"),
                )
            ],
            (
                top + PIPELINE + table + f'second = "{LABEL1}"\n',
                f"linear.agent_labels.first and linear.agent_labels.second have the same label id {LABEL1}",
            ),
            (
                f'linear.agent_label_group = "{TASK_GROUP}"\n' + PIPELINE + table,
                "linear.agent_label_group and linear.task_label_group must be different",
            ),
            (
                top + PIPELINE + table + f'[linear.task_labels]\nbuild = "{LABEL1}"\n',
                f"linear.agent_labels.first and linear.task_labels.build have the same label id {LABEL1}",
            ),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_frozen(self):
        cfg = self.load(
            'linear.human_members = ["a@x.com"]\n'
            + PIPELINE
            + f'[linear.task_labels]\nbuild = "{LABEL1}"\n'
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
        for name in (
            "states",
            "roles",
            "task_labels",
            "task_tiers",
            "project_repos",
            "local_clones",
            "stages",
            "delegates",
        ):
            with self.subTest(name), self.assertRaises(TypeError):
                getattr(cfg, name)["x"] = 1
        with self.assertRaises(TypeError):
            cfg.docs.dirs["x"] = "y"
        self.assertEqual(
            (cfg.human_members, cfg.roles["pm"].tasks), (("a@x.com",), ("product-design",))
        )

    def test_roles_keep_delegate_runs_and_task_indexes_with_core_overlay(self):
        cfg = self.load(PIPELINE + "[core.roles.pm]\ntier = 1\n")
        overlay = {"roles": {"pm": {"tier": 1}}}
        roles = ["researcher", "pm", "engineer"]
        self.assertEqual(
            {r: x.tasks for r, x in cfg.roles.items()},
            {r: ROLES[r][2] for r in roles},
        )
        self.assertEqual(
            {r: x.run for r, x in cfg.roles.items()},
            {r: HARNESS_DELEGATE.resolve(r, overlays=(overlay,)) for r in roles},
        )

    def test_a_config_error_carries_the_log_dir_once_work_dir_is_valid(self):
        e = self.fails(
            "orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'",
            'work_dir = "w"\n' + PIPELINE,
        )
        self.assertIsNone(e.logs_dir)
        self.assertIsInstance(e, config.ConfigError)
        logs = os.path.join(os.path.realpath(self.agent_pm), "logs")
        for message, text in (
            (
                "orchestrator/config.toml: " + missing("linear.team"),
                PIPELINE.replace(f'linear.team = "{TEAM}"\n', ""),
            ),
            (
                "orchestrator/config.toml: role 'ghost' is not in delegate/config.toml",
                PIPELINE + role("ghost"),
            ),
        ):
            with self.subTest(message):
                e = self.fails(message, text)
                self.assertEqual((e.msg, e.logs_dir), (message, logs))


class RemovedSelectionKeys(Case):
    """Local keys model selection used (docs/upgrading.md): no reader knows them any more."""

    def setUp(self):
        self.local = os.path.join(self.agent_pm, "config.local.toml")
        self.board = HEADER + role("researcher") + role("pm") + role("engineer")

    def load(self, extra):
        write(self.local, CORE_LOCAL + "\n" + local_section(self.board, "orchestrator") + extra)
        return config.load()

    def test_leftover_selection_local_section_is_rejected(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load('[orchestrator.selection]\nresolvers = ["tier"]\n')
        self.assertIn("unknown key 'selection' in [orchestrator]", str(cm.exception))

    def test_leftover_choice_translator_role_local_section(self):
        for extra in (
            '[orchestrator.core.roles.choice-translator]\ncommands = ["x"]\n',
            "[delegate.roles.choice-translator]\ntier = 4\n",
        ):
            with self.subTest(extra=extra), self.assertRaises(config.ConfigError) as cm:
                self.load(extra)
            self.assertIn("choice-translator", str(cm.exception))

    def test_leftover_selector_section_is_ignored(self):
        self.assertEqual(
            self.load(""), self.load('[selector.jev]\nurl = "http://127.0.0.1:8000"\n')
        )


class CommittedConfiguration(Case):
    def test_harness_uses_the_public_delegate_export(self):
        with files("agent_pm.orchestrator").joinpath("config.toml").open("rb") as source:
            committed = tomllib.load(source)
        self.assertEqual(
            committed["delegates"]["harness"]["implementation"],
            "agent_pm.delegate:HarnessDelegate",
        )
        self.assertIs(HARNESS_DELEGATE, delegate.HarnessDelegate)

    def test_launchd_jobs_and_the_packaged_gate_run_agent_pm_orchestrator(self):
        for job in ("router", "promote"):
            plist = os.path.join(REPO, "orchestrator", "launchd", f"com.ophis.agent-pm.{job}.plist")
            with open(plist, "rb") as f:
                program = plistlib.load(f)["ProgramArguments"][0]
            self.assertTrue(program.endswith("/bin/agent-pm-orchestrator"), program)
        with files("agent_pm.orchestrator").joinpath("config.toml").open("rb") as source:
            committed = tomllib.load(source)
        self.assertEqual(
            committed["core"]["roles"]["researcher"]["gate"],
            "agent-pm-orchestrator router --brake researcher",
        )

    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.local = os.path.join(self.agent_pm, "config.local.toml")

    def load(self, local=None):
        if local is not None:
            write(self.local, CORE_LOCAL + "\n" + local_section(local, "orchestrator"))
        return config.load()

    def test_committed_config_holds_no_deployment_values(self):
        with files("agent_pm.orchestrator").joinpath("config.toml").open("rb") as f:
            committed = tomllib.load(f)
        self.assertEqual(
            {k: committed[k] for k in ("task_tiers", "local_clones", "work_dir")},
            {"task_tiers": {}, "local_clones": {}, "work_dir": "~/.agent-pm"},
        )
        self.assertEqual(committed["linear"], LINEAR_DEFAULTS)
        self.assertEqual(
            committed["roles"], dict.fromkeys(("researcher", "pm", "engineer"), {"max_runs": 3})
        )

    def test_packaged_client_max_runs(self):
        local = HEADER + role("researcher") + role("pm") + role("engineer")
        self.assertEqual(self.load(local).client_max_runs, {})
        self.assertEqual(
            self.load(local + "[client_max_runs]\nopencode = 1\n").client_max_runs, {"opencode": 1}
        )
        self.assertEqual(
            self.load(local + "[client_max_runs]\nclaude = 3\nopencode = 2\n").client_max_runs,
            {"opencode": 2, "claude": 3},
        )

    def test_without_local_names_what_is_missing(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load()
        self.assertEqual(
            str(cm.exception),
            "orchestrator/config.toml: "
            + missing(
                "linear.team, linear.harness_key, linear.task_label_group, linear.states.todo, "
                "linear.states.in_progress, linear.states.in_review, linear.states.handoff, "
                "linear.states.done, linear.states.canceled, linear.roles.researcher.account, "
                "linear.roles.pm.account, linear.roles.engineer.account, linear.roles.researcher.key, "
                "linear.roles.pm.key, linear.roles.engineer.key"
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

    def test_every_linear_key_under_orchestrator_linear_loads(self):
        accounts = "".join(
            f'[orchestrator.linear.roles.{r}]\naccount = "{r}@agents.test"\nkey = "linear-api-key-{r}"\n'
            for r in ("researcher", "pm", "engineer")
        )
        write(
            self.local,
            CORE_LOCAL
            + '\n[orchestrator]\nwork_dir = "~/.agent-pm"\n'
            + "[orchestrator.task_tiers]\nlight-build = 3\n"
            + "[orchestrator.client_max_runs]\nopencode = 1\n"
            + "[orchestrator.roles.engineer]\nmax_runs = 2\n"
            + f'[orchestrator.linear]\nteam = "{TEAM}"\nhuman_members = ["me@x.com"]\n'
            + f'harness_key = "linear-api-key"\ntask_label_group = "{TASK_GROUP}"\n'
            + f'agent_label_group = "{AGENT_GROUP}"\n'
            + "[orchestrator.linear.states]\n"
            + "".join(f'{k} = "{v}"\n' for k, v in STATES.items())
            + f'[orchestrator.linear.task_labels]\nbuild = "{LABEL1}"\n'
            + f'[orchestrator.linear.agent_labels]\nclaude = "{LABEL2}"\n'
            + accounts
            + f'[orchestrator.linear.project_repos]\n"{P1}" = "ophis/x"\n',
        )
        cfg = self.load()
        self.assertEqual(
            (cfg.team, cfg.human_members, cfg.harness_key, cfg.task_label_group, cfg.states),
            (TEAM, ("me@x.com",), "linear-api-key", TASK_GROUP, STATES),
        )
        self.assertEqual(
            (cfg.task_labels, cfg.agent_label_group, cfg.agent_labels, cfg.project_repos),
            ({"build": LABEL1}, AGENT_GROUP, {"claude": LABEL2}, {P1: "ophis/x"}),
        )
        self.assertEqual(
            (cfg.work_dir, cfg.task_tiers, cfg.client_max_runs, cfg.local_clones),
            (os.path.realpath(self.agent_pm), {"light-build": 3}, {"opencode": 1}, {}),
        )
        self.assertEqual(
            fields(cfg.roles),
            {
                "researcher": (*ROLES["researcher"][:3], 3, "pm"),
                "pm": (*ROLES["pm"][:3], 3, "engineer"),
                "engineer": (*ROLES["engineer"][:3], 2, None),
            },
        )

    def test_a_local_flow_key_overrides_only_that_key(self):
        local = HEADER + role("researcher") + role("engineer")
        for pm, want in (
            (role("pm", "require_instructions = true"), ("engineer", True)),
            (role("pm", 'next = ""'), (None, False)),
        ):
            with self.subTest(pm):
                cfg = self.load(local + pm)
                self.assertEqual(
                    {r: (x.next, x.require_instructions) for r, x in cfg.roles.items()},
                    {"researcher": ("pm", True), "pm": want, "engineer": (None, True)},
                )

    def test_a_bad_workflow_stops_load(self):
        local = HEADER + role("researcher") + role("pm") + role("engineer")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        flow = Path(tmp.name, "workflow.toml")
        for text, message in (
            ("[roles\n", "Expected ']'"),
            (
                '[roles.pm]\ninput = "x"\n',
                "roles.pm.input: want one of research, design, build: 'x'",
            ),
        ):
            flow.write_text(text, encoding="utf-8")
            with self.subTest(text):
                with (
                    mock.patch("importlib.resources.files", lambda _: Path(tmp.name)),
                    self.assertRaises(config.ConfigError) as cm,
                ):
                    self.load(local)
                self.assertTrue(str(cm.exception).startswith(f"{workflow.REL}: "), cm.exception)
                self.assertIn(message, str(cm.exception))
        # a bad read keeps the last good one
        self.assertEqual(config.TASKS["pm"].title_prefix, "PRD")

    def test_local_lines_uncommented_set_every_required_key(self):
        with files("agent_pm.orchestrator").joinpath("config.toml").open() as f:
            local = "".join(
                line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
            )
        write(self.local, CORE_LOCAL + "\n" + local)
        with self.assertRaises(config.ConfigError) as cm:
            self.load()
        zero = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(
            str(cm.exception),
            f"orchestrator/config.toml: linear.project_repos.{zero} must map a Linear project id "
            "(UUID) to <owner>/<name>: '<owner>/<name>'",
        )

    def test_agent_local_lines_load(self):
        with files("agent_pm.orchestrator").joinpath("config.toml").open() as source:
            local = tomllib.loads(
                "".join(
                    line.removeprefix("# local: ")
                    for line in source
                    if line.startswith("# local: ")
                )
            )["orchestrator"]["linear"]
        cfg = self.load(
            f'linear.agent_label_group = "{local["agent_label_group"]}"\n'
            + HEADER
            + role("researcher")
            + role("pm")
            + role("engineer")
            + "[linear.agent_labels]\n"
            + "".join(f'"{key}" = "{value}"\n' for key, value in local["agent_labels"].items())
        )
        self.assertEqual(
            (cfg.agent_label_group, dict(cfg.agent_labels)),
            (local["agent_label_group"], local["agent_labels"]),
        )


class WritableDirectories(Case):
    def test_work_then_the_temp_dirs(self):
        self.assertEqual(config.writable("/w"), ("/w", *temp_dirs()))
        with self.assertRaises(TypeError):
            config.writable()


class WorkDirectories(ConfigFile, Case):
    def test_unset_is_in_home(self):
        work = os.path.realpath(self.agent_pm)
        cfg = self.load(PIPELINE)
        self.assertEqual(
            (cfg.work_dir, cfg.workdirs, cfg.logs_dir, cfg.runs_log, cfg.issue_workdir("TASK-9")),
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
            "orchestrator/config.toml: work_dir must neither lie in nor contain "
            f"local_clones.ophis/x {home}: '~/.agent-pm'",
            PIPELINE + '[local_clones]\n"ophis/x" = "~"\n',
        )


class DelegateRegistry(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        CatalogDelegate.calls = []
        CatalogDelegate.trusted = frozenset()

    def catalog(self):
        self.local(
            '[delegates]\ndefault = "catalog"\n[delegates.catalog]\nimplementation = "orchestrator_tests.unit.engine.config_test:CatalogDelegate"\n'
        )

    def test_registered_class_owns_catalog_resolution_and_trust(self):
        self.catalog()
        cfg = self.load(PIPELINE + "[core.roles.pm]\ntier = 1\n")
        self.assertEqual((cfg.delegate_kind, cfg.delegate_cls), ("catalog", CatalogDelegate))
        self.assertEqual(
            CatalogDelegate.calls,
            [
                ("trusted_dirs",),
                ("roles",),
                *(
                    ("resolve", name, ({"roles": {"pm": {"tier": 1}}},))
                    for name in ("researcher", "pm", "engineer")
                ),
            ],
        )
        self.assertEqual(
            {name: role.tasks for name, role in cfg.roles.items()},
            {name: tuple(tasks) for name, tasks in CatalogDelegate.ROLES.items()},
        )
        self.assertEqual({role.run.client for role in cfg.roles.values()}, {"catalog"})
        self.assertIn("harness", cfg.delegates)

    def test_registered_trust_prevents_work_dir_loading_project_settings(self):
        self.catalog()
        CatalogDelegate.trusted = frozenset((os.path.realpath(self.home),))
        self.fails(
            f"core: trusted_dirs: {os.path.realpath(self.home)} is or contains work_dir {os.path.realpath(self.agent_pm)}",
            PIPELINE,
        )

    def test_a_default_or_registration_that_is_not_a_delegate_class_is_rejected(self):
        for local, message in (
            ('[delegates]\ndefault = "missing"\n', "[delegates] default: want one of harness"),
            (
                '[delegates.harness]\nextra = "no"\n',
                "[delegates.harness] takes only implementation",
            ),
            (
                '[delegates.harness]\nimplementation = "builtins:dict"\n',
                "[delegates.harness]: builtins:dict must be a concrete Delegate subclass",
            ),
        ):
            with self.subTest(message):
                self.local(local)
                self.fails("orchestrator/config.toml: " + message, PIPELINE)

    def test_registration_error_has_the_work_log_directory(self):
        self.local('[delegates.harness]\nimplementation = "agent_pm.delegate:Delegate"\n')
        with self.assertRaises(config.ConfigError) as caught:
            self.load(PIPELINE)
        self.assertEqual(
            caught.exception.logs_dir, os.path.join(os.path.realpath(self.agent_pm), "logs")
        )
        self.assertIn("concrete Delegate subclass", str(caught.exception))


class DeploymentWritePaths(ConfigFile, Case):
    def test_the_deployment_checkout_or_another_dir_is_a_valid_cwd_and_write_dir(self):
        for directory in (
            REPO,
            os.path.join(REPO, "delegate"),
            os.path.dirname(REPO),
            os.path.join(self.home, "external-work"),
        ):
            with self.subTest(directory=directory):
                cfg = self.load(
                    PIPELINE
                    + f"[core.roles.engineer]\ncwd = {json.dumps(directory)}\n"
                    + f"write = [{json.dumps(directory)}]\n"
                )
                self.assertEqual(
                    (cfg.roles["engineer"].run.cwd, cfg.roles["engineer"].run.write),
                    (directory, [directory]),
                )


class DeploymentDirectories(Case):
    def test_local_clone_equal_containment_and_symlink_boundaries(self):
        root = Path(self.home).resolve()
        clone = root / "clone"
        clone.mkdir()
        alias = root / "alias"
        alias.symlink_to(clone, target_is_directory=True)
        for work in (clone, clone / "missing", clone.parent, alias):
            with (
                self.subTest(work=work),
                self.assertRaisesRegex(config.ConfigError, "local_clones.ophis/x"),
            ):
                config._work_dir({"work_dir": str(work)}, {"ophis/x": str(clone)})
        sibling = root / "sibling" / "missing"
        self.assertEqual(
            config._work_dir({"work_dir": str(sibling)}, {"ophis/x": str(clone)}), str(sibling)
        )
        self.assertFalse(sibling.exists())


class ExecutablePath(Case):
    def path(self, home, env):
        """config.path() with HOME `home` and AGENT_PM_PATH as in `env` (unset when absent)."""
        rest = {k: v for k, v in os.environ.items() if k != "AGENT_PM_PATH"}
        with mock.patch.dict(os.environ, {**rest, "HOME": home, **env}, clear=True):
            return config.path()

    def test_agent_pm_path_then_this_installations_bin_then_the_default(self):
        installed = os.path.join(REPO, "bin")
        self.assertTrue(os.path.isfile(os.path.join(installed, "agent-pm-orchestrator")))
        for home in (self.home, os.path.join(self.home, "other")):
            default = (
                f"{installed}:/opt/homebrew/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
            )
            for env, front in (
                ({"AGENT_PM_PATH": "/fakes/bin:/more"}, "/fakes/bin:/more:"),
                ({}, ""),
                ({"AGENT_PM_PATH": ""}, ""),
            ):
                with self.subTest(home=home, env=env):
                    self.assertEqual(self.path(home, env), front + default)


class ShellExecution(Case):
    def test_clone_default_shell_uses_configured_path_and_inherited_environment(self):
        clone = os.path.join(self.home, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        result = CompletedProcess([], 0, "https://github.com/ophis/agent-pm\n", "")
        for front in ("", "/tools/bin"):
            with (
                self.subTest(front=front),
                mock.patch("subprocess.run", return_value=result) as run,
                mock.patch.dict(os.environ, {"AGENT_PM_PATH": front, "KEEP": "1"}),
            ):
                self.assertIsNone(config.clone_error(clone, SLUG))
                self.assertEqual(run.call_args.kwargs["env"], {**os.environ, "PATH": config.path()})
                self.assertEqual(run.call_args.kwargs["timeout"], utils.SHORT)

    def test_configured_environment_preserves_inherited_values(self):
        with (
            mock.patch("subprocess.run", return_value="res") as run,
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "KEEP": "1"}),
        ):
            env = {**os.environ, "PATH": config.path()}
            self.assertEqual(config.shell(["git", "status"], 5), "res")
        run.assert_called_once_with(
            ["git", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=DEVNULL,
            env=env,
        )
        self.assertEqual(env["KEEP"], "1")


class TuiSessionNames(Case):
    def test_prefix_of_a_role_and_issue(self):
        for role_name in ("engineer", "dummy-tester", "pm2"):
            with self.subTest(role=role_name):
                self.assertEqual(config.tui_prefix(role_name, ID), f"{role_name}-{ID}-")

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

    def test_the_name_is_the_prefix_and_the_sid_head(self):
        self.assertEqual(config.tui_name("engineer", ID, SID), A)


class RunnableRoles(ConfigFile, Case):
    def runs(self, text=PIPELINE):
        return self.load(text).roles

    def test_roles_in_pipeline_order_tasks_from_the_index(self):
        roles = self.runs()
        self.assertEqual(fields(roles), ROLES)
        self.assertEqual(list(roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            {r: x.tasks for r, x in roles.items()},
            {r: tuple(HARNESS_DELEGATE.roles()[r]) for r in roles},
        )
        roles = self.runs(HEADER + role("engineer") + role("researcher", "max_runs = 3"))
        self.assertEqual(
            [(r, x.max_runs) for r, x in roles.items()], [("engineer", 1), ("researcher", 3)]
        )
        text = PIPELINE + f'[linear.task_labels]\nlight-research = "{LABEL1}"\nbuild = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])

    def test_a_replaced_config_leaves_unset_flow_keys_unset(self):
        roles = self.runs(HEADER + role("researcher") + role("pm") + role("engineer"))
        self.assertEqual(
            {r: (x.next, x.require_instructions) for r, x in roles.items()},
            dict.fromkeys(("researcher", "pm", "engineer"), (None, True)),
        )

    def test_tasks_by_role(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        self.assertEqual(
            {r: (x.input, x.title_prefix, x.files) for r, x in config.TASKS.items()},
            {
                "researcher": ("research", "", False),
                "pm": ("design", "PRD", False),
                "engineer": ("build", "ENG", True),
            },
        )

    def test_role_for(self):
        roles = self.runs()
        for email in ("researcher@agents.test", "RESEARCHER@Agents.Test"):
            self.assertEqual(config.role_for(roles, email), "researcher")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(config.role_for(roles, email))


class ConfigurationRoot(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.agent_pm, "config.local.toml"))
        self.root = os.path.join(self.dir, "my root")
        self.dir = os.path.join(self.root, "orchestrator")
        os.makedirs(self.dir)
        os.makedirs(os.path.join(self.root, "delegate"))
        self.core(CORE_TOML)

    def core(self, text):
        write(os.path.join(self.agent_pm, "config.local.toml"), local_section(text, "delegate"))

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(path=path)

    def test_work_dir_may_equal_lie_in_contain_or_link_to_the_checkout(self):
        root = os.path.realpath(self.root)
        link = os.path.join(self.home, "deployment-link")
        os.symlink(root, link)
        packages = tuple(
            os.path.join(REPO, project, "src", "agent_pm", project.replace("-", "_"))
            for project in ("orchestrator", "delegate", "session", "utils")
        )
        for work in (
            root,
            os.path.join(root, "state", "missing"),
            os.path.dirname(root),
            link,
            REPO,
            *packages,
            os.path.join(packages[0], "missing"),
        ):
            with self.subTest(work=work):
                self.assertEqual(config._work_dir({"work_dir": work}), os.path.realpath(work))

    def test_explicit_paths_preserve_nested_overlay_values_and_local_precedence(self):
        local = os.path.join(self.agent_pm, "config.local.toml")
        write(
            local,
            local_section(
                CORE_TOML.replace("[roles.engineer]\n", "[roles.engineer]\ntier = 1\n"), "delegate"
            ),
        )
        cfg = self.load(
            PIPELINE + '[core.roles.engineer]\ntier = 3\ncommands = ["echo unchanged"]\n'
        )
        self.assertEqual(
            (cfg.roles["engineer"].run.tier, cfg.roles["engineer"].run.commands),
            (3, ["echo unchanged"]),
        )

    def test_tasks_are_the_core_configs_index(self):
        with self.charter(
            "# Researcher\n\n## Tasks\n\n"
            "unsure → `deep-research` (`<tasks>/deep-research.md`)\n\n"
            "- `deep-research` (`<tasks>/deep-research.md`): research.\n"
        ):
            self.assertEqual(self.load(PIPELINE).roles["researcher"].tasks, ("deep-research",))

    def charter(self, text):
        path = os.path.join(
            REPO, "delegate", "src", "agent_pm", "delegate", "team", "roles", "researcher.md"
        )
        opened = open

        def read(file, *args, **kwargs):
            return io.StringIO(text) if file == path else opened(file, *args, **kwargs)

        return mock.patch.object(builtins, "open", side_effect=read)

    def test_reads_only_the_paths_passed_the_projects_config_files_and_the_workflow(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), "[[[ not toml")
        path = os.path.join(self.dir, "config.toml")
        local = os.path.join(self.agent_pm, "config.local.toml")
        write(path, PIPELINE)
        write(
            local,
            local_section('language = "French"\n' + CORE_TOML, "delegate")
            + local_section(role("engineer", "max_runs = 2"), "orchestrator"),
        )
        with (
            mock.patch.object(builtins, "open", side_effect=open) as opened,
            mock.patch.object(io, "open", side_effect=io.open) as resource_opened,
        ):
            cfg = config.load(path=path)
        projects = {
            path
            for root in ROOTS
            for path in (
                os.path.join(os.path.dirname(root), "config.toml"),
                os.path.join(
                    root,
                    "agent_pm",
                    os.path.basename(os.path.dirname(root)).replace("-", "_"),
                    "config.toml",
                ),
            )
        }
        self.assertEqual(
            {
                str(c.args[0])
                for c in [*opened.call_args_list, *resource_opened.call_args_list]
                if str(c.args[0]).endswith(".toml")
            }
            - projects,
            {
                path,
                local,
                str(files("agent_pm.orchestrator.engine.flow").joinpath("workflow.toml")),
            },
        )
        engineer = cfg.roles["engineer"]
        self.assertEqual((engineer.max_runs, engineer.run.language), (2, "French"))

    def test_an_index_error_stops_the_caller(self):
        with self.charter(
            "# Researcher\n\n## Tasks\n\n"
            "unsure → `ghost` (`<tasks>/ghost.md`)\n\n"
            "- `ghost` (`<tasks>/ghost.md`): absent.\n"
        ):
            self.fails("core: roles/researcher.md: lists 'ghost' without tasks/ghost.md", PIPELINE)

    def test_overlay_keeps_its_values_verbatim(self):
        cfg = self.load(
            PIPELINE
            + '[core]\ntier = 3\ncommands = ["env PYTHONPATH={{pythonpath}} python3 -P", "true"]\n'
            + '[core.roles.researcher]\ngate = "python3 {{root}}/x"\n'
            + '[core.roles.pm]\nlanguage = "French"\n'
        )
        run = cfg.roles["researcher"].run
        self.assertEqual(
            (run.tier, run.commands, run.gate),
            (3, ["env PYTHONPATH={{pythonpath}} python3 -P", "true"], "python3 {{root}}/x"),
        )
        self.assertEqual((cfg.roles["pm"].run.gate, cfg.roles["pm"].run.language), ("", "French"))

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

    def test_no_core_table(self):
        self.assertEqual(self.load(PIPELINE).roles["researcher"].run.gate, "")

    def test_unknown_overlay_keys_roles_and_output_keys(self):
        unknown = "core: run overlay: unknown key "
        cases = [
            ("[core]\nfoo = 1\n", unknown + "'foo' in [overlay]"),
            ('[core]\nusers = ["octocat"]\n', unknown + "'users' in [overlay]"),
            ('[core]\ntrusted_dirs = ["~/x"]\n', unknown + "'trusted_dirs' in [overlay]"),
            ("[core]\nworkers_per_column = 2\n", unknown + "'workers_per_column' in [overlay]"),
            ('[core]\ngrid_retile = "off"\n', unknown + "'grid_retile' in [overlay]"),
            ('[core]\nteam_dirs = ["~/.agent-pm/team"]\n', unknown + "'team_dirs' in [overlay]"),
            ("[core.clients.claude]\nflags = []\n", unknown + "'clients' in [overlay]"),
            ("[core.roles.researcher]\nfoo = 1\n", unknown + "'foo' in [overlay.roles.researcher]"),
        ]
        cases += [
            (
                '[core.roles.researcher]\ndefault_task = "light-research"\nfoo = 1\n',
                old_key("default_task"),
            ),
            ('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n', old_key("tasks")),
            (
                "[core.roles.reseacher]\ntier = 2\n",
                "core: run overlay: unknown role 'reseacher' in [overlay.roles]",
            ),
            ('[core.output]\ntype = "local"\ntypo = 1\n', unknown + "'typo' in [overlay.output]"),
            (
                '[core.roles.researcher.output]\ntype = "local"\ntypo = 1\n',
                unknown + "'typo' in [overlay.roles.researcher.output]",
            ),
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails(message, PIPELINE + text)

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


# A local table that stays empty is dropped (utils/src/agent_pm/utils/config_file.py), so a role
# the replaced config.toml lacks needs a key in its local [roles.<role>].
LOCAL_PIPELINE = PIPELINE.replace(role("engineer"), role("engineer", "max_runs = 1"))


class LocalOverlay(ConfigFile, Case):
    def test_local_on_top(self):
        self.local(
            HEADER
            + 'linear.human_members = ["b@x.com"]\n'
            + role("researcher", "max_runs = 2")
            + role("pm")
            + f'[linear.project_repos]\n"{P2}" = "ophis/y"\n'
        )
        cfg = self.load(
            'linear.human_members = ["a@x.com", "c@x.com"]\n'
            '[roles.researcher]\nnext = "pm"\nmax_runs = 3\n[roles.pm]\n'
            f'[linear.project_repos]\n"{P1}" = "ophis/x"\n[core.roles.pm]\ntier = 1\n'
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
        self.local(LOCAL_PIPELINE)
        self.assertEqual(self.load("").roles, self.load(PIPELINE).roles)
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.assertEqual(self.load(PIPELINE).team, TEAM)

    def test_an_empty_local_role_table_adds_no_role(self):
        self.local(LOCAL_PIPELINE)
        want = set(self.load("").roles)
        self.local(LOCAL_PIPELINE + "[roles.ghost]\n")
        self.assertEqual(set(self.load("").roles), want)
        self.local(LOCAL_PIPELINE + role("ghost"))
        self.fails("orchestrator/config.toml: [linear.roles.ghost] names no [roles.ghost]", "")

    def test_a_bad_value_wrong_type_unknown_or_old_task_local_key_is_rejected_by_name(self):
        for local, text, message in (
            (
                '[core.roles.researcher.tasks.deep-research]\ngate = "x"\n',
                PIPELINE,
                old_key("tasks"),
            ),
            (
                f'linear.team = "{TEAM}"\nlinear.states = {{ done = "Done" }}\n',
                BASE,
                "orchestrator/config.toml: linear.states.done must be a Linear workflow state id (UUID): 'Done'",
            ),
            (
                "linear.harness_key = 1\n",
                BASE,
                "orchestrator/config.toml: linear.harness_key must be a Keychain service name: 1",
            ),
            (
                'linear = "x"\n',
                BASE,
                "orchestrator/config.toml: [linear] must be a table",
            ),
            (
                "linear.typo = 1\n",
                BASE,
                "orchestrator/config.toml: unknown keys in [linear]: typo",
            ),
            (
                "[linear.roles.pm]\ntypo = 1\n",
                BASE,
                "orchestrator/config.toml: [linear.roles.pm] has unknown keys: typo",
            ),
            *[
                (
                    flat,
                    BASE,
                    "orchestrator/config.toml: ~/.agent-pm/config.local.toml: unknown key "
                    f"{key!r} in [orchestrator]",
                )
                for key, flat in (
                    ("team", f'team = "{TEAM}"\n'),
                    ("harness_key", 'harness_key = "linear-api-key"\n'),
                    ("human_members", 'human_members = ["a@x.com"]\n'),
                    ("states", f'[states]\ndone = "{STATES["done"]}"\n'),
                    ("task_labels", f'[task_labels]\nbuild = "{LABEL1}"\n'),
                    ("project_repos", f'[project_repos]\n"{P1}" = "ophis/x"\n'),
                )
            ],
            (
                '[roles.researcher]\naccount = "researcher@agents.test"\nkey = "k"\n',
                BASE,
                "orchestrator/config.toml: [roles.researcher] has unknown keys: account, key",
            ),
            (
                role("researcher", 'max_runs = "2"'),
                BASE,
                "orchestrator/config.toml: [roles.researcher] max_runs must be a whole number >= 1: '2'",
            ),
            (
                "bogus = 1\n",
                BASE,
                "orchestrator/config.toml: ~/.agent-pm/config.local.toml: unknown key 'bogus' in [orchestrator]",
            ),
            (
                role("researcher", "bogus = 1"),
                BASE,
                "orchestrator/config.toml: [roles.researcher] has unknown keys: bogus",
            ),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.fails(message, text)
