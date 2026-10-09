import glob
import io
import os
import re
import shlex
import shutil
import sys
import tempfile
import textwrap
import tomllib
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402

class Plain:
    """A client with the default scripts, methods and tasks paths (or `methods`, `tasks`), no handover and no inline
    workdir or input."""
    def __init__(self, handover="", methods=None, tasks=None, inline_workdir="", inline_input=""):
        self._handover = handover
        self._methods = methods
        self._tasks = tasks
        self.inline_workdir = inline_workdir
        self.inline_input = inline_input

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
RUN = replace(PARAMS, out="/runs/W-1/out.md", workdir="/runs/W-1")   # a workdir no other prompt text holds

INTRO = "Pick the task below that fits the input; unsure → `{0}` (`<tasks>/{0}.md`).\n\n"
WRITER = ("# Writer\n\nWrite well.\n\n## Tasks\n\n" + INTRO.format("short-note") +
          "- `short-note` (`<tasks>/short-note.md`): a note; most times; light.\n"
          "- `long-note` (`<tasks>/long-note.md`): a long note; when asked; heavy.\n\n"
          "## Style\n\n- `draft`: not a task.\n")
FILES = {
    "team/guide.md": ("# Guide\n\nYou are {{role}}. Task: {{task|pick one}}; files in `<tasks>`.\n\n"
                      "On conflict: [Principles](#principles) > [{{role}} rules](#{{role_anchor}}) > your task's.\n"),
    "team/principles.md": "# Principles\n",
    "team/roles/writer.md": WRITER,
    "team/roles/editor.md": ("# Editor\n\nEdit well.\n\n## Tasks\n\n" + INTRO.format("long-note") +
                             "- `long-note` (`<tasks>/long-note.md`): a long note; always; heavy.\n"),
    "team/tasks/short-note.md": "# Short Note\n\nWrite a note.\n",
    "team/tasks/long-note.md": "# Long Note\n\nWrite a long note.\n",
    "team/templates/note.md": "# Note: [Title]\n",
    "output/output.md": "# Output\n\nWrite `Output:`.\n",
    "output/destinations/local.md": "## Destination\n\nKeep it local.\n",
    "output/destinations/github.md": "## Destination\n\nPush to `<out-repo>` on `<out-branch>`.\n",
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


def parameters(prompt):
    """{name: value} of the prompt's # Parameters lines after compose.RULE, in order; a line of another shape fails."""
    section = re.search(r"^# Parameters\n\n(.*?)\n(?=# )", prompt, re.M | re.S).group(1)
    lines = section.removeprefix(f"{compose.RULE}\n\n").splitlines()
    return dict(re.fullmatch(r"- `([^`]+)`: (.+)", line).groups() for line in lines)


def expanded(prompt):
    """`prompt` with each # Parameters name written as its value, as the rule says a run writes a command: `report` at
    a code span's start, then the names its value holds and every other."""
    values = {name: value.strip("`") for name, value in parameters(prompt).items()}
    if report := values.pop(compose.REPORT, None):
        prompt = re.sub(r"`report(?=[` ])", lambda m: f"`{report}", prompt)
    for name, value in values.items():
        prompt = prompt.replace(name, value)
    return prompt


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

    def test_gate_defaults_to_none_and_a_layers_gate_is_its_value_verbatim(self):
        self.write({"team/roles/writer.md": WRITER + "\nGate: `<gate>`.\n"})
        prompt, run = self.compose()
        self.assertEqual(run.gate, "")
        self.assertEqual(parameters(prompt)["<gate>"], "`none`")
        gate = "python3 /u/usage.py --below 80 *"
        prompt, run = self.compose(layers=[{"roles": {"writer": {"gate": gate}}}])
        self.assertEqual(run.gate, gate)
        self.assertEqual(parameters(prompt)["<gate>"], f"`{gate}`")
        self.assertIn("Gate: `<gate>`.", prompt)

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
    def test_the_tasks_section_lists_each_task_and_its_description_in_order(self):
        self.assertEqual(list(compose.index(self.root, "writer").items()),
                         [("short-note", "a note; most times; light."), ("long-note", "a long note; when asked; heavy.")])
        self.assertEqual(list(compose.index(self.root, "editor")), ["long-note"])

    def test_no_tasks_section_or_an_empty_one(self):
        for text in ("# Writer\n\nWrite well.\n", "# Writer\n\n## Tasks\n\n" + INTRO.format("x") +
                     "## Style\n\n- `short-note` (`<tasks>/short-note.md`): a.\n"):
            with self.subTest(text=text):
                self.write({"team/roles/writer.md": text})
                with self.assertRaisesRegex(compose.ConfigError, r"roles/writer\.md: no task index"):
                    compose.index(self.root, "writer")
                self.fails("roles/writer.md: no task index")

    def test_a_task_path_other_than_its_file(self):
        line = "- `long-note` (`<tasks>/long-note.md`): "
        for path, wrong in (("`<tasks>/long.md`", "'<tasks>/long.md'"),
                            ("`tasks/long-note.md`", "'tasks/long-note.md'"),
                            ("`<tasks>/short-note.md`", "'<tasks>/short-note.md'"), (None, "''")):
            with self.subTest(path=path):
                new = "- `long-note`: " if path is None else f"- `long-note` ({path}): "
                self.write({"team/roles/writer.md": WRITER.replace(line, new)})
                msg = f"roles/writer.md: 'long-note' has path {wrong}, not '<tasks>/long-note.md'"
                with self.assertRaisesRegex(compose.ConfigError, re.escape(msg)):
                    compose.index(self.root, "writer")
                self.fails(msg, task="short-note")

    def test_a_default_unlisted_with_a_wrong_path_or_missing(self):
        intro = INTRO.format("short-note")
        for new, msg in ((INTRO.format("essay"), "the default 'essay' is not a listed task"),
                         (intro.replace("`<tasks>/short-note.md`", "`<tasks>/note.md`"),
                          "'short-note' has path '<tasks>/note.md', not '<tasks>/short-note.md'"),
                         ("Pick the task below that fits the input.\n\n", "## Tasks opens without its default"),
                         ("", "## Tasks opens without its default")):
            with self.subTest(new=new):
                self.write({"team/roles/writer.md": WRITER.replace(intro, new)})
                with self.assertRaisesRegex(compose.ConfigError, re.escape(f"roles/writer.md: {msg}")):
                    compose.index(self.root, "writer")

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

    def test_missing_output_value(self):
        self.config(CONFIG.replace('repo = "o/docs"\n', ""))
        self.fails("<out-repo> is used in destinations/github.md but Parameters doesn't define it "
                   "(output.repo is not set)", "editor")

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
            self.assertEqual(heads, ["# Guide", "# Parameters", "# Principles", "# Writer", "## Tasks", "## Style",
                                     "# Template: `templates/note.md`", "# Output", "## Destination", "# Input"], task)

    def test_the_guide_names_the_task_only_when_one_is_named(self):
        prompt, _ = self.compose()
        guide = prompt.split("# Principles", 1)[0]
        self.assertIn("You are Writer. Task: pick one; files in `<tasks>`.", guide)
        self.assertIn("On conflict: [Principles](#principles) > [Writer rules](#writer) > your task's.", guide)
        self.assertEqual(prompt.count("On conflict:"), 1)
        prompt, _ = self.compose(task="long-note")
        self.assertIn("You are Writer. Task: `long-note` (`<tasks>/long-note.md`); files in",
                      prompt.split("# Principles", 1)[0])

    def test_no_task_text_reaches_the_prompt(self):
        for task in (None, "short-note", "long-note"):
            prompt, _ = self.compose(task=task)
            for text in ("Write a note.", "Write a long note.", "# Short Note", "# Long Note"):
                self.assertNotIn(text, prompt, task)

    def test_missing_guide(self):
        os.remove(os.path.join(self.root, "team", "guide.md"))
        self.fails("missing file guide.md")

    def test_destination_keeps_its_names(self):
        prompt, _ = self.compose("editor")
        self.assertIn("Push to `<out-repo>` on `<out-branch>`.", prompt)

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
            self.assertTrue(prompt.endswith(f"\n# Input\n\n{given}\n"), given)
            self.assertEqual(parameters(prompt)["<Workdir>"], f"`{os.path.abspath('wd')}`")
            self.assertNotIn("question", prompt)

    def test_input_last_with_free_text(self):
        prompt, _ = self.compose(input="Compare cmux and tmux.\nKeep it short.")
        self.assertTrue(prompt.endswith("Keep it local.\n\n# Input\n\nCompare cmux and tmux.\nKeep it short.\n"))

    def test_an_input_with_headings_and_rules_is_verbatim_after_the_one_input_heading(self):
        given = "# Heading\n\nText.\n\n---\n\n# Input\n\nMore."
        prompt, _ = self.compose(input=f"\n{given}\n\n")
        self.assertEqual(prompt.split("\n# Input\n\n", 1)[1], f"{given}\n")
        self.assertEqual(prompt.count("\n---\n"), 1)

    def test_handover_closes_the_output_section(self):
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, PARAMS, client=Plain("Say it back."))
        body = prompt.rsplit("\n# Input\n", 1)[0]
        self.assertTrue(body.rstrip().endswith("Keep it local.\n\n## Return\n\nSay it back."))

    def test_the_report_command_quotes_its_paths(self):
        self.assertEqual(compose.report_command("/s", PARAMS), "python3 /s/report.py --to /w/run.jsonl")
        spaced = replace(PARAMS, workdir="/my w")
        self.assertEqual(compose.report_command("/s s", spaced), "python3 '/s s/report.py' --to '/my w/run.jsonl'")
        self.assertEqual(spaced.channel, "/my w/run.jsonl")

    def test_texts_keep_the_names_and_parameters_give_the_clients_paths(self):
        self.write({"team/principles.md": "# Principles\n\nRun `<scripts>/x.py`; tasks in `<tasks>`.\n",
                    "team/roles/writer.md": WRITER + "\nKnow `<methods>` and `<tasks>/short-note.md`.\n"})
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, PARAMS, client=Plain(methods="/m", tasks="/t"))
        self.assertIn("Run `<scripts>/x.py`; tasks in `<tasks>`.", prompt)
        self.assertIn("Know `<methods>` and `<tasks>/short-note.md`.", prompt)
        self.assertEqual(parameters(prompt), {"<Workdir>": "`/w`", "<scripts>": f"`{self.root}/src`", "<tasks>": "`/t`",
                                              "<methods>": "`/m`"})

    def test_load_then_render_with_another_output(self):
        run = compose.load_run(self.root, "editor")
        prompt = compose.render(self.root, replace(run, output={"type": "local"}), client=Plain())
        self.assertIn("Keep it local.", prompt)
        self.assertNotIn("Push to", prompt)

    def test_no_params_no_workdir_line_or_input(self):
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, client=Plain())
        self.assertEqual(list(parameters(prompt)), ["<scripts>", "<tasks>"])
        self.assertNotIn("# Input", prompt)
        self.assertTrue(prompt.endswith("Keep it local.\n"))
        self.assertEqual((run.role_title, run.task), ("Writer", ""))

    def test_no_params_the_clients_inline_workdir_and_input(self):
        run = compose.load_run(self.root, "writer")
        client = Plain(inline_workdir="the dir `mktemp -d` prints", inline_input="Given with this prompt.")
        prompt = compose.render(self.root, run, client=client)
        self.assertEqual(list(parameters(prompt).items())[0], ("<Workdir>", "the dir `mktemp -d` prints"))
        self.assertTrue(prompt.endswith("Keep it local.\n\n# Input\n\nGiven with this prompt.\n"))

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
        self.assertTrue(prompt.endswith("\n# Input\n\nAnswers:\n- Use B.\n"))

    def test_leftover_placeholder_in_a_rule_file(self):
        self.write({"team/roles/writer.md": WRITER + "\nUse {{tool}}.\n"})
        self.fails("unfilled placeholder {{tool}}")

    def test_a_run_value_placeholder_is_gone(self):
        for name in ("scripts", "tasks", "methods", "gate", "repo", "branch", "dir", "host", "report"):
            with self.subTest(name=name):
                self.write({"team/roles/writer.md": WRITER + f"\nUse `{{{{{name}}}}}`.\n"})
                self.fails(f"unfilled placeholder {{{{{name}}}}} in roles/writer.md")


