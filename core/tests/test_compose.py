import io
import json
import os
import re
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import compose  # noqa: E402

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS = dict(input="Research X.", out="/w/out.md", workdir="/w")

FILES = {
    "principles.md": "# Principles\n\nOn conflict: [Principles](#principles) > [{{role}} rules](#{{role_anchor}}) > [{{task}} rules](#{{task_anchor}}).\n",
    "delegate-core/roles/writer.md": "# Writer\n\nWrite well.\n",
    "delegate-core/tasks/short-note.md": "# Short Note\n\nWrite a note.\n",
    "delegate-core/tasks/long-note.md": "# Long Note\n\nWrite a long note.\n",
    "delegate-core/templates/note.md": "# Note: [Title]\n",
    "delegate-core/output/output.md": "# Output\n\nWrite `Output:`.\n",
    "delegate-core/output/destinations/local.md": "## Destination\n\nKeep it local.\n",
    "delegate-core/output/destinations/github.md": "## Destination\n\nPush to `{{repo}}` on `{{branch}}`.\n",
}

CONFIG = """
tier = 2
effort = "high"

[output]
type = "local"

[roles.writer]
default_task = "short-note"
effort = "medium"

[roles.writer.tasks.short-note]
templates = ["note"]
read = ["repo"]

[roles.writer.tasks.long-note]
tier = 1

[roles.writer.tasks.long-note.output]
type = "github"
repo = "o/docs"
branch = "main"
"""


class Fake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.write(FILES)
        self.config(CONFIG)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, files):
        for rel, text in files.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def config(self, text):
        self.write({"config.toml": textwrap.dedent(text)})

    def compose(self, role="writer", task=None, **kw):
        return compose.compose(self.root, role, task, **{**PATHS, **kw})

    def fails(self, msg, role="writer", task=None):
        with self.assertRaises(compose.ConfigError) as cm:
            self.compose(role, task)
        self.assertIn(msg, str(cm.exception))


class Resolve(Fake):
    def test_task_overrides_role_overrides_global(self):
        _, run = self.compose(task="long-note")
        self.assertEqual((run["tier"], run["effort"]), (1, "medium"))

    def test_output_is_replaced_whole(self):
        _, run = self.compose(task="long-note")
        self.assertEqual(run["output"], {"type": "github", "repo": "o/docs", "branch": "main"})
        _, run = self.compose(task="short-note")
        self.assertEqual(run["output"], {"type": "local"})

    def test_list_defaults_are_empty(self):
        _, run = self.compose(task="long-note")
        self.assertEqual((run["read"], run["write"], run["commands"], run["templates"]), ([], [], [], []))

    def test_no_task_uses_the_role_default(self):
        _, run = self.compose()
        self.assertEqual((run["role"], run["task"]), ("writer", "short-note"))


class Validate(Fake):
    def test_unknown_role(self):
        self.fails("unknown role 'nobody'", role="nobody")

    def test_unknown_task(self):
        self.fails("task 'essay' is not one of writer's tasks", task="essay")

    def test_unknown_key(self):
        self.config(CONFIG.replace('effort = "medium"', 'effort = "medium"\nmodel = "opus"'))
        self.fails("unknown key 'model' in roles.writer")

    def test_tier_out_of_range(self):
        self.config(CONFIG.replace("tier = 1", "tier = 5"))
        self.fails("tier must be an integer 1–4", task="long-note")

    def test_bad_effort(self):
        self.config(CONFIG.replace('effort = "medium"', 'effort = "ultra"'))
        self.fails("effort must be one of")

    def test_unknown_destination(self):
        self.config(CONFIG.replace('type = "github"', 'type = "s3"'))
        self.fails("no destination 's3'", task="long-note")

    def test_missing_placeholder_value(self):
        self.config(CONFIG.replace('repo = "o/docs"\n', ""))
        self.fails("unfilled placeholder {{repo}}", task="long-note")

    def test_missing_template(self):
        self.config(CONFIG.replace('templates = ["note"]', 'templates = ["memo"]'))
        self.fails("missing file templates/memo.md")

    def test_missing_task_file(self):
        os.remove(os.path.join(self.root, "delegate-core", "tasks", "long-note.md"))
        self.fails("missing file tasks/long-note.md", task="long-note")

    def test_default_task_must_exist(self):
        self.config(CONFIG.replace('default_task = "short-note"', 'default_task = "essay"'))
        self.fails("default_task 'essay' is not one of writer's tasks")


