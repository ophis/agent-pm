import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
from board_ids import HEADER, STATES, TASK_GROUP, TEAM, role  # noqa: E402
import config  # noqa: E402
import clients  # noqa: E402
import drive  # noqa: E402
import repo  # noqa: E402

BASE = HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer") + role("solo")

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
P1, P2 = "a0000000-0000-4000-8000-000000000031", "b0000000-0000-4000-8000-000000000032"
SLUG = "ophis/agent-pm"
HTTPS = "https://github.com/ophis/agent-pm.git"
STATES_LINE = HEADER[HEADER.index("states = "):HEADER.index("task_label_group")]


def missing(keys):
    return f"missing {keys}: set them in ~/.agent-pm/orchestrator.local.toml (orchestrator/config.toml's `# local:` lines)"


class ConfigFile:
    """setUp and load for TestCases that write a orchestrator/config.toml into self.dir."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(text)
        return config.load_config(path)


class Config(ConfigFile, unittest.TestCase):
    def test_tests_never_read_this_machines_local_file(self):
        self.assertIn(os.path.realpath(os.path.expanduser(config.LOCAL)), hermetic.HIDDEN)

    def test_tests_home_already_gone_at_exit_prints_nothing(self):
        res = subprocess.run([sys.executable, "-c", "import shutil, hermetic; shutil.rmtree(hermetic.HOME)"],
                             cwd=os.path.dirname(os.path.abspath(hermetic.__file__)), capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_stage_order(self):
        self.assertEqual(config.stage_order(self.load(BASE)),
                         {"researcher": 0, "pm": 1, "engineer": 2, "solo": 0})

    def test_rejected(self):
        body = BASE[BASE.index("[roles"):]
        group = "task_label_group must be a Linear label group id (UUID): "
        label = BASE + f'[task_labels]\nlight-research = "{LABEL1}"\n'
        upper = "ABCDEF00-0000-4000-8000-000000000021"
        solo = role("solo")
        x = BASE + "[roles.x]\n"
        cases = [
            (BASE + role("a", 'next = "b"') + role("b", 'next = "a"'), "the next chain from 'a' has a cycle"),
            (HEADER + 'docs = { repo = "acme/notes", clone = "/nonexistent/notes", branch = "trunk" }\n' + body, "unknown keys: docs"),
            (HEADER + '[projects.p]\nnext = "q"\n' + body, "unknown keys: projects"),
            (BASE + '[tasks.x]\nmodel = "opus"\n[docs]\nrepo = "acme/notes"\n', "unknown keys: docs, tasks"),
            (BASE.replace(solo, role("solo", 'role = "x"', 'task = "y"')), "[roles.solo] has unknown keys: role, task"),
            (BASE.replace(solo, role("solo", 'tasks = ["x"]', "read_only = []")), "[roles.solo] has unknown keys: read_only, tasks"),
            (x + 'key = "k-x"\n', missing("roles.x.account")),
            (x + 'account = ""\nkey = "k-x"\n', "[roles.x] account must be the role's Linear email: ''"),
            (x + 'account = 5\nkey = "k-x"\n', "[roles.x] account must be the role's Linear email: 5"),
            (x + 'account = "x@agents.test"\n', missing("roles.x.key")),
            (BASE + "[roles.x]\n[roles.y]\nkey = 'k-y'\n", missing("roles.x.account, roles.x.key, roles.y.account")),
            (x + 'account = "x@agents.test"\nkey = ""\n', "[roles.x] key must be a Keychain service name: ''"),
            (x + 'account = "x@agents.test"\nkey = ["k"]\n', "[roles.x] key must be a Keychain service name: ['k']"),
            *[(BASE + role("x", f"max_runs = {value}"), f"[roles.x] max_runs must be a whole number >= 1: {shown}")
              for shown, value in {"0": "0", "-1": "-1", "'2'": '"2"', "True": "true", "1.5": "1.5"}.items()],
            (BASE.replace('harness_key = "linear-api-key"\n', ""), missing("harness_key")),
            (BASE.replace('harness_key = "linear-api-key"', "harness_key = 1"), "harness_key must be a Keychain service name: 1"),
            (BASE.replace(TEAM, "Frank's Agents"), "team must be a Linear team id (UUID): \"Frank's Agents\""),
            (BASE.replace(f'"{TEAM}"', "5"), "team must be a Linear team id (UUID): 5"),
            (BASE.replace(f'team = "{TEAM}"\n', ""), missing("team")),
            (body, missing("team, harness_key, task_label_group, states.todo, states.in_progress, states.in_review, "
                           "states.handoff, states.done, states.canceled")),
            (BASE.replace(STATES_LINE, ""), missing("states.todo, states.in_progress, states.in_review, states.handoff, "
                                                    "states.done, states.canceled")),
            (BASE.replace(STATES_LINE, 'states = "x"\n'), missing("states.todo, states.in_progress, states.in_review, "
                                                                 "states.handoff, states.done, states.canceled")),
            (BASE.replace(f', canceled = "{STATES["canceled"]}"', ""), missing("states.canceled")),
            (BASE.replace(" }\n", ', backlog = "x" }\n', 1), "[states] has unknown keys: backlog"),
            (BASE.replace(STATES["done"], "Done"), "states.done must be a Linear workflow state id (UUID): 'Done'"),
            (BASE.replace(f'task_label_group = "{TASK_GROUP}"\n', ""), missing("task_label_group")),
            (BASE.replace(TASK_GROUP, "not-a-uuid"), group + "'not-a-uuid'"),
            (BASE.replace(TASK_GROUP, "ABCDEF00-0000-4000-8000-000000000002"), group + "'ABCDEF00-0000-4000-8000-000000000002'"),
            (BASE.replace(f'"{TASK_GROUP}"', "5"), group + "5"),
            *[(HEADER + f"task_labels = {value}\n" + body, 'task_labels must be a table of <task> = "<Linear label id>"')
              for value in ('"x"', "[1]", "5")],
            *[(label + f"deep-research = {value}\n", f"task_labels.deep-research must be a Linear label id (UUID): {shown}")
              for shown, value in {"'Light Research'": '"Light Research"', repr(upper): f'"{upper}"', "5": "5", "['x']": '["x"]',
                                   "''": '""', "True": "true"}.items()],
            (label + f'other = "{LABEL2}"\ndeep-research = "{LABEL1}"\n',
             f"task_labels.light-research and task_labels.deep-research have the same label id {LABEL1}"),
            ('logs_dir = "/x"\n' + BASE, "unknown keys: logs_dir"),
            (HEADER + 'project_repos = "x"\n' + body,
             'project_repos must be a table of "<Linear project id>" = "<owner>/<name>"'),
            *[(HEADER + f"local_clones = {value}\n" + body, 'local_clones must be a table of "<owner>/<name>" = "<path>"')
              for value in ('"/x"', '["/x"]', "5")],
            (BASE + f'[local_clones]\n"{SLUG}" = "/x"\n"Ophis/Agent-PM" = "/y"\n',
             f"local_clones.{SLUG} and local_clones.Ophis/Agent-PM are the same repo"),
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                with self.assertRaises(SystemExit) as cm:
                    self.load(text)
                self.assertEqual(str(cm.exception.code), "orchestrator/config.toml: " + message)

    def test_bad_table_entry_rejected(self):
        """A [project_repos] or [local_clones] entry: the message names the entry and shows the value."""
        cases = [("project_repos", key, value) for key, value in (
                     ("not-a-uuid", "ophis/x"), (P1.upper(), "ophis/x"), (P1, "https://github.com/ophis/x"), (P1, "ophis/."),
                     (P1, "ophis/.."), (P1, ""), (P1, 42), (P1, "ophis"))]
        cases += [("local_clones", key, value) for key, value in (
                      ("agent-pm", "/x"), ("https://github.com/ophis/agent-pm", "/x"), ("ophis/..", "/x"), (SLUG, 42), (SLUG, True),
                      (SLUG, ["/x"]), (SLUG, ""), (SLUG, "x/y"), (SLUG, "./x"), (SLUG, " /x"), (SLUG, "/x\ny"), (SLUG, "/x\x85y"),
                      (SLUG, "/x\u2028y"), (SLUG, "/x\u2029y"), (SLUG, "/x\ty"), (SLUG, "/x\x00y"))]
        for table, key, value in cases:
            with self.subTest(table=table, key=key, value=value):
                with self.assertRaises(SystemExit) as cm:
                    self.load(BASE + f'[{table}]\n"{key}" = {json.dumps(value)}\n')
                msg = str(cm.exception.code)
                self.assertTrue(msg.startswith(f"orchestrator/config.toml: {table}.{key} "), msg)
                self.assertIn(repr(value), msg)

    def test_loads(self):
        cfg = self.load(BASE)
        self.assertEqual((cfg["harness_key"], cfg["task_label_group"]), ("linear-api-key", TASK_GROUP))
        self.assertEqual(cfg["roles"]["pm"], {"account": "pm@agents.test", "key": "linear-api-key-pm", "next": "engineer"})
        self.assertEqual((cfg["task_labels"], cfg["project_repos"], cfg["local_clones"]), ({}, {}, {}))
        self.assertEqual(self.load(HEADER)["roles"], {})
        self.assertEqual(self.load(BASE + role("x", "max_runs = 3"))["roles"]["x"]["max_runs"], 3)
        self.assertEqual(self.load(BASE + '[core.roles.pm]\ntier = 1\n')["core"], {"roles": {"pm": {"tier": 1}}})
        cfg = self.load(BASE + f'[task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n')
        self.assertEqual(cfg["task_labels"], {"light-research": LABEL1, "deep-research": LABEL2})
        cfg = self.load(BASE + f'[project_repos]\n"{P1}" = "ophis/x"\n"{P2}" = "ophis/x"\n')
        self.assertEqual(cfg["project_repos"], {P1: "ophis/x", P2: "ophis/x"})


PIPELINE = HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer")
RESEARCHER = config.Role("researcher@agents.test", "linear-api-key-researcher", ("deep-research", "light-research"))
PM = config.Role("pm@agents.test", "linear-api-key-pm", ("product-design",))
ENGINEER = config.Role("engineer@agents.test", "linear-api-key-engineer", ("build", "light-build"))
GATE = f"python3 {shlex.quote(config.ROOT)}/orchestrator/src/router.py --brake"


def old_key(key):
    return (f"orchestrator/config.toml [core]: [core.roles.researcher] has {key!r}: task-level config and default_task "
            "are gone; set run keys in [core.roles.researcher] (a run without a task picks one)")


class Runnable(ConfigFile, unittest.TestCase):
    """runnable() over the repo's core config and orchestrator/config.toml's [core]."""
    def runs(self, text=PIPELINE):
        return config.runnable(self.load(text))

    def fails(self, message, text):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        self.assertEqual(cm.exception.code, message)

    def test_roles_in_pipeline_order_tasks_from_the_index(self):
        runs = self.runs()
        self.assertEqual(runs, {"researcher": RESEARCHER, "pm": PM, "engineer": ENGINEER})
        self.assertEqual(list(runs), ["researcher", "pm", "engineer"])
        self.assertEqual({r: x.tasks for r, x in runs.items()}, {r: config.role_tasks(r) for r in runs})
        self.assertEqual(config.role_tasks("researcher"), tuple(config.compose.index(config.CORE, "researcher")))
        self.assertFalse(hasattr(RESEARCHER, "default"))
        runs = self.runs(HEADER + role("engineer") + role("researcher", "max_runs = 3"))
        self.assertEqual([(r, x.max_runs) for r, x in runs.items()], [("engineer", 1), ("researcher", 3)])
        text = PIPELINE + f'[task_labels]\nlight-research = "{LABEL1}"\nbuild = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])

    def test_tasks_by_role(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        self.assertEqual({r: (x.kind, x.prefix, x.files) for r, x in config.TASKS.items()},
                         {"researcher": ("research", "", False), "pm": ("design", "PRD", False),
                          "engineer": ("build", "ENG", True)})

    def test_rejected(self):
        cases = [
            (PIPELINE + role("ghost"), "role 'ghost' is not in core/config.toml"),
            (PIPELINE + role("dummy-tester"), "role 'dummy-tester' has no entry in config.TASKS"),
            (HEADER + role("researcher", 'next = "pm"'), "next of 'researcher' names undefined role 'pm'"),
            (HEADER + role("researcher") + role("pm", 'next = "researcher"'),
             "next of 'pm' is role 'researcher', whose config.TASKS entry has no prefix"),
            (PIPELINE.replace("linear-api-key-pm", "linear-api-key-researcher"),
             "[roles.pm] key 'linear-api-key-researcher' is also [roles.researcher]'s"),
            (PIPELINE.replace("linear-api-key-engineer", "linear-api-key"), "[roles.engineer] key 'linear-api-key' is harness_key"),
            *[(text + f'[task_labels]\n{task} = "{LABEL1}"\n',
               f"task_labels.{task} is not a task of a role in orchestrator/config.toml")
              for task, text in (("echo", PIPELINE), ("ghost", PIPELINE), ("product-design", HEADER + role("researcher")))],
        ]
        for text, message in cases:
            with self.subTest(message):
                self.fails("orchestrator/config.toml: " + message, text)

    def test_role_for(self):
        runs = self.runs()
        for email in ("researcher@agents.test", "RESEARCHER@Agents.Test"):
            self.assertEqual(config.role_for(runs, email), "researcher")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(config.role_for(runs, email))

    def test_run_config(self):
        for task in (None, "deep-research", "light-research"):
            with self.subTest(task):
                run = config.run_config("researcher", task)
                self.assertEqual((run.gate, run.task), (GATE, task or ""))
        self.assertEqual(config.overlay(), {"roles": {"researcher": {"gate": GATE}}})
        self.assertEqual(config.layers(), [config.overlay()])
        self.assertEqual(config.run_config("pm").language, "Chinese")

    def test_run_config_layers(self):
        core = os.path.join(self.dir, "core")
        for task in (None, "product-design"):
            with self.subTest(task), mock.patch.object(config, "overlay", return_value={"tier": 3}) as overlay, \
                    mock.patch.object(config.clients, "load_config", return_value={"flags": []}) as client, \
                    mock.patch.object(config.compose, "load_run", return_value="run") as load_run:
                self.assertEqual(config.run_config("pm", task, self.dir), "run")
                overlay.assert_called_once_with(self.dir)
                client.assert_called_once_with("claude", core)
                load_run.assert_called_once_with(core, "pm", task, layers=[{"flags": []}, {"tier": 3}])

    def test_clones_match_core(self):
        self.assertEqual(config.CLONES, ("src", "publish"))
        with open(os.path.join(config.CORE, config.compose.CONFIG)) as f:
            dirs = re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read())
        self.assertTrue(dirs)
        self.assertEqual(set(dirs), {config.CLONES[0]})
        with open(os.path.join(config.CORE, "output", "destinations", "github.md")) as f:
            self.assertIn(f"`<Workdir>/{config.CLONES[1]}`", f.read())
        self.assertEqual(config.CORE, os.path.join(config.ROOT, "core"))


CORE_TOML = """tier = 2
effort = "high"
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


