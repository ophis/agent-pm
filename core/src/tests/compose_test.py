import glob
import os
import re
import shutil
import sys
import tempfile
import textwrap
import tomllib
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402

class Plain:
    """A client with the default scripts, methods and tasks paths (or `methods`, `tasks`) and no handover."""
    def __init__(self, handover="", methods=None, tasks=None):
        self._handover = handover
        self._methods = methods
        self._tasks = tasks

    def scripts_path(self, root):
        return os.path.join(os.path.abspath(root), "src")

    def methods_path(self, root):
        return self._methods or os.path.join(os.path.abspath(root), "team", "methods")

    def tasks_path(self, root):
        return self._tasks or os.path.join(os.path.abspath(root), "team", "tasks")

    def handover(self):
        return self._handover


CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PARAMS = compose.RunParams(input="Research X.", out="/w/out.md", workdir="/w", sid="11111111-2222-3333-4444-555555555555")

WRITER = ("# Writer\n\nWrite well.\n\n## Tasks\n\nUnsure → `short-note`.\n\n- `short-note`: a note; most times; light.\n"
          "- `long-note`: a long note; when asked; heavy.\n\n## Style\n\n- `draft`: not a task.\n")
FILES = {
    "team/guide.md": ("# Guide\n\nYou are {{role}}. Task: {{task|pick one}}; files in `{{tasks}}`.\n\n"
                      "On conflict: [Principles](#principles) > [{{role}} rules](#{{role_anchor}}) > your task's.\n"),
    "team/principles.md": "# Principles\n",
    "team/roles/writer.md": WRITER,
    "team/roles/editor.md": "# Editor\n\nEdit well.\n\n## Tasks\n\n- `long-note`: a long note; always; heavy.\n",
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
effort = "medium"
templates = ["note"]
read = ["repo"]

[roles.editor]
tier = 1

[roles.editor.output]
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
        self.write({compose.CONFIG: textwrap.dedent(text)})

    def local(self, text):
        with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
            f.write(text)

    def compose(self, role="writer", task=None, *, layers=(), **params):
        run = compose.load_run(self.root, role, task, layers=layers)
        return compose.render(self.root, run, replace(PARAMS, **params), client=Plain()), run

    def fails(self, msg, role="writer", task=None, layers=()):
        with self.assertRaises(compose.ConfigError) as cm:
            self.compose(role, task, layers=layers)
        self.assertIn(msg, str(cm.exception))


class Resolve(Fake):
    def test_role_overrides_global(self):
        _, run = self.compose("editor")
        self.assertEqual((run.tier, run.effort), (1, "high"))
        _, run = self.compose()
        self.assertEqual((run.tier, run.effort), (2, "medium"))

    def test_output_is_replaced_whole(self):
        self.assertEqual(self.compose("editor")[1].output, {"type": "github", "repo": "o/docs", "branch": "main"})
        self.assertEqual(self.compose()[1].output, {"type": "local"})

    def test_list_defaults_are_empty(self):
        _, run = self.compose("editor")
        self.assertEqual((run.read, run.write, run.commands, run.templates), ([], [], [], []))

    def test_a_task_is_the_named_one_else_empty(self):
        self.assertEqual(self.compose()[1].task, "")
        self.assertEqual(self.compose(task="long-note")[1].task, "long-note")
        self.assertEqual(self.compose("editor", "long-note")[1].task, "long-note")

    def test_later_layers_replace_run_keys_at_any_level(self):
        layer = {"effort": "low", "roles": {"editor": {"tier": 3, "output": {"type": "local"}}}}
        _, run = self.compose("editor", layers=[layer])
        self.assertEqual((run.tier, run.effort, run.output), (3, "low", {"type": "local"}))
        _, run = self.compose("editor", layers=[layer, {"tier": 4}])
        self.assertEqual(run.tier, 4)

    def test_gate_defaults_to_none_and_a_layers_gate_renders_verbatim(self):
        self.write({"team/roles/writer.md": WRITER + "\nGate: `{{gate}}`.\n"})
        prompt, run = self.compose()
        self.assertEqual(run.gate, "")
        self.assertIn("Gate: `none`.", prompt)
        gate = "python3 /u/usage.py --below 80 *"
        prompt, run = self.compose(layers=[{"roles": {"writer": {"gate": gate}}}])
        self.assertEqual(run.gate, gate)
        self.assertIn(f"Gate: `{gate}`.", prompt)

    def test_gate_is_a_config_toml_run_key_too(self):
        self.config(CONFIG.replace('effort = "medium"', 'effort = "medium"\ngate = "make gate"'))
        self.assertEqual(self.compose()[1].gate, "make gate")

    def test_no_language_no_language_line(self):
        self.write({"team/principles.md": FILES["team/principles.md"] + "\n- Write well.\n- Reports are in {{language}}.\n- Be brief.\n"})
        prompt, run = self.compose("editor")
        self.assertEqual(run.language, "")
        self.assertIn("- Write well.\n- Be brief.\n", prompt)
        self.assertNotIn("Reports are in", prompt)

    def test_language_renders_its_line_once_at_any_level(self):
        self.write({"team/principles.md": FILES["team/principles.md"] + "\n- Reports are in {{language}}.\n"})
        self.config(CONFIG.replace('effort = "high"', 'effort = "high"\nlanguage = "French"'))
        prompt, run = self.compose("editor")
        self.assertEqual(prompt.count("Reports are in French."), 1)
        _, run = self.compose("editor", layers=[{"roles": {"editor": {"language": "German"}}}])
        self.assertEqual(run.language, "German")
        prompt, run = self.compose("editor", layers=[{"language": ""}])
        self.assertNotIn("Reports are in", prompt)

    def test_the_local_file_goes_on_top(self):
        self.local('language = "French"\nshow = ""\ncwd = "/srv"\n'
                   '[roles.editor]\ntier = 3\n[roles.editor.output]\nbranch = "trunk"\n'
                   '[roles.writer]\nread = ["/data"]\n')
        _, run = self.compose("editor")
        self.assertEqual((run.language, run.show, run.cwd, run.tier, run.effort), ("French", "", "/srv", 3, "high"))
        self.assertEqual(run.output, {"type": "github", "repo": "o/docs", "branch": "trunk"})
        self.assertEqual(self.compose()[1].read, ["/data"])

    def test_layers_ignore_their_own_keys(self):
        _, run = self.compose("editor", layers=[{"flags": ["-x"], "description": "d"}])
        self.assertEqual(run.tier, 1)


class Index(Fake):
    def test_the_tasks_section_lists_each_task_and_its_line_in_order(self):
        self.assertEqual(list(compose.index(self.root, "writer").items()),
                         [("short-note", "a note; most times; light."), ("long-note", "a long note; when asked; heavy.")])
        self.assertEqual(list(compose.index(self.root, "editor")), ["long-note"])

    def test_no_tasks_section_or_an_empty_one(self):
        for text in ("# Writer\n\nWrite well.\n", "# Writer\n\n## Tasks\n\nUnsure → `x`.\n\n## Style\n\n- `short-note`: a.\n"):
            with self.subTest(text=text):
                self.write({"team/roles/writer.md": text})
                with self.assertRaisesRegex(compose.ConfigError, r"roles/writer\.md: no task index"):
                    compose.index(self.root, "writer")
                self.fails("roles/writer.md: no task index")

    def test_a_listed_task_without_its_file(self):
        os.remove(os.path.join(self.root, "team", "tasks", "long-note.md"))
        with self.assertRaisesRegex(compose.ConfigError, "roles/writer.md lists 'long-note' without tasks/long-note.md"):
            compose.index(self.root, "writer")
        for task in (None, "short-note"):
            self.fails("without tasks/long-note.md", task=task)


class OldKeys(Fake):
    def test_task_tables_or_default_task_in_any_role_table(self):
        for local in ('[roles.writer]\ndefault_task = "short-note"\n', '[roles.writer.tasks.short-note]\ntier = 3\n',
                      '[roles.writer]\ndefault_task = "x"\nmodel = "y"\n'):
            for role in ("writer", "editor"):
                with self.subTest(local=local, role=role):
                    self.local(local)
                    with self.assertRaises(compose.ConfigError) as cm:
                        self.compose(role)
                    self.assertIn("[roles.writer]", str(cm.exception))
                    self.assertNotIn("unknown key", str(cm.exception))

    def test_in_a_clients_role_table(self):
        self.local('[clients.skill.roles.writer.tasks.short-note.output]\ntype = "orchestrator"\n')
        self.fails("[clients.skill.roles.writer]", "editor")

    def test_the_check_names_its_layers_table(self):
        for table in ({"default_task": "x"}, {"tasks": {}}, {"tier": 1, "tasks": {"t": {}}}):
            with self.subTest(table=table), self.assertRaises(compose.ConfigError) as cm:
                compose.check_old_keys(table, "researcher", "core.")
            self.assertIn("[core.roles.researcher]", str(cm.exception))
        compose.check_old_keys({"tier": 1, "output": {}}, "researcher")


class Validate(Fake):
    def test_config_toml_must_be_valid_on_its_own(self):
        self.config(CONFIG.replace("tier = 1", "tier = 9"))
        self.fails("tier must be an integer 1–4", "editor", layers=[{"tier": 2}])

    def test_the_local_file_is_checked_with_config_toml(self):
        self.local('[roles.editor]\nmodel = "opus"\n')
        self.fails("unknown key 'model' in roles.editor", "editor")

    def test_config_errors_come_before_missing_rule_files(self):
        self.config(CONFIG.replace("tier = 1", "tier = 9"))
        os.remove(os.path.join(self.root, "team", "tasks", "long-note.md"))
        os.remove(os.path.join(self.root, "team", "roles", "editor.md"))
        self.fails("tier must be an integer 1–4", "editor")

    def test_invalid_layer_value(self):
        self.fails("tier must be an integer 1–4", "editor", layers=[{"tier": 9}])

    def test_gate_is_one_line_without_backticks(self):
        for gate in ("a\nb", "echo `id`", 7):
            self.fails("gate must be one line of shell command without backticks", "editor", layers=[{"gate": gate}])
        _, run = self.compose("editor")
        with self.assertRaises(compose.ConfigError):
            replace(run, gate="a\nb")

    def test_language_is_one_line_of_text(self):
        self.assertIn("language", compose.RUN_KEYS)
        for language in ("a\nb", 7):
            self.fails("language must be one line of text", "editor", layers=[{"language": language}])

    def test_show_is_unset_by_default_and_a_layer_sets_it_at_any_level(self):
        self.assertIn("show", compose.RUN_KEYS)
        self.assertIsNone(self.compose("editor")[1].show)
        _, run = self.compose("editor", layers=[{"show": ""}])
        self.assertEqual(run.show, "")
        _, run = self.compose("editor", layers=[{"show": "open-it {{session}}"}])
        self.assertEqual(run.show, "open-it {{session}}")
        _, run = self.compose("editor", layers=[{"roles": {"editor": {"show": "x {{session}}"}}}])
        self.assertEqual(run.show, "x {{session}}")

    def test_show_is_one_line_of_text(self):
        for show in ("a\nb", 7):
            self.fails("show must be one line of shell command", "editor", layers=[{"show": show}])

    def test_replace_is_checked(self):
        _, run = self.compose("editor")
        with self.assertRaises(compose.ConfigError):
            replace(run, effort="ultra")

    def test_resume_needs_a_sid(self):
        with self.assertRaises(compose.ConfigError):
            compose.RunParams(input="x", out="o", workdir="w", resume=True)

    def test_new_params_get_a_sid(self):
        self.assertRegex(compose.RunParams(input="x", out="o", workdir="w").sid, r"^[0-9a-f-]{36}$")

    def test_a_prefix_is_a_tmux_session_name(self):
        for prefix in ("bad name", "", "a.b"):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(compose.ConfigError, "prefix"):
                compose.RunParams(input="x", out="o", workdir="w", prefix=prefix)
        self.assertEqual(compose.RunParams(input="x", out="o", workdir="w", prefix="A_b-0").prefix, "A_b-0")

    def test_unknown_role(self):
        self.fails("unknown role 'nobody'", role="nobody")

    def test_unknown_task_or_another_roles(self):
        self.fails("task 'essay' is not one of writer's tasks (short-note, long-note)", task="essay")
        self.fails("task 'short-note' is not one of editor's tasks (long-note)", "editor", "short-note")

    def test_unknown_key(self):
        self.config(CONFIG.replace('effort = "medium"', 'effort = "medium"\nmodel = "opus"'))
        self.fails("unknown key 'model' in roles.writer")

    def test_tier_out_of_range(self):
        self.config(CONFIG.replace("tier = 1", "tier = 5"))
        self.fails("tier must be an integer 1–4", "editor")

    def test_bad_effort(self):
        self.config(CONFIG.replace('effort = "medium"', 'effort = "ultra"'))
        self.fails("effort must be one of")

    def test_unknown_destination(self):
        self.config(CONFIG.replace('type = "github"', 'type = "s3"'))
        self.fails("no destination 's3'", "editor")

    def test_missing_placeholder_value(self):
        self.config(CONFIG.replace('repo = "o/docs"\n', ""))
        self.fails("unfilled placeholder {{repo}}", "editor")

    def test_missing_template(self):
        self.config(CONFIG.replace('templates = ["note"]', 'templates = ["memo"]'))
        self.fails("missing file templates/memo.md")

    def test_missing_role_file(self):
        os.remove(os.path.join(self.root, "team", "roles", "editor.md"))
        self.fails("missing file roles/editor.md", "editor")


class Prompt(Fake):
    def test_sections_in_order(self):
        for task in (None, "long-note"):
            prompt, _ = self.compose(task=task)
            prose = re.sub(r"```markdown\n.*?\n```\n", "", prompt, flags=re.S)
            heads = [line for line in prose.splitlines() if line.startswith(("# ", "## "))]
            self.assertEqual(heads, ["# Guide", "# Principles", "# Writer", "## Tasks", "## Style",
                                     "# Template: `templates/note.md`", "# Output", "## Destination"], task)

    def test_the_guide_names_the_task_only_when_one_is_named(self):
        tasks = os.path.join(self.root, "team", "tasks")
        prompt, _ = self.compose()
        guide = prompt.split("# Principles", 1)[0]
        self.assertIn(f"You are Writer. Task: pick one; files in `{tasks}`.", guide)
        self.assertIn("On conflict: [Principles](#principles) > [Writer rules](#writer) > your task's.", guide)
        self.assertEqual(prompt.count("On conflict:"), 1)
        prompt, _ = self.compose(task="long-note")
        self.assertIn("You are Writer. Task: `long-note`; files in", prompt.split("# Principles", 1)[0])

    def test_no_task_text_reaches_the_prompt(self):
        for task in (None, "short-note", "long-note"):
            prompt, _ = self.compose(task=task)
            for text in ("Write a note.", "Write a long note.", "# Short Note", "# Long Note"):
                self.assertNotIn(text, prompt, task)

    def test_missing_guide(self):
        os.remove(os.path.join(self.root, "team", "guide.md"))
        self.fails("missing file guide.md")

    def test_destination_is_filled(self):
        prompt, _ = self.compose("editor")
        self.assertIn("Push to `o/docs` on `main`.", prompt)

    def test_template_is_fenced(self):
        prompt, _ = self.compose()
        self.assertIn("```markdown\n# Note: [Title]\n```", prompt)

    def test_fence_outgrows_backticks_in_template(self):
        self.write({"team/templates/note.md": "```js\nx\n```\n"})
        prompt, _ = self.compose()
        self.assertIn("````markdown\n```js\nx\n```\n````", prompt)

    def test_an_input_naming_a_file_is_text(self):
        self.write({"input.md": "question"})
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        for given in ("input.md", os.path.join(self.root, "input.md")):
            prompt, _ = self.compose(input=given, out="o.md", workdir="wd")
            tail = prompt.rsplit("\n---\n", 1)[1]
            self.assertEqual(tail.strip().splitlines(), [f"Workdir: {os.path.abspath('wd')}", "Input:", "", given])
            self.assertNotIn("question", prompt)

    def test_tail_with_free_text_input(self):
        prompt, _ = self.compose(input="Compare cmux and tmux.\nKeep it short.")
        tail = prompt.rsplit("\n---\n", 1)[1]
        self.assertEqual(tail.strip().splitlines(), ["Workdir: /w", "Input:", "", "Compare cmux and tmux.", "Keep it short."])

    def test_handover_closes_the_output_section(self):
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, PARAMS, client=Plain("Say it back."))
        body = prompt.rsplit("\n---\n", 1)[0]
        self.assertTrue(body.rstrip().endswith("Keep it local.\n\n## Return\n\nSay it back."))

    def test_handover_gets_the_report_command(self):
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, PARAMS, client=Plain("Run `{{report}} outcome <file>`."))
        self.assertIn(f"Run `python3 {self.root}/src/report.py --to /w/run.jsonl outcome <file>`.", prompt)
        spaced = replace(PARAMS, workdir="/my w")
        self.assertEqual(compose.report_command("/s s", spaced), "python3 '/s s/report.py' --to '/my w/run.jsonl'")
        self.assertEqual(spaced.channel, "/my w/run.jsonl")

    def test_guide_principles_and_role_text_name_the_clients_paths(self):
        self.write({"team/principles.md": "# Principles\n\nRun `{{scripts}}/x.py`; tasks in `{{tasks}}`.\n",
                    "team/roles/writer.md": WRITER + "\nKnow `{{methods}}` and `{{tasks}}/short-note.md`.\n"})
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, PARAMS, client=Plain(methods="/m", tasks="/t"))
        self.assertIn(f"Run `{self.root}/src/x.py`; tasks in `/t`.", prompt)
        self.assertIn("Know `/m` and `/t/short-note.md`.", prompt)
        self.assertIn("files in `/t`.", prompt)

    def test_load_then_render_with_another_output(self):
        run = compose.load_run(self.root, "editor")
        prompt = compose.render(self.root, replace(run, output={"type": "local"}), client=Plain())
        self.assertIn("Keep it local.", prompt)
        self.assertNotIn("Push to", prompt)

    def test_no_params_no_tail(self):
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, client=Plain())
        self.assertNotIn("Workdir: ", prompt)
        self.assertTrue(prompt.rstrip().endswith("Keep it local."))
        self.assertEqual((run.role_title, run.task), ("Writer", ""))

    def test_resume_starts_with_resumed_run_and_names_no_task(self):
        self.assertIn("Continue the task this session already picked or was given; never pick it again.", compose.RESUME)
        prompt, _ = self.compose(resume=True)
        self.assertTrue(prompt.startswith(compose.RESUME + "# Guide"))
        self.assertIn("Task: pick one;", prompt)
        prompt, _ = self.compose()
        self.assertTrue(prompt.startswith("# Guide"))

    def test_a_resumes_input_adds_to_the_sessions_earlier_input(self):
        self.assertIn("The input below is current: it adds to this session's earlier input.", compose.RESUME)
        self.assertNotIn("re-read the input", compose.RESUME)
        prompt, _ = self.compose(resume=True, input="Answers:\n- Use B.")
        self.assertTrue(prompt.startswith(compose.RESUME))
        self.assertTrue(prompt.endswith("\nInput:\n\nAnswers:\n- Use B.\n"))

    def test_leftover_placeholder_in_a_rule_file(self):
        self.write({"team/roles/writer.md": WRITER + "\nUse {{tool}}.\n"})
        self.fails("unfilled placeholder {{tool}}")


