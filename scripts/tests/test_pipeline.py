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
from board_ids import DOCS_CLONE, HEADER, STATES, TEAM, team_node  # noqa: E402
import pipeline  # noqa: E402

BASE = HEADER + """[roles.researcher]
next = "pm"
[roles.pm]
next = "engineer"
[roles.engineer]
[roles.solo]
"""


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

    def test_cycle_rejected(self):
        with self.assertRaises(SystemExit):
            self.load(BASE + '[roles.a]\nnext = "b"\n[roles.b]\nnext = "a"\n')

    def test_harness_key_required(self):
        for text in (BASE.replace('harness_key = "linear-api-key"\n', ""),
                     BASE.replace('harness_key = "linear-api-key"', "harness_key = 1")):
            with self.subTest(text[:80]):
                with self.assertRaises(SystemExit) as cm:
                    self.load(text)
                self.assertIn("pipeline.toml: harness_key", str(cm.exception.code))
        self.assertEqual(self.load(BASE)["harness_key"], "linear-api-key")

    def test_ids_required(self):
        states = "states = { " + ", ".join(f'{k} = "{v}"' for k, v in STATES.items()) + " }\n"
        body = BASE[BASE.index("[roles"):]
        cases = {
            "team must be a Linear team id (UUID): \"Frank's Agents\"": 'team = "Frank\'s Agents"\n' + states + body,
            "team must be a Linear team id (UUID): None": states + body,
            "[states] is missing: todo, in_progress, in_review, handoff, done, canceled": f'team = "{TEAM}"\n' + body,
            "[states] is missing: canceled": f'team = "{TEAM}"\n' + states.replace(f', canceled = "{STATES["canceled"]}"', "") + body,
            "[states] has unknown keys: backlog": f'team = "{TEAM}"\n' + states.replace(" }", ', backlog = "x" }') + body,
            "states.done must be a Linear workflow state id (UUID): 'Done'": f'team = "{TEAM}"\n' + states.replace(STATES["done"], "Done") + body,
        }
        for fragment, text in cases.items():
            with self.subTest(fragment):
                with self.assertRaises(SystemExit) as cm:
                    self.load(text)
                self.assertTrue(str(cm.exception.code).startswith("pipeline.toml: "), cm.exception.code)
                self.assertIn(fragment, str(cm.exception.code))


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

    def test_table_form_accepted(self):
        body = BASE[BASE.index("[roles"):]
        text = HEADER[:HEADER.index("docs = ")] + body + '[docs]\nrepo = "acme/notes"\nclone = "/nonexistent/x"\nbranch = "trunk"\n'
        self.assertEqual(self.load(text)["docs"]["clone"], "/nonexistent/x")

    def test_table_missing_or_not_a_table(self):
        for label, value in (("missing", None), ("string", '"x"'), ("list", "[1]")):
            with self.subTest(label):
                self.rejects("[docs] is missing: repo, clone, branch", with_docs(value))

    def test_missing_keys(self):
        for key in DOCS:
            with self.subTest(key):
                self.rejects(f"[docs] is missing: {key}", with_docs({k: v for k, v in DOCS.items() if k != key}))
        self.rejects("[docs] is missing: clone, branch", with_docs({"repo": "acme/notes"}))

    def test_unknown_keys(self):
        self.rejects("[docs] has unknown keys: folder", with_docs({**DOCS, "folder": "x"}))
        self.rejects("[docs] has unknown keys: a, b", with_docs({**DOCS, "b": "x", "a": "x"}))

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
        for label, clone in (("root", pipeline.ROOT), ("under root", os.path.join(pipeline.ROOT, "docs")),
                             ("ancestor of root", os.path.dirname(pipeline.ROOT)), ("filesystem root", "/")):
            with self.subTest(label):
                self.rejects("docs.clone", with_docs({**DOCS, "clone": clone}))

    def test_clone_symlink_into_root_rejected(self):
        link = os.path.join(self.dir, "link")
        os.symlink(pipeline.ROOT, link)
        self.rejects("docs.clone", with_docs({**DOCS, "clone": link}))

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

    def test_bad_branch(self):
        for branch in ("", 5, ["trunk"], "-x", "a..b", "a b", "a\nb", "a*", "a:b"):
            with self.subTest(branch=branch):
                self.rejects("docs.branch", with_docs({**DOCS, "branch": branch}))

    def test_ref(self):
        for ref in ("main", "trunk", "feature/a-b_c.d", "TASK-99-build"):
            self.assertTrue(pipeline.REF.fullmatch(ref), ref)
        for ref in ("", "-x", "a..b", "a b", "a\nb"):
            self.assertFalse(pipeline.REF.fullmatch(ref), repr(ref))


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

    def test_session_per_role(self):
        self.assertEqual(pipeline.session("engineer"), "agent-pm-engineer")

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
                         ["task_name", "task", "charter", "instructions", "memory", "read_only", "key", "account"])
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

    def test_read_only_normalized(self):
        self.write("roles/engineer.toml", 'read_only = ["~/playground/private_docs/"]\n' + ENGINEER_ID)
        self.assertEqual(self.runs()["engineer"].read_only, (os.path.expanduser("~/playground/private_docs"),))

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

    def test_states_is_a_top_key(self):
        self.assertEqual(list(self.load()["states"]), list(pipeline.STATES))
        self.assertEqual(sorted(self.runs()), ["engineer", "researcher"])

    def test_docs_is_a_top_key(self):
        self.assertEqual(self.load()["docs"], DOCS)
        self.assertEqual(sorted(self.runs()), ["engineer", "researcher"])

    def test_load_config_ignores_role_checks(self):
        cfg = self.load(ROLES + '[roles.ghost]\nfoo = "x"\n[projects.old]\n')
        self.assertIn("ghost", cfg["roles"])

    def test_default_task_is_first(self):
        self.write("roles/engineer.toml", self.read_role("engineer").replace('tasks = ["engineering"]', 'tasks = ["engineering", "deep-research"]'))
        self.assertEqual(self.runs()["engineer"].task_name, "engineering")

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

    def test_unreferenced_orphan_rejected(self):
        self.write("tasks/light-research.toml", 'model = "opus"\neffort = "high"\n')
        self.rejects("tasks/light-research.md", prefix="tasks/light-research.toml")

    def test_principles_has_no_toml(self):
        self.write("roles/principles.toml", "")
        self.rejects("principles", prefix="roles/principles.toml")

    def test_other_files_ignored(self):
        self.write("roles/.DS_Store", "x")
        self.write("roles/notes.txt", "x")
        self.write("tasks/.engineering.toml.swp", "x")
        self.write("roles/drafts/x.md", "")
        self.assertEqual(sorted(self.runs()), ["engineer", "researcher"])

    def test_bad_names(self):
        self.write("roles/Reviewer.md", "")
        self.write("roles/Reviewer.toml", "")
        self.rejects("kebab", prefix="roles/Reviewer.md")
        self.remove("roles/Reviewer.md")
        self.remove("roles/Reviewer.toml")
        self.write("tasks/deep_research.md", "")
        self.write("tasks/deep_research.toml", 'model = "opus"\neffort = "high"\n')
        self.rejects("kebab", prefix="tasks/deep_research.md")

    def test_bad_toml(self):
        self.write("tasks/engineering.toml", "model = \n")
        self.rejects("", prefix="tasks/engineering.toml")

    def test_task_needs_model_and_effort(self):
        self.write("tasks/deep-research.toml", 'model = "opus"\n')
        self.rejects("effort", prefix="tasks/deep-research.toml")
        self.write("tasks/deep-research.toml", 'effort = "high"\n')
        self.rejects("model", prefix="tasks/deep-research.toml")

    def test_unknown_keys(self):
        self.write("roles/engineer.toml", 'readonly = ["~/playground/private_docs"]\n' + ENGINEER_ID)
        self.rejects("readonly", prefix="roles/engineer.toml")
        self.write("roles/engineer.toml", FILES["roles/engineer.toml"])
        self.write("tasks/engineering.toml", ENGINEERING + 'instructions = "x"\n')
        self.rejects("instructions", prefix="tasks/engineering.toml")

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

    def test_role_unknown_task(self):
        self.write("roles/engineer.toml", ENGINEER_ID.replace('["engineering"]', '["engineering", "nope"]'))
        self.rejects("nope", prefix="roles/engineer.toml")

    def test_role_key_shared(self):
        self.write("roles/engineer.toml", ENGINEER_ID.replace("k-engineer", "k-researcher"))
        self.rejects("k-researcher", prefix="roles/researcher.toml")

    def test_role_key_is_harness_key(self):
        self.write("roles/engineer.toml", ENGINEER_ID.replace("k-engineer", "linear-api-key"))
        self.rejects("harness_key", prefix="roles/engineer.toml")

    def test_unused_role_validated(self):
        self.write("roles/pm.md", "")
        self.write("roles/pm.toml", 'tasks = ["deep-research"]\naccount = "p@x.com"\n')
        self.rejects("key", prefix="roles/pm.toml")

    def test_read_only_paths(self):
        for bad in ('["playground/private_docs"]', '["~/playground/../private_docs"]', '["{repo}/x"]', '"/"'):
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
        with mock.patch.object(pipeline, "load_config", side_effect=AssertionError("read the config")):
            self.write("roles/engineer.toml", 'read_only = ["~/docs"]\n' + ENGINEER_ID)
            self.write("tasks/deep-research.toml", 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/docs"]\n')
            roles, tasks = pipeline.registry(self.root)
            self.assertEqual((roles["engineer"].read_only, tasks["deep-research"]["add_dirs"]), ((os.path.expanduser("~/docs"),), ["~/docs"]))
            self.write("tasks/deep-research.toml", 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["{docs_clone}"]\n')
            with self.assertRaises(AssertionError):
                pipeline.registry(self.root)
            self.assertEqual(pipeline.registry(self.root, "/d")[1]["deep-research"]["add_dirs"], ["/d"])

    def test_registry_without_clone_loads_the_repo_config(self):
        with mock.patch.object(pipeline, "load_config", return_value={"docs": {"clone": "/from/config"}}) as load:
            roles, tasks = pipeline.registry(self.root)
        load.assert_called_once_with()
        self.assertEqual((roles["engineer"].read_only, tasks["deep-research"]["add_dirs"]), (("/from/config",), ["/from/config"]))

    def test_docs_clone_read_only_rejected_forms(self):
        for bad in ('["{docs_clone}/x"]', '["{docs_clone}", "{docs_clone}/x"]', '["x{docs_clone}"]', '["{docs_clone"]', '["{DOCS_CLONE}"]'):
            with self.subTest(bad):
                self.write("roles/engineer.toml", f"read_only = {bad}\n" + ENGINEER_ID)
                self.rejects("read_only", prefix="roles/engineer.toml")

    def test_docs_clone_add_dirs_rejected_forms(self):
        for bad in ('["{docs_clone}/x"]', '["{repo}"]', '["x{docs_clone}"]', '["{docs_clone"]', '["a}"]', '"{docs_clone}"', '"~/a"', "[5]", '[["~/a"]]'):
            with self.subTest(bad):
                self.write("tasks/deep-research.toml", f'model = "opus"\neffort = "xhigh"\nadd_dirs = {bad}\n')
                self.rejects("add_dirs", prefix="tasks/deep-research.toml")

    def test_docs_clone_not_an_allowed_tools_placeholder(self):
        rule = "Bash(git -C {docs_clone} status)"
        self.write("tasks/engineering.toml", ENGINEERING + f'allowed_tools = ["{rule}"]\n')
        self.rejects("unknown placeholder {docs_clone}", prefix="tasks/engineering.toml")

    def test_docs_clone_memory_overlap(self):
        clone = os.path.join(self.outside, "docs")
        os.makedirs(os.path.join(clone, "sub"))
        text = ROLES.replace(DOCS_CLONE, clone)
        cases = [("at clone", clone), ("under clone", os.path.join(clone, "sub")), ("ancestor of clone", self.outside)]
        for label, path in cases:
            with self.subTest(label):
                self.memory(path, read_only="{docs_clone}")
                self.rejects("memory", prefix="roles/engineer.toml", text=text)
        apart = os.path.join(self.outside, "engineer")
        os.makedirs(apart)
        self.memory(apart, read_only="{docs_clone}")
        self.assertEqual(self.runs(text)["engineer"].memory, apart)

    def test_repo_read_only(self):
        self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n' + ENGINEER_ID)
        self.assertEqual(self.runs()["engineer"].read_only, ("{repo}",))
        self.write("tasks/engineering.toml", ENGINEERING + "allowed_tools = []\n")
        self.rejects("read_only {repo} needs default task 'engineering' with repo_from_issue and no allowed_tools", prefix="roles/engineer.toml")
        self.write("tasks/engineering.toml", ENGINEERING)
        self.write("roles/researcher.toml", 'read_only = ["{repo}"]\n' + RESEARCHER_ID)
        self.rejects("read_only {repo} needs default task 'deep-research' with repo_from_issue and no allowed_tools", prefix="roles/researcher.toml")

    def memory(self, path, read_only="~/playground/private_docs"):
        self.write("roles/engineer.toml", f'read_only = ["{read_only}"]\nmemory = "{path}"\n' + ENGINEER_ID)

    def test_memory_ok(self):
        mem = os.path.join(self.outside, "engineer")
        os.makedirs(mem)
        self.memory(mem)
        self.assertEqual(self.runs()["engineer"].memory, mem)

    def test_memory_rejected(self):
        ro = os.path.join(self.outside, "docs")
        for d in (ro, os.path.join(ro, "sub"), os.path.join(self.root, "notes")):
            os.makedirs(d, exist_ok=True)
        cases = [("relative", "mem"), ("missing", os.path.join(self.outside, "nope")),
                 ("under root", os.path.join(self.root, "notes")), ("root itself", self.root),
                 ("roles", os.path.join(self.root, "roles")), ("ancestor of root", os.path.dirname(self.root)),
                 ("at read_only", ro), ("under read_only", os.path.join(ro, "sub")), ("ancestor of read_only", self.outside)]
        for label, path in cases:
            with self.subTest(label):
                self.memory(path, read_only=ro)
                self.rejects("memory", prefix="roles/engineer.toml")

    def test_memory_symlink_into_root_rejected(self):
        link = os.path.join(self.outside, "link")
        os.symlink(os.path.join(self.root, "templates"), link)
        self.memory(link)
        self.rejects("memory", prefix="roles/engineer.toml")

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
        self.rejects("wildcard", prefix="tasks/engineering.toml")
        rule = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
        self.write("tasks/engineering.toml", ENGINEERING + f'allowed_tools = ["{rule}"]\n')
        self.assertEqual(self.runs()["engineer"].task["allowed_tools"], [rule])

    def test_load_config_does_not_need_files(self):
        self.remove("tasks/engineering.md")
        self.assertIn("researcher", self.load()["roles"])


class RealConfig(unittest.TestCase):
    def test_three_runs(self):
        cfg = pipeline.load_config()
        runs = pipeline.runnable(cfg)
        got = {k: (r.task_name, r.task["model"], r.task["effort"], bool(r.task.get("repo_from_issue")), r.read_only, r.memory,
                   os.path.basename(r.charter))
               for k, r in runs.items()}
        private = os.path.expanduser("~/playground/private_docs")
        self.assertEqual(got, {
            "researcher": ("deep-research", "opus", "xhigh", False, (), None, "researcher.md"),
            "pm": ("product-design", "opus", "high", False, (), None, "pm.md"),
            "engineer": ("engineering", "opus", "xhigh", True, (private,), None, "engineer.md"),
        })
        self.assertNotIn("allowed_tools", runs["engineer"].task)
        self.assertEqual({k: r.key for k, r in runs.items()}, {
            "researcher": "linear-api-key-researcher",
            "pm": "linear-api-key-pm",
            "engineer": "linear-api-key-engineer",
        })
        self.assertEqual({k: r.account for k, r in runs.items()}, {k: pipeline.registry()[0][k].account for k in runs})
        self.assertEqual(pipeline.stage_order(cfg), {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertIs(cfg["roles"]["pm"]["require_instructions"], False)
        self.assertEqual(cfg["harness_key"], "linear-api-key")
        tasks = pipeline.registry()[1]
        self.assertEqual([tasks[t].get("prefix") for t in ("product-design", "engineering", "deep-research")], ["PRD", "ENG", None])

    def test_real_docs(self):
        self.assertEqual(pipeline.load_config()["docs"], {
            "repo": "ophis/private_docs", "clone": os.path.expanduser("~/playground/private_docs"), "branch": "main"})

    def test_real_docs_clone_reaches_runs(self):
        private = os.path.expanduser("~/playground/private_docs")
        roles, tasks = pipeline.registry()
        self.assertEqual(roles["engineer"].read_only, (private,))
        self.assertEqual({t: tasks[t]["add_dirs"] for t in ("deep-research", "product-design", "engineering")}, {
            "deep-research": [private], "product-design": [private], "engineering": [private]})
        self.assertEqual({k: r.task["add_dirs"] for k, r in pipeline.runnable(pipeline.load_config()).items()}, {
            "researcher": [private], "pm": [private], "engineer": [private]})

    def test_real_config_ids(self):
        cfg = pipeline.load_config()
        self.assertEqual(list(cfg["states"]), list(pipeline.STATES))
        self.assertEqual(cfg["team"], "06159b6b-5efe-4bc5-a27b-875701f40d61")


P1, P2 = "121166b1-191a-4461-bec4-42f1c2dc0ddd", "ae72ede7-67a6-469d-a959-8ea51ab71fb8"


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

    def test_runnable_accepts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel, text in FILES.items():
            path = os.path.join(tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        os.makedirs(os.path.join(tmp.name, "templates"))
        cfg = self.load(ROLES + f'[project_repos]\n"{P1}" = "ophis/x"\n')
        self.assertEqual(sorted(pipeline.runnable(cfg, root=tmp.name)), ["engineer", "researcher"])

    def test_real_config(self):
        self.assertEqual(pipeline.load_config()["project_repos"],
                         {P1: "ophis/agent-pm", P2: "ophis/claude-autopilot"})


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
        t = pipeline.team(gql, self.cfg())
        self.assertEqual(t, pipeline.Team(TEAM, "Team", dict(STATES)))
        (query, v), = gql.calls
        self.assertEqual((query, v), (pipeline.Q_TEAM, {"t": TEAM}))
        self.assertIn("teams(filter: { id: { eq: $t } })", query)
        self.assertNotIn("projects", query)

    def test_team_not_found(self):
        with self.assertRaises(SystemExit) as cm:
            pipeline.team(self.gql([]), self.cfg())
        self.assertEqual(str(cm.exception.code), f"pipeline.toml: team {TEAM} not found in Linear")

    def test_states_outside_team(self):
        other = [i for k, i in STATES.items() if k not in ("handoff", "done")]
        with self.assertRaises(SystemExit) as cm:
            pipeline.team(self.gql([team_node(other)]), self.cfg())
        self.assertEqual(str(cm.exception.code), "pipeline.toml: [states] not workflow states of team 'Team': "
                         f"handoff {STATES['handoff']}, done {STATES['done']}")


class Paths(unittest.TestCase):
    def test_slug_and_project_log(self):
        self.assertEqual(pipeline.slug("Deep Research"), "deep-research")
        with tempfile.TemporaryDirectory() as d:
            path = pipeline.project_log("Product Design", logs=d)
            self.assertEqual(path, os.path.join(d, "projects", "product-design.log"))
            self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_work_under_root(self):
        self.assertEqual(pipeline.WORK, os.path.join(pipeline.ROOT, "work"))

    def test_escape(self):
        self.assertEqual(pipeline.escape("/Users/a_b/x.y"), "-Users-a-b-x-y")

    def test_run_dir(self):
        self.assertTrue(pipeline.run_dir("TASK-9").endswith("/work/TASK-9"))

    def test_transcript(self):
        sid = "0f0f0f0f-1111-2222-3333-444444444444"
        self.assertEqual(pipeline.transcript("TASK-9", sid, projects="/p"),
                         "/p/" + pipeline.escape(pipeline.run_dir("TASK-9")) + f"/{sid}.jsonl")

    def test_transcript_rejects_non_uuid(self):
        self.assertIsNone(pipeline.transcript("TASK-9", "../x"))


class AllowedTools(unittest.TestCase):
    RULE = ("Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u "
            "git@github.com:{owner}/{name}.git {branch})")

    def check(self, allowed_tools, repo_from_issue=True):
        p = {"allowed_tools": allowed_tools}
        if repo_from_issue:
            p["repo_from_issue"] = True
        pipeline.check_allowed_tools("tasks/engineering.toml", p)

    def test_accepts_exact_template(self):
        self.check([self.RULE])

    def test_rejects_wildcard(self):
        with self.assertRaises(SystemExit):
            self.check(["Bash(git push origin *)"])

    def test_rejects_unknown_placeholder(self):
        with self.assertRaises(SystemExit):
            self.check(["Bash(git push {remote})"])

    def test_rejects_root(self):
        with self.assertRaises(SystemExit):
            self.check([f"Bash(cat {pipeline.ROOT}/secret)"])

    def test_rejects_root_in_home_forms(self):
        home = os.path.expanduser("~")
        for form in ("~", "$HOME", "${HOME}"):
            with self.assertRaises(SystemExit, msg=form):
                pipeline.check_allowed_tools("tasks/e.toml", {"repo_from_issue": True, "allowed_tools": [f"Bash(cat {form}/x/agent-pm/secret)"]},
                                             root=os.path.join(home, "x", "agent-pm"))

    def test_rejects_interpreter_on_script(self):
        for rule in ("Bash(python3 /tmp/x.py)", "Bash(bash ./do.sh)", "Bash(node x.js)", "Bash(python3.12 x.py)",
                     "Bash(make)", "Bash(make -C {worktree} test)", "Bash(npm test)", "Bash(npx jest)",
                     "Bash(pnpm test)", "Bash(yarn build)", "Bash(bun run x)", "Bash(cargo test)", "Bash(go test ./...)",
                     "Bash(pytest)", "Bash(uv run x)"):
            with self.assertRaises(SystemExit, msg=rule):
                self.check([rule])

    def test_malformed_template_is_config_error(self):
        for rule in ("Bash(git -C {worktree push)", "Bash(git -C worktree} push)"):
            with self.assertRaises(SystemExit) as cm:
                self.check([rule])
            self.assertTrue(str(cm.exception.code).startswith("tasks/engineering.toml: "), cm.exception.code)

    def test_rejects_without_repo_from_issue(self):
        with self.assertRaises(SystemExit):
            self.check(["Bash(git status)"], repo_from_issue=False)


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

    def test_harness_service_reads_pipeline_toml(self):
        pipeline.harness_service.cache_clear()
        self.addCleanup(pipeline.harness_service.cache_clear)
        self.assertEqual(pipeline.harness_service(), "linear-api-key")


if __name__ == "__main__":
    unittest.main()