class OtherRoot(ConfigFile, unittest.TestCase):
    """A repo root with its own orchestrator/config.toml (load() writes it) and core/config.toml; core's text and
    [clients.*] tables are the real ones."""
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.dir, "my root")
        self.dir = os.path.join(self.root, "orchestrator")
        os.makedirs(self.dir)
        os.makedirs(os.path.join(self.root, "core", "config"))
        for rel in ("team", "output"):
            os.symlink(os.path.join(config.CORE, rel), os.path.join(self.root, "core", rel))
        self.core(CORE_TOML)

    def core(self, text):
        with open(os.path.join(config.CORE, config.compose.CONFIG)) as f:
            real = f.read()
        with open(os.path.join(self.root, "core", config.compose.CONFIG), "w") as f:
            f.write(text + real[real.index("\n[clients."):])

    def researcher_line(self, task, line):
        """Replaces `task`'s line of researcher's index in a copy of core's team text."""
        team = os.path.join(self.root, "core", "team")
        os.remove(team)
        shutil.copytree(os.path.join(config.CORE, "team"), team)
        path = os.path.join(team, "roles", "researcher.md")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(re.sub(rf"^- `{task}` .*\n", line, text, count=1, flags=re.M))

    def runs(self, text=PIPELINE):
        return config.runnable(self.load(text), root=self.root)

    def fails(self, message, text=PIPELINE):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        self.assertEqual(cm.exception.code, message)

    def test_tasks_are_the_roots_index(self):
        self.researcher_line("light-research", "")
        self.assertEqual((self.runs()["researcher"].tasks, config.role_tasks("researcher", self.root)),
                         (("deep-research",), ("deep-research",)))

    def test_an_index_task_without_its_file_stops_the_caller(self):
        self.researcher_line("light-research", "- `ghost` (`<tasks>/ghost.md`): a task; when; light.\n")
        self.fails("core: roles/researcher.md: lists 'ghost' without tasks/ghost.md")

    def test_overlay_fills_root(self):
        self.load(PIPELINE + '[core]\ntier = 3\ncommands = ["ls {{root}}/a", "true"]\n'
                  '[core.roles.researcher]\ngate = "python3 {{root}}/x --y {{root}}"\n[core.roles.pm]\nlanguage = "French"\n')
        q = shlex.quote(self.root)
        self.assertTrue(q.startswith("'"), q)
        want = {"tier": 3, "commands": [f"ls {q}/a", "true"],
                "roles": {"researcher": {"gate": f"python3 {q}/x --y {q}"}, "pm": {"language": "French"}}}
        self.assertEqual(config.overlay(self.root), want)
        self.assertEqual(config.layers(self.root), [want])
        run = config.run_config("researcher", root=self.root)
        self.assertEqual((run.tier, run.commands, run.gate), (3, [f"ls {q}/a", "true"], f"python3 {q}/x --y {q}"))
        run = config.run_config("pm", root=self.root)
        self.assertEqual((run.gate, run.language), ("", "French"))

    def test_a_trusted_dir_holding_the_work_dir_stops_the_caller(self):
        work = os.path.join(os.path.realpath(self.dir), "state", "agent-pm")
        os.makedirs(work)
        with mock.patch.object(config, "WORK_DIR", work):
            for entry in (work, os.path.dirname(work)):
                with self.subTest(entry):
                    self.core(f'trusted_dirs = ["{entry}"]\n' + CORE_TOML)
                    self.fails(f"core: trusted_dirs: {entry} is or contains work_dir {work}")
            self.core(f'trusted_dirs = ["{os.path.join(work, "x")}", "{self.dir}/other"]\n' + CORE_TOML)
            self.runs()

    def test_no_core_table(self):
        self.load(PIPELINE)
        self.assertEqual((config.overlay(self.root), config.layers(self.root)), ({}, [{}]))
        self.assertEqual(config.run_config("researcher", root=self.root).gate, "")

    def test_overlay_unknown_keys(self):
        unknown = "orchestrator/config.toml [core]: unknown key "
        cases = [("[core]\nfoo = 1\n", unknown + "'foo' in the global table"),
                 ('[core]\nusers = ["octocat"]\n', unknown + "'users' in the global table"),
                 ('[core]\ntrusted_dirs = ["~/x"]\n', unknown + "'trusted_dirs' in the global table"),
                 ("[core]\nworkers_per_column = 2\n", unknown + "'workers_per_column' in the global table"),
                 ('[core]\ngrid_retile = "off"\n', unknown + "'grid_retile' in the global table"),
                 ('[core]\nteam_dirs = ["~/.agent-pm/team"]\n', unknown + "'team_dirs' in the global table"),
                 ("[core.clients.claude]\nflags = []\n", unknown + "'clients' in the global table"),
                 ("[core.roles.researcher]\nfoo = 1\n", unknown + "'foo' in roles.researcher")]
        cases += [('[core.roles.researcher]\ndefault_task = "light-research"\nfoo = 1\n', old_key("default_task")),
                  ('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n', old_key("tasks"))]
        for text, message in cases:
            with self.subTest(message):
                self.load(PIPELINE + text)
                with self.assertRaises(SystemExit) as cm:
                    config.overlay(self.root)
                self.assertEqual(cm.exception.code, message)

    def test_core_errors(self):
        """A core config error, in core's file or the overlay, stops the caller."""
        cases = [('trusted_dirs = ["rel/dir"]\n' + CORE_TOML, PIPELINE,
                  "core: trusted_dirs: 'rel/dir' is not an absolute or expandable ~ path"),
                 (CORE_TOML, PIPELINE + '[core]\ncwd = "rel"\n', "core: cwd must be a printable absolute or ~ path, got 'rel'"),
                 (CORE_TOML.replace("[roles.engineer]\n", "[roles.engineer]\ntier = 9\n"), PIPELINE,
                  "core: tier must be an integer 1–4, got 9"),
                 (CORE_TOML, PIPELINE + '[core.roles.researcher]\ngate = "echo `id`"\n',
                  "core: gate must be one line of shell command without backticks")]
        for core, text, message in cases:
            with self.subTest(message):
                self.core(core)
                self.fails(message, text)

    def test_docs(self):
        runs = self.runs()
        self.assertEqual(config.docs(runs, self.root), config.Docs("acme/notes", "trunk", DIRS))
        self.assertEqual(config.docs({"pm": runs["pm"], "engineer": runs["engineer"]}, self.root),
                         config.Docs("acme/notes", "trunk", {"pm": "Designs/"}))
        self.core(CORE_TOML.replace(DESIGN, DESIGN.replace(" }", ', host = "github.com" }')))
        self.assertEqual(config.docs(runs, self.root), config.Docs("acme/notes", "trunk", DIRS))

    def test_docs_mismatch(self):
        message = "core: document roles must publish to one github.com repo and branch (their [output] in ~/.agent-pm/core.local.toml)"
        for design in (DESIGN.replace("acme/notes", "acme/other"), DESIGN.replace('"trunk"', '"main"'),
                       DESIGN.replace(" }", ', host = "ghe.example.com" }')):
            with self.subTest(design):
                self.core(CORE_TOML.replace(DESIGN, design))
                self.fails(message)
        self.core(CORE_TOML)
        self.fails(message, HEADER + role("engineer"))