class Deliverable(Fake):
    DEST = {"output/destinations/local.md": "## Destination\n\nKeep it local.\nWrite it to `{{deliverable}}`.\n"}

    def test_it_is_out_under_the_real_workdir_else_tmp_deliverable(self):
        wd, elsewhere = (os.path.join(os.path.realpath(self.root), d) for d in ("wd", "elsewhere"))
        os.makedirs(os.path.join(wd, "sub"))
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(wd, "link"))
        os.symlink(wd, os.path.join(self.root, "via"))
        tmp = os.path.join(wd, "tmp", "deliverable.md")
        for out, want in ((f"{wd}/out.md", f"{wd}/out.md"), (f"{wd}/sub/../out.md", f"{wd}/out.md"),
                          (f"{elsewhere}/out.md", tmp), (f"{wd}/../elsewhere/out.md", tmp),
                          (f"{wd}/link/out.md", tmp), (f"{self.root}/via/out.md", f"{self.root}/via/out.md")):
            with self.subTest(out=out):
                self.assertEqual(compose.RunParams(input="x", out=out, workdir=wd).deliverable, want)

    def test_a_destination_names_it(self):
        self.write(self.DEST)
        prompt, _ = self.compose()
        self.assertIn("Keep it local.\nWrite it to `/w/out.md`.\n", prompt)
        prompt, _ = self.compose(out="/elsewhere/out.md")
        self.assertIn("Write it to `/w/tmp/deliverable.md`.", prompt)

    def test_without_params_its_lines_are_dropped(self):
        self.write(self.DEST)
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, client=Plain())
        self.assertNotIn("Write it to", prompt)
        self.assertTrue(prompt.rstrip().endswith("Keep it local."))

    def test_an_unfilled_placeholder_in_another_destination_line_still_fails(self):
        self.write({"output/destinations/local.md": "## Destination\n\nWrite it to `{{deliverable}}`.\nSee {{dir}}.\n"})
        run = compose.load_run(self.root, "writer")
        with self.assertRaises(compose.ConfigError) as cm:
            compose.render(self.root, run, client=Plain())
        self.assertIn("unfilled placeholder {{dir}}", str(cm.exception))


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


