import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import HEADER, STATES, TASK_GROUP, TEAM, role  # noqa: E402
import config  # noqa: E402
import clients  # noqa: E402
import repo  # noqa: E402

BASE = HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer") + role("solo")

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
OTHER = "00000000-0000-4000-8000-000000000023"


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
    def test_stage_order(self):
        self.assertEqual(config.stage_order(self.load(BASE)),
                         {"researcher": 0, "pm": 1, "engineer": 2, "solo": 0})

    def test_rejected(self):
        states = "states = { " + ", ".join(f'{k} = "{v}"' for k, v in STATES.items()) + " }\n"
        body = BASE[BASE.index("[roles"):]
        team = f'team = "{TEAM}"\n'
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
            (x + 'key = "k-x"\n', "[roles.x] account must be the role's Linear email: None"),
            (x + 'account = ""\nkey = "k-x"\n', "[roles.x] account must be the role's Linear email: ''"),
            (x + 'account = 5\nkey = "k-x"\n', "[roles.x] account must be the role's Linear email: 5"),
            (x + 'account = "x@agents.test"\n', "[roles.x] key must be a Keychain service name: None"),
            (x + 'account = "x@agents.test"\nkey = ""\n', "[roles.x] key must be a Keychain service name: ''"),
            (x + 'account = "x@agents.test"\nkey = ["k"]\n', "[roles.x] key must be a Keychain service name: ['k']"),
            *[(BASE + role("x", f"max_runs = {value}"), f"[roles.x] max_runs must be a whole number >= 1: {shown}")
              for shown, value in {"0": "0", "-1": "-1", "'2'": '"2"', "True": "true", "1.5": "1.5"}.items()],
            (BASE.replace('harness_key = "linear-api-key"\n', ""), "harness_key must be a Keychain service name: None"),
            (BASE.replace('harness_key = "linear-api-key"', "harness_key = 1"), "harness_key must be a Keychain service name: 1"),
            ('team = "Frank\'s Agents"\n' + states + body, "team must be a Linear team id (UUID): \"Frank's Agents\""),
            (states + body, "team must be a Linear team id (UUID): None"),
            (team + body, "[states] is missing: todo, in_progress, in_review, handoff, done, canceled"),
            (team + states.replace(f', canceled = "{STATES["canceled"]}"', "") + body, "[states] is missing: canceled"),
            (team + states.replace(" }", ', backlog = "x" }') + body, "[states] has unknown keys: backlog"),
            (team + states.replace(STATES["done"], "Done") + body, "states.done must be a Linear workflow state id (UUID): 'Done'"),
            (BASE.replace(f'task_label_group = "{TASK_GROUP}"\n', ""), group + "None"),
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
        ]
        for text, message in cases:
            with self.subTest(message, text=text):
                with self.assertRaises(SystemExit) as cm:
                    self.load(text)
                self.assertEqual(str(cm.exception.code), "orchestrator/config.toml: " + message)
        cfg = self.load(BASE)
        self.assertEqual((cfg["harness_key"], cfg["task_label_group"]), ("linear-api-key", TASK_GROUP))
        self.assertEqual(cfg["roles"]["pm"], {"account": "pm@agents.test", "key": "linear-api-key-pm", "next": "engineer"})
        self.assertEqual(self.load(HEADER)["roles"], {})
        self.assertEqual(self.load(BASE + role("x", "max_runs = 3"))["roles"]["x"]["max_runs"], 3)
        self.assertEqual(self.load(BASE + '[core.roles.pm]\ntier = 1\n')["core"], {"roles": {"pm": {"tier": 1}}})

    def test_task_labels_loads(self):
        self.assertEqual(self.load(BASE)["task_labels"], {})
        cfg = self.load(BASE + f'[task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n')
        self.assertEqual(cfg["task_labels"], {"light-research": LABEL1, "deep-research": LABEL2})


PIPELINE = HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer")
RESEARCHER = config.Role("researcher@agents.test", "linear-api-key-researcher", ("deep-research", "light-research"))
PM = config.Role("pm@agents.test", "linear-api-key-pm", ("product-design",))
ENGINEER = config.Role("engineer@agents.test", "linear-api-key-engineer", ("engineering",))
GATE = f"python3 {shlex.quote(config.ROOT)}/orchestrator/src/router.py --brake"


