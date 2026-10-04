import os
import re
import sys
import tempfile
import textwrap
import tomllib
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import compose  # noqa: E402

class Plain:
    """A vehicle with the default scripts path and no handover."""
    def __init__(self, handover=""):
        self._handover = handover

    def scripts_path(self, root):
        return os.path.join(os.path.abspath(root), "src")

    def handover(self):
        return self._handover


CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PARAMS = compose.RunParams(input="Research X.", out="/w/out.md", workdir="/w", sid="11111111-2222-3333-4444-555555555555")

FILES = {
    "team/guide.md": "# Guide\n\nYou are {{role}} doing {{task}}.\n\nOn conflict: [Principles](#principles) > [{{role}} rules](#{{role_anchor}}) > [{{task}} rules](#{{task_anchor}}).\n",
    "team/principles.md": "# Principles\n",
    "team/roles/writer.md": "# Writer\n\nWrite well.\n",
    "team/tasks/short-note.md": "# Short Note\n\nWrite a note.\n",
    "team/tasks/long-note.md": "# Long Note\n\nWrite a long note.\n",
    "team/templates/note.md": "# Note: [Title]\n",
    "output/output.md": "# Output\n\nWrite `Output:`.\n",
    "output/destinations/local.md": "## Destination\n\nKeep it local.\n",
    "output/destinations/github.md": "## Destination\n\nPush to `{{repo}}` on `{{branch}}`.\n",
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
        self.write({"config/config.toml": textwrap.dedent(text)})

    def compose(self, role="writer", task=None, *, layers=(), **params):
        run = compose.load_run(self.root, role, task, layers=layers)
        return compose.render(self.root, run, replace(PARAMS, **params), vehicle=Plain()), run

    def fails(self, msg, role="writer", task=None, layers=()):
        with self.assertRaises(compose.ConfigError) as cm:
            self.compose(role, task, layers=layers)
        self.assertIn(msg, str(cm.exception))


class Resolve(Fake):
    def test_task_overrides_role_overrides_global(self):
        _, run = self.compose(task="long-note")
        self.assertEqual((run.tier, run.effort), (1, "medium"))

    def test_output_is_replaced_whole(self):
        _, run = self.compose(task="long-note")
        self.assertEqual(run.output, {"type": "github", "repo": "o/docs", "branch": "main"})
        _, run = self.compose(task="short-note")
        self.assertEqual(run.output, {"type": "local"})

    def test_list_defaults_are_empty(self):
        _, run = self.compose(task="long-note")
        self.assertEqual((run.read, run.write, run.commands, run.templates), ([], [], [], []))

    def test_no_task_uses_the_role_default(self):
        _, run = self.compose()
        self.assertEqual((run.role, run.task), ("writer", "short-note"))

    def test_later_layers_replace_run_keys_at_any_level(self):
        layer = {"effort": "low", "roles": {"writer": {"tasks": {"long-note": {"tier": 3, "output": {"type": "local"}}}}}}
        _, run = self.compose(task="long-note", layers=[layer])
        self.assertEqual((run.tier, run.effort, run.output), (3, "low", {"type": "local"}))
        _, run = self.compose(task="long-note", layers=[layer, {"tier": 4}])
        self.assertEqual(run.tier, 4)

    def test_gate_defaults_to_none_and_a_layers_gate_renders_verbatim(self):
        self.write({"team/tasks/short-note.md": "# Short Note\n\nGate: `{{gate}}`.\n"})
        prompt, run = self.compose(task="short-note")
        self.assertEqual(run.gate, "")
        self.assertIn("Gate: `none`.", prompt)
        gate = "python3 /u/usage.py --below 80 *"
        prompt, run = self.compose(task="short-note", layers=[{"roles": {"writer": {"gate": gate}}}])
        self.assertEqual(run.gate, gate)
        self.assertIn(f"Gate: `{gate}`.", prompt)

    def test_gate_is_a_config_toml_run_key_too(self):
        self.config(CONFIG.replace('effort = "medium"', 'effort = "medium"\ngate = "make gate"'))
        self.assertEqual(self.compose(task="long-note")[1].gate, "make gate")

    def test_no_language_no_language_line(self):
        self.write({"team/principles.md": FILES["team/principles.md"] + "\n- Write well.\n- Reports are in {{language}}.\n- Be brief.\n"})
        prompt, run = self.compose(task="long-note")
        self.assertEqual(run.language, "")
        self.assertIn("- Write well.\n- Be brief.\n", prompt)
        self.assertNotIn("Reports are in", prompt)

    def test_language_renders_its_line_once_at_any_level(self):
        self.write({"team/principles.md": FILES["team/principles.md"] + "\n- Reports are in {{language}}.\n"})
        self.config(CONFIG.replace('effort = "high"', 'effort = "high"\nlanguage = "French"'))
        prompt, run = self.compose(task="long-note")
        self.assertEqual(prompt.count("Reports are in French."), 1)
        _, run = self.compose(task="long-note", layers=[{"roles": {"writer": {"tasks": {"long-note": {"language": "German"}}}}}])
        self.assertEqual(run.language, "German")
        prompt, run = self.compose(task="long-note", layers=[{"language": ""}])
        self.assertNotIn("Reports are in", prompt)

    def test_progress_names_come_from_the_task_in_order_once_each(self):
        self.write({"team/tasks/short-note.md": "# Short Note\n\n1. a\n   [agent-pm-progress:start] what\n"
                    "   - [agent-pm-progress:round-1] x\n   [agent-pm-progress:start] again\n"
                    "see [agent-pm-progress:mid] mid-line\n"})
        self.assertEqual(self.compose(task="short-note")[1].progress, ["start", "round-1"])
        self.assertEqual(self.compose(task="long-note")[1].progress, [])

    def test_layers_ignore_their_own_keys(self):
        _, run = self.compose(task="long-note", layers=[{"flags": ["-x"], "description": "d"}])
        self.assertEqual(run.tier, 1)


class Validate(Fake):
    def test_config_toml_must_be_valid_on_its_own(self):
        self.config(CONFIG.replace("tier = 1", "tier = 9"))
        self.fails("tier must be an integer 1–4", task="long-note", layers=[{"tier": 2}])

    def test_config_errors_come_before_missing_rule_files(self):
        self.config(CONFIG.replace("tier = 1", "tier = 9"))
        os.remove(os.path.join(self.root, "team", "tasks", "long-note.md"))
        self.fails("tier must be an integer 1–4", task="long-note")

    def test_invalid_layer_value(self):
        self.fails("tier must be an integer 1–4", task="long-note", layers=[{"tier": 9}])

    def test_gate_is_a_run_key(self):
        self.assertIn("gate", compose.RUN_KEYS)

    def test_gate_is_one_line_without_backticks(self):
        for gate in ("a\nb", "echo `id`", 7):
            self.fails("gate must be one line of shell command without backticks", task="long-note",
                       layers=[{"gate": gate}])
        _, run = self.compose(task="long-note")
        with self.assertRaises(compose.ConfigError):
            replace(run, gate="a\nb")

    def test_language_is_one_line_of_text(self):
        self.assertIn("language", compose.RUN_KEYS)
        for language in ("a\nb", 7):
            self.fails("language must be one line of text", task="long-note", layers=[{"language": language}])

    def test_replace_is_checked(self):
        _, run = self.compose(task="long-note")
        with self.assertRaises(compose.ConfigError):
            replace(run, effort="ultra")

    def test_resume_needs_a_sid(self):
        with self.assertRaises(compose.ConfigError):
            compose.RunParams(input="x", out="o", workdir="w", resume=True)

    def test_new_params_get_a_sid(self):
        self.assertRegex(compose.RunParams(input="x", out="o", workdir="w").sid, r"^[0-9a-f-]{36}$")

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
        os.remove(os.path.join(self.root, "team", "tasks", "long-note.md"))
        self.fails("missing file tasks/long-note.md", task="long-note")

    def test_default_task_must_exist(self):
        self.config(CONFIG.replace('default_task = "short-note"', 'default_task = "essay"'))
        self.fails("default_task 'essay' is not one of writer's tasks")


class Prompt(Fake):
    def test_sections_in_order(self):
        prompt, _ = self.compose(task="short-note")
        prose = re.sub(r"```markdown\n.*?\n```\n", "", prompt, flags=re.S)
        heads = [line for line in prose.splitlines() if line.startswith(("# ", "## "))]
        self.assertEqual(heads, ["# Guide", "# Principles", "# Writer", "# Short Note", "# Template: `templates/note.md`",
                                 "# Output", "## Destination"])

    def test_precedence_line_names_role_and_task(self):
        prompt, _ = self.compose(task="short-note")
        self.assertIn("[Writer rules](#writer) > [Short Note rules](#short-note)", prompt)

    def test_guide_is_filled_and_holds_the_precedence_line(self):
        prompt, _ = self.compose(task="short-note")
        guide = prompt.split("# Principles", 1)[0]
        self.assertIn("You are Writer doing Short Note.", guide)
        self.assertEqual(prompt.count("On conflict:"), 1)
        self.assertIn("On conflict:", guide)

    def test_missing_guide(self):
        os.remove(os.path.join(self.root, "team", "guide.md"))
        self.fails("missing file guide.md")

    def test_destination_is_filled(self):
        prompt, _ = self.compose(task="long-note")
        self.assertIn("Push to `o/docs` on `main`.", prompt)

    def test_template_is_fenced(self):
        prompt, _ = self.compose(task="short-note")
        self.assertIn("```markdown\n# Note: [Title]\n```", prompt)

    def test_fence_outgrows_backticks_in_template(self):
        self.write({"team/templates/note.md": "```js\nx\n```\n"})
        prompt, _ = self.compose(task="short-note")
        self.assertIn("````markdown\n```js\nx\n```\n````", prompt)

    def test_tail_with_a_file_input(self):
        path = os.path.join(self.root, "in.md")
        self.write({"in.md": "question"})
        prompt, _ = self.compose(task="short-note", input=path, out="o.md", workdir="wd")
        tail = prompt.rsplit("\n---\n", 1)[1]
        self.assertEqual(tail.strip().splitlines(), [f"Input: {path}", f"Workdir: {os.path.abspath('wd')}"])

    def test_tail_with_free_text_input(self):
        prompt, _ = self.compose(task="short-note", input="Compare cmux and tmux.\nKeep it short.")
        tail = prompt.rsplit("\n---\n", 1)[1]
        self.assertEqual(tail.strip().splitlines(), ["Workdir: /w", "Input:", "", "Compare cmux and tmux.", "Keep it short."])

    def test_handover_closes_the_output_section(self):
        run = compose.load_run(self.root, "writer", "short-note")
        prompt = compose.render(self.root, run, PARAMS, vehicle=Plain("Say it back."))
        body = prompt.rsplit("\n---\n", 1)[0]
        self.assertTrue(body.rstrip().endswith("Keep it local.\n\n## Return\n\nSay it back."))

    def test_handover_gets_the_report_command(self):
        run = compose.load_run(self.root, "writer", "short-note")
        prompt = compose.render(self.root, run, PARAMS, vehicle=Plain("Run `{{report}} outcome <file>`."))
        self.assertIn(f"Run `python3 {self.root}/src/report.py --to /w/.report.jsonl outcome <file>`.", prompt)
        spaced = replace(PARAMS, workdir="/my w")
        self.assertEqual(compose.report_command("/s s", spaced), "python3 '/s s/report.py' --to '/my w/.report.jsonl'")
        self.assertEqual(spaced.channel, "/my w/.report.jsonl")

    def test_load_then_render_with_another_output(self):
        run = compose.load_run(self.root, "writer", "long-note")
        prompt = compose.render(self.root, replace(run, output={"type": "local"}), vehicle=Plain())
        self.assertIn("Keep it local.", prompt)
        self.assertNotIn("Push to", prompt)

    def test_no_params_no_tail(self):
        run = compose.load_run(self.root, "writer", "short-note")
        prompt = compose.render(self.root, run, vehicle=Plain())
        self.assertNotIn("Workdir: ", prompt)
        self.assertTrue(prompt.rstrip().endswith("Keep it local."))
        self.assertEqual((run.role_title, run.task_title, run.task_summary), ("Writer", "Short Note", "Write a note."))

    def test_resume_starts_with_resumed_run(self):
        prompt, _ = self.compose(task="short-note", resume=True)
        self.assertTrue(prompt.startswith(compose.RESUME + "# Guide"))
        prompt, _ = self.compose(task="short-note")
        self.assertTrue(prompt.startswith("# Guide"))

    def test_leftover_placeholder_in_a_rule_file(self):
        self.write({"team/tasks/short-note.md": "# Short Note\n\nUse {{tool}}.\n"})
        self.fails("unfilled placeholder {{tool}}", task="short-note")


class Fill(unittest.TestCase):
    def test_default_used_when_value_missing(self):
        self.assertEqual(compose.fill("https://{{host|github.com}}/{{repo}}", {"repo": "o/n"}, "t"), "https://github.com/o/n")

    def test_value_beats_default(self):
        self.assertEqual(compose.fill("{{host|github.com}}", {"host": "ghe.example.com"}, "t"), "ghe.example.com")

    def test_missing_without_default_fails(self):
        with self.assertRaises(compose.ConfigError):
            compose.fill("{{repo}}", {}, "t")


class Anchor(unittest.TestCase):
    def test_github_style(self):
        self.assertEqual(compose.anchor("Light Research"), "light-research")
        self.assertEqual(compose.anchor("Template: `x.md`"), "template-xmd")


with open(os.path.join(CORE, "config", "config.toml"), "rb") as _f:
    ALL = [(r, t) for r, role in tomllib.load(_f)["roles"].items() for t in role.get("tasks", {})]


def composed(role, task=None):
    run = compose.load_run(CORE, role, task)
    return compose.render(CORE, run, PARAMS, vehicle=Plain()), run


LANGUAGE_RULE = "headings and fixed labels included"


class RealCore(unittest.TestCase):
    def test_core_config_renders_the_language_rule_once(self):
        for role, task in ALL:
            prompt, run = composed(role, task)
            self.assertEqual(run.language, "Chinese")
            self.assertEqual(prompt.count(LANGUAGE_RULE), 1, task)
            self.assertIn("are in Chinese", prompt, task)

    def test_no_language_no_language_rule(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("team", "output", "src"):
                os.symlink(os.path.join(CORE, d), os.path.join(root, d))
            with open(os.path.join(CORE, compose.CONFIG)) as f:
                cfg = f.read()
            self.assertIn('\nlanguage = "Chinese"\n', cfg)
            os.makedirs(os.path.join(root, "config"))
            with open(os.path.join(root, compose.CONFIG), "w") as f:
                f.write(cfg.replace('\nlanguage = "Chinese"\n', "\n"))
            for role, task in ALL:
                run = compose.load_run(root, role, task)
                prompt = compose.render(root, run, PARAMS, vehicle=Plain())
                self.assertEqual(run.language, "", task)
                self.assertNotIn(LANGUAGE_RULE, prompt, task)
                self.assertNotIn("Chinese", prompt, task)

    def test_every_prompt_has_the_progress_rule_once(self):
        for role, task in ALL:
            self.assertEqual(composed(role, task)[0].count("is a point to tell the user your progress"), 1, task)

    def test_every_task_compiles_without_placeholders(self):
        for role, task in ALL:
            prompt, run = composed(role, task)
            self.assertNotIn("{{", prompt, task)
            self.assertEqual(run.task, task)

    def test_default_tasks(self):
        for role, task in (("pm", "product-design"), ("engineer", "engineering")):
            self.assertEqual(composed(role)[1].task, task)

    def test_every_prompt_opens_with_the_guide(self):
        for role, task in ALL:
            prompt, run = composed(role, task)
            guide = prompt.split("\n# Principles\n", 1)[0]
            self.assertTrue(guide.startswith("# Guide\n"), task)
            self.assertIn(f"[{run.task_title}](#{compose.anchor(run.task_title)})", guide, task)
            self.assertEqual(prompt.count("On conflict:"), 1, task)
            self.assertIn("On conflict:", guide, task)

    def test_product_design(self):
        prompt, run = composed("pm", "product-design")
        self.assertIn("[PM rules](#pm) > [Product Design rules](#product-design)", prompt)
        self.assertIn("# Template: `templates/prd.md`", prompt)
        self.assertEqual(run.output["dir"], "Product Design/")
        self.assertEqual((run.read, run.write), ([], []))

    def test_engineering(self):
        prompt, run = composed("engineer", "engineering")
        self.assertIn("[Engineer rules](#engineer) > [Engineering rules](#engineering)", prompt)
        self.assertIn("gh pr create", prompt)
        self.assertEqual((run.effort, run.write, run.output), ("xhigh", [], {"type": "pull-request"}))

    def test_researcher_defaults_to_deep_research(self):
        _, run = composed("researcher")
        self.assertEqual(run.task, "deep-research")

    def test_researcher_deep_research_compiles(self):
        prompt, run = composed("researcher", "deep-research")
        self.assertIn("[Researcher rules](#researcher) > [Deep Research rules](#deep-research)", prompt)
        self.assertIn("# Template: `templates/research-report.md`", prompt)
        self.assertIn("`ophis/private_docs`", prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual((run.tier, run.effort, run.read), (2, "high", []))

    def test_pre_approved_commands_match_the_task_text(self):
        for role, task in ALL:
            prompt, run = composed(role, task)
            for cmd in run.commands:
                cmd = compose.fill(cmd, {"scripts": os.path.join(CORE, "src"), "workdir": "<Workdir>"}, task).removesuffix(" *")
                self.assertIn(f"`{cmd}", prompt, task)

    def test_no_orchestration_references(self):
        for role, task in ALL:
            prompt, _ = composed(role, task)
            for word in ("router.py", "research.py", "usage.py", "eng.py", "Linear", "In Review", "Todo", "Handoff", "issue"):
                self.assertNotIn(word, prompt, f"{task}: {word}")

    def test_deep_research_gate_defaults_to_none_and_a_layers_gate_is_named(self):
        self.assertIn("the gate is `none`", composed("researcher", "deep-research")[0])
        gate = "python3 /u/usage.py --below 80"
        run = compose.load_run(CORE, "researcher", "deep-research", layers=[{"gate": gate}])
        self.assertIn(f"the gate is `{gate}`", compose.render(CORE, run, PARAMS, vehicle=Plain()))

    def test_each_task_marks_its_start_once_and_never_a_budget(self):
        # build_cutoff needs the start mark: a task without it silently loses its start comment.
        for task in ("deep-research", "light-research", "product-design", "engineering"):
            with open(os.path.join(CORE, "team", "tasks", f"{task}.md")) as f:
                text = f.read()
            self.assertEqual(len(re.findall(r"^\s*\[agent-pm-progress:start\] \S", text, re.M)), 1, task)
            self.assertNotIn("agent-pm-progress:budget", text, task)

    def test_progress_names_of_the_real_tasks(self):
        self.assertEqual(compose.load_run(CORE, "researcher", "deep-research").progress, ["start", "round"])
        self.assertEqual(compose.load_run(CORE, "pm", "product-design").progress, ["start"])
        self.assertEqual(compose.load_run(CORE, "engineer", "engineering").progress, ["start", "step"])

    def test_github_host_defaults_and_overrides(self):
        run = compose.load_run(CORE, "researcher", "light-research")
        self.assertIn("https://github.com/ophis/private_docs/blob/main/", compose.render(CORE, run, vehicle=Plain()))
        prompt = compose.render(CORE, replace(run, output={**run.output, "host": "ghe.example.com"}), vehicle=Plain())
        self.assertIn("gh repo clone ghe.example.com/ophis/private_docs", prompt)
        self.assertIn("https://ghe.example.com/ophis/private_docs/blob/main/", prompt)

    def test_researcher_light_research_compiles(self):
        prompt, run = composed("researcher", "light-research")
        self.assertIn("[Researcher rules](#researcher) > [Light Research rules](#light-research)", prompt)
        self.assertIn("`ophis/private_docs`", prompt)
        self.assertNotIn("{{", prompt)
        self.assertEqual((run.tier, run.effort, run.read), (2, "high", []))


if __name__ == "__main__":
    unittest.main()