with open(os.path.join(CORE, compose.CONFIG), "rb") as _f:
    ROLES = list(tomllib.load(_f)["roles"])
ALL = [(r, t) for r in ROLES for t in compose.index(CORE, r)]
TASKS = os.path.join(CORE, "team", "tasks")


def composed(role, task=None):
    run = compose.load_run(CORE, role, task)
    return compose.render(CORE, run, PARAMS, client=Plain()), run


def task_text(task):
    with open(os.path.join(TASKS, f"{task}.md")) as f:
        return f.read()


def guide(prompt):
    return prompt.split("\n# Principles\n", 1)[0]


LANGUAGE_RULE = "headings and fixed labels included"
CHECKOUT_RULE = ("- **Checkout**: `[--name <checkout>]` in a command → `--name <checkout>`, `<checkout>` the input's "
                 "`Checkout:`; no `Checkout:` → drop it.")
PICK = ("**Your task**: pick it from your charter's Tasks section by the input; unsure → the default it names. Read only "
        f"that task's file, `{TASKS}/<task>.md`, and follow its steps in order.")


class RealCore(unittest.TestCase):
    def test_core_config_renders_the_language_rule_once(self):
        for role in ROLES:
            prompt, run = composed(role)
            self.assertEqual(run.language, "Chinese")
            self.assertEqual(prompt.count(LANGUAGE_RULE), 1, role)
            self.assertIn("are in Chinese", prompt, role)

    def test_no_language_no_language_rule(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("team", "output", "src"):
                os.symlink(os.path.join(CORE, d), os.path.join(root, d))
            with open(os.path.join(CORE, compose.CONFIG)) as f:
                cfg = f.read()
            self.assertIn('\nlanguage = "English"\n', cfg)
            os.makedirs(os.path.join(root, "config"))
            with open(os.path.join(root, compose.CONFIG), "w") as f:
                f.write(cfg.replace('\nlanguage = "English"\n', "\n"))
            for role in ROLES:
                run = compose.load_run(root, role)
                prompt = compose.render(root, run, PARAMS, client=Plain())
                self.assertEqual(run.language, "", role)
                self.assertNotIn(LANGUAGE_RULE, prompt, role)
                self.assertNotIn("Chinese", prompt, role)

    def test_a_local_users_trusted_dirs_and_workers_per_column_are_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("team", "output"):
                os.symlink(os.path.join(CORE, d), os.path.join(root, d))
            shutil.copy(os.path.join(CORE, compose.CONFIG), root)
            with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
                f.write('users = ["octocat"]\ntrusted_dirs = ["~/data-repo"]\nshow = ""\nworkers_per_column = 2\n')
            for role, task in ALL:
                run = compose.load_run(root, role, task)
                self.assertEqual((run.task, run.show), (task, ""))

    def test_local_lines_uncommented_are_a_valid_local_file(self):
        with open(os.path.join(CORE, compose.CONFIG)) as f:
            local = tomllib.loads("".join(line.removeprefix("# local: ") for line in f if line.startswith("# local: ")))
        self.assertLessEqual({"show", "cwd", "users", "trusted_dirs", "workers_per_column"}, set(local))
        self.assertLessEqual({"researcher", "pm"}, set(local["roles"]))
        for role, table in local["roles"].items():
            compose.check_old_keys(table, role)

    def test_every_prompt_has_the_progress_rule_once(self):
        for role in ROLES:
            self.assertEqual(composed(role)[0].count("is a point to tell the user your progress"), 1, role)

    def test_each_charter_opens_with_its_index_naming_a_listed_default(self):
        defaults = {"researcher": "deep-research", "pm": "product-design", "engineer": "build", "dummy-tester": "echo"}
        self.assertEqual(set(ROLES), set(defaults))
        for role, default in defaults.items():
            with open(os.path.join(CORE, "team", "roles", f"{role}.md")) as f:
                text = f.read()
            self.assertTrue(text.split("\n## ")[1].startswith(f"Tasks\n\nUnsure → `{default}`.\n\n- `"), role)
            self.assertIn(default, compose.index(CORE, role), role)
            for line in compose.index(CORE, role).values():
                self.assertRegex(line, r"; (light|heavy)\.$", role)
        self.assertEqual(sorted(t for _, t in ALL), sorted(f.removesuffix(".md") for f in os.listdir(TASKS)))

    def test_engineers_index_gives_the_users_examples(self):
        tasks = compose.index(CORE, "engineer")
        self.assertIn("e.g. a PRD feature spanning several modules like TASK-227", tasks["build"])
        self.assertIn("e.g. a template or wording tweak like TASK-226", tasks["light-build"])

    def test_task_files_have_no_frontmatter_no_placeholder_and_one_start_mark(self):
        # The start mark: the root CLAUDE.md › Gotchas.
        for name in os.listdir(TASKS):
            text = task_text(name.removesuffix(".md"))
            self.assertTrue(text.startswith("# "), name)
            self.assertNotIn("{{", text, name)
            self.assertEqual(len(re.findall(r"^\s*\[agent-pm-progress:start\] \S", text, re.M)), 1, name)
            self.assertNotIn("agent-pm-progress:budget", text, name)

    def test_local_and_orchestrator_destinations_have_the_deliverable_line_once(self):
        for dest in ("local", "orchestrator"):
            run = replace(compose.load_run(CORE, "researcher"), output={"type": dest})
            with_params = compose.render(CORE, run, PARAMS, client=Plain())
            self.assertEqual(with_params.count("`/w/out.md`"), 1, dest)
            self.assertIn("Write the deliverable to `/w/out.md` and return that file as the outcome's `deliverable`",
                          with_params, dest)
            without = compose.render(CORE, run, client=Plain())
            self.assertNotIn("Write the deliverable to", without, dest)
            self.assertIn("Put the deliverable in the outcome's `deliverable`", without, dest)
            self.assertIn("Leave `url` empty.", without, dest)

    def test_every_run_compiles_without_placeholders(self):
        for role, task in [(r, None) for r in ROLES] + ALL:
            prompt, run = composed(role, task)
            self.assertNotIn("{{", prompt, (role, task))
            self.assertEqual(run.task, task or "")

    def test_every_prompt_opens_with_the_guide(self):
        for role, task in [(r, None) for r in ROLES] + ALL:
            prompt, run = composed(role, task)
            g = guide(prompt)
            self.assertTrue(g.startswith("# Guide\n"), task)
            self.assertIn(f"`{TASKS}/<task>.md`", g, task)
            self.assertIn(f"[{run.role_title} rules](#{compose.anchor(run.role_title)}) > your task file's rules", g)
            self.assertEqual(prompt.count("On conflict:"), 1, task)
            self.assertIn("On conflict:", g, task)

    def test_the_guide_names_a_given_task_else_the_run_picks_it(self):
        named = guide(composed("researcher", "light-research")[0])
        self.assertIn("**Your task**: `light-research`. Read only that task's file", named)
        self.assertNotIn("pick it from", named)
        picked = guide(composed("researcher")[0])
        self.assertIn(PICK, picked)
        self.assertIn("Your start progress report names the task and why.", picked)
        self.assertNotIn("light-research", picked)

    def test_a_prompt_has_its_roles_index_and_no_task_steps(self):
        for role, task in [(r, None) for r in ROLES] + ALL:
            prompt, _ = composed(role, task)
            for t, line in compose.index(CORE, role).items():
                self.assertIn(f"\n- `{t}`: {line}\n", prompt, (role, task))
                for step in task_text(t).splitlines():
                    if len(step.strip()) >= 30 and not step.startswith("#"):
                        self.assertNotIn(step.strip(), prompt, (role, task, t))

    def test_a_task_from_another_roles_index_is_an_error(self):
        with self.assertRaisesRegex(compose.ConfigError, "task 'deep-research' is not one of engineer's tasks"):
            compose.load_run(CORE, "engineer", "deep-research")

    def test_pm(self):
        prompt, run = composed("pm")
        self.assertIn("[PM rules](#pm) > your task file's rules", prompt)
        self.assertIn("# Template: `templates/prd.md`", prompt)
        self.assertEqual(run.output["dir"], "Product Design/")
        self.assertEqual((run.read, run.write), ([], []))

    def test_engineer(self):
        prompt, run = composed("engineer")
        self.assertIn("[Engineer rules](#engineer) > your task file's rules", prompt)
        self.assertIn("gh pr create", prompt)
        self.assertEqual((run.effort, run.write, run.output), ("xhigh", [], {"type": "pull-request"}))

    def test_researcher(self):
        for task in (None, "deep-research", "light-research"):
            prompt, run = composed("researcher", task)
            self.assertIn("[Researcher rules](#researcher) > your task file's rules", prompt)
            self.assertIn("# Template: `templates/research-report.md`", prompt)
            self.assertIn("`ophis/private_docs`", prompt)
            self.assertEqual((run.tier, run.effort, run.read), (2, "high", ["{{methods}}"]))

    def test_deep_research_falls_back_to_both_method_files_the_researcher_names(self):
        prompt, _ = composed("researcher")
        for name in METHOD_NAMES:
            self.assertIn(f"`{os.path.join(CORE, 'team', 'methods', name)}.md`", prompt)
        for phrase in ("No such tool → follow the deep-research method (Researcher › Methods).",
                       "per the ultracode method (Researcher › Methods)", "No Workflow tool → follow that method.",
                       "Before the second, the brake (Researcher › Methods).",
                       "No subagents with a round's tools → skip that round, under Gaps."):
            self.assertIn(phrase, task_text("deep-research"))

    def test_researcher_names_no_harness_tool(self):
        with open(os.path.join(CORE, "team", "roles", "researcher.md")) as f:
            text = f.read()
        for word in ("Read, Grep", "Glob", "Workflow tool", "journal.jsonl"):
            self.assertNotIn(word, text, word)

    def test_pre_approved_commands_match_the_prompt(self):
        for role in ROLES:
            prompt, run = composed(role)
            for cmd in run.commands:
                cmd = compose.fill(cmd, {"scripts": os.path.join(CORE, "src"), "workdir": "<Workdir>"}, role).removesuffix(" *")
                self.assertIn(f"`{cmd}", prompt, role)

    def test_every_repo_py_command_takes_the_inputs_checkout(self):
        found = {}
        for role in ROLES:
            prompt, _ = composed(role)
            text = prompt + "".join(task_text(t) for t in compose.index(CORE, role))
            cmds = re.findall(r"`python3 \S+/repo\.py (worktree|status) ([^`]*)`", text)
            found[role] = [cmd for cmd, _ in cmds]
            for _, args in cmds:
                self.assertEqual(args, "--dir <Workdir>/src --branch <branch> [--name <checkout>] <repo>", role)
            self.assertEqual(rule(prompt, "Checkout"), [CHECKOUT_RULE], role)
        self.assertEqual(found, {"researcher": ["worktree"], "pm": ["worktree"], "engineer": ["worktree", "status"],
                                 "dummy-tester": ["worktree"]})

    def test_every_repo_named_by_url_may_be_a_local_clone_path(self):
        for role in ROLES:
            text = composed(role)[0] + "".join(task_text(t) for t in compose.index(CORE, role))
            self.assertIn("repo URL", text, role)
            self.assertEqual(text.count("repo URL"), len(re.findall(r"repo URLs? or local clone paths?", text)), role)

    def test_builds_fail_without_push_permission(self):
        self.assertIn("`push` false → `failed`", composed("engineer")[0])

    def test_builds_wait_for_the_prs_checks(self):
        for task in ("build", "light-build"):
            checks = section(composed("engineer", task)[0], "Checks")
            for literal in ("`gh pr checks <branch> --repo <host>/<owner>/<name> --watch --fail-fast`",
                            "`gh run view <run-id> --repo <host>/<owner>/<name> --log-failed`",
                            "`git -C <worktree> push -u origin <branch>`", "3 fixes", "20 minutes",
                            "Engineer › Finish › Failure"):
                self.assertIn(literal, checks, task)

    def test_builds_merge_the_default_branch_before_autopilot_at_resume_and_before_the_pr(self):
        prompt, _ = composed("engineer")
        merge = section(prompt, "Merge")
        for literal in ("Engineer › Repo step 1", "`git -C <worktree> status`", "a merge in progress",
                        "`git -C <worktree> merge --no-edit origin/<default>`", "`git -C <worktree> merge --abort`"):
            self.assertIn(literal, merge)
        for where in ("Autopilot", "Finish", "Resume"):
            self.assertIn("Engineer › Merge", section(prompt, where), where)
        self.assertIn("`origin/<default>`", section(prompt, "Autopilot"))
        for task in ("build", "light-build"):
            self.assertIn("Engineer › Resume.", task_text(task), task)

    def test_light_builds_cutoff_skips_merge_commits(self):
        self.assertIn("`git -C <worktree> log -1 --first-parent --no-merges --format=%cI`", task_text("light-build"))

    def test_research_hand_off_phrases(self):
        light, deep = task_text("light-research"), task_text("deep-research")
        self.assertIn("`Light Research. Angles: ", light)
        self.assertIn("`Suggest upgrading to Deep Research: ", light)
        self.assertIn("`Light Research.` line", deep)

    def test_read_only_tasks_branch_is_id_dash_task(self):
        # target.py leaves <id>-<task> branches of read-only tasks out of a build's branch check.
        self.assertIn("`<branch>` `<id>-<task>`, `<task>` this task, `deep-research` or `light-research`",
                      composed("researcher")[0])
        self.assertIn("`<branch>` `<id>-product-design`", task_text("product-design"))

    def test_light_research_prepares_before_its_start_mark(self):
        text = task_text("light-research")
        self.assertLess(text.index("**Prepare**"), text.index("[agent-pm-progress:start]"))

    def test_a_run_reads_the_methods_dir_its_text_names(self):
        claude = clients.load_config("claude", CORE)
        named = []
        for role in ROLES:
            with open(os.path.join(CORE, "team", "roles", f"{role}.md")) as f:
                text = f.read() + "".join(task_text(t) for t in compose.index(CORE, role))
            if "{{methods}}" in text:
                named.append(role)
                self.assertIn("{{methods}}", compose.load_run(CORE, role, layers=[claude]).read, role)
        self.assertEqual(named, ["researcher"])

    def test_no_orchestration_references(self):
        for role in ROLES:
            text = composed(role)[0] + "".join(task_text(t) for t in compose.index(CORE, role))
            for word in ("router.py", "research.py", "usage.py", "eng.py", "Linear", "In Review", "Todo", "Handoff", "issue"):
                self.assertNotIn(word, text, f"{role}: {word}")

    def test_no_skill_names_the_tracker(self):
        skills = glob.glob(os.path.join(CORE, "skills", "*", "SKILL.md"))
        for name in ("act-as", "manage"):
            self.assertIn(os.path.join(CORE, "skills", name, "SKILL.md"), skills)
        for path in skills:
            with open(path) as f:
                self.assertNotIn("Linear", f.read(), path)

    def test_only_the_user_loads_the_manager_guidelines(self):
        with open(os.path.join(CORE, "skills", "manage", "SKILL.md")) as f:
            front = re.match(r"---\n(.*?)\n---\n", f.read(), re.S)
        self.assertIn("disable-model-invocation: true", front.group(1).splitlines())
        with open(os.path.join(CORE, "skills", "tmux", "SKILL.md")) as f:
            tmux = f.read()
        for path in ("manager.md", "manage/SKILL.md"):
            self.assertNotIn(path, tmux)

    def test_every_researcher_run_names_the_gate_default_none(self):
        gate = "python3 /u/usage.py --below 80"
        for task in (None, "deep-research", "light-research"):
            self.assertIn("the gate is `none`", composed("researcher", task)[0], task)
            run = compose.load_run(CORE, "researcher", task, layers=[{"gate": gate}])
            self.assertIn(f"the gate is `{gate}`", compose.render(CORE, run, PARAMS, client=Plain()), task)

    def test_github_host_defaults_and_overrides(self):
        run = compose.load_run(CORE, "researcher")
        self.assertIn("https://github.com/ophis/private_docs/blob/main/", compose.render(CORE, run, client=Plain()))
        prompt = compose.render(CORE, replace(run, output={**run.output, "host": "ghe.example.com"}), client=Plain())
        self.assertIn("gh repo clone ghe.example.com/ophis/private_docs", prompt)
        self.assertIn("https://ghe.example.com/ophis/private_docs/blob/main/", prompt)


METHOD_NAMES = ("deep-research", "ultracode")


def method(name):
    with open(os.path.join(CORE, "team", "methods", f"{name}.md")) as f:
        return f.read()


def rule(text, name):
    return [line for line in text.splitlines() if line.startswith(f"- **{name}**:")]


def section(text, name):
    m = re.search(rf"^## {name}\n(.*?)(?=^#)", text, re.M | re.S)
    return m.group(1) if m else ""


class Methods(unittest.TestCase):
    def test_voting_is_the_same_in_both_and_pipeline_shares_its_start(self):
        deep, ultra = (method(m) for m in METHOD_NAMES)
        for m, text in zip(METHOD_NAMES, (deep, ultra)):
            for name in ("Voting", "Pipeline"):
                self.assertEqual(len(rule(text, name)), 1, f"{m}: {name}")
            self.assertTrue(rule(text, "Pipeline")[0].startswith(
                "- **Pipeline**: ≤ 10 subagents running at once. Each result of a stage goes to the next stage as soon "
                "as it arrives, never in batches;"), m)
        self.assertEqual(rule(deep, "Voting"), rule(ultra, "Voting"))
        self.assertTrue(rule(ultra, "Pipeline")[0].endswith("only verification waits for all claims, to rank them."))
        for phrase in ("fetch selection waits for all search results", "verification waits for all claims"):
            self.assertIn(phrase, rule(deep, "Pipeline")[0])

    def test_both_state_voting_pipeline_and_restrictions(self):
        for m in METHOD_NAMES:
            text = method(m)
            self.assertTrue(text.startswith("# "), m)
            self.assertNotRegex(text, r"\b[Yy]ou\b", m)
            for phrase in ("≥ 2 refutes → refuted", "else ≥ 2 valid votes → confirmed",
                           "else (agent errors, missing votes) → unverified", "votes that came back",
                           "unsure → votes refuted", "≤ 10 subagents running at once",
                           "verification waits for all claims", "the calling task's restrictions for",
                           "into every subagent prompt, voters included"):
                self.assertIn(phrase, text, m)

    def test_methods_are_harness_neutral(self):
        for m in METHOD_NAMES:
            text = method(m)
            for word in ("{{", "Read, Grep", "Glob", "Workflow tool", "journal.jsonl"):
                self.assertNotIn(word, text, f"{m}: {word}")

    def test_deep_research_limits_and_public_material(self):
        text = method("deep-research")
        for phrase in ("5 complementary web search angles", "after every search agent has returned",
                       "rank the whole set by relevance (high → low)", "dispatch fetches for the first ≤ 15",
                       "top 25", "≤ 100",
                       "start each web agent with fresh context (no inherited conversation), so it sees only its "
                       "prompt",
                       "only from the brief and web results", "Dispatch fetches only for URLs a search agent returned",
                       "**Page text**:"):
            self.assertIn(phrase, text)
        self.assertNotIn("as each search agent's results arrive", text)

    def test_ultracode_gaps_and_room_for_votes(self):
        text = method("ultracode")
        for phrase in ("A fixed method for the ultracode round", "room in the cap for the key claims' votes",
                       "while ≥ 3 cap slots remain", "undispatched subquestions and unverified claims go under Gaps"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