class Runnable(ConfigFile, unittest.TestCase):
    """runnable() over the repo's core config and orchestrator/config.toml's [core]."""
    def runs(self, text=PIPELINE):
        return config.runnable(self.load(text))

    def fails(self, message, text):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        self.assertEqual(cm.exception.code, message)

    def test_roles_in_pipeline_order_default_first(self):
        runs = self.runs()
        self.assertEqual(runs, {"researcher": RESEARCHER, "pm": PM, "engineer": ENGINEER})
        self.assertEqual(list(runs), ["researcher", "pm", "engineer"])
        self.assertEqual((RESEARCHER.default, PM.default), ("deep-research", "product-design"))
        self.assertEqual(list(self.runs(HEADER + role("engineer") + role("researcher"))), ["engineer", "researcher"])

    def test_max_runs(self):
        self.assertEqual({r: x.max_runs for r, x in self.runs().items()}, {"researcher": 1, "pm": 1, "engineer": 1})
        runs = self.runs(HEADER + role("researcher", "max_runs = 3") + role("engineer"))
        self.assertEqual((runs["researcher"].max_runs, runs["engineer"].max_runs), (3, 1))

    def test_role_not_in_core(self):
        self.fails("orchestrator/config.toml: role 'ghost' is not in core/config.toml", PIPELINE + role("ghost"))

    def test_task_without_tasks_entry(self):
        self.fails("orchestrator/config.toml: dummy-tester's task 'echo' has no entry in config.TASKS", PIPELINE + role("dummy-tester"))

    def test_next(self):
        self.fails("orchestrator/config.toml: next of 'researcher' names undefined role 'pm'", HEADER + role("researcher", 'next = "pm"'))
        self.fails("orchestrator/config.toml: next of 'pm' is role 'researcher', whose default task 'deep-research' has no prefix",
                   HEADER + role("researcher") + role("pm", 'next = "researcher"'))

    def test_keys(self):
        self.fails("orchestrator/config.toml: [roles.pm] key 'linear-api-key-researcher' is also [roles.researcher]'s",
                   PIPELINE.replace("linear-api-key-pm", "linear-api-key-researcher"))
        self.fails("orchestrator/config.toml: [roles.engineer] key 'linear-api-key' is harness_key",
                   PIPELINE.replace("linear-api-key-engineer", "linear-api-key"))

    def test_task_labels_keys(self):
        text = PIPELINE + f'[task_labels]\nlight-research = "{LABEL1}"\nengineering = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])
        for task, text in (("echo", PIPELINE), ("ghost", PIPELINE), ("product-design", HEADER + role("researcher"))):
            with self.subTest(task):
                self.fails(f"orchestrator/config.toml: task_labels.{task} is not a task of a role in orchestrator/config.toml",
                           text + f'[task_labels]\n{task} = "{LABEL1}"\n')

    def test_role_for(self):
        runs = self.runs()
        for email in ("researcher@agents.test", "RESEARCHER@Agents.Test"):
            self.assertEqual(config.role_for(runs, email), "researcher")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(config.role_for(runs, email))

    def test_run_config(self):
        self.assertEqual(config.run_config("researcher", "deep-research").gate, GATE)
        self.assertEqual(config.run_config("researcher", "light-research").gate, "")
        self.assertEqual(config.overlay(), {"roles": {"researcher": {"tasks": {"deep-research": {"gate": GATE}}}}})
        self.assertEqual(config.layers(), [config.overlay()])
        self.assertEqual(config.run_config("pm", "product-design").language, "Chinese")

    def test_run_config_layers(self):
        core = os.path.join(self.dir, "core")
        with mock.patch.object(config, "overlay", return_value={"tier": 3}) as overlay, \
                mock.patch.object(config.clients, "load_config", return_value={"flags": []}) as client, \
                mock.patch.object(config.compose, "load_run", return_value="run") as load_run:
            self.assertEqual(config.run_config("pm", "product-design", self.dir), "run")
        overlay.assert_called_once_with(self.dir)
        client.assert_called_once_with("claude", core)
        load_run.assert_called_once_with(core, "pm", "product-design", layers=[{"flags": []}, {"tier": 3}])

    def test_clones_match_core(self):
        self.assertEqual(config.CLONES, ("src", "publish"))
        with open(os.path.join(config.CORE, config.compose.CONFIG)) as f:
            dirs = re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read())
        self.assertTrue(dirs)
        self.assertEqual(set(dirs), {config.CLONES[0]})
        with open(os.path.join(config.CORE, "output", "destinations", "github.md")) as f:
            self.assertIn(f"`<Workdir>/{config.CLONES[1]}`", f.read())

    def test_paths(self):
        self.assertEqual(config.CORE, os.path.join(config.ROOT, "core"))