DEPLOYMENT = {"team", "states", "human_members", "harness_key", "task_label_group", "task_labels", "project_repos",
              "local_clones", "work_dir"}


class Local(ConfigFile, unittest.TestCase):
    """config.toml with the local file on top."""
    def setUp(self):
        super().setUp()
        self.home = hermetic.home(self)

    def local(self, text):
        with open(os.path.join(self.home, "orchestrator.local.toml"), "w") as f:
            f.write(text)

    def test_local_on_top(self):
        self.local(HEADER + 'human_members = ["b@x.com"]\n' + role("researcher", "max_runs = 2") + role("pm")
                   + f'[project_repos]\n"{P2}" = "ophis/y"\n')
        cfg = self.load('human_members = ["a@x.com", "c@x.com"]\n[roles.researcher]\nnext = "pm"\nmax_runs = 3\n'
                        f'[project_repos]\n"{P1}" = "ophis/x"\n[core.roles.pm]\ntier = 1\n')
        self.assertEqual((cfg["team"], cfg["states"], cfg["human_members"]), (TEAM, STATES, ["b@x.com"]))
        self.assertEqual(cfg["roles"], {"researcher": {"next": "pm", "max_runs": 2, "account": "researcher@agents.test",
                                                       "key": "linear-api-key-researcher"},
                                        "pm": {"account": "pm@agents.test", "key": "linear-api-key-pm"}})
        self.assertEqual(cfg["project_repos"], {P1: "ophis/x", P2: "ophis/y"})
        self.assertEqual(cfg["core"], {"roles": {"pm": {"tier": 1}}})

    def test_local_alone_and_no_local(self):
        self.local(BASE)
        self.assertEqual(self.load("")["roles"], self.load(BASE)["roles"])
        os.remove(os.path.join(self.home, "orchestrator.local.toml"))
        self.assertEqual(self.load(BASE)["team"], TEAM)

    def test_old_task_keys_in_the_local_overlay_stop_runnable(self):
        self.local('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n')
        with self.assertRaises(SystemExit) as cm:
            config.runnable(self.load(PIPELINE))
        self.assertEqual(cm.exception.code, old_key("tasks"))

    def test_a_bad_local_value_is_rejected(self):
        self.local(f'team = "{TEAM}"\nstates = {{ done = "Done" }}\n')
        with self.assertRaises(SystemExit) as cm:
            self.load(BASE)
        self.assertEqual(cm.exception.code, "orchestrator/config.toml: states.done must be a Linear workflow state id (UUID): 'Done'")