class Parameters(Fake):
    GITHUB = FILES["output/destinations/github.md"]

    def test_with_params_and_no_handover_the_workdir_scripts_and_tasks(self):
        self.assertEqual(list(parameters(self.compose()[0]).items()), [
            ("<Workdir>", "`/w`"), ("<scripts>", f"`{self.root}/src`"), ("<tasks>", f"`{self.root}/team/tasks`")])

    def test_the_rule_opens_the_section(self):
        self.assertIn(f"\n# Parameters\n\n{compose.RULE}\n\n- `<Workdir>`: `/w`\n", self.compose()[0])

    def test_report_only_with_params_and_a_handover_using_it_in_code(self):
        run = compose.load_run(self.root, "writer")
        for handover in ("Run `report outcome <file>`.", "Report through `report`."):
            prompt = compose.render(self.root, run, PARAMS, client=Plain(handover))
            self.assertEqual(list(parameters(prompt)), ["<Workdir>", "<scripts>", "<tasks>", "report"], handover)
            self.assertEqual(parameters(prompt)["report"], "`python3 <scripts>/report.py --to <Workdir>/run.jsonl`")
            self.assertIn(f"## Return\n\n{handover}\n", prompt)
        for handover in ("Say it back.", "Send your report; `report.py` gets it."):
            for params in (PARAMS, None):
                prompt = compose.render(self.root, run, params, client=Plain(handover))
                self.assertNotIn("report", parameters(prompt), (handover, params))

    def test_a_handover_using_report_needs_params(self):
        run = compose.load_run(self.root, "writer")
        with self.assertRaises(compose.ConfigError) as cm:
            compose.render(self.root, run, client=Plain("Run `report progress x`."))
        self.assertTrue(str(cm.exception).startswith("report is used in handover but Parameters doesn't define it ("))

    def test_workdir_used_needs_params_or_the_clients_inline_workdir(self):
        self.write({"team/principles.md": "# Principles\n\nTemp files go in `<Workdir>/tmp/`.\n"})
        run = compose.load_run(self.root, "writer")
        with self.assertRaises(compose.ConfigError) as cm:
            compose.render(self.root, run, client=Plain())
        self.assertTrue(str(cm.exception).startswith(
            "<Workdir> is used in principles.md but Parameters doesn't define it ("))
        prompt = compose.render(self.root, run, client=Plain(inline_workdir="a temp dir"))
        self.assertEqual(parameters(prompt)["<Workdir>"], "a temp dir")

    def test_a_task_file_using_a_name_its_run_cannot_define(self):
        self.write({"team/tasks/long-note.md": "# Long Note\n\nOpen `<out-repo>`.\n"})
        self.fails("<out-repo> is used in tasks/long-note.md but Parameters doesn't define it (output.repo is not set)")

    def test_methods_used_by_name_in_the_role(self):
        self.write({"team/roles/writer.md": WRITER + "\nKnow `<methods>`.\n"})
        names = parameters(self.compose()[0])
        self.assertEqual(list(names), ["<Workdir>", "<scripts>", "<tasks>", "<methods>"])
        self.assertEqual(names["<methods>"], f"`{self.root}/team/methods`")

    def test_gate_used_by_name_in_an_indexed_task_file_only(self):
        self.write({"team/tasks/short-note.md": "# Short Note\n\nRun `<gate>` first.\n"})
        self.assertEqual(parameters(self.compose()[0])["<gate>"], "`none`")
        self.assertEqual(parameters(self.compose(layers=[{"gate": "make gate"}])[0])["<gate>"], "`make gate`")
        self.assertNotIn("<gate>", parameters(self.compose("editor")[0]))

    def test_gate_used_in_a_method_file_a_text_names(self):
        self.write({"team/tasks/long-note.md": "# Long Note\n\nFollow `<methods>/m.md`, `<methods>/gone.md`.\n",
                    "team/methods/m.md": "# M\n\nRun `<gate>`.\n", "team/methods/other.md": "# O\n\n`<out-dir>`\n"})
        names = parameters(self.compose()[0])
        self.assertEqual(list(names), ["<Workdir>", "<scripts>", "<tasks>", "<methods>", "<gate>"])
        self.write({"team/tasks/long-note.md": "# Long Note\n\nFollow `{{methods}}/m.md`.\n"})
        self.assertNotIn("<gate>", parameters(self.compose()[0]))

    def test_github_output_its_used_keys(self):
        names = parameters(self.compose("editor")[0])
        self.assertEqual(list(names.items())[3:], [("<out-repo>", "`o/docs`"), ("<out-branch>", "`main`")])

    def test_an_unset_host_is_github_com(self):
        self.write({"output/destinations/github.md": self.GITHUB + "Clone `<out-host>/<out-repo>`.\n"})
        self.assertEqual(parameters(self.compose("editor")[0])["<out-host>"], "`github.com`")
        layer = {"roles": {"editor": {"output": {"type": "github", "repo": "o/d", "branch": "b", "host": "ghe.x"}}}}
        self.assertEqual(parameters(self.compose("editor", layers=[layer])[0])["<out-host>"], "`ghe.x`")

    def test_another_used_output_key_unset(self):
        self.write({"output/destinations/github.md": self.GITHUB + "Into `<out-dir>`.\n"})
        self.fails("output.dir is not set", "editor")

    def test_a_value_holding_backticks_is_inline_code(self):
        run = compose.load_run(self.root, "editor")
        prompt = compose.render(self.root, replace(run, output={**run.output, "repo": "o/`d``x"}), PARAMS,
                                client=Plain())
        self.assertEqual(parameters(prompt)["<out-repo>"], "``` o/`d``x ```")


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

    def test_a_destination_names_it_under_the_workdirs_name(self):
        self.write(self.DEST)
        prompt, _ = self.compose()
        self.assertIn("Keep it local.\nWrite it to `<Workdir>/out.md`.\n", prompt)
        prompt, _ = self.compose(out="/elsewhere/out.md")
        self.assertIn("Write it to `<Workdir>/tmp/deliverable.md`.", prompt)
        wd = os.path.join(os.path.realpath(self.root), "wd")
        os.makedirs(wd)
        os.symlink(wd, os.path.join(self.root, "via"))
        prompt, _ = self.compose(workdir=os.path.join(self.root, "via"), out=os.path.join(wd, "sub", "out.md"))
        self.assertIn("Write it to `<Workdir>/sub/out.md`.", prompt)

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


