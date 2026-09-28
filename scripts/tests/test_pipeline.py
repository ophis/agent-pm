import os
import sys
import tempfile
import unittest

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

    def test_runnable(self):
        cfg = self.load(BASE.replace('next = "Product Design"', 'next = "Product Design"\ninstructions = "stages/x.md"\n'
                                     'model = "opus"\neffort = "high"'))
        os.makedirs(os.path.join(self.dir, "stages"))
        with self.assertRaises(SystemExit):  # instructions file missing
            pipeline.runnable(cfg, root=self.dir)
        open(os.path.join(self.dir, "stages", "x.md"), "w").close()
        self.assertEqual(list(pipeline.runnable(cfg, root=self.dir)), ["Deep Research"])

    def test_runnable_needs_model_and_effort(self):
        cfg = self.load(BASE.replace('next = "Product Design"', 'next = "Product Design"\ninstructions = "stages/x.md"'))
        with self.assertRaises(SystemExit):
            pipeline.runnable(cfg, root=self.dir)

    def test_missing_stage_file_does_not_break_load_config(self):
        cfg = self.load(BASE.replace('next = "Product Design"', 'next = "Product Design"\ninstructions = "stages/gone.md"\n'
                                     'model = "opus"\neffort = "high"'))
        self.assertIn("Deep Research", cfg["projects"])

    def test_load_config_rejects_bad_allowed_tools(self):
        text = BASE.replace('[projects.Engineering]\nprefix = "TDD"\n',
                             '[projects.Engineering]\nprefix = "TDD"\nallowed_tools = ["Bash(git push origin *)"]\n')
        with self.assertRaises(SystemExit):
            self.load(text)

    def test_load_config_accepts_valid_allowed_tools(self):
        text = BASE.replace(
            '[projects.Engineering]\nprefix = "TDD"\n',
            '[projects.Engineering]\nprefix = "TDD"\nrepo_from_issue = true\n'
            'allowed_tools = ["Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u '
            'git@github.com:{owner}/{name}.git {branch})"]\n')
        cfg = self.load(text)
        self.assertIn("Engineering", cfg["projects"])


class RealConfig(unittest.TestCase):
    def test_engineering_runnable(self):
        p = pipeline.runnable(pipeline.load_config())["ddbff8bf-b633-4b8c-9272-d1d5ee923747"]
        self.assertEqual((p["instructions"], p["prefix"], p["effort"], p.get("repo_from_issue")),
                         ("stages/engineering.md", "ENG", "xhigh", True))
        self.assertNotIn("allowed_tools", p)


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

    def test_rejects_root_in_tilde_form(self):
        home = os.path.expanduser("~")
        with self.assertRaises(SystemExit):
            pipeline.check_allowed_tools("E", {"repo_from_issue": True, "allowed_tools": ["Bash(cat ~/x/agent-pm/secret)"]},
                                         root=os.path.join(home, "x", "agent-pm"))

    def test_rejects_interpreter_on_script(self):
        for rule in ("Bash(python3 /tmp/x.py)", "Bash(bash ./do.sh)", "Bash(node x.js)", "Bash(python3.12 x.py)",
                     "Bash(make)", "Bash(make -C {worktree} test)", "Bash(npm test)", "Bash(npx jest)"):
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
