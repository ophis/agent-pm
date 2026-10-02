import dataclasses
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import DOCS_CLONE, HEADER, STATES, TASK_GROUP, TEAM, team_node  # noqa: E402
import pipeline  # noqa: E402

BASE = HEADER + """[roles.researcher]
next = "pm"
[roles.pm]
next = "engineer"
[roles.engineer]
[roles.solo]
"""

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
OTHER = "00000000-0000-4000-8000-000000000023"


class ConfigFile:
    """setUp, load and rejects for TestCases that write a pipeline.toml into self.dir."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def load(self, text):
        path = os.path.join(self.dir, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def rejects(self, fragment, text):
        with self.assertRaises(SystemExit) as cm:
            self.load(text)
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith("pipeline.toml: "), msg)
        self.assertIn(fragment, msg)


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
        cases = [
            (BASE + '[roles.a]\nnext = "b"\n[roles.b]\nnext = "a"\n', "the next chain from 'a' has a cycle"),
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

    def test_task_labels_loads(self):
        self.assertEqual(self.load(BASE)["task_labels"], {})
        cfg = self.load(BASE + f'[task_labels]\nlight-research = "{LABEL1}"\ndeep-research = "{LABEL2}"\n')
        self.assertEqual(cfg["task_labels"], {"light-research": LABEL1, "deep-research": LABEL2})


DOCS = {"repo": "acme/notes", "clone": DOCS_CLONE, "branch": "trunk"}


def with_docs(value):
    """BASE with its docs line replaced: a dict becomes an inline table, anything else a bare value, None drops the key."""
    if isinstance(value, dict):
        value = "{ " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in value.items()) + " }"
    head = BASE[:BASE.index("docs = ")]
    return head + ("" if value is None else f"docs = {value}\n") + BASE[BASE.index("[roles"):]


class Docs(ConfigFile, unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(self.load(BASE)["docs"], DOCS)

    def test_clone_normalized(self):
        cases = {"~/docs-x": os.path.expanduser("~/docs-x"), "/nonexistent/a/../notes/": "/nonexistent/notes"}
        for clone, want in cases.items():
            with self.subTest(clone):
                self.assertEqual(self.load(with_docs({**DOCS, "clone": clone}))["docs"]["clone"], want)

    def test_missing_or_unknown_keys(self):
        cases = [("[docs] is missing: repo, clone, branch", value) for value in (None, '"x"', "[1]")]
        cases += [(f"[docs] is missing: {key}", {k: v for k, v in DOCS.items() if k != key}) for key in DOCS]
        cases += [("[docs] is missing: clone, branch", {"repo": "acme/notes"}),
                  ("[docs] has unknown keys: folder", {**DOCS, "folder": "x"}),
                  ("[docs] has unknown keys: a, b", {**DOCS, "b": "x", "a": "x"})]
        for fragment, value in cases:
            with self.subTest(fragment, value=value):
                self.rejects(fragment, with_docs(value))

    def test_bad_repo(self):
        for repo in ("acme", "https://github.com/acme/notes", "acme/", "acme/..", "", 5, ["acme/notes"]):
            with self.subTest(repo=repo):
                self.rejects("docs.repo", with_docs({**DOCS, "repo": repo}))

    def test_bad_clone(self):
        cases = ["notes", "", "~someone-nobody/x", 5, ["/nonexistent/x"], "/nonexistent/a\nb", "/nonexistent/a\tb", "/nonexistent/a\x7fb",
                 "/nonexistent/a*", "/nonexistent/a?", "/nonexistent/[a]", "/nonexistent/a,b", "/nonexistent/(a)", "/nonexistent/{a}"]
        for clone in cases:
            with self.subTest(clone=clone):
                self.rejects("docs.clone", with_docs({**DOCS, "clone": clone}))

    def test_clone_overlapping_root(self):
        link = os.path.join(self.dir, "link")
        os.symlink(pipeline.ROOT, link)
        for label, clone in (("root", pipeline.ROOT), ("under root", os.path.join(pipeline.ROOT, "docs")),
                             ("ancestor of root", os.path.dirname(pipeline.ROOT)), ("filesystem root", "/"),
                             ("symlink into root", link)):
            with self.subTest(label):
                self.rejects("docs.clone", with_docs({**DOCS, "clone": clone}))

    def test_clone_checked_as_stored(self):
        root = os.path.join(self.dir, "parent", "root")
        deep = os.path.join(self.dir, "elsewhere", "deep")
        os.makedirs(root)
        os.makedirs(deep)
        os.symlink(deep, os.path.join(self.dir, "parent", "link"))
        clone = os.path.join(self.dir, "parent", "link", "..", "root", "sub")
        with mock.patch.object(pipeline, "ROOT", root):
            self.rejects("docs.clone", with_docs({**DOCS, "clone": clone}))

    def test_clone_overlapping_protected(self):
        home = os.path.join(self.dir, "home")
        with mock.patch.dict(os.environ, {"HOME": home}):
            for d in (".claude", ".claude/x", "Library/LaunchAgents", "Library/LaunchAgents/x", "Library", ""):
                with self.subTest(d or "home"):
                    self.rejects("docs.clone", with_docs({**DOCS, "clone": os.path.join(home, d).rstrip("/")}))
            self.assertEqual(self.load(with_docs({**DOCS, "clone": "~/notes"}))["docs"]["clone"], os.path.join(home, "notes"))

    def test_branch(self):
        for branch in ("main", "trunk", "feature/a-b_c.d", "TASK-99-build"):
            with self.subTest(branch=branch):
                self.assertEqual(self.load(with_docs({**DOCS, "branch": branch}))["docs"]["branch"], branch)
        for branch in ("", 5, ["trunk"], "-x", "a..b", "a b", "a\nb", "a*", "a:b"):
            with self.subTest(branch=branch):
                self.rejects("docs.branch", with_docs({**DOCS, "branch": branch}))


ROLES = HEADER + '[roles.researcher]\nnext = "engineer"\n'
RESEARCHER_ID = 'tasks = ["deep-research"]\naccount = "r@x.com"\nkey = "k-researcher"\n'
ENGINEER_ID = 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = "k-engineer"\n'
FILES = {
    "roles/principles.md": "",
    "roles/researcher.md": "", "roles/researcher.toml": RESEARCHER_ID,
    "roles/engineer.md": "", "roles/engineer.toml": 'read_only = ["{docs_clone}"]\n' + ENGINEER_ID,
    "tasks/deep-research.md": "",
    "tasks/deep-research.toml": 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["{docs_clone}"]\n',
    "tasks/engineering.md": "", "tasks/engineering.toml": 'model = "opus"\neffort = "high"\nrepo_from_issue = true\nprefix = "ENG"\n',
}
ENGINEERING = FILES["tasks/engineering.toml"]


class Runnable(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "root")
        self.outside = os.path.join(tmp.name, "outside")
        os.makedirs(self.outside)
        os.makedirs(os.path.join(self.root, "templates"))
        for rel, text in FILES.items():
            self.write(rel, text)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read_role(self, name):
        with open(os.path.join(self.root, "roles", f"{name}.toml")) as f:
            return f.read()

    def remove(self, rel):
        os.remove(os.path.join(self.root, rel))

    def load(self, text=ROLES):
        path = os.path.join(self.outside, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def runs(self, text=ROLES):
        return pipeline.runnable(self.load(text), root=self.root)

    def rejects(self, fragment, prefix="pipeline.toml", text=ROLES):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith(prefix + ":") or msg.startswith(prefix + " "), msg)
        self.assertIn(fragment, msg)

    def test_role_for(self):
        runs = self.runs()
        for email in ("r@x.com", "R@X.COM"):
            self.assertEqual(pipeline.role_for(runs, email), "researcher")
        self.assertEqual(pipeline.role_for(runs, "E@x.com"), "engineer")
        for email in ("nobody@x.com", None, ""):
            self.assertIsNone(pipeline.role_for(runs, email))

    def test_runs(self):
        runs = self.runs()
        self.assertEqual(sorted(runs), ["engineer", "researcher"])
        r, e = runs["researcher"], runs["engineer"]
        self.assertEqual([f.name for f in dataclasses.fields(pipeline.Run)],
                         ["task_name", "task", "charter", "instructions", "memory", "read_only", "key", "account", "tasks", "max_runs"])
        self.assertEqual((r.key, e.key), ("k-researcher", "k-engineer"))
        self.assertEqual((r.account, e.account), ("r@x.com", "e@x.com"))
        self.assertEqual((r.task_name, e.task_name), ("deep-research", "engineering"))
        self.assertEqual((r.task["effort"], r.memory, r.read_only), ("xhigh", None, ()))
        self.assertEqual(r.charter, os.path.join(self.root, "roles", "researcher.md"))
        self.assertEqual(e.charter, os.path.join(self.root, "roles", "engineer.md"))
        self.assertEqual(r.instructions, os.path.join(self.root, "tasks", "deep-research.md"))
        self.assertEqual(e.read_only, (DOCS_CLONE,))
        self.assertEqual(r.task["add_dirs"], [DOCS_CLONE])
        self.assertTrue(e.task["repo_from_issue"])

    def two_tasks(self, task=None, read_only='["{docs_clone}"]'):
        """engineer runs engineering, then light-research (task: its toml, default repo_from_issue)."""
        self.write("roles/engineer.toml", f"read_only = {read_only}\n" + ENGINEER_ID.replace('["engineering"]', '["engineering", "light-research"]'))
        self.write("tasks/light-research.md", "")
        self.write("tasks/light-research.toml", task or 'model = "sonnet"\neffort = "low"\nrepo_from_issue = true\nprefix = "LR"\n')

    def test_run_tasks_in_role_order(self):
        self.two_tasks()
        runs = self.runs()
        e, r = runs["engineer"], runs["researcher"]
        self.assertEqual(list(e.tasks), ["engineering", "light-research"])
        self.assertEqual(e.tasks["engineering"], e.task)
        self.assertEqual(e.tasks["light-research"], {"model": "sonnet", "effort": "low", "repo_from_issue": True, "prefix": "LR"})
        self.assertEqual(e.task_name, "engineering")
        self.assertEqual(r.tasks, {"deep-research": r.task})
        self.write("roles/engineer.toml", self.read_role("engineer").replace('"engineering", "light-research"', '"light-research", "engineering"'))
        e = self.runs()["engineer"]
        self.assertEqual((list(e.tasks), e.task_name), (["light-research", "engineering"], "light-research"))

    def test_with_task(self):
        self.two_tasks()
        e = self.runs()["engineer"]
        light = e.with_task("light-research")
        self.assertEqual((light.task_name, light.task["model"], light.instructions),
                         ("light-research", "sonnet", os.path.join(self.root, "tasks", "light-research.md")))
        self.assertEqual(light.task, e.tasks["light-research"])
        for field in ("charter", "memory", "read_only", "key", "account", "tasks", "max_runs"):
            self.assertEqual(getattr(light, field), getattr(e, field), field)
        self.assertEqual(e.with_task("engineering"), e)
        self.assertEqual(e.task_name, "engineering")
        for bad in ("deep-research", "../x", "", "Light-Research", "nope"):
            with self.subTest(bad):
                with self.assertRaises(KeyError):
                    e.with_task(bad)

    def test_max_runs(self):
        self.assertEqual({n: r.max_runs for n, r in self.runs().items()}, {"researcher": 1, "engineer": 1})
        self.write("roles/researcher.toml", RESEARCHER_ID + "max_runs = 2\n")
        runs = self.runs()
        self.assertEqual((runs["researcher"].max_runs, runs["engineer"].max_runs), (2, 1))
        self.assertEqual(pipeline.registry(self.root, DOCS_CLONE)[0]["researcher"].max_runs, 2)
        self.write("roles/engineer.toml", self.read_role("engineer") + "max_runs = 1\n")
        self.assertEqual(self.runs()["engineer"].max_runs, 1)

    def test_max_runs_rejected(self):
        for toml, shown in (("0", "0"), ("-1", "-1"), ('"2"', "'2'"), ("2.0", "2.0"), ("true", "True"), ("[2]", "[2]")):
            with self.subTest(toml):
                self.write("roles/researcher.toml", RESEARCHER_ID + f"max_runs = {toml}\n")
                self.rejects(f"max_runs must be a positive integer: {shown}", prefix="roles/researcher.toml")

    def test_max_runs_above_one_needs_no_memory(self):
        mem = os.path.join(self.outside, "researcher")
        os.makedirs(mem)
        self.write("roles/researcher.toml", RESEARCHER_ID + f'memory = "{mem}"\n')
        self.assertEqual(self.runs()["researcher"].max_runs, 1)
        self.write("roles/researcher.toml", RESEARCHER_ID + f'memory = "{mem}"\nmax_runs = 1\n')
        self.assertEqual(self.runs()["researcher"].memory, mem)
        self.write("roles/researcher.toml", RESEARCHER_ID + f'memory = "{mem}"\nmax_runs = 2\n')
        self.rejects("max_runs 2 would share the memory dir", prefix="roles/researcher.toml")

    def test_max_runs_above_one_bars_repo_from_issue(self):
        self.write("roles/engineer.toml", self.read_role("engineer") + "max_runs = 2\n")
        self.rejects("max_runs 2 with task 'engineering', which has repo_from_issue", prefix="roles/engineer.toml")
        self.two_tasks()
        self.write("tasks/engineering.toml", ENGINEERING.replace("repo_from_issue = true\n", ""))
        self.write("roles/engineer.toml", self.read_role("engineer") + "max_runs = 2\n")
        self.rejects("max_runs 2 with task 'light-research', which has repo_from_issue", prefix="roles/engineer.toml")
        self.write("tasks/light-research.toml", 'model = "sonnet"\neffort = "low"\nprefix = "LR"\n')
        self.assertEqual(self.runs()["engineer"].max_runs, 2)

    def test_projects_table_rejected(self):
        self.rejects("pipeline.toml has unknown keys: projects", text=ROLES + '[projects.p]\nnext = "q"\n')
        self.rejects("pipeline.toml has unknown keys: tasks", text=ROLES + '[tasks.x]\nmodel = "opus"\n')

    def test_next_names_undefined_role(self):
        self.rejects("pipeline.toml: next of 'researcher' names undefined role 'ghost'",
                     text=ROLES.replace('next = "engineer"', 'next = "ghost"'))

    def test_next_role_default_task_needs_prefix(self):
        self.write("tasks/engineering.toml", ENGINEERING.replace('prefix = "ENG"\n', ""))
        self.rejects("pipeline.toml: next of 'researcher' is role 'engineer', whose default task 'engineering' has no prefix")

    def test_hands_off_to_repo(self):
        self.write("tasks/deep-research.toml", FILES["tasks/deep-research.toml"] + 'prefix = "DR"\n')
        cases = [
            (ROLES, "researcher", True),  # next's default task has repo_from_issue
            (ROLES + "[roles.engineer]\n", "engineer", False),  # no next
            (ROLES, "engineer", False),  # no [roles.engineer] entry
            (HEADER + '[roles.engineer]\nnext = "researcher"\n', "engineer", False),  # next's task lacks repo_from_issue
        ]
        for text, role, want in cases:
            with self.subTest(text=text, role=role):
                self.assertIs(pipeline.hands_off_to_repo(self.load(text), self.runs(text), role), want)

    def test_roles_table_needs_a_role_pair(self):
        self.rejects("pipeline.toml: [roles.ghost] has no roles/<role>.md + .toml pair", text=ROLES + "[roles.ghost]\n")
        self.rejects("pipeline.toml: [roles.principles] has no roles/<role>.md + .toml pair", text=ROLES + "[roles.principles]\n")

    def test_roles_table_unknown_keys(self):
        self.rejects("pipeline.toml: [roles.researcher] has unknown keys: role, task",
                     text=ROLES + 'role = "x"\ntask = "y"\n')

    def test_load_config_ignores_role_checks(self):
        self.remove("tasks/engineering.md")
        self.assertIn("researcher", self.load()["roles"])
        self.assertIn("ghost", self.load(ROLES + '[roles.ghost]\nfoo = "x"\n[projects.old]\n')["roles"])
        self.assertEqual(self.load(ROLES + f'[task_labels]\nghost = "{LABEL1}"\n')["task_labels"], {"ghost": LABEL1})

    def test_role_ids(self):
        runs = self.runs()
        gql = lambda q, **v: {"users": {"nodes": [{"id": "u-" + v["e"]}]}}
        self.assertEqual(pipeline.role_ids(gql, runs), {"u-e@x.com": "engineer", "u-r@x.com": "researcher"})
        with self.assertRaises(SystemExit) as cm:
            pipeline.role_ids(lambda q, **v: {"users": {"nodes": []}}, {"engineer": runs["engineer"]})
        self.assertEqual(str(cm.exception.code), "roles/engineer.toml: account 'e@x.com' not found in Linear")

    def test_orphans(self):
        for rel, other in (("roles/engineer.toml", "roles/engineer.md"), ("roles/engineer.md", "roles/engineer.toml"),
                           ("tasks/engineering.toml", "tasks/engineering.md"), ("tasks/engineering.md", "tasks/engineering.toml")):
            with self.subTest(rel):
                self.remove(rel)
                self.rejects(rel, prefix=other)
                self.write(rel, FILES[rel])

    def test_principles_has_no_toml(self):
        self.write("roles/principles.toml", "")
        self.rejects("principles", prefix="roles/principles.toml")

    def test_other_files_ignored(self):
        self.write("roles/.DS_Store", "x")
        self.write("roles/notes.txt", "x")
        self.write("tasks/.engineering.toml.swp", "x")
        self.write("roles/drafts/x.md", "")
        self.assertEqual(sorted(self.runs()), ["engineer", "researcher"])

    def test_bad_files_rejected(self):
        placeholder = "Bash(git -C {docs_clone} status)"
        cases = [
            ("roles/Reviewer.md", "kebab", {"roles/Reviewer.md": "", "roles/Reviewer.toml": ""}),
            ("tasks/deep_research.md", "kebab", {"tasks/deep_research.md": "", "tasks/deep_research.toml": 'model = "opus"\neffort = "high"\n'}),
            ("tasks/engineering.toml", "", {"tasks/engineering.toml": "model = \n"}),
            ("tasks/deep-research.toml", "effort", {"tasks/deep-research.toml": 'model = "opus"\n'}),
            ("tasks/deep-research.toml", "model", {"tasks/deep-research.toml": 'effort = "high"\n'}),
            ("roles/engineer.toml", "readonly", {"roles/engineer.toml": 'readonly = ["~/playground/private_docs"]\n' + ENGINEER_ID}),
            ("tasks/engineering.toml", "instructions", {"tasks/engineering.toml": ENGINEERING + 'instructions = "x"\n'}),
            ("roles/engineer.toml", "nope", {"roles/engineer.toml": ENGINEER_ID.replace('["engineering"]', '["engineering", "nope"]')}),
            ("roles/researcher.toml", "k-researcher", {"roles/engineer.toml": ENGINEER_ID.replace("k-engineer", "k-researcher")}),
            ("roles/engineer.toml", "harness_key", {"roles/engineer.toml": ENGINEER_ID.replace("k-engineer", "linear-api-key")}),
            ("roles/pm.toml", "key", {"roles/pm.md": "", "roles/pm.toml": 'tasks = ["deep-research"]\naccount = "p@x.com"\n'}),
            ("tasks/engineering.toml", f"tasks/engineering.toml: allowed_tools rule has unknown placeholder {{docs_clone}}: {placeholder!r}",
             {"tasks/engineering.toml": ENGINEERING + f'allowed_tools = ["{placeholder}"]\n'}),
        ]
        for prefix, fragment, files in cases:
            with self.subTest(prefix, fragment=fragment):
                for rel, text in files.items():
                    self.write(rel, text)
                self.rejects(fragment, prefix=prefix)
                for rel in files:
                    if rel in FILES:
                        self.write(rel, FILES[rel])
                    else:
                        self.remove(rel)

    def test_role_identity_rejected(self):
        ro = 'read_only = ["~/playground/private_docs"]\n'
        cases = {
            "tasks": ro + 'account = "e@x.com"\nkey = "k-engineer"\n',
            "tasks ": ro + 'tasks = []\naccount = "e@x.com"\nkey = "k-engineer"\n',
            "tasks  ": ro + 'tasks = "engineering"\naccount = "e@x.com"\nkey = "k-engineer"\n',
            "account": ro + 'tasks = ["engineering"]\nkey = "k-engineer"\n',
            "account ": ro + 'tasks = ["engineering"]\naccount = ""\nkey = "k-engineer"\n',
            "key": ro + 'tasks = ["engineering"]\naccount = "e@x.com"\n',
            "key ": ro + 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = ""\n',
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write("roles/engineer.toml", text)
                self.rejects(label.strip(), prefix="roles/engineer.toml")

    def test_read_only_paths(self):
        for bad in ('["playground/private_docs"]', '["~/playground/../private_docs"]', '["{repo}/x"]', '"/"', '["{docs_clone}/x"]',
                    '["{docs_clone}", "{docs_clone}/x"]', '["x{docs_clone}"]', '["{docs_clone"]', '["{DOCS_CLONE}"]'):
            with self.subTest(bad):
                self.write("roles/engineer.toml", f"read_only = {bad}\n" + ENGINEER_ID)
                self.rejects("read_only", prefix="roles/engineer.toml")

    def test_docs_clone_in_read_only_and_add_dirs(self):
        self.write("tasks/deep-research.toml", 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/a", "{docs_clone}", "/b/c/"]\n')
        runs = self.runs()
        self.assertEqual(runs["researcher"].task["add_dirs"], ["~/a", DOCS_CLONE, "/b/c/"])
        self.assertEqual(runs["engineer"].read_only, (DOCS_CLONE,))
        _, tasks = pipeline.registry(self.root, DOCS_CLONE)
        self.assertEqual(tasks["engineering"], {"model": "opus", "effort": "high", "repo_from_issue": True, "prefix": "ENG"})

    def test_docs_clone_follows_the_config(self):
        clone = os.path.join(self.outside, "notes")
        runs = self.runs(ROLES.replace(DOCS_CLONE, clone))
        self.assertEqual(runs["engineer"].read_only, (clone,))
        self.assertEqual(runs["researcher"].task["add_dirs"], [clone])

    def test_docs_clone_normalized_like_a_literal_path(self):
        self.assertEqual(pipeline.registry(self.root, "/nonexistent/notes/")[0]["engineer"].read_only, ("/nonexistent/notes",))
        for bad in ("notes", "/nonexistent/../notes", "/nonexistent/{x}"):
            with self.subTest(bad):
                with self.assertRaises(SystemExit) as cm:
                    pipeline.registry(self.root, bad)
                self.assertTrue(str(cm.exception.code).startswith("roles/engineer.toml: read_only"), cm.exception.code)

    def test_registry_reads_the_config_only_for_the_placeholder(self):
        with mock.patch.object(pipeline, "load_config", return_value={"docs": {"clone": "/from/config"}}) as load:
            roles, tasks = pipeline.registry(self.root)
            load.assert_called_once_with()
            self.assertEqual((roles["engineer"].read_only, tasks["deep-research"]["add_dirs"]), (("/from/config",), ["/from/config"]))
            self.assertEqual(pipeline.registry(self.root, "/d")[1]["deep-research"]["add_dirs"], ["/d"])
            self.write("roles/engineer.toml", 'read_only = ["~/docs"]\n' + ENGINEER_ID)
            self.write("tasks/deep-research.toml", 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/docs"]\n')
            roles, tasks = pipeline.registry(self.root)
            self.assertEqual((roles["engineer"].read_only, tasks["deep-research"]["add_dirs"]), ((os.path.expanduser("~/docs"),), ["~/docs"]))
            self.assertEqual(load.call_count, 1)
            self.write("tasks/deep-research.toml", 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["{docs_clone}"]\n')
            self.assertEqual(pipeline.registry(self.root)[1]["deep-research"]["add_dirs"], ["/from/config"])
            self.assertEqual(load.call_count, 2)

    def test_docs_clone_add_dirs_rejected_forms(self):
        for bad in ('["{docs_clone}/x"]', '["{repo}"]', '["x{docs_clone}"]', '["{docs_clone"]', '["a}"]', '"{docs_clone}"', '"~/a"', "[5]", '[["~/a"]]'):
            with self.subTest(bad):
                self.write("tasks/deep-research.toml", f'model = "opus"\neffort = "xhigh"\nadd_dirs = {bad}\n')
                self.rejects("add_dirs", prefix="tasks/deep-research.toml")

    def test_repo_read_only(self):
        self.write("roles/researcher.toml", 'read_only = ["{repo}"]\n' + RESEARCHER_ID)
        self.rejects("read_only {repo} needs default task 'deep-research' with repo_from_issue and no allowed_tools", prefix="roles/researcher.toml")
        self.write("roles/researcher.toml", RESEARCHER_ID)
        self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n' + ENGINEER_ID)
        self.assertEqual(self.runs()["engineer"].read_only, ("{repo}",))
        self.two_tasks(read_only='["{repo}"]')
        self.assertEqual(sorted(self.runs()["engineer"].tasks), ["engineering", "light-research"])
        bad = {"no repo_from_issue": 'model = "sonnet"\neffort = "low"\n',
               "allowed_tools": 'model = "sonnet"\neffort = "low"\nrepo_from_issue = true\nallowed_tools = []\n'}
        for label, task in bad.items():
            with self.subTest(label):
                self.write("tasks/light-research.toml", task)
                self.rejects("read_only {repo} needs task 'light-research' with repo_from_issue and no allowed_tools",
                             prefix="roles/engineer.toml")
        self.write("tasks/engineering.toml", ENGINEERING + "allowed_tools = []\n")
        self.rejects("read_only {repo} needs default task 'engineering' with repo_from_issue and no allowed_tools",
                     prefix="roles/engineer.toml")
        self.write("tasks/engineering.toml", ENGINEERING)
        self.two_tasks(task=bad["no repo_from_issue"], read_only='["/nonexistent/x"]')
        self.assertEqual(sorted(self.runs()["engineer"].tasks), ["engineering", "light-research"])

    def memory(self, path, read_only):
        self.write("roles/engineer.toml", f'read_only = ["{read_only}"]\nmemory = "{path}"\n' + ENGINEER_ID)

    def test_memory(self):
        ro, mem, link = (os.path.join(self.outside, d) for d in ("docs", "engineer", "link"))
        for d in (os.path.join(ro, "sub"), os.path.join(self.root, "notes"), mem):
            os.makedirs(d)
        os.symlink(os.path.join(self.root, "templates"), link)
        cases = [("relative", "mem"), ("missing", os.path.join(self.outside, "nope")),
                 ("under root", os.path.join(self.root, "notes")), ("root itself", self.root),
                 ("roles", os.path.join(self.root, "roles")), ("ancestor of root", os.path.dirname(self.root)),
                 ("symlink into root", link),
                 ("at read_only", ro), ("under read_only", os.path.join(ro, "sub")), ("ancestor of read_only", self.outside)]
        text = ROLES.replace(DOCS_CLONE, ro)
        for read_only in (ro, "{docs_clone}"):
            for label, path in cases:
                with self.subTest(label, read_only=read_only):
                    self.memory(path, read_only=read_only)
                    self.rejects("memory", prefix="roles/engineer.toml", text=text)
            self.memory(mem, read_only=read_only)
            self.assertEqual(self.runs(text)["engineer"].memory, mem)

    def test_memory_protected_both_ways(self):
        home = os.path.join(self.outside, "home")
        for d in (".claude/mem", "Library/LaunchAgents/mem", "notes"):
            os.makedirs(os.path.join(home, d))
        ro = os.path.join(self.outside, "docs")
        os.makedirs(ro)
        with mock.patch.dict(os.environ, {"HOME": home}):
            for d in (".claude", ".claude/mem", "Library/LaunchAgents", "Library/LaunchAgents/mem", "Library", ""):
                with self.subTest(d or "home"):
                    self.memory(os.path.join(home, d).rstrip("/"), read_only=ro)
                    self.rejects("memory", prefix="roles/engineer.toml")
            self.memory(os.path.join(home, "notes"), read_only=ro)
            self.assertEqual(self.runs()["engineer"].memory, os.path.join(home, "notes"))

    def test_allowed_tools_checked_in_task_file(self):
        self.write("tasks/engineering.toml", ENGINEERING + 'allowed_tools = ["Bash(git push origin *)"]\n')
        self.rejects("tasks/engineering.toml: allowed_tools rule has a wildcard: 'Bash(git push origin *)'", prefix="tasks/engineering.toml")
        rule = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
        self.write("tasks/engineering.toml", ENGINEERING + f'allowed_tools = ["{rule}"]\n')
        self.assertEqual(self.runs()["engineer"].task["allowed_tools"], [rule])

    def test_read_repo(self):
        self.write("tasks/deep-research.toml", FILES["tasks/deep-research.toml"] + "read_repo = true\n")
        self.assertIs(self.runs()["researcher"].task["read_repo"], True)
        self.write("tasks/deep-research.toml", FILES["tasks/deep-research.toml"] + "read_repo = true\nrepo_from_issue = true\n")
        self.rejects("read_repo and repo_from_issue are exclusive", prefix="tasks/deep-research.toml")

    def test_task_labels_key_needs_task_pair(self):
        self.rejects("pipeline.toml: task_labels.ghost has no tasks/ghost.md + .toml pair",
                     text=ROLES + f'[task_labels]\nghost = "{LABEL1}"\n')
        self.rejects("pipeline.toml: task_labels.light-research has no tasks/light-research.md + .toml pair",
                     text=ROLES + f'[task_labels]\nengineering = "{LABEL1}"\nlight-research = "{LABEL2}"\n')
        self.two_tasks()
        text = ROLES + f'[task_labels]\nlight-research = "{LABEL1}"\nengineering = "{LABEL2}"\n'
        self.assertEqual(sorted(self.runs(text)), ["engineer", "researcher"])


P1, P2 = "121166b1-191a-4461-bec4-42f1c2dc0ddd", "ae72ede7-67a6-469d-a959-8ea51ab71fb8"


class RealConfig(unittest.TestCase):
    """The repo's pipeline.toml values that test_launch's RealConfig does not pin."""
    def test_real_config(self):
        cfg = pipeline.load_config()
        runs = pipeline.runnable(cfg)
        self.assertEqual(sorted(runs), ["engineer", "pm", "researcher"])
        self.assertEqual({n: r.max_runs for n, r in runs.items()}, {"researcher": 2, "pm": 2, "engineer": 1})
        self.assertEqual(cfg["team"], "06159b6b-5efe-4bc5-a27b-875701f40d61")
        self.assertEqual(cfg["docs"], {
            "repo": "ophis/private_docs", "clone": os.path.expanduser("~/playground/private_docs"), "branch": "main"})
        self.assertEqual(cfg["task_labels"], {"light-research": "7cb3a7cc-05b4-4dec-bbf8-d4fce87cea1d"})
        self.assertEqual(cfg["project_repos"], {P1: "ophis/agent-pm", P2: "ophis/claude-autopilot"})
        self.assertEqual(pipeline.stage_order(cfg), {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertIs(cfg["roles"]["pm"]["require_instructions"], False)
        tasks = pipeline.registry()[1]
        self.assertEqual([tasks[t].get("prefix") for t in ("product-design", "engineering", "deep-research")], ["PRD", "ENG", None])


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


class AllowedTools(unittest.TestCase):
    def check(self, allowed_tools, repo_from_issue=True):
        p = {"allowed_tools": allowed_tools}
        if repo_from_issue:
            p["repo_from_issue"] = True
        pipeline.check_allowed_tools("tasks/engineering.toml", p)

    def rejects(self, what, rule, **kw):
        with self.assertRaises(SystemExit) as cm:
            self.check([rule], **kw)
        self.assertEqual(cm.exception.code, f"tasks/engineering.toml: allowed_tools {what}: {rule!r}")

    def test_rejects_root(self):
        self.rejects("rule contains root", f"Bash(cat {pipeline.ROOT}/secret)")

    def test_rejects_root_in_home_forms(self):
        home = os.path.expanduser("~")
        for form in ("~", "$HOME", "${HOME}"):
            rule = f"Bash(cat {form}/x/agent-pm/secret)"
            with self.assertRaises(SystemExit, msg=form) as cm:
                pipeline.check_allowed_tools("tasks/e.toml", {"repo_from_issue": True, "allowed_tools": [rule]},
                                             root=os.path.join(home, "x", "agent-pm"))
            self.assertEqual(cm.exception.code, f"tasks/e.toml: allowed_tools rule contains root: {rule!r}", form)

    def test_rejects_interpreter_on_script(self):
        for rule in ("Bash(python3 /tmp/x.py)", "Bash(bash ./do.sh)", "Bash(node x.js)", "Bash(python3.12 x.py)",
                     "Bash(make)", "Bash(make -C {worktree} test)", "Bash(npm test)", "Bash(npx jest)",
                     "Bash(pnpm test)", "Bash(yarn build)", "Bash(bun run x)", "Bash(cargo test)", "Bash(go test ./...)",
                     "Bash(pytest)", "Bash(uv run x)"):
            with self.subTest(rule):
                self.rejects("rule runs an interpreter on a script", rule)

    def test_malformed_template_is_config_error(self):
        for rule in ("Bash(git -C {worktree push)", "Bash(git -C worktree} push)"):
            with self.assertRaises(SystemExit) as cm:
                self.check([rule])
            self.assertTrue(str(cm.exception.code).startswith("tasks/engineering.toml: allowed_tools rule is not a valid template ("),
                            cm.exception.code)

    def test_rejects_without_repo_from_issue(self):
        with self.assertRaises(SystemExit) as cm:
            self.check(["Bash(git status)"], repo_from_issue=False)
        self.assertEqual(cm.exception.code, "tasks/engineering.toml: allowed_tools without repo_from_issue")


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


if __name__ == "__main__":
    unittest.main()
