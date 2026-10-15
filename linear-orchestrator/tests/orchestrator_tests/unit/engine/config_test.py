"""Configuration loading, validation, frozen values and driver names."""

import builtins
import dataclasses
import io
import json
import os
import re
import tomllib
from importlib.resources import files
from pathlib import Path
from unittest import mock

from agent_pm import delegate, utils
from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import State
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

P1, P2 = "a0000000-0000-4000-8000-000000000031", "b0000000-0000-4000-8000-000000000032"


STATES_LINE = HEADER[HEADER.index("states = ") : HEADER.index("task_label_group")]


def missing(keys):
    return (
        f"required, unset: {keys}: set them in ~/.agent-pm/config.local.toml (linear-orchestrator/config.toml's "
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


ID = "TASK-12"

A = "engineer-TASK-12-0b6f2c1e"


class ConfigurationLoading(ConfigFile, Case):
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
                (
                    f"task_labels.light-research and task_labels.deep-research have the same label id {LABEL1}"
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
        for name in (
            "states",
            "roles",
            "task_labels",
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


class CommittedConfiguration(Case):
    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.local = os.path.join(self.agent_pm, "config.local.toml")

    def load(self, local=None):
        if local is not None:
            write(self.local, CORE_LOCAL + "\n" + local_section(local, "linear_orchestrator"))
        return config.load()

    def test_committed_config_holds_no_deployment_values(self):
        with files("agent_pm.linear_orchestrator").joinpath("config.toml").open("rb") as f:
            committed = tomllib.load(f)
        self.assertEqual(
            {k: committed[k] for k in DEPLOYMENT},
            {
                **dict.fromkeys(("team", "harness_key", "task_label_group"), ""),
                "states": dict.fromkeys(State, ""),
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
        with files("agent_pm.linear_orchestrator").joinpath("config.toml").open() as f:
            local = "".join(
                line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
            )
        write(self.local, CORE_LOCAL + "\n" + local)
        with self.assertRaises(config.ConfigError) as cm:
            self.load()
        zero = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(
            str(cm.exception),
            f"linear-orchestrator/config.toml: project_repos.{zero} must map a Linear project id "
            "(UUID) to <owner>/<name>: '<owner>/<name>'",
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

    def test_default_must_name_a_registered_class(self):
        self.local('[delegates]\ndefault = "missing"\n')
        self.fails(
            "linear-orchestrator/config.toml: [delegates] default: want one of harness", PIPELINE
        )

    def test_registration_requires_only_implementation(self):
        self.local('[delegates.harness]\nextra = "no"\n')
        self.fails(
            "linear-orchestrator/config.toml: [delegates.harness] takes only implementation",
            PIPELINE,
        )

    def test_registration_must_implement_delegate(self):
        self.local('[delegates.harness]\nimplementation = "builtins:dict"\n')
        self.fails(
            "linear-orchestrator/config.toml: [delegates.harness]: builtins:dict must be a concrete Delegate subclass",
            PIPELINE,
        )

    def test_registration_error_has_the_work_log_directory(self):
        self.local('[delegates.harness]\nimplementation = "agent_pm.delegate:Delegate"\n')
        with self.assertRaises(config.ConfigError) as caught:
            self.load(PIPELINE)
        self.assertEqual(
            caught.exception.logs_dir, os.path.join(os.path.realpath(self.agent_pm), "logs")
        )
        self.assertIn("concrete Delegate subclass", str(caught.exception))


class DeploymentWritePaths(ConfigFile, Case):
    def test_deployment_checkout_is_a_valid_cwd_and_write_dir(self):
        for directory in (REPO, os.path.join(REPO, "delegate"), os.path.dirname(REPO)):
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

    def test_separate_cwd_and_write_directories_remain_valid(self):
        directory = os.path.join(self.home, "external-work")
        cfg = self.load(
            PIPELINE
            + f"[core.roles.engineer]\ncwd = {json.dumps(directory)}\nwrite = [{json.dumps(directory)}]\n"
        )
        self.assertEqual(
            (cfg.roles["engineer"].run.cwd, cfg.roles["engineer"].run.write),
            (directory, [directory]),
        )


class DeploymentDirectories(Case):
    def test_deployment_checkout_and_packages_are_valid_work_dirs(self):
        packages = tuple(
            os.path.join(REPO, project, "src", "agent_pm", project.replace("-", "_"))
            for project in ("linear-orchestrator", "delegate", "session", "utils")
        )
        for directory in (REPO, *packages, os.path.join(packages[0], "missing")):
            with self.subTest(directory=directory):
                self.assertEqual(
                    config._work_dir({"work_dir": directory}), os.path.realpath(directory)
                )

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
        self.assertTrue(os.path.isfile(os.path.join(installed, "agent-pm-linear")))
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
                        f"task_labels.{task} is not a task of a role in linear-orchestrator/config.toml"
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
        packaged = files("agent_pm.delegate").joinpath("config.toml")
        with packaged.open() as f:
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
            str(packaged),
            os.path.join(REPO, "delegate", "src", "agent_pm", "delegate", "config.toml"),
        )


class ConfigurationRoot(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.agent_pm, "config.local.toml"))
        self.root = os.path.join(self.dir, "my root")
        self.dir = os.path.join(self.root, "linear-orchestrator")
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
        for work in (root, os.path.join(root, "state", "missing"), os.path.dirname(root), link):
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

    def test_reads_only_the_paths_passed_and_the_projects_config_files(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), "[[[ not toml")
        path = os.path.join(self.dir, "config.toml")
        local = os.path.join(self.agent_pm, "config.local.toml")
        write(path, PIPELINE)
        write(
            local,
            local_section('language = "French"\n' + CORE_TOML, "delegate")
            + local_section(role("engineer", "max_runs = 2"), "linear_orchestrator"),
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
            {path, local},
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

    def test_overlay_unknown_keys(self):
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
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails(message, PIPELINE + text)

    def test_resolution_rejects_unknown_overlay_roles_and_output_keys(self):
        core = "core: run overlay: "
        for text, message in (
            ("[core.roles.reseacher]\ntier = 2\n", "unknown role 'reseacher' in [overlay.roles]"),
            ('[core.output]\ntype = "local"\ntypo = 1\n', "unknown key 'typo' in [overlay.output]"),
            (
                '[core.roles.researcher.output]\ntype = "local"\ntypo = 1\n',
                "unknown key 'typo' in [overlay.roles.researcher.output]",
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


class LocalOverlay(ConfigFile, Case):
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
            (
                "linear-orchestrator/config.toml: states.done must be a Linear workflow state id (UUID): 'Done'"
            ),
            BASE,
        )

    def test_a_wrong_type_or_unknown_local_key_is_rejected_by_name(self):
        for local, message in (
            (
                "harness_key = 1\n",
                "linear-orchestrator/config.toml: harness_key must be a Keychain service name: 1",
            ),
            (
                role("researcher", 'max_runs = "2"'),
                "linear-orchestrator/config.toml: [roles.researcher] max_runs must be a whole number >= 1: '2'",
            ),
            (
                "bogus = 1\n",
                "linear-orchestrator/config.toml: ~/.agent-pm/config.local.toml: unknown key 'bogus' in [linear_orchestrator]",
            ),
            (
                role("researcher", "bogus = 1"),
                "linear-orchestrator/config.toml: [roles.researcher] has unknown keys: bogus",
            ),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.fails(message, BASE)