STEPS = ("\n## Steps\n\n1. **Report progress:**\n   [agent-pm-progress:start] the topic\n2. **Write.** Write it.\n\n"
         "## Resume\n\nRedo step 2.\n")
PM_EXT = "## Tasks\n\n- `one-pager` (`<tasks>/one-pager.md`): a one-page brief; when asked; light.\n"


class TeamDirsIgnored(unittest.TestCase):
    """The orchestrator never reads core's team_dirs (a drive.py-only key): its roles, tasks and prompts are core's."""
    def setUp(self):
        self.home = hermetic.home(self)
        self.team = os.path.realpath(os.path.join(self.home, "team"))
        for rel, text in (("roles/pm.md", PM_EXT), ("tasks/one-pager.md", "# One-Pager\n" + STEPS)):
            path = os.path.join(self.team, rel)
            for d in (self.team, os.path.dirname(path)):
                os.makedirs(d, exist_ok=True)
                os.chmod(d, 0o755)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
        self.work = os.path.join(os.path.dirname(self.home), "work")

    def seen(self):
        """role_tasks, run_config and drive.plan (as run.py calls it) for pm."""
        params = config.compose.RunParams(input="x", out=os.path.join(self.work, "out.md"), workdir=self.work,
                                          sid="11111111-2222-3333-4444-555555555555")
        launch, run = drive.plan(config.CORE, clients.get("claude", config.CORE), "pm", params=params,
                                 layers=config.layers(), cwd=self.work)
        return config.role_tasks("pm"), config.run_config("pm"), launch, run

    def test_team_dirs_change_no_orchestrator_run(self):
        before = self.seen()
        with open(os.path.join(self.home, "core.local.toml"), "w") as f:
            f.write(f"team_dirs = {json.dumps([self.team])}\n[roles.writer]\ntier = 3\n")
        self.assertIn("one-pager", config.compose.index(config.CORE, "pm", config.compose.team(
            config.CORE, config.compose.team_dirs(config.CORE))))
        after = self.seen()
        self.assertEqual(after, before)
        self.assertNotIn("one-pager", after[0])
        self.assertIsNone(after[2].view)
        self.assertNotIn("one-pager", after[2].argv[2])
        self.assertFalse(os.path.exists(self.work))