CORE_TOML = """tier = 2
effort = "high"
output = { type = "local" }

[roles.researcher]
default_task = "deep-research"

[roles.researcher.tasks.deep-research]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Research/" }

[roles.researcher.tasks.light-research]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Research/" }

[roles.pm]
default_task = "product-design"

[roles.pm.tasks.product-design]
output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }

[roles.engineer]
default_task = "engineering"

[roles.engineer.tasks.engineering]
output = { type = "pull-request" }
"""
DESIGN = 'output = { type = "github", repo = "acme/notes", branch = "trunk", dir = "Designs/" }'
DIRS = {"deep-research": "Research/", "light-research": "Research/", "product-design": "Designs/"}


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

    def runs(self, text=PIPELINE):
        return config.runnable(self.load(text), root=self.root)

    def fails(self, message, text=PIPELINE):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        self.assertEqual(cm.exception.code, message)

    def test_default_task_first_then_core_order(self):
        self.core(CORE_TOML.replace('default_task = "deep-research"', 'default_task = "light-research"'))
        researcher = self.runs()["researcher"]
        self.assertEqual((researcher.tasks, researcher.default), (("light-research", "deep-research"), "light-research"))

    def test_overlay_fills_root(self):
        self.load(PIPELINE + '[core]\ntier = 3\ncommands = ["ls {{root}}/a", "true"]\n'
                  '[core.roles.researcher.tasks.deep-research]\ngate = "python3 {{root}}/x --y {{root}}"\n')
        q = shlex.quote(self.root)
        self.assertTrue(q.startswith("'"), q)
        want = {"tier": 3, "commands": [f"ls {q}/a", "true"], "roles": {"researcher": {"tasks": {"deep-research": {"gate": f"python3 {q}/x --y {q}"}}}}}
        self.assertEqual(config.overlay(self.root), want)
        self.assertEqual(config.layers(self.root), [want])
        run = config.run_config("researcher", "deep-research", self.root)
        self.assertEqual((run.tier, run.commands, run.gate), (3, [f"ls {q}/a", "true"], f"python3 {q}/x --y {q}"))
        self.assertEqual(config.run_config("pm", "product-design", self.root).gate, "")

    def test_a_bad_trusted_dirs_or_cwd_stops_the_caller(self):
        self.core('trusted_dirs = ["rel/dir"]\n' + CORE_TOML)
        self.fails("core: trusted_dirs: 'rel/dir' is not an absolute or expandable ~ path")
        self.core(CORE_TOML)
        self.fails("core: cwd must be a printable absolute or ~ path, got 'rel'", PIPELINE + '[core]\ncwd = "rel"\n')

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

    def test_overlay_language_reaches_run_config(self):
        self.load(PIPELINE + '[core.roles.pm]\nlanguage = "French"\n')
        self.assertEqual(config.run_config("pm", "product-design", self.root).language, "French")

    def test_no_core_table(self):
        self.load(PIPELINE)
        self.assertEqual((config.overlay(self.root), config.layers(self.root)), ({}, [{}]))
        self.assertEqual(config.run_config("researcher", "deep-research", self.root).gate, "")

    def test_overlay_unknown_keys(self):
        cases = [("[core]\nfoo = 1\n", "'foo' in the global table"),
                 ('[core]\nusers = ["octocat"]\n', "'users' in the global table"),
                 ('[core]\ntrusted_dirs = ["~/x"]\n', "'trusted_dirs' in the global table"),
                 ("[core.clients.claude]\nflags = []\n", "'clients' in the global table"),
                 ('[core.roles.researcher]\ndefault_task = "light-research"\n', "'default_task' in roles.researcher"),
                 ('[core.roles.researcher.tasks.deep-research]\ntasks = 1\n', "'tasks' in roles.researcher.tasks.deep-research")]
        for text, message in cases:
            with self.subTest(message):
                self.load(PIPELINE + text)
                with self.assertRaises(SystemExit) as cm:
                    config.overlay(self.root)
                self.assertEqual(cm.exception.code, "orchestrator/config.toml [core]: unknown key " + message)

    def test_core_errors(self):
        self.core(CORE_TOML.replace("[roles.engineer.tasks.engineering]\n", "[roles.engineer.tasks.engineering]\ntier = 9\n"))
        self.fails("core: tier must be an integer 1–4, got 9")
        self.core(CORE_TOML)
        self.fails("core: gate must be one line of shell command without backticks",
                   PIPELINE + '[core.roles.researcher.tasks.light-research]\ngate = "echo `id`"\n')

    def test_docs(self):
        runs = self.runs()
        self.assertEqual(config.docs(runs, self.root), config.Docs("acme/notes", "trunk", DIRS))
        self.assertEqual(config.docs({"pm": runs["pm"], "engineer": runs["engineer"]}, self.root),
                         config.Docs("acme/notes", "trunk", {"product-design": "Designs/"}))
        self.core(CORE_TOML.replace(DESIGN, DESIGN.replace(" }", ', host = "github.com" }')))
        self.assertEqual(config.docs(runs, self.root), config.Docs("acme/notes", "trunk", DIRS))

    def test_docs_mismatch(self):
        message = "core: document tasks must publish to one github.com repo and branch"
        for design in (DESIGN.replace("acme/notes", "acme/other"), DESIGN.replace('"trunk"', '"main"'),
                       DESIGN.replace(" }", ', host = "ghe.example.com" }')):
            with self.subTest(design):
                self.core(CORE_TOML.replace(DESIGN, design))
                self.fails(message)
        self.core(CORE_TOML)
        self.fails(message, HEADER + role("engineer"))