class Prompt(Fake):
    def test_sections_in_order(self):
        prompt, _ = self.compose(task="short-note")
        prose = re.sub(r"```markdown\n.*?\n```\n", "", prompt, flags=re.S)
        heads = [line for line in prose.splitlines() if line.startswith(("# ", "## "))]
        self.assertEqual(heads, ["# Principles", "# Writer", "# Short Note", "# Template: `templates/note.md`",
                                 "# Output", "## Destination"])

    def test_precedence_line_names_role_and_task(self):
        prompt, _ = self.compose(task="short-note")
        self.assertIn("[Writer rules](#writer) > [Short Note rules](#short-note)", prompt)

    def test_destination_is_filled(self):
        prompt, _ = self.compose(task="long-note")
        self.assertIn("Push to `o/docs` on `main`.", prompt)

    def test_template_is_fenced(self):
        prompt, _ = self.compose(task="short-note")
        self.assertIn("```markdown\n# Note: [Title]\n```", prompt)

    def test_fence_outgrows_backticks_in_template(self):
        self.write({"delegate-core/templates/note.md": "```js\nx\n```\n"})
        prompt, _ = self.compose(task="short-note")
        self.assertIn("````markdown\n```js\nx\n```\n````", prompt)

    def test_tail_with_a_file_input(self):
        path = os.path.join(self.root, "in.md")
        self.write({"in.md": "question"})
        prompt, _ = self.compose(task="short-note", input=path, out="o.md", workdir="wd")
        tail = prompt.rsplit("\n---\n", 1)[1]
        self.assertEqual(tail.strip().splitlines(), [f"Input: {path}", f"Output: {os.path.abspath('o.md')}",
                                                     f"Workdir: {os.path.abspath('wd')}"])

    def test_tail_with_free_text_input(self):
        prompt, _ = self.compose(task="short-note", input="Compare cmux and tmux.\nKeep it short.")
        tail = prompt.rsplit("\n---\n", 1)[1]
        self.assertEqual(tail.strip().splitlines(), ["Output: /w/out.md", "Workdir: /w", "Input:", "",
                                                     "Compare cmux and tmux.", "Keep it short."])

    def test_resume_starts_with_resumed_run(self):
        prompt, _ = self.compose(task="short-note", resume=True)
        self.assertTrue(prompt.startswith("Resumed run"))
        prompt, _ = self.compose(task="short-note")
        self.assertTrue(prompt.startswith("# Principles"))

    def test_leftover_placeholder_in_a_rule_file(self):
        self.write({"delegate-core/tasks/short-note.md": "# Short Note\n\nUse {{tool}}.\n"})
        self.fails("unfilled placeholder {{tool}}", task="short-note")


class Anchor(unittest.TestCase):
    def test_github_style(self):
        self.assertEqual(compose.anchor("Light Research"), "light-research")
        self.assertEqual(compose.anchor("Template: `x.md`"), "template-xmd")


class Main(Fake):
    def run_main(self, *argv, stdin="q"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
            rc = compose.main(["--role", "writer", "--input", "-", "--out", "o", "--workdir", "w", *argv], root=self.root)
        return rc, out.getvalue(), err.getvalue()

    def test_prints_prompt(self):
        rc, out, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("# Principles"))

    def test_json_has_prompt_and_run(self):
        rc, out, _ = self.run_main("--task", "long-note", "--json")
        data = json.loads(out)
        self.assertEqual((rc, data["run"]["tier"]), (0, 1))
        self.assertIn("# Long Note", data["prompt"])

    def test_input_from_stdin(self):
        rc, out, _ = self.run_main(stdin="from stdin\n")
        self.assertTrue(out.rstrip().endswith("Input:\n\nfrom stdin"))

    def test_config_error_exits_2(self):
        rc, out, err = self.run_main("--task", "essay")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("compose.py: task 'essay'", err)


ALL = [("researcher", "deep-research"), ("researcher", "light-research"), ("pm", "product-design"), ("engineer", "engineering")]


class RealCore(unittest.TestCase):
    def test_every_task_compiles_without_placeholders(self):
        for role, task in ALL:
            prompt, run = compose.compose(CORE, role, task, **PATHS)
            self.assertNotIn("{{", prompt, task)
            self.assertEqual(run["task"], task)

    def test_default_tasks(self):
        for role, task in (("pm", "product-design"), ("engineer", "engineering")):
            self.assertEqual(compose.compose(CORE, role, **PATHS)[1]["task"], task)

    def test_product_design(self):
        prompt, run = compose.compose(CORE, "pm", "product-design", **PATHS)
        self.assertIn("[PM rules](#pm) > [Product Design rules](#product-design)", prompt)
        self.assertIn("# Template: `templates/prd.md`", prompt)
        self.assertEqual(run["output"]["dir"], "Product Design/")
        self.assertEqual((run["read"], run["write"]), ([], []))

    def test_engineering(self):
        prompt, run = compose.compose(CORE, "engineer", "engineering", **PATHS)
        self.assertIn("[Engineer rules](#engineer) > [Engineering rules](#engineering)", prompt)
        self.assertIn("gh pr create", prompt)
        self.assertEqual((run["effort"], run["write"], run["output"]), ("xhigh", ["repo"], {"type": "pull-request"}))

    def test_researcher_defaults_to_deep_research(self):
        _, run = compose.compose(CORE, "researcher", **PATHS)
        self.assertEqual(run["task"], "deep-research")

    def test_researcher_deep_research_compiles(self):
        prompt, run = compose.compose(CORE, "researcher", "deep-research", **PATHS)
        self.assertIn("[Researcher rules](#researcher) > [Deep Research rules](#deep-research)", prompt)
        self.assertIn("# Template: `templates/research-report.md`", prompt)
        self.assertIn("`ophis/private_docs`", prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual((run["tier"], run["effort"], run["read"]), (2, "high", ["repo"]))
        self.assertEqual(run["commands"], [])

    def test_pre_approved_commands_match_the_task_text(self):
        for role, task in ALL:
            prompt, run = compose.compose(CORE, role, task, **PATHS)
            for cmd in run["commands"]:
                self.assertIn(f"`{cmd}`", prompt, task)

    def test_no_orchestration_references(self):
        for role, task in ALL:
            prompt, _ = compose.compose(CORE, role, task, **PATHS)
            for word in ("router.py", "research.py", "usage.py", "eng.py", "Linear", "In Review", "Todo", "Handoff", "issue"):
                self.assertNotIn(word, prompt, f"{task}: {word}")

    def test_researcher_light_research_compiles(self):
        prompt, run = compose.compose(CORE, "researcher", "light-research", **PATHS)
        self.assertIn("[Researcher rules](#researcher) > [Light Research rules](#light-research)", prompt)
        self.assertIn("`ophis/private_docs`", prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual((run["tier"], run["effort"], run["read"]), (2, "high", ["repo"]))


if __name__ == "__main__":
    unittest.main()
