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


REGISTRY = """team = "T"
[roles.researcher]
[roles.engineer]
read_only = ["~/playground/private_docs"]
[tasks.deep-research]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
[tasks.engineering]
model = "opus"
effort = "high"
repo_from_issue = true
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


class Runnable(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "root")
        self.outside = os.path.join(tmp.name, "outside")
        os.makedirs(self.outside)
        for d, names in (("roles", ("principles", "researcher", "engineer")), ("tasks", ("deep-research", "engineering"))):
            os.makedirs(os.path.join(self.root, d))
            for n in names:
                open(os.path.join(self.root, d, f"{n}.md"), "w").close()
        os.makedirs(os.path.join(self.root, "templates"))

    def load(self, text):
        path = os.path.join(self.outside, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def runs(self, text):
        return pipeline.runnable(self.load(text), root=self.root)

    def rejects(self, text, fragment):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        self.assertTrue(str(cm.exception.code).startswith("pipeline.toml: "), cm.exception.code)
        self.assertIn(fragment, str(cm.exception.code))

    def test_runs(self):
        runs = self.runs(REGISTRY)
        self.assertEqual(sorted(runs), ["dr", "eng"])
        dr, eng = runs["dr"], runs["eng"]
        self.assertEqual((dr.role_name, dr.task_name, dr.task["effort"], dr.memory, dr.read_only),
                         ("researcher", "deep-research", "xhigh", None, ()))
        self.assertEqual(dr.charter, os.path.join(self.root, "roles", "researcher.md"))
        self.assertEqual(dr.instructions, os.path.join(self.root, "tasks", "deep-research.md"))
        self.assertEqual(eng.read_only, (os.path.expanduser("~/playground/private_docs"),))
        self.assertEqual(eng.project["prefix"], "ENG")

    def test_read_only_normalized(self):
        runs = self.runs(REGISTRY.replace('"~/playground/private_docs"]\n[tasks', '"~/playground/private_docs/"]\n[tasks'))
        self.assertEqual(runs["eng"].read_only, (os.path.expanduser("~/playground/private_docs"),))

    def test_moved_keys_rejected(self):
        for key, value in (("instructions", '"tasks/x.md"'), ("model", '"opus"'), ("effort", '"high"'),
                           ("add_dirs", "[]"), ("repo_from_issue", "true"), ("allowed_tools", "[]")):
            self.rejects(REGISTRY.replace('[projects.idle]\nprefix = "I"\n', f'[projects.idle]\nprefix = "I"\n{key} = {value}\n'), key)

    def test_load_config_ignores_moved_keys(self):
        cfg = self.load(REGISTRY + 'instructions = "stages/gone.md"\n')
        self.assertIn("idle", cfg["projects"])

    def test_role_and_task_together(self):
        self.rejects(REGISTRY.replace('role = "engineer"\n', ""), "both role and task")
        self.rejects(REGISTRY.replace('task = "engineering"\n[projects.idle]', "[projects.idle]"), "both role and task")

    def test_unknown_role_or_task(self):
        self.rejects(REGISTRY.replace('role = "engineer"\n', 'role = "em"\n'), "em")
        self.rejects(REGISTRY.replace('task = "engineering"\n[projects.idle]', 'task = "work-breakdown"\n[projects.idle]'), "work-breakdown")

    def test_missing_files(self):
        os.remove(os.path.join(self.root, "roles", "engineer.md"))
        self.rejects(REGISTRY, "engineer.md")
        open(os.path.join(self.root, "roles", "engineer.md"), "w").close()
        os.remove(os.path.join(self.root, "tasks", "engineering.md"))
        self.rejects(REGISTRY, "engineering.md")

    def test_unused_entries_checked(self):
        self.rejects(REGISTRY + "[tasks.light-research]\nmodel = \"opus\"\neffort = \"high\"\n", "light-research")

    def test_bad_names(self):
        open(os.path.join(self.root, "roles", "Researcher.md"), "w").close()
        self.rejects(REGISTRY.replace("[roles.researcher]", "[roles.Researcher]").replace('role = "researcher"', 'role = "Researcher"'), "Researcher")
        self.rejects(REGISTRY + "[roles.principles]\n", "principles")
        self.rejects(REGISTRY + '[tasks.deep_research]\nmodel = "opus"\neffort = "high"\n', "deep_research")

    def test_task_needs_model_and_effort(self):
        self.rejects(REGISTRY.replace('effort = "xhigh"\n', ""), "effort")
        self.rejects(REGISTRY.replace('model = "opus"\neffort = "xhigh"', 'effort = "xhigh"'), "model")

    def test_unknown_keys(self):
        self.rejects(REGISTRY.replace('read_only = ["~/playground/private_docs"]', 'readonly = ["~/playground/private_docs"]'), "readonly")
        self.rejects(REGISTRY.replace('repo_from_issue = true\n', 'repo_from_issue = true\ninstructions = "x"\n'), "instructions")

    def test_read_only_paths(self):
        for bad in ('"playground/private_docs"', '"~/playground/../private_docs"', '"{repo}/x"'):
            self.rejects(REGISTRY.replace('read_only = ["~/playground/private_docs"]', f"read_only = [{bad}]"), "read_only")
        self.rejects(REGISTRY.replace('read_only = ["~/playground/private_docs"]', 'read_only = "/"'), "read_only")

    def test_repo_read_only(self):
        repo = REGISTRY.replace('read_only = ["~/playground/private_docs"]', 'read_only = ["{repo}"]')
        self.assertEqual(self.runs(repo)["eng"].read_only, ("{repo}",))
        self.rejects(repo.replace('role = "researcher"', 'role = "engineer"'), "{repo}")
        self.rejects(repo.replace("repo_from_issue = true\n", "repo_from_issue = true\nallowed_tools = []\n"), "{repo}")

    def memory(self, path, read_only="~/playground/private_docs"):
        return REGISTRY.replace('read_only = ["~/playground/private_docs"]',
                                f'read_only = ["{read_only}"]\nmemory = "{path}"')

    def test_memory_ok(self):
        mem = os.path.join(self.outside, "engineer")
        os.makedirs(mem)
        self.assertEqual(self.runs(self.memory(mem))["eng"].memory, mem)

    def test_memory_rejected(self):
        ro = os.path.join(self.outside, "docs")
        for d in (ro, os.path.join(ro, "sub"), os.path.join(self.root, "notes"), os.path.join(self.root, "roles")):
            os.makedirs(d, exist_ok=True)
        cases = [("relative", "mem"), ("missing", os.path.join(self.outside, "nope")),
                 ("under root", os.path.join(self.root, "notes")), ("root itself", self.root),
                 ("roles", os.path.join(self.root, "roles")), ("ancestor of root", os.path.dirname(self.root)),
                 ("at read_only", ro), ("under read_only", os.path.join(ro, "sub")), ("ancestor of read_only", self.outside)]
        for label, path in cases:
            with self.subTest(label):
                self.rejects(self.memory(path, read_only=ro), "memory")

    def test_memory_symlink_into_root_rejected(self):
        link = os.path.join(self.outside, "link")
        os.symlink(os.path.join(self.root, "templates"), link)
        self.rejects(self.memory(link), "memory")

    def test_memory_under_claude_config_rejected(self):
        home = os.path.join(self.outside, "home")
        for d in (".claude/mem", "Library/LaunchAgents/mem"):
            os.makedirs(os.path.join(home, d))
        with mock.patch.dict(os.environ, {"HOME": home}):
            for d in (".claude/mem", "Library/LaunchAgents/mem"):
                self.rejects(self.memory(os.path.join(home, d)), "memory")

    def test_load_config_checks_task_allowed_tools(self):
        with self.assertRaises(SystemExit):
            self.load(REGISTRY.replace("repo_from_issue = true\n", 'repo_from_issue = true\nallowed_tools = ["Bash(git push origin *)"]\n'))
        cfg = self.load(REGISTRY.replace("repo_from_issue = true\n", "repo_from_issue = true\nallowed_tools = "
                                         '["Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"]\n'))
        self.assertIn("allowed_tools", cfg["tasks"]["engineering"])

    def test_load_config_does_not_need_files(self):
        os.remove(os.path.join(self.root, "tasks", "engineering.md"))
        self.assertIn("eng", self.load(REGISTRY)["projects"])


class RealConfig(unittest.TestCase):
    def test_three_runs(self):
        runs = pipeline.runnable(pipeline.load_config())
        got = {k: (r.role_name, r.task_name, r.task["model"], r.task["effort"], bool(r.task.get("repo_from_issue")), r.read_only, r.memory)
               for k, r in runs.items()}
        private = os.path.expanduser("~/playground/private_docs")
        self.assertEqual(got, {
            "03495382-48f7-4280-a11c-4375df80a561": ("researcher", "deep-research", "opus", "xhigh", False, (), None),
            "ba0738ba-ade7-4525-8d79-1b9944334e74": ("pm", "product-design", "opus", "high", False, (), None),
            "ddbff8bf-b633-4b8c-9272-d1d5ee923747": ("engineer", "engineering", "opus", "xhigh", True, (private,), None),
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
        pipeline.check_allowed_tools("Engineering", p)

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
                pipeline.check_allowed_tools("E", {"repo_from_issue": True, "allowed_tools": [f"Bash(cat {form}/x/agent-pm/secret)"]},
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
            self.assertTrue(str(cm.exception.code).startswith("pipeline.toml: "), cm.exception.code)

    def test_rejects_without_repo_from_issue(self):
        with self.assertRaises(SystemExit):
            self.check(["Bash(git status)"], repo_from_issue=False)


if __name__ == "__main__":
    unittest.main()