def parameter_names(role, task=None, **changes):
    """The # Parameters names of the claude client's prompt for `role`, its run changed by `changes`."""
    run = replace(compose.load_run(CORE, role, task), **changes)
    return list(parameters(compose.render(CORE, run, PARAMS, client=clients.get("claude", CORE))))


TICKET = re.compile(r"TASK-\d+")   # an internal ticket id
# The format examples shipped core text may hold: core path → its examples, each allowed once.
TICKET_EXAMPLES = {"team/principles.md": ["(e.g. `TASK-142`)"],
                   "skills/tmux/SKILL.md": ["`pm-TASK-9-…`", "`pm-TASK-10-…`"]}


def tickets(root):
    """{core path: its ticket ids beyond TICKET_EXAMPLES} of root's shipped text: team/, output/, skills/ (tests aside)
    and config.toml."""
    paths = [os.path.join(root, compose.CONFIG)]
    for d in (compose.TEXT, compose.OUTPUT, "skills"):
        for top, dirs, files in os.walk(os.path.join(root, d)):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            paths += [os.path.join(top, f) for f in files if not f.endswith("_test.py")]
    found = {}
    for path in paths:
        rel = os.path.relpath(path, root)
        with open(path, errors="replace") as f:
            text = f.read()
        for example in TICKET_EXAMPLES.get(rel, ()):
            text = text.replace(example, "", 1)
        if ids := TICKET.findall(text):
            found[rel] = ids
    return found