P1, P2 = "121166b1-191a-4461-bec4-42f1c2dc0ddd", "ae72ede7-67a6-469d-a959-8ea51ab71fb8"


class RealConfig(unittest.TestCase):
    """The repo's orchestrator/config.toml, core config and TASKS together."""
    def test_real_config(self):
        cfg = config.load_config()
        runs = config.runnable(cfg)
        self.assertEqual(runs, {
            "researcher": config.Role("frank.agent.w+researcher@gmail.com", "linear-api-key-researcher", ("deep-research", "light-research"), 3),
            "pm": config.Role("frank.agent.w+pm@gmail.com", "linear-api-key-pm", ("product-design",), 3),
            "engineer": config.Role("frank.agent.w+engineer@gmail.com", "linear-api-key-engineer", ("engineering",), 3)})
        self.assertEqual(list(runs), ["researcher", "pm", "engineer"])
        self.assertEqual({r: p["max_runs"] for r, p in cfg["roles"].items()}, {"researcher": 3, "pm": 3, "engineer": 3})
        self.assertEqual(config.docs(runs), config.Docs("ophis/private_docs", "main", {
            "deep-research": "Research/", "light-research": "Research/", "product-design": "Product Design/"}))
        self.assertEqual(cfg["team"], "06159b6b-5efe-4bc5-a27b-875701f40d61")
        self.assertNotIn("docs", cfg)
        self.assertEqual(cfg["core"], {"roles": {"researcher": {"tasks": {"deep-research": {
            "gate": "python3 {{root}}/orchestrator/src/router.py --brake"}}}}})
        self.assertEqual(cfg["task_labels"], {"light-research": "7cb3a7cc-05b4-4dec-bbf8-d4fce87cea1d"})
        self.assertEqual(cfg["project_repos"], {P1: "ophis/agent-pm", P2: "ophis/claude-autopilot"})
        home = os.path.expanduser("~/playground")
        self.assertEqual(cfg["local_clones"], {"ophis/agent-pm": os.path.realpath(f"{home}/agent-pm"),
                                               "ophis/claude-autopilot": os.path.realpath(f"{home}/claude-autopilot")})
        self.assertEqual(config.stage_order(cfg), {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertIs(cfg["roles"]["pm"]["require_instructions"], False)
        self.assertEqual({t: (x.kind, x.prefix) for t, x in config.TASKS.items()},
                         {"deep-research": ("research", ""), "light-research": ("research", ""),
                          "product-design": ("design", "PRD"), "engineering": ("build", "ENG")})


class ProjectRepos(ConfigFile, unittest.TestCase):
    def test_absent_is_empty(self):
        self.assertEqual(self.load(BASE)["project_repos"], {})

    def test_two_projects_one_repo(self):
        cfg = self.load(BASE + f'[project_repos]\n"{P1}" = "ophis/x"\n"{P2}" = "ophis/x"\n')
        self.assertEqual(cfg["project_repos"], {P1: "ophis/x", P2: "ophis/x"})

    def test_bad_entry_rejected(self):
        cases = [("not-a-uuid", "ophis/x"), (P1.upper(), "ophis/x"), (P1, "https://github.com/ophis/x"),
                 (P1, "ophis/."), (P1, "ophis/.."), (P1, ""), (P1, 42), (P1, "ophis")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(SystemExit) as cm:
                    self.load(BASE + f'[project_repos]\n"{key}" = {json.dumps(value)}\n')
                msg = str(cm.exception.code)
                self.assertTrue(msg.startswith(f"orchestrator/config.toml: project_repos.{key} "), msg)
                self.assertIn(repr(value), msg)

    def test_not_a_table_rejected(self):
        body = BASE[BASE.index("[roles"):]
        with self.assertRaises(SystemExit) as cm:
            self.load(HEADER + 'project_repos = "x"\n' + body)
        self.assertTrue(str(cm.exception.code).startswith("orchestrator/config.toml: project_repos"), cm.exception.code)


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True, stdin=subprocess.DEVNULL)


SLUG = "ophis/agent-pm"
HTTPS = "https://github.com/ophis/agent-pm.git"


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
    def entry(self, key, value):
        return BASE + f'[local_clones]\n"{key}" = {json.dumps(value)}\n'

    def test_absent_is_empty(self):
        self.assertEqual(self.load(BASE)["local_clones"], {})

    def test_entries_stored_as_realpaths(self):
        home = os.path.join(self.dir, "home")
        real = os.path.join(home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.dir, "link"))
        with mock.patch.dict(os.environ, {"HOME": home}):
            cfg = self.load(f'work_dir = "{self.dir}/w"\n' + BASE
                            + f'[local_clones]\n"{SLUG}" = "{self.dir}/link"\n"ophis/x" = "~/real"\n"ophis/y" = "~"\n')
        self.assertEqual(cfg["local_clones"], {SLUG: real, "ophis/x": real, "ophis/y": home})

    def test_bad_entry_rejected(self):
        cases = [("agent-pm", "/x"), ("https://github.com/ophis/agent-pm", "/x"), ("ophis/..", "/x"), (SLUG, 42), (SLUG, True),
                 (SLUG, ["/x"]), (SLUG, ""), (SLUG, "x/y"), (SLUG, "./x"), (SLUG, " /x"), (SLUG, "/x\ny"), (SLUG, "/x\x85y"),
                 (SLUG, "/x\u2028y"), (SLUG, "/x\u2029y"), (SLUG, "/x\ty"), (SLUG, "/x\x00y")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(SystemExit) as cm:
                    self.load(self.entry(key, value))
                msg = str(cm.exception.code)
                self.assertTrue(msg.startswith(f"orchestrator/config.toml: local_clones.{key} "), msg)
                self.assertIn(repr(value), msg)

    def test_not_a_table_rejected(self):
        body = BASE[BASE.index("[roles"):]
        for value in ('"/x"', '["/x"]', "5"):
            with self.subTest(value):
                with self.assertRaises(SystemExit) as cm:
                    self.load(HEADER + f"local_clones = {value}\n" + body)
                self.assertTrue(str(cm.exception.code).startswith("orchestrator/config.toml: local_clones must be a table"), cm.exception.code)

    def test_keys_equal_ignoring_case_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            self.load(BASE + f'[local_clones]\n"{SLUG}" = "/x"\n"Ophis/Agent-PM" = "/y"\n')
        self.assertEqual(cm.exception.code, f"orchestrator/config.toml: local_clones.{SLUG} and local_clones.Ophis/Agent-PM are the same repo")

    def test_two_keys_one_clone(self):
        cfg = self.load(BASE + '[local_clones]\n"ophis/a" = "/x"\n"ophis/b" = "/x"\n')
        self.assertEqual(cfg["local_clones"], {"ophis/a": os.path.realpath("/x"), "ophis/b": os.path.realpath("/x")})


class CloneError(Clones):
    def test_matching_origin(self):
        for name, origin, slug in (("https", HTTPS, SLUG), ("scp", "git@github.com:ophis/agent-pm.git", SLUG),
                                   ("bare", "https://github.com/ophis/agent-pm", SLUG), ("case", HTTPS, "OPHIS/Agent-PM"),
                                   ("origin case", "https://GitHub.com/Ophis/AGENT-PM.git", SLUG)):
            with self.subTest(name):
                self.assertIsNone(config.clone_error(self.clone(name, origin), slug))

    def test_reasons(self):
        for name, path, reason in self.bad():
            with self.subTest(name):
                self.assertEqual(config.clone_error(path, SLUG), reason)

    def test_non_printable_path(self):
        for path in ("/x\ny", "/x\x85y", "/x\u2028y"):
            with self.subTest(path=path):
                self.assertEqual(config.clone_error(path, SLUG), f"{path!r} has a non-printable character")

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

    def test_matching_origins_pass(self):
        for name, origin, key in (("https", HTTPS, SLUG), ("scp", "git@github.com:ophis/agent-pm.git", SLUG),
                                  ("case", HTTPS, "OPHIS/Agent-PM")):
            with self.subTest(name):
                self.assertEqual(list(self.runs(key, self.clone(name, origin))), ["researcher", "pm", "engineer"])

    def test_bad_entry_names_its_key(self):
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
                         (work, work + "/work", work + "/logs", work + "/logs/runs.log"))
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

    def test_logs_dir_is_unknown(self):
        with self.assertRaises(SystemExit) as cm:
            self.load('logs_dir = "/x"\n' + BASE)
        self.assertEqual(cm.exception.code, "orchestrator/config.toml: unknown keys: logs_dir")


class Paths(unittest.TestCase):
    def test_slug_and_project_log(self):
        self.assertEqual(config.slug("Deep Research"), "deep-research")
        with tempfile.TemporaryDirectory() as d:
            path = config.project_log("light-research", logs=d)
            self.assertEqual(path, os.path.join(d, "projects", "light-research.log"))
            self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_run_dir_and_transcript(self):
        self.assertEqual(config.run_dir("TASK-9"), os.path.join(config.RUNS_DIR, "TASK-9"))
        sid = "0f0f0f0f-1111-2222-3333-444444444444"
        with tempfile.TemporaryDirectory() as work, mock.patch.object(config, "RUNS_DIR", work):
            rd = config.run_dir("TASK-9")
            self.assertEqual(config.transcript("TASK-9", sid, projects="/p"), clients.claude.transcript(rd, sid, "/p"))
            os.makedirs(rd)
            with open(os.path.join(rd, "run.json"), "w") as f:
                json.dump({"sessions": [{"sid": sid, "cwd": "/data/x.y", "project": True}]}, f)
            self.assertEqual(config.transcript("TASK-9", sid, projects="/p"), f"/p/-data-x-y/{sid}.jsonl")
        self.assertIsNone(config.transcript("TASK-9", "../x"))


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
