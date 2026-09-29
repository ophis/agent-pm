import dataclasses
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pipeline  # noqa: E402

BASE = """team = "T"
[projects."Deep Research"]
next = "Product Design"
[projects."Product Design"]
prefix = "PRD"
next = "Engineering"
[projects.Engineering]
prefix = "TDD"
[projects.Solo]
"""


class Config(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def load(self, text):
        path = os.path.join(self.dir, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def test_stage_order(self):
        self.assertEqual(pipeline.stage_order(self.load(BASE)),
                         {"Deep Research": 0, "Product Design": 1, "Engineering": 2, "Solo": 0})

    def test_cycle_rejected(self):
        with self.assertRaises(SystemExit):
            self.load(BASE + '[projects.A]\nprefix = "A"\nnext = "B"\n[projects.B]\nprefix = "B"\nnext = "A"\n')

    def test_next_without_prefix_rejected(self):
        with self.assertRaises(SystemExit):
            self.load(BASE.replace('prefix = "TDD"\n', ""))


PROJECTS = """team = "T"
[projects.dr]
next = "eng"
role = "researcher"
task = "deep-research"
[projects.eng]
prefix = "ENG"
role = "engineer"
task = "engineering"
[projects.idle]
prefix = "I"
"""
FILES = {
    "roles/principles.md": "",
    "roles/researcher.md": "", "roles/researcher.toml": "",
    "roles/engineer.md": "", "roles/engineer.toml": 'read_only = ["~/playground/private_docs"]\n',
    "tasks/deep-research.md": "",
    "tasks/deep-research.toml": 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/playground/private_docs"]\n',
    "tasks/engineering.md": "", "tasks/engineering.toml": 'model = "opus"\neffort = "high"\nrepo_from_issue = true\n',
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

    def remove(self, rel):
        os.remove(os.path.join(self.root, rel))

    def load(self, text=PROJECTS):
        path = os.path.join(self.outside, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def runs(self, text=PROJECTS):
        return pipeline.runnable(self.load(text), root=self.root)

    def rejects(self, fragment, prefix="pipeline.toml", text=PROJECTS):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith(prefix + ":") or msg.startswith(prefix + " "), msg)
        self.assertIn(fragment, msg)

    def test_runs(self):
        runs = self.runs()
        self.assertEqual(sorted(runs), ["dr", "eng"])
        dr, eng = runs["dr"], runs["eng"]
        self.assertEqual([f.name for f in dataclasses.fields(pipeline.Run)],
                         ["task_name", "task", "charter", "instructions", "memory", "read_only"])
        self.assertEqual((dr.task_name, dr.task["effort"], dr.memory, dr.read_only), ("deep-research", "xhigh", None, ()))
        self.assertEqual(dr.charter, os.path.join(self.root, "roles", "researcher.md"))
        self.assertEqual(dr.instructions, os.path.join(self.root, "tasks", "deep-research.md"))
        self.assertEqual(eng.read_only, (os.path.expanduser("~/playground/private_docs"),))
        self.assertTrue(eng.task["repo_from_issue"])

    def test_read_only_normalized(self):
        self.write("roles/engineer.toml", 'read_only = ["~/playground/private_docs/"]\n')
        self.assertEqual(self.runs()["eng"].read_only, (os.path.expanduser("~/playground/private_docs"),))

    def test_old_tables_rejected(self):
        self.rejects("unknown keys: roles", text=PROJECTS + "[roles.researcher]\n")
        self.rejects("unknown keys: tasks", text=PROJECTS + '[tasks.x]\nmodel = "opus"\n')

    def test_project_keys_rejected(self):
        for key, value in (("instructions", '"tasks/x.md"'), ("model", '"opus"'), ("effort", '"high"'), ("add_dirs", "[]"),
                           ("repo_from_issue", "true"), ("allowed_tools", "[]"), ("prefx", '"I"')):
            with self.subTest(key):
                self.rejects(key, text=PROJECTS.replace('prefix = "I"\n', f'prefix = "I"\n{key} = {value}\n'))

    def test_load_config_ignores_old_tables(self):
        cfg = self.load(PROJECTS + '[roles.researcher]\n[projects.old]\ninstructions = "stages/gone.md"\n')
        self.assertIn("old", cfg["projects"])

    def test_role_and_task_together(self):
        self.rejects("both role and task", text=PROJECTS.replace('role = "engineer"\n', ""))
        self.rejects("both role and task", text=PROJECTS.replace('task = "engineering"\n', ""))

    def test_unknown_role_or_task(self):
        self.rejects("'em'", text=PROJECTS.replace('role = "engineer"', 'role = "em"'))
        self.rejects("'principles'", text=PROJECTS.replace('role = "engineer"', 'role = "principles"'))
        self.rejects("'work-breakdown'", text=PROJECTS.replace('task = "engineering"', 'task = "work-breakdown"'))

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
        self.assertEqual(sorted(self.runs()), ["dr", "eng"])

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
        self.write("roles/engineer.toml", 'readonly = ["~/playground/private_docs"]\n')
        self.rejects("readonly", prefix="roles/engineer.toml")
        self.write("roles/engineer.toml", FILES["roles/engineer.toml"])
        self.write("tasks/engineering.toml", ENGINEERING + 'instructions = "x"\n')
        self.rejects("instructions", prefix="tasks/engineering.toml")

    def test_read_only_paths(self):
        for bad in ('["playground/private_docs"]', '["~/playground/../private_docs"]', '["{repo}/x"]', '"/"'):
            with self.subTest(bad):
                self.write("roles/engineer.toml", f"read_only = {bad}\n")
                self.rejects("read_only", prefix="roles/engineer.toml")

    def test_repo_read_only(self):
        self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n')
        self.assertEqual(self.runs()["eng"].read_only, ("{repo}",))
        self.rejects("{repo}", text=PROJECTS.replace('role = "researcher"', 'role = "engineer"'))
        self.write("tasks/engineering.toml", ENGINEERING + "allowed_tools = []\n")
        self.rejects("{repo}")

    def memory(self, path, read_only="~/playground/private_docs"):
        self.write("roles/engineer.toml", f'read_only = ["{read_only}"]\nmemory = "{path}"\n')

    def test_memory_ok(self):
        mem = os.path.join(self.outside, "engineer")
        os.makedirs(mem)
        self.memory(mem)
        self.assertEqual(self.runs()["eng"].memory, mem)

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
            self.assertEqual(self.runs()["eng"].memory, os.path.join(home, "notes"))

    def test_allowed_tools_checked_in_task_file(self):
        self.write("tasks/engineering.toml", ENGINEERING + 'allowed_tools = ["Bash(git push origin *)"]\n')
        self.rejects("wildcard", prefix="tasks/engineering.toml")
        rule = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
        self.write("tasks/engineering.toml", ENGINEERING + f'allowed_tools = ["{rule}"]\n')
        self.assertEqual(self.runs()["eng"].task["allowed_tools"], [rule])

    def test_load_config_does_not_need_files(self):
        self.remove("tasks/engineering.md")
        self.assertIn("eng", self.load()["projects"])


class Uncommitted(unittest.TestCase):
    def test_uncommitted_parse(self):
        out = "\0".join([" M roles/engineer.md", "?? tasks/new.toml", "!! roles/hidden.md", "?? roles/.DS_Store",
                         "!! tasks/.x.toml.swp", "R  roles/b.toml", "roles/a.toml", "?? roles/with space.toml",
                         "D  tasks/old.md", "?? roles/notes.txt"]) + "\0"
        self.assertEqual(pipeline.uncommitted(out), ["roles/engineer.md", "tasks/new.toml", "roles/hidden.md",
                                                     "roles/b.toml", "roles/with space.toml", "tasks/old.md"])

    def test_clean(self):
        self.assertEqual(pipeline.uncommitted(""), [])


class RealConfig(unittest.TestCase):
    def test_three_runs(self):
        runs = pipeline.runnable(pipeline.load_config())
        got = {k: (r.task_name, r.task["model"], r.task["effort"], bool(r.task.get("repo_from_issue")), r.read_only, r.memory,
                   os.path.basename(r.charter))
               for k, r in runs.items()}
        private = os.path.expanduser("~/playground/private_docs")
        self.assertEqual(got, {
            "03495382-48f7-4280-a11c-4375df80a561": ("deep-research", "opus", "xhigh", False, (), None, "researcher.md"),
            "ba0738ba-ade7-4525-8d79-1b9944334e74": ("product-design", "opus", "high", False, (), None, "pm.md"),
            "ddbff8bf-b633-4b8c-9272-d1d5ee923747": ("engineering", "opus", "xhigh", True, (private,), None, "engineer.md"),
        })
        self.assertNotIn("allowed_tools", runs["ddbff8bf-b633-4b8c-9272-d1d5ee923747"].task)


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


if __name__ == "__main__":
    unittest.main()