GENERIC_NAME = re.compile(r"(?<![/\w])(agent-pm|autopilot):[\w-]+|\bthe [\w-]+ skill\b")


def generic_names(text):
    return [line for line in text.splitlines() if GENERIC_NAME.search(line)]


LANGUAGE_RULE = "headings and fixed labels included"
REPO_ARGS = {"worktree": "--dir <Workdir>/src --branch <branch> [--name <checkout>] <repo>",
             "status": "--dir <Workdir>/src --branch <branch> [--name <checkout>] [--base <Base:>] <repo>"}
CHECKOUT_RULE = ("- **Checkout**: `[--name <checkout>]` in a command → `--name <checkout>`, `<checkout>` the input's "
                 "`Checkout:`; no `Checkout:` → drop it.")
PICK = ("**Your task**: pick it from your charter's Tasks section as its opening sentence says; a task the input names "
        "wins. Read only that task's file, `<tasks>/<task>.md`, and follow its steps in order.")
COMMANDS_RULE = ("- **Commands**: run each command this prompt gives exactly, written as Parameters says, as its own "
                 "command (no `cd`, pipe, redirect or `&&`).")
RETURN = """Report through `report`, never in a reply:

- **Progress:** at each `[agent-pm-progress:<name>] …` line in your steps, before calling the next tool, run `report progress <name> <your report>`, e.g. `report progress start <what that line asks you to report>`.
- **Outcome:** as your last action, after everything else is done and any background work you started has finished, run `report outcome --status <done|needs_input|failed> --title <one line> --summary <text> [--question <q>]... [--url <url>] [--file <path>]... [--deliverable <file>]`: `--question` and `--file` once per item; `--deliverable` a file holding the deliverable, which is read as its text. If it fails, fix it and run it again."""