class RealConfig(unittest.TestCase):
    """The repo's committed orchestrator/config.toml with a fixture local file, core config and TASKS together."""
    def setUp(self):
        self.local = os.path.join(hermetic.home(self), "orchestrator.local.toml")

    def load(self, local=None):
        if local is not None:
            with open(self.local, "w") as f:
                f.write(local)
        return config.load_config()

    def test_committed_config_holds_no_deployment_values(self):
        with open(config.CONFIG, "rb") as f:
            committed = tomllib.load(f)
        self.assertFalse(DEPLOYMENT & set(committed))
        self.assertEqual({r: set(p) & {"account", "key"} for r, p in committed["roles"].items()},
                         {"researcher": set(), "pm": set(), "engineer": set()})

    def test_without_local_names_what_is_missing(self):
        with self.assertRaises(SystemExit) as cm:
            self.load()
        self.assertEqual(cm.exception.code, "orchestrator/config.toml: " + missing(
            "team, harness_key, task_label_group, states.todo, states.in_progress, states.in_review, states.handoff, "
            "states.done, states.canceled, roles.researcher.account, roles.researcher.key, roles.pm.account, roles.pm.key, "
            "roles.engineer.account, roles.engineer.key"))

    def test_real_config(self):
        cfg = self.load(HEADER + role("researcher") + role("pm") + role("engineer"))
        runs = config.runnable(cfg)
        self.assertEqual(runs, {
            "researcher": config.Role("researcher@agents.test", "linear-api-key-researcher", ("deep-research", "light-research"), 3),
            "pm": config.Role("pm@agents.test", "linear-api-key-pm", ("product-design",), 3),
            "engineer": config.Role("engineer@agents.test", "linear-api-key-engineer", ("build", "light-build"), 3)})
        self.assertEqual(list(runs), ["researcher", "pm", "engineer"])
        self.assertEqual({r: p["max_runs"] for r, p in cfg["roles"].items()}, {"researcher": 3, "pm": 3, "engineer": 3})
        self.assertEqual(config.docs(runs), config.Docs("ophis/private_docs", "main", {
            "researcher": "Research/", "pm": "Product Design/"}))
        self.assertEqual(cfg["team"], TEAM)
        self.assertNotIn("docs", cfg)
        self.assertEqual(cfg["core"], {"roles": {"researcher": {"gate": "python3 {{root}}/orchestrator/src/router.py --brake"}}})
        self.assertEqual((cfg["task_labels"], cfg["project_repos"], cfg["local_clones"]), ({}, {}, {}))
        self.assertEqual(config.stage_order(cfg), {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertIs(cfg["roles"]["pm"]["require_instructions"], False)

    def test_local_lines_uncommented_set_every_required_key(self):
        with open(config.CONFIG) as f:
            local = "".join(line.removeprefix("# local: ") for line in f if line.startswith("# local: "))
        with open(self.local, "w") as f:
            f.write(local)
        self.assertEqual(config._missing(repo.read_config(config.CONFIG, config.LOCAL)), [])


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


class Clones(ConfigFile, unittest.TestCase):
    """Real git clones (no network) in self.dir, a realpath."""
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
        """[(name, path, reason)]: what is no local clone of github.com/ophis/agent-pm."""
        dotgit_file = os.path.join(self.dir, "file")
        os.makedirs(dotgit_file)
        with open(os.path.join(dotgit_file, ".git"), "w") as f:
            f.write("gitdir: /elsewhere\n")
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
            ("non-github host", gitlab, "origin is gitlab.com/ophis/agent-pm, not github.com/ophis/agent-pm"),
            ("other owner/name", other, "origin is github.com/ophis/other, not github.com/ophis/agent-pm"),
        ]


class LocalClones(Clones):
    def test_entries_stored_as_realpaths(self):
        home = os.path.join(self.dir, "home")
        real = os.path.join(home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.dir, "link"))
        with mock.patch.dict(os.environ, {"HOME": home}):
            cfg = self.load(f'work_dir = "{self.dir}/w"\n' + BASE
                            + f'[local_clones]\n"{SLUG}" = "{self.dir}/link"\n"ophis/x" = "~/real"\n"ophis/y" = "~"\n')
        self.assertEqual(cfg["local_clones"], {SLUG: real, "ophis/x": real, "ophis/y": home})


