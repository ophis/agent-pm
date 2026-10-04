import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import ACCOUNTS, HEADER, STATES, TASK_GROUP, TEAM, role, team_node  # noqa: E402
import pipeline  # noqa: E402

BASE = HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer") + role("solo")

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
OTHER = "00000000-0000-4000-8000-000000000023"


class ConfigFile:
    """setUp and load for TestCases that write a pipeline.toml into self.dir."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def load(self, text):
        path = os.path.join(self.dir, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)


class Config(ConfigFile, unittest.TestCase):
    def test_stage_order(self):
        self.assertEqual(pipeline.stage_order(self.load(BASE)),
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
                self.assertEqual(str(cm.exception.code), "pipeline.toml: " + message)
        cfg = self.load(BASE)
        self.assertEqual((cfg["harness_key"], cfg["task_label_group"]), ("linear-api-key", TASK_GROUP))
        self.assertEqual(cfg["roles"]["pm"], {"account": "pm@agents.test", "key": "linear-api-key-pm", "next": "engineer"})
        self.assertEqual(self.load(HEADER)["roles"], {})
        self.assertEqual(self.load(BASE + '[core.roles.pm]\ntier = 1\n')["core"], {"roles": {"pm": {"tier": 1}}})

    def test_task_labels_loads(self):
        self.assertEqual(self.load(BASE)["task_labels"], {})
        cfg = self.load(BASE + f'[task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n')
        self.assertEqual(cfg["task_labels"], {"light-research": LABEL1, "deep-research": LABEL2})


PIPELINE = HEADER + role("researcher", 'next = "pm"') + role("pm", 'next = "engineer"') + role("engineer")
RESEARCHER = pipeline.Role("researcher@agents.test", "linear-api-key-researcher", ("deep-research", "light-research"))
PM = pipeline.Role("pm@agents.test", "linear-api-key-pm", ("product-design",))
ENGINEER = pipeline.Role("engineer@agents.test", "linear-api-key-engineer", ("engineering",))
GATE = f"python3 {shlex.quote(pipeline.ROOT)}/scripts/router.py --brake"


class Runnable(ConfigFile, unittest.TestCase):
    """runnable() over the repo's core config and pipeline.toml's [core]."""
    def runs(self, text=PIPELINE):
        return pipeline.runnable(self.load(text))

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

    def test_core_roles_outside_pipeline_toml_ignored(self):
        self.assertEqual(self.runs(HEADER + role("researcher")), {"researcher": RESEARCHER})

    def test_role_not_in_core(self):
        self.fails("pipeline.toml: role 'ghost' is not in core/config/config.toml", PIPELINE + role("ghost"))

    def test_task_without_tasks_entry(self):
        self.fails("pipeline.toml: dummy-tester's task 'echo' has no entry in pipeline.TASKS", PIPELINE + role("dummy-tester"))

    def test_next(self):
        self.fails("pipeline.toml: next of 'researcher' names undefined role 'pm'", HEADER + role("researcher", 'next = "pm"'))
        self.fails("pipeline.toml: next of 'pm' is role 'researcher', whose default task 'deep-research' has no prefix",
                   HEADER + role("researcher") + role("pm", 'next = "researcher"'))

    def test_keys(self):
        self.fails("pipeline.toml: [roles.pm] key 'linear-api-key-researcher' is also [roles.researcher]'s",
                   PIPELINE.replace("linear-api-key-pm", "linear-api-key-researcher"))
        self.fails("pipeline.toml: [roles.engineer] key 'linear-api-key' is harness_key",
                   PIPELINE.replace("linear-api-key-engineer", "linear-api-key"))

    def test_task_labels_keys(self):
        text = PIPELINE + f'[task_labels]\nlight-research = "{LABEL1}"\nengineering = "{LABEL2}"\n'
        self.assertEqual(list(self.runs(text)), ["researcher", "pm", "engineer"])
        for task, text in (("echo", PIPELINE), ("ghost", PIPELINE), ("product-design", HEADER + role("researcher"))):
            with self.subTest(task):
                self.fails(f"pipeline.toml: task_labels.{task} is not a task of a role in pipeline.toml",
                           text + f'[task_labels]\n{task} = "{LABEL1}"\n')

    def test_role_for(self):
        runs = self.runs()
        for email in ("researcher@agents.test", "RESEARCHER@Agents.Test"):
            self.assertEqual(pipeline.role_for(runs, email), "researcher")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(pipeline.role_for(runs, email))

    def test_role_ids(self):
        runs = self.runs()
        gql = lambda q, **v: {"users": {"nodes": [{"id": "u-" + v["e"]}]}}  # noqa: E731
        self.assertEqual(pipeline.role_ids(gql, runs), {f"u-{a}": r for r, a in ACCOUNTS.items()})
        with self.assertRaises(SystemExit) as cm:
            pipeline.role_ids(lambda q, **v: {"users": {"nodes": []}}, {"engineer": runs["engineer"]})
        self.assertEqual(cm.exception.code, "pipeline.toml [roles.engineer]: account 'engineer@agents.test' not found in Linear")

    def test_run_config(self):
        self.assertEqual(pipeline.run_config("researcher", "deep-research").gate, GATE)
        self.assertEqual(pipeline.run_config("researcher", "light-research").gate, "")
        self.assertEqual(pipeline.overlay(), {"roles": {"researcher": {"tasks": {"deep-research": {"gate": GATE}}}}})
        self.assertEqual(pipeline.layers(), [pipeline.overlay()])

    def test_run_config_layers(self):
        core = os.path.join(self.dir, "core")
        with mock.patch.object(pipeline, "overlay", return_value={"tier": 3}) as overlay, \
                mock.patch.object(pipeline.clients, "load_config", return_value={"flags": []}) as client, \
                mock.patch.object(pipeline.compose, "load_run", return_value="run") as load_run:
            self.assertEqual(pipeline.run_config("pm", "product-design", self.dir), "run")
        overlay.assert_called_once_with(self.dir)
        client.assert_called_once_with("claude", core)
        load_run.assert_called_once_with(core, "pm", "product-design", layers=[{"flags": []}, {"tier": 3}])

    def test_clones_match_core(self):
        self.assertEqual(pipeline.CLONES, ("src", "publish"))
        with open(os.path.join(pipeline.CORE, "config", "config.toml")) as f:
            dirs = re.findall(r"--dir \{\{workdir\}\}/([^\s\"]+)", f.read())
        self.assertTrue(dirs)
        self.assertEqual(set(dirs), {pipeline.CLONES[0]})
        with open(os.path.join(pipeline.CORE, "output", "destinations", "github.md")) as f:
            self.assertIn(f"`<Workdir>/{pipeline.CLONES[1]}`", f.read())

    def test_paths(self):
        self.assertEqual(pipeline.CORE, os.path.join(pipeline.ROOT, "core"))
        self.assertEqual((pipeline.SHORT, pipeline.LONG), (60, 600))


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
    """A repo root with its own pipeline.toml (load() writes it) and core/config/config.toml; core's text and client
    configs are the real ones."""
    def setUp(self):
        super().setUp()
        self.dir = self.root = os.path.join(self.dir, "my root")
        os.makedirs(os.path.join(self.root, "core", "config"))
        for rel in ("team", "output", os.path.join("config", "clients")):
            os.symlink(os.path.join(pipeline.CORE, rel), os.path.join(self.root, "core", rel))
        self.core(CORE_TOML)

    def core(self, text):
        with open(os.path.join(self.root, "core", "config", "config.toml"), "w") as f:
            f.write(text)

    def runs(self, text=PIPELINE):
        return pipeline.runnable(self.load(text), root=self.root)

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
        self.assertEqual(pipeline.overlay(self.root), want)
        self.assertEqual(pipeline.layers(self.root), [want])
        run = pipeline.run_config("researcher", "deep-research", self.root)
        self.assertEqual((run.tier, run.commands, run.gate), (3, [f"ls {q}/a", "true"], f"python3 {q}/x --y {q}"))
        self.assertEqual(pipeline.run_config("pm", "product-design", self.root).gate, "")

    def test_no_core_table(self):
        self.load(PIPELINE)
        self.assertEqual((pipeline.overlay(self.root), pipeline.layers(self.root)), ({}, [{}]))
        self.assertEqual(pipeline.run_config("researcher", "deep-research", self.root).gate, "")

    def test_overlay_unknown_keys(self):
        cases = [("[core]\nfoo = 1\n", "'foo' in the global table"),
                 ('[core]\nusers = ["octocat"]\n', "'users' in the global table"),
                 ('[core.roles.researcher]\ndefault_task = "light-research"\n', "'default_task' in roles.researcher"),
                 ('[core.roles.researcher.tasks.deep-research]\ntasks = 1\n', "'tasks' in roles.researcher.tasks.deep-research")]
        for text, message in cases:
            with self.subTest(message):
                self.load(PIPELINE + text)
                with self.assertRaises(SystemExit) as cm:
                    pipeline.overlay(self.root)
                self.assertEqual(cm.exception.code, "pipeline.toml [core]: unknown key " + message)

    def test_core_errors(self):
        self.core(CORE_TOML.replace("[roles.engineer.tasks.engineering]\n", "[roles.engineer.tasks.engineering]\ntier = 9\n"))
        self.fails("core: tier must be an integer 1–4, got 9")
        self.core(CORE_TOML)
        self.fails("core: gate must be one line of shell command without backticks",
                   PIPELINE + '[core.roles.researcher.tasks.light-research]\ngate = "echo `id`"\n')

    def test_docs(self):
        runs = self.runs()
        self.assertEqual(pipeline.docs(runs, self.root), pipeline.Docs("acme/notes", "trunk", DIRS))
        self.assertEqual(pipeline.docs({"pm": runs["pm"], "engineer": runs["engineer"]}, self.root),
                         pipeline.Docs("acme/notes", "trunk", {"product-design": "Designs/"}))
        self.core(CORE_TOML.replace(DESIGN, DESIGN.replace(" }", ', host = "github.com" }')))
        self.assertEqual(pipeline.docs(runs, self.root), pipeline.Docs("acme/notes", "trunk", DIRS))

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
    """The repo's pipeline.toml, core config and TASKS together."""
    def test_real_config(self):
        cfg = pipeline.load_config()
        runs = pipeline.runnable(cfg)
        self.assertEqual(runs, {
            "researcher": pipeline.Role("frank.agent.w+researcher@gmail.com", "linear-api-key-researcher", ("deep-research", "light-research")),
            "pm": pipeline.Role("frank.agent.w+pm@gmail.com", "linear-api-key-pm", ("product-design",)),
            "engineer": pipeline.Role("frank.agent.w+engineer@gmail.com", "linear-api-key-engineer", ("engineering",))})
        self.assertEqual(list(runs), ["researcher", "pm", "engineer"])
        self.assertEqual(pipeline.docs(runs), pipeline.Docs("ophis/private_docs", "main", {
            "deep-research": "Research/", "light-research": "Research/", "product-design": "Product Design/"}))
        self.assertEqual(cfg["team"], "06159b6b-5efe-4bc5-a27b-875701f40d61")
        self.assertNotIn("docs", cfg)
        self.assertEqual(cfg["core"], {"roles": {"researcher": {"tasks": {"deep-research": {
            "gate": "python3 {{root}}/scripts/router.py --brake"}}}}})
        self.assertEqual(cfg["task_labels"], {"light-research": "7cb3a7cc-05b4-4dec-bbf8-d4fce87cea1d"})
        self.assertEqual(cfg["project_repos"], {P1: "ophis/agent-pm", P2: "ophis/claude-autopilot"})
        self.assertEqual(pipeline.stage_order(cfg), {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertIs(cfg["roles"]["pm"]["require_instructions"], False)
        self.assertEqual({t: (x.kind, x.prefix) for t, x in pipeline.TASKS.items()},
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
                self.assertTrue(msg.startswith(f"pipeline.toml: project_repos.{key} "), msg)
                self.assertIn(repr(value), msg)

    def test_not_a_table_rejected(self):
        body = BASE[BASE.index("[roles"):]
        with self.assertRaises(SystemExit) as cm:
            self.load(HEADER + 'project_repos = "x"\n' + body)
        self.assertTrue(str(cm.exception.code).startswith("pipeline.toml: project_repos"), cm.exception.code)


class RepoSlug(unittest.TestCase):
    def test_slug(self):
        cases = {"ophis/agent-pm": ("ophis", "agent-pm"), "ophis/.github": ("ophis", ".github"),
                 "-a/b": None, "a/-b": None, "a/..": None, "a/.": None, "a b/c": None, "ophis": None,
                 "https://github.com/ophis/x": None, "": None, None: None, 42: None}
        for value, want in cases.items():
            with self.subTest(value=value):
                self.assertEqual(pipeline.repo_slug(value), want)


class TeamCheck(unittest.TestCase):
    def cfg(self):
        return {"team": TEAM, "states": dict(STATES)}

    def gql(self, nodes):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            return {"teams": {"nodes": nodes}}
        gql.calls = calls
        return gql

    def test_ok(self):
        gql = self.gql([team_node()])
        self.assertEqual(pipeline.team(gql, self.cfg()), pipeline.Team(TEAM, "Team", dict(STATES)))
        self.assertEqual(gql.calls, [(pipeline.Q_TEAM, {"t": TEAM})])

    def test_team_not_found_or_states_outside_team(self):
        other = [i for k, i in STATES.items() if k not in ("handoff", "done")]
        cases = {f"team {TEAM} not found in Linear": [],
                 f"[states] not workflow states of team 'Team': handoff {STATES['handoff']}, done {STATES['done']}": [team_node(other)]}
        for message, nodes in cases.items():
            with self.subTest(message):
                with self.assertRaises(SystemExit) as cm:
                    pipeline.team(self.gql(nodes), self.cfg())
                self.assertEqual(str(cm.exception.code), "pipeline.toml: " + message)


class TaskGroupCheck(unittest.TestCase):
    LABELS = {"light-research": LABEL1, "deep-research": LABEL2}

    def cfg(self, labels=LABELS):
        return {"task_label_group": TASK_GROUP, "task_labels": labels}

    def gql(self, node=None, error=None):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if error:
                raise SystemExit(error)
            return {"issueLabel": node}
        gql.calls = calls
        return gql

    def group(self, *ids):
        return {"isGroup": True, "children": {"nodes": [{"id": i} for i in ids]}}

    def test_group_ok(self):
        for group, labels in ((self.group(LABEL1, LABEL2, OTHER), self.LABELS), (self.group(), {})):
            with self.subTest(labels=labels):
                gql = self.gql(group)
                self.assertIsNone(pipeline.task_group(gql, self.cfg(labels)))
                self.assertEqual(gql.calls, [(pipeline.Q_TASK_GROUP, {"i": TASK_GROUP})])
        self.assertIn("issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } }", pipeline.Q_TASK_GROUP)

    def test_fails(self):
        error = "linear api error: [{'message': 'Entity not found: IssueLabel'}]"
        not_labels = f"[task_labels] not labels of task_label_group {TASK_GROUP}: "
        cases = [
            (self.gql(self.group(LABEL1)), self.LABELS, not_labels + f"deep-research {LABEL2}"),
            (self.gql(self.group(OTHER)), {"deep-research": LABEL2, "light-research": LABEL1, "ok": OTHER},
             not_labels + f"deep-research {LABEL2}, light-research {LABEL1}"),
            (self.gql(error=error), self.LABELS, f"task_label_group {TASK_GROUP} not found in Linear: {error}"),
            (self.gql({"isGroup": False, "children": {"nodes": [{"id": LABEL1}, {"id": LABEL2}]}}), self.LABELS,
             f"task_label_group {TASK_GROUP} is not a label group"),
        ]
        for gql, labels, message in cases:
            with self.subTest(message):
                with self.assertRaises(SystemExit) as cm:
                    pipeline.task_group(gql, self.cfg(labels))
                self.assertEqual(str(cm.exception.code), "pipeline.toml: " + message)
                self.assertEqual(len(gql.calls), 1)


class Paths(unittest.TestCase):
    def test_slug_and_project_log(self):
        self.assertEqual(pipeline.slug("Deep Research"), "deep-research")
        with tempfile.TemporaryDirectory() as d:
            path = pipeline.project_log("light-research", logs=d)
            self.assertEqual(path, os.path.join(d, "projects", "light-research.log"))
            self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_run_dir_and_transcript(self):
        self.assertEqual(pipeline.WORK, os.path.join(pipeline.ROOT, "work"))
        self.assertEqual(pipeline.run_dir("TASK-9"), os.path.join(pipeline.WORK, "TASK-9"))
        self.assertEqual(pipeline.escape("/Users/a_b/x.y"), "-Users-a-b-x-y")
        sid = "0f0f0f0f-1111-2222-3333-444444444444"
        self.assertEqual(pipeline.transcript("TASK-9", sid, projects="/p"),
                         "/p/" + pipeline.escape(pipeline.run_dir("TASK-9")) + f"/{sid}.jsonl")
        self.assertIsNone(pipeline.transcript("TASK-9", "../x"))


class LinearGql(unittest.TestCase):
    def test_harness_key_by_service_only(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(stdout="secret\n")
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"data": {"viewer": {"id": "v"}}}')
        with mock.patch.object(pipeline, "harness_service", return_value="svc-h"), \
                mock.patch.object(pipeline.subprocess, "run", run), \
                mock.patch.object(pipeline.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.assertEqual(pipeline.linear_gql("query { viewer { id } }"), {"viewer": {"id": "v"}})
        self.assertEqual(calls, [["security", "find-generic-password", "-s", "svc-h", "-w"]])
        self.assertEqual(urlopen.call_args[0][0].get_header("Authorization"), "secret")

    def test_timeout_bounds_keychain_and_request(self):
        for kw, want in (({}, 30), ({"timeout": 5}, 5)):
            with self.subTest(timeout=want):
                run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
                resp = mock.MagicMock()
                resp.__enter__.return_value = io.BytesIO(b'{"data": {}}')
                with mock.patch.object(pipeline, "harness_service", return_value="svc-h"), \
                        mock.patch.object(pipeline.subprocess, "run", run), \
                        mock.patch.object(pipeline.urllib.request, "urlopen", return_value=resp) as urlopen:
                    pipeline.linear_gql("query($i: String!) { issue(id: $i) { id } }", i="TASK-1", **kw)
                self.assertEqual(run.call_args.kwargs["timeout"], want)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], want)
                self.assertEqual(json.loads(urlopen.call_args[0][0].data)["variables"], {"i": "TASK-1"})

    def test_harness_service_reads_pipeline_toml(self):
        pipeline.harness_service.cache_clear()
        self.addCleanup(pipeline.harness_service.cache_clear)
        self.assertEqual(pipeline.harness_service(), "linear-api-key")

    def test_service_names_the_keychain_item(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"data": {}}')
        with mock.patch.object(pipeline, "harness_service", side_effect=AssertionError("harness key read")), \
                mock.patch.object(pipeline.subprocess, "run", run), \
                mock.patch.object(pipeline.urllib.request, "urlopen", return_value=resp) as urlopen:
            pipeline.linear_gql("query { viewer { id } }", service="linear-api-key-pm")
        self.assertEqual(run.call_args[0][0], ["security", "find-generic-password", "-s", "linear-api-key-pm", "-w"])
        self.assertEqual(json.loads(urlopen.call_args[0][0].data)["variables"], {})


class Shell(unittest.TestCase):
    def test_sh_run(self):
        with mock.patch.object(pipeline.subprocess, "run", return_value="res") as run, \
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "KEEP": "1"}):
            self.assertEqual(pipeline.sh_run(["git", "status"], 5), "res")
            env = {**os.environ, "PATH": pipeline.PATH}
        run.assert_called_once_with(["git", "status"], capture_output=True, text=True, timeout=5,
                                    stdin=subprocess.DEVNULL, env=env)
        self.assertEqual(env["KEEP"], "1")

    def test_err_text(self):
        for stderr, want in ((None, ""), ("", ""), ("  HTTP 404: Not Found \n", "HTTP 404: Not Found"), ("x" * 300, "x" * 200)):
            with self.subTest(stderr=stderr):
                self.assertEqual(pipeline.err_text(SimpleNamespace(stderr=stderr)), want)


class AtomicWrite(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "input.md")

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_writes_and_replaces(self):
        pipeline.atomic_write(self.path, "one\n")
        pipeline.atomic_write(self.path, "two\n")
        self.assertEqual((self.read(self.path), os.listdir(self.dir)), ("two\n", ["input.md"]))

    def test_planted_symlink_replaced_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.symlink(outside, self.path)
        pipeline.atomic_write(self.path, "new\n")
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual((self.read(self.path), self.read(outside)), ("new\n", "keep\n"))

    def test_no_temp_left_on_error(self):
        pipeline.atomic_write(self.path, "old\n")
        with mock.patch.object(pipeline.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pipeline.atomic_write(self.path, "new\n")
        self.assertEqual((self.read(self.path), os.listdir(self.dir)), ("old\n", ["input.md"]))


if __name__ == "__main__":
    unittest.main()