def claude_prompt(role, task=None):
    """The claude client's prompt for `role` with RUN, and its run."""
    claude = clients.get("claude", CORE)
    run = compose.load_run(CORE, role, task, layers=[claude.config])
    return compose.render(CORE, run, RUN, client=claude), run


class GenericNames(unittest.TestCase):
    def test_a_plugin_skill_without_its_slash(self):
        text = "run autopilot:build\nthen `agent-pm:tmux`\nor x/agent-pm:act-as"
        self.assertEqual(generic_names(text), ["run autopilot:build", "then `agent-pm:tmux`"])

    def test_the_x_skill(self):
        text = "the act-as skill\nthe tmux skill's workers\nthe skill"
        self.assertEqual(generic_names(text), ["the act-as skill", "the tmux skill's workers"])

    def test_slash_commands_and_workflow_names_pass(self):
        self.assertEqual(generic_names("/autopilot:build, `/agent-pm:tmux`'s workers, /deep-research"), [])


class Tickets(unittest.TestCase):
    def test_an_id_beyond_the_examples_is_caught(self):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in {"team/roles/x.md": "when asked, e.g. a wording tweak like TASK-300; light.",
                              "team/principles.md": "the id (e.g. `TASK-142`); twice (e.g. `TASK-142`)",
                              "output/output.md": "the id (e.g. `TASK-142`)",
                              "skills/tmux/SKILL.md": "(`pm-TASK-9-…` before `pm-TASK-10-…`)",
                              "skills/tmux/scripts/x_test.py": "TASK-1", "skills/tmux/scripts/x.py": "# TASK-2",
                              "config.toml": "# like TASK-7"}.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w") as f:
                    f.write(text)
            self.assertEqual(tickets(root), {"team/roles/x.md": ["TASK-300"], "team/principles.md": ["TASK-142"],
                                             "output/output.md": ["TASK-142"], "skills/tmux/scripts/x.py": ["TASK-2"],
                                             "config.toml": ["TASK-7"]})


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
            self.assertTrue(text.split("\n## ")[1].startswith(f"Tasks\n\n{INTRO.format(default)}- `"), role)
            self.assertIn(default, compose.index(CORE, role), role)
            for t, line in compose.index(CORE, role).items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", text, role)
                self.assertRegex(line, r"; (light|heavy)\.$", role)
        self.assertEqual(sorted(t for _, t in ALL), sorted(f.removesuffix(".md") for f in os.listdir(TASKS)))

    def test_shipped_text_has_no_ticket_id_but_the_format_examples(self):
        self.assertEqual(tickets(CORE), {})
        for rel, examples in TICKET_EXAMPLES.items():
            with open(os.path.join(CORE, rel)) as f:
                text = f.read()
            for example in examples:
                self.assertEqual(text.count(example), 1, (rel, example))

    def test_engineers_tasks_name_their_skill_by_slash_command(self):
        tasks = compose.index(CORE, "engineer")
        for t in ("build", "light-build"):
            self.assertIn(f" with `/autopilot:{t}` (", tasks[t])
            self.assertIn(f"`/autopilot:{t}`", task_text(t))
            self.assertNotRegex(task_text(t), r"(?<!/)autopilot:")

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
            self.assertEqual(with_params.count("`<Workdir>/out.md`"), 1, dest)
            self.assertIn("Write the deliverable to `<Workdir>/out.md` and return that file as the outcome's "
                          "`deliverable`", with_params, dest)
            without = compose.render(CORE, run, client=Plain(inline_workdir="a temp dir"))
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
            self.assertIn("`<tasks>/<task>.md`", g, task)
            self.assertIn(f"[{run.role_title} rules](#{compose.anchor(run.role_title)}) > your task file's rules", g)
            self.assertEqual(prompt.count("On conflict:"), 1, task)
            self.assertIn("On conflict:", g, task)

    def test_every_guide_lists_parameters_first_and_input_last(self):
        for role, task in [(r, None) for r in ROLES] + ALL:
            g = guide(composed(role, task)[0])
            items = re.findall(r"^- \*\*.+$", g, re.M)
            self.assertEqual(items[0], "- **Parameters**: the value of each name this prompt and your task's file use.", (role, task))
            self.assertEqual(items[-1], "- **Input**: the last section; everything after its heading is the input text, "
                                        "verbatim (it may contain `#` or `---`).", (role, task))
            self.assertNotIn("final `---`", g, (role, task))
            self.assertNotIn("your Workdir and the Input text", g, (role, task))

    def test_the_guide_names_a_given_task_else_the_run_picks_it(self):
        named = guide(composed("researcher", "light-research")[0])
        self.assertIn("**Your task**: `light-research` (`<tasks>/light-research.md`). Read only that task's file",
                      named)
        self.assertNotIn("pick it from", named)
        picked = guide(composed("researcher")[0])
        self.assertIn(PICK, picked)
        self.assertNotIn("unsure → the default it names", picked)
        self.assertIn("Your start progress report names the task and why.", picked)
        self.assertNotIn("light-research", picked)

    def test_a_prompt_has_its_roles_index_and_no_task_steps(self):
        for role, task in [(r, None) for r in ROLES] + ALL:
            prompt, _ = composed(role, task)
            for t, line in compose.index(CORE, role).items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", prompt, (role, task))
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

    def test_each_roles_parameters_follow_what_its_texts_use(self):
        base, out = ["<Workdir>", "<scripts>", "<tasks>"], ["<out-repo>", "<out-branch>", "<out-dir>", "<out-host>"]
        want = {"engineer": [*base, "report"], "pm": [*base, *out, "report"],
                "researcher": [*base, "<methods>", "<gate>", *out, "report"]}
        for role, task in [(r, None) for r in want] + [(r, t) for r, t in ALL if r in want]:
            self.assertEqual(parameter_names(role, task), want[role], (role, task))

    def test_a_local_output_leaves_the_researcher_no_out_parameters(self):
        self.assertEqual(parameter_names("researcher", output={"type": "local"}),
                         ["<Workdir>", "<scripts>", "<tasks>", "<methods>", "<gate>", "report"])

    def test_deep_research_falls_back_to_both_method_files_the_researcher_names(self):
        prompt, _ = composed("researcher")
        self.assertEqual(parameters(prompt)["<methods>"], f"`{os.path.join(CORE, 'team', 'methods')}`")
        for name in METHOD_NAMES:
            self.assertTrue(os.path.isfile(os.path.join(CORE, "team", "methods", f"{name}.md")), name)
            self.assertIn(f"`<methods>/{name}.md`", prompt)
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

    def test_names_written_as_their_values_each_pre_approved_command_starts_a_command(self):
        scripts = os.path.join(CORE, "src")
        report = compose.report_command(scripts, RUN)
        for role in ROLES:
            prompt, run = claude_prompt(role)
            prompt = expanded(prompt)
            for cmd in run.commands:
                cmd = compose.fill(cmd, {"scripts": scripts, "workdir": RUN.workdir}, role).removesuffix(" *")
                self.assertIn(f"`{cmd}", prompt, role)
            self.assertIn(f"Report through `{report}`, never in a reply", prompt, role)
            self.assertIn(f"`{report} progress <name> <your report>`", prompt, role)
            self.assertIn(f"`{report} outcome --status", prompt, role)

    def test_a_claude_prompt_defines_each_run_value_once_under_the_rule(self):
        for role, task in [(r, None) for r in ROLES] + ALL:
            prompt, _ = claude_prompt(role, task)
            for value in ("report.py", RUN.workdir, os.path.join(CORE, "src"), compose.RULE):
                self.assertEqual(prompt.count(value), 1, (role, task, value))
            self.assertEqual(rule(prompt, "Commands"), [COMMANDS_RULE], (role, task))

    def test_a_claude_prompts_return_is_the_report_text(self):
        for role in ROLES:
            self.assertIn(f"\n## Return\n\n{RETURN}\n\n# Input\n", claude_prompt(role)[0], role)

    def test_a_skill_prompt_has_the_rule_once_and_no_report(self):
        skill = clients.get("skill", CORE)
        for role in ROLES:
            prompt = compose.render(CORE, compose.load_run(CORE, role, layers=[skill.config]), client=skill)
            self.assertEqual(prompt.count(compose.RULE), 1, role)
            self.assertNotIn(compose.REPORT, parameters(prompt), role)

    def test_text_holds_only_the_placeholders_compose_fills_and_no_client_the_report_one(self):
        inline = {"role", "role_anchor", "task", "language", "deliverable"}
        for d in ("team", "output"):
            for path in glob.glob(os.path.join(CORE, d, "**", "*.md"), recursive=True):
                with open(path) as f:
                    self.assertLessEqual(set(re.findall(r"\{\{(\w+)", f.read())), inline, path)
        for path in glob.glob(os.path.join(CORE, "src", "clients", "*.py")):
            with open(path) as f:
                self.assertNotIn("{{report}}", f.read(), path)

    def test_every_repo_py_command_takes_the_inputs_checkout(self):
        found = {}
        for role in ROLES:
            prompt, _ = composed(role)
            text = prompt + "".join(task_text(t) for t in compose.index(CORE, role))
            cmds = re.findall(r"`python3 \S+/repo\.py (worktree|status) ([^`]*)`", text)
            found[role] = [cmd for cmd, _ in cmds]
            for cmd, args in cmds:
                self.assertEqual(args, REPO_ARGS[cmd], role)
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

    def test_builds_merge_the_base_branch_before_autopilot_at_resume_and_before_the_pr(self):
        prompt, _ = composed("engineer")
        self.assertIn("JSON `base` → `<base>`: the PR's base branch, else `Base:`, else `<default>`.", section(prompt, "Repo"))
        merge = section(prompt, "Merge")
        for literal in ("Engineer › Repo step 1", "`git -C <worktree> status`", "a merge in progress",
                        "`git -C <worktree> merge --no-edit origin/<base>`", "`git -C <worktree> merge --abort`"):
            self.assertIn(literal, merge)
        for where in ("Autopilot", "Finish", "Resume"):
            self.assertIn("Engineer › Merge", section(prompt, where), where)
        self.assertIn("`origin/<base>`", section(prompt, "Autopilot"))
        for literal in ("--head <branch> --base <base> --title", "/compare/<base>...<branch>?expand=1"):
            self.assertIn(literal, prompt)
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
            if "<methods>" in text:
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

    def test_skills_and_config_comments_name_skills_by_slash_command(self):
        for path in glob.glob(os.path.join(CORE, "skills", "*", "SKILL.md")):
            with open(path) as f:
                self.assertEqual(generic_names(f.read()), [], path)
        with open(os.path.join(CORE, compose.CONFIG)) as f:
            self.assertEqual(generic_names("\n".join(re.findall(r"#.*", f.read()))), [])

    def test_the_tmux_skills_role_listing_gives_descriptions_without_paths(self):
        with open(os.path.join(CORE, "skills", "tmux", "SKILL.md")) as f:
            cmd = re.search(r"`(python3 -c '.*?' \$\{CLAUDE_SKILL_DIR\}/\.\./\.\./src)`", f.read()).group(1)
        code, src = shlex.split(cmd.replace("${CLAUDE_SKILL_DIR}", os.path.join(CORE, "skills", "tmux")))[2:]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", src]), mock.patch.object(sys, "path", list(sys.path)), \
                redirect_stdout(out):
            exec(code, {})
        self.assertIn("\n  build: a PRD into a pull request with `/autopilot:build` (", out.getvalue())
        self.assertNotIn("<tasks>", out.getvalue())

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
            prompt = composed("researcher", task)[0]
            self.assertIn("the gate is `<gate>`", prompt, task)
            self.assertEqual(parameters(prompt)["<gate>"], "`none`", task)
            run = compose.load_run(CORE, "researcher", task, layers=[{"gate": gate}])
            self.assertEqual(parameters(compose.render(CORE, run, PARAMS, client=Plain()))["<gate>"], f"`{gate}`", task)

    def test_github_host_defaults_and_overrides(self):
        prompt, run = composed("researcher")
        self.assertIn("gh repo clone <out-host>/<out-repo> <pub>", prompt)
        self.assertIn("https://<out-host>/<out-repo>/blob/<out-branch>/<path>", prompt)
        self.assertEqual(parameters(prompt)["<out-host>"], "`github.com`")
        prompt = compose.render(CORE, replace(run, output={**run.output, "host": "ghe.example.com"}), PARAMS,
                                client=Plain())
        self.assertEqual(parameters(prompt)["<out-host>"], "`ghe.example.com`")


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