class CloneError(Clones):
    def test_matching_origin(self):
        for name, origin, slug in (("https", HTTPS, SLUG), ("scp", "git@github.com:ophis/agent-pm.git", SLUG),
                                   ("bare", "https://github.com/ophis/agent-pm", SLUG), ("case", HTTPS, "OPHIS/Agent-PM"),
                                   ("origin case", "https://GitHub.com/Ophis/AGENT-PM.git", SLUG)):
            with self.subTest(name):
                self.assertIsNone(config.clone_error(self.clone(name, origin), slug))

    def test_reasons(self):
        bad = self.bad() + [(repr(path), path, f"{path!r} has a non-printable character") for path in ("/x\ny", "/x\x85y", "/x\u2028y")]
        for name, path, reason in bad:
            with self.subTest(name):
                self.assertEqual(config.clone_error(path, SLUG), reason)

    def test_run_failures_are_reasons(self):
        clone = self.clone()
        for error in (OSError("boom"), subprocess.TimeoutExpired(["git"], 10),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")):
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
        self.assertEqual(calls, [(["git", *repo.GUARD, "-C", clone, "remote", "get-url", "origin"], repo.SHORT)])


class LocalClonesRunnable(Clones):
    def runs(self, key, path):
        return config.runnable(self.load(PIPELINE + f'[local_clones]\n"{key}" = {json.dumps(path)}\n'))

    def test_each_entry_is_checked_and_a_bad_one_names_its_key(self):
        self.assertEqual(list(self.runs(SLUG, self.clone())), ["researcher", "pm", "engineer"])
        for name, path, reason in self.bad():
            with self.subTest(name):
                with self.assertRaises(SystemExit) as cm:
                    self.runs(SLUG, path)
                self.assertEqual(cm.exception.code, f"orchestrator/config.toml: local_clones.{SLUG}: {reason}")

    def test_no_entries_runs_no_git(self):
        with mock.patch.object(config, "clone_error") as clone_error:
            self.assertEqual(list(config.runnable(self.load(PIPELINE))), ["researcher", "pm", "engineer"])
        clone_error.assert_not_called()


class Writable(unittest.TestCase):
    def test_work_then_the_temp_dirs(self):
        with mock.patch.object(repo, "temp_dirs", return_value=("/t1", "/t2")):
            self.assertEqual(config.writable("/w"), ("/w", "/t1", "/t2"))
            self.assertEqual(config.writable(), (config.RUNS_DIR, "/t1", "/t2"))


class RepoSlug(unittest.TestCase):
    def test_slug(self):
        cases = {"ophis/agent-pm": ("ophis", "agent-pm"), "ophis/.github": ("ophis", ".github"),
                 "-a/b": None, "a/-b": None, "a/..": None, "a/.": None, "a b/c": None, "ophis": None,
                 "https://github.com/ophis/x": None, "": None, None: None, 42: None}
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(config.repo_slug(value), want)


class WorkDir(ConfigFile, unittest.TestCase):
    def test_unset_is_in_home(self):
        work = os.path.realpath(os.path.expanduser("~/.agent-pm"))
        self.assertEqual((config.WORK_DIR, config.RUNS_DIR, config.LOGS_DIR, config.RUNS_LOG),
                         (work, work + "/work", work + "/logs", work + "/logs/runs.jsonl"))
        home = os.path.realpath(self.dir)
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(config.work_dir({}, os.path.join(home, "repo")), home + "/.agent-pm")
            with self.assertRaises(SystemExit) as cm:
                self.load(BASE + '[local_clones]\n"ophis/x" = "~"\n')
        self.assertEqual(cm.exception.code, "orchestrator/config.toml: work_dir must neither lie in nor contain "
                                            f"local_clones.ophis/x {home}: '~/.agent-pm'")

    def test_set(self):
        home = os.path.realpath(self.dir)
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(config.work_dir({"work_dir": "~/d"}, os.path.join(home, "repo")), os.path.join(home, "d"))
        cfg = self.load(f'work_dir = "{home}/d"\n' + BASE + f'[local_clones]\n"ophis/x" = "{home}/d-c"\n"ophis/y" = "{home}/c"\n')
        self.assertEqual(cfg["work_dir"], f"{home}/d")

    def test_refused(self):
        real = os.path.realpath(config.ROOT)
        d = os.path.realpath(self.dir)
        link, clink = os.path.join(d, "link"), os.path.join(d, "clink")
        os.symlink(real, link)
        os.makedirs(os.path.join(d, "c"))
        os.symlink(os.path.join(d, "c"), clink)
        repo = f"work_dir must neither lie in nor contain the repo {real}: "
        clone = lambda path: f'[local_clones]\n"ophis/x" = "{path}"\n'
        in_clone = lambda path: f"work_dir must neither lie in nor contain local_clones.ophis/x {path}: "
        cases = [(f'work_dir = "{real}"\n', "", f"{repo}{real!r}"),
                 (f'work_dir = "{real}/w"\n', "", f"{repo}{real + '/w'!r}"),
                 (f'work_dir = "{link}/w"\n', "", f"{repo}{link + '/w'!r}"),
                 (f'work_dir = "{os.path.dirname(real)}"\n', "", f"{repo}{os.path.dirname(real)!r}"),
                 (f'work_dir = "{d}/c"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{d + '/c'!r}"),
                 (f'work_dir = "{clink}"\n', clone(f"{d}/c/x"), f"{in_clone(d + '/c/x')}{clink!r}"),
                 (f'work_dir = "{clink}/w"\n', clone(f"{d}/c"), f"{in_clone(d + '/c')}{clink + '/w'!r}"),
                 ('work_dir = "w"\n', "", "work_dir must be a printable absolute or ~ path: 'w'"),
                 ("work_dir = 5\n", "", "work_dir must be a printable absolute or ~ path: 5")]
        for text, tail, msg in cases:
            with self.subTest(text=text, tail=tail), self.assertRaises(SystemExit) as cm:
                self.load(text + BASE + tail)
            self.assertEqual(cm.exception.code, f"orchestrator/config.toml: {msg}")


class Paths(unittest.TestCase):
    def test_run_dir_and_transcript(self):
        self.assertEqual(config.run_dir("TASK-9"), os.path.join(config.RUNS_DIR, "TASK-9"))
        sid = "0f0f0f0f-1111-2222-3333-444444444444"
        with tempfile.TemporaryDirectory() as work, mock.patch.object(config, "RUNS_DIR", work):
            rd = config.run_dir("TASK-9")
            self.assertEqual(config.transcript("TASK-9", sid, projects="/p"), clients.claude.transcript(rd, sid, "/p"))
            os.makedirs(rd)
            with open(os.path.join(rd, "run.jsonl"), "w") as f:
                f.write(json.dumps({"ts": "t", "kind": "session", "sid": sid, "cwd": "/data/x.y",
                                    "project": True}) + "\n")
            self.assertEqual(config.transcript("TASK-9", sid, projects="/p"), f"/p/-data-x-y/{sid}.jsonl")
        self.assertIsNone(config.transcript("TASK-9", "../x"))


class PathSeam(unittest.TestCase):
    """config.PATH is computed at import: each case reads it in a fresh process."""
    def read(self, **env):
        tests = os.path.dirname(os.path.abspath(hermetic.__file__))
        env = {**{k: v for k, v in os.environ.items() if k != "AGENT_PM_PATH"}, **env,
               "PYTHONPATH": os.path.dirname(tests)}
        res = subprocess.run([sys.executable, "-c", "import hermetic, config, json, os; print(json.dumps([os.environ['HOME'], config.PATH]))"],
                             cwd=tests, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        home, path = json.loads(res.stdout)
        return f"/opt/homebrew/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", path

    def test_agent_pm_path_goes_in_front_empty_or_unset_is_the_default(self):
        for env, front in (({"AGENT_PM_PATH": "/fakes/bin:/more"}, "/fakes/bin:/more:"), ({}, ""), ({"AGENT_PM_PATH": ""}, "")):
            with self.subTest(env=env):
                default, path = self.read(**env)
                self.assertEqual(path, front + default)


class Shell(unittest.TestCase):
    def test_sh_run(self):
        with mock.patch.object(config.subprocess, "run", return_value="res") as run, \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "KEEP": "1"}):
            self.assertEqual(config.sh_run(["git", "status"], 5), "res")
            env = {**os.environ, "PATH": config.PATH}
        run.assert_called_once_with(["git", "status"], capture_output=True, text=True, timeout=5,
                                    stdin=subprocess.DEVNULL, env=env)
        self.assertEqual(env["KEEP"], "1")


if __name__ == "__main__":
    unittest.main()
