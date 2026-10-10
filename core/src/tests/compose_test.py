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
import drive  # noqa: E402

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
    def test_role_overrides_global_output_is_replaced_whole_and_lists_default_empty(self):
        for role, tier, effort, output in (("editor", 1, "high", {"type": "github", "repo": "o/docs", "branch": "main"}),
                                           ("writer", 2, "medium", {"type": "local"})):
            with self.subTest(role=role):
                _, run = self.compose(role)
                self.assertEqual((run.tier, run.effort, run.output), (tier, effort, output))
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

    def test_gate_defaults_to_none_and_a_layers_or_config_tomls_gate_is_its_value_verbatim(self):
        self.write({"team/roles/writer.md": WRITER + "\nGate: `<gate>`.\n"})
        prompt, run = self.compose()
        self.assertEqual(run.gate, "")
        self.assertEqual(parameters(prompt)["<gate>"], "`none`")
        gate = "python3 /u/usage.py --below 80 *"
        prompt, run = self.compose(layers=[{"roles": {"writer": {"gate": gate}}}])
        self.assertEqual(run.gate, gate)
        self.assertEqual(parameters(prompt)["<gate>"], f"`{gate}`")
        self.assertIn("Gate: `<gate>`.", prompt)
        self.config(CONFIG.replace('effort = "medium"', 'effort = "medium"\ngate = "make gate"'))
        self.assertEqual(self.compose()[1].gate, "make gate")

    def test_language_renders_its_line_once_at_any_level_and_no_language_drops_it(self):
        self.write({"team/principles.md": FILES["team/principles.md"] + "\n- Write well.\n- Reports are in {{language}}.\n- Be brief.\n"})
        prompt, run = self.compose("editor")
        self.assertEqual(run.language, "")
        self.assertIn("- Write well.\n- Be brief.\n", prompt)
        self.assertNotIn("Reports are in", prompt)
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
        with self.assertRaisesRegex(compose.ConfigError, "roles/writer.md: lists 'long-note' without tasks/long-note.md"):
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
    TIER = "tier must be an integer 1–4"

    def test_an_invalid_config_toml_fails_whatever_the_layers(self):
        for old, new, msg, role, layers in (
                ("tier = 1", "tier = 9", self.TIER, "editor", [{"tier": 2}]),
                ("tier = 1", "tier = 5", self.TIER, "editor", ()),
                ('effort = "medium"', 'effort = "medium"\nmodel = "opus"', "unknown key 'model' in roles.writer",
                 "writer", ()),
                ('effort = "medium"', 'effort = "ultra"', "effort must be one of", "writer", ()),
                ('type = "github"', 'type = "s3"', "no destination 's3'", "editor", ()),
                ('repo = "o/docs"\n', "", "<out-repo> is used in destinations/github.md but Parameters doesn't define "
                 "it (output.repo is not set)", "editor", ()),
                ('templates = ["note"]', 'templates = ["memo"]', "missing file templates/memo.md", "writer", ())):
            with self.subTest(new=new):
                self.config(CONFIG.replace(old, new))
                self.fails(msg, role, layers=layers)

    def test_the_local_file_is_checked_with_config_toml(self):
        self.local('[roles.editor]\nmodel = "opus"\n')
        self.fails("unknown key 'model' in roles.editor", "editor")

    def test_config_errors_come_before_missing_rule_files(self):
        self.config(CONFIG.replace("tier = 1", "tier = 9"))
        os.remove(os.path.join(self.root, "team", "tasks", "long-note.md"))
        os.remove(os.path.join(self.root, "team", "roles", "editor.md"))
        self.fails(self.TIER, "editor")

    def test_a_missing_rule_file(self):
        for rel, msg, role in (("roles/editor.md", "roles.editor: no charter roles/editor.md in core or team_dirs", "editor"),
                               ("guide.md", "missing file guide.md", "writer")):
            with self.subTest(rel=rel):
                self.write(FILES)
                os.remove(os.path.join(self.root, "team", rel))
                self.fails(msg, role)

    def test_an_invalid_layer_value(self):
        self.assertLessEqual({"language", "show"}, set(compose.RUN_KEYS))
        gate, language, show = ("gate must be one line of shell command without backticks",
                                "language must be one line of text", "show must be one line of shell command")
        for layer, msg in (({"tier": 9}, self.TIER), ({"gate": "a\nb"}, gate), ({"gate": "echo `id`"}, gate),
                           ({"gate": 7}, gate), ({"language": "a\nb"}, language), ({"language": 7}, language),
                           ({"show": "a\nb"}, show), ({"show": 7}, show)):
            with self.subTest(layer=layer):
                self.fails(msg, "editor", layers=[layer])

    def test_replace_is_checked(self):
        _, run = self.compose("editor")
        for change in ({"effort": "ultra"}, {"gate": "a\nb"}):
            with self.subTest(change=change), self.assertRaises(compose.ConfigError):
                replace(run, **change)

    def test_show_is_unset_by_default_and_a_layer_sets_it_at_any_level(self):
        self.assertIsNone(self.compose("editor")[1].show)
        _, run = self.compose("editor", layers=[{"show": ""}])
        self.assertEqual(run.show, "")
        _, run = self.compose("editor", layers=[{"show": "open-it {{session}}"}])
        self.assertEqual(run.show, "open-it {{session}}")
        _, run = self.compose("editor", layers=[{"roles": {"editor": {"show": "x {{session}}"}}}])
        self.assertEqual(run.show, "x {{session}}")

    def test_new_params_get_a_sid_and_a_resume_needs_one(self):
        self.assertRegex(compose.RunParams(input="x", out="o", workdir="w").sid, r"^[0-9a-f-]{36}$")
        with self.assertRaises(compose.ConfigError):
            compose.RunParams(input="x", out="o", workdir="w", resume=True)

    def test_a_prefix_is_a_tmux_session_name(self):
        for prefix in ("bad name", "", "a.b"):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(compose.ConfigError, "prefix"):
                compose.RunParams(input="x", out="o", workdir="w", prefix=prefix)
        self.assertEqual(compose.RunParams(input="x", out="o", workdir="w", prefix="A_b-0").prefix, "A_b-0")

    def test_unknown_role_or_task_or_another_roles(self):
        for role, task, msg in (("nobody", None, "unknown role 'nobody'"),
                                ("writer", "essay", "task 'essay' is not one of writer's tasks (short-note, long-note)"),
                                ("editor", "short-note", "task 'short-note' is not one of editor's tasks (long-note)")):
            with self.subTest(role=role, task=task):
                self.fails(msg, role, task)


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

    def test_destination_keeps_its_names(self):
        prompt, _ = self.compose("editor")
        self.assertIn("Push to `<out-repo>` on `<out-branch>`.", prompt)

    def test_template_is_fenced_and_the_fence_outgrows_its_backticks(self):
        for template, want in (("# Note: [Title]\n", "```markdown\n# Note: [Title]\n```"),
                               ("```js\nx\n```\n", "````markdown\n```js\nx\n```\n````")):
            with self.subTest(template=template):
                self.write({"team/templates/note.md": template})
                self.assertIn(want, self.compose()[0])

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

    def test_no_params_no_workdir_line_or_input_but_the_clients_inline_ones(self):
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, client=Plain())
        self.assertEqual(list(parameters(prompt)), ["<scripts>", "<tasks>"])
        self.assertNotIn("# Input", prompt)
        self.assertTrue(prompt.endswith("Keep it local.\n"))
        self.assertEqual((run.role_title, run.task), ("Writer", ""))
        client = Plain(inline_workdir="the dir `mktemp -d` prints", inline_input="Given with this prompt.")
        prompt = compose.render(self.root, run, client=client)
        self.assertEqual(list(parameters(prompt).items())[0], ("<Workdir>", "the dir `mktemp -d` prints"))
        self.assertTrue(prompt.endswith("Keep it local.\n\n# Input\n\nGiven with this prompt.\n"))

    def test_resume_starts_with_resumed_run_names_no_task_and_its_input_adds_to_the_earlier_one(self):
        self.assertIn("Continue the task this session already picked or was given, following your task file's "
                      "`## Resume` section; never pick it again.", compose.RESUME)
        self.assertIn("The input below is current: it adds to this session's earlier input.", compose.RESUME)
        self.assertNotIn("re-read the input", compose.RESUME)
        prompt, _ = self.compose(resume=True, input="Answers:\n- Use B.")
        self.assertTrue(prompt.startswith(compose.RESUME + "# Guide"))
        self.assertIn("Task: pick one;", prompt)
        self.assertTrue(prompt.endswith("\n# Input\n\nAnswers:\n- Use B.\n"))
        prompt, _ = self.compose()
        self.assertTrue(prompt.startswith("# Guide"))

    def test_a_leftover_or_run_value_placeholder_in_a_rule_file(self):
        for name in ("tool", "scripts", "tasks", "methods", "gate", "repo", "branch", "dir", "host", "report"):
            with self.subTest(name=name):
                self.write({"team/roles/writer.md": WRITER + f"\nUse `{{{{{name}}}}}`.\n"})
                self.fails(f"unfilled placeholder {{{{{name}}}}} in roles/writer.md")


def own_text(prompt):
    """`prompt` up to and including its # Input heading line: the input text, always last, is dropped."""
    return prompt.split("\n# Input\n\n", 1)[0] + "\n# Input\n"


def heading_anchors(prompt):
    """The anchor of each heading of the prompt's own text, in order."""
    return [a for _, _, a, heading in compose.outline(own_text(prompt)) if heading]


POET = ("# Poet\n\nRhyme.\n\n## Tasks\n\n" + INTRO.format("verse") +
        "- `verse` (`<tasks>/verse.md`): a verse; always; light.\n")
STEPS = "\n## Steps\n\n1. **Report progress**:\n   [agent-pm-progress:start] the topic.\n\n## Resume\n\nGo on.\n"
VERSE = "# Verse\n" + STEPS
TINY = "# Tiny Note\n" + STEPS
EXT = "## Tasks\n\n- `tiny-note` (`<tasks>/tiny-note.md`): a tiny note; rarely; light.\n"
FRONT = "---\ncommands: [\"rm *\"]\n---\n"


class TeamDirs(Fake):
    """Team dirs under a hermetic HOME, passed to compose as a Team."""
    def setUp(self):
        super().setUp()
        self.home = hermetic.home(self)

    def local(self, text):
        with open(os.path.join(self.home, "core.local.toml"), "w") as f:
            f.write(text)

    def team_dir(self, files, name="team"):
        """Writes {rel: text} under <home>/<name> (files 0o644, dirs 0o755) and returns its real path."""
        top = os.path.realpath(os.path.join(self.home, name))
        for rel, text in files.items():
            path = os.path.join(top, rel)
            for d in (top, os.path.dirname(path)):
                os.makedirs(d, exist_ok=True)
                os.chmod(d, 0o755)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
        return top

    def team(self, files, name="team"):
        return compose.team(self.root, (self.team_dir(files, name),))

    def go(self, team, role="writer", task=None, layers=()):
        run = compose.load_run(self.root, role, task, layers=layers, team=team)
        return compose.render(self.root, run, PARAMS, client=Plain(), team=team), run

    def refused(self, team, msg, role="writer", task=None):
        with self.assertRaises(compose.ConfigError) as cm:
            self.go(team, role, task)
        self.assertIn(msg, str(cm.exception))
        return str(cm.exception)

    def path(self, rel, name="team"):
        return os.path.join(os.path.realpath(os.path.join(self.home, name)), rel)

    def cfg(self):
        return compose.repo.read_config(os.path.join(self.root, compose.CONFIG))

    # FR-1
    def test_team_dirs_values(self):
        self.assertEqual(compose.team_dirs(self.root), ())
        top = self.team_dir({"roles/poet.md": POET})
        self.local('team_dirs = ["~/.agent-pm/team"]\n')
        self.assertEqual(compose.team_dirs(self.root), (top,))
        for value, msg in (('"x"', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
                           ('["rel"]', "team_dirs: 'rel' is not an absolute or ~ path"),
                           ('[1]', "team_dirs: 1 is not an absolute or ~ path"),
                           ('["~nobody-here-x/t"]', "team_dirs: '~nobody-here-x/t' is not an absolute or ~ path"),
                           ('["~/missing"]', "team_dirs: ~/missing is not a directory"),
                           (f'["~/.agent-pm/team", "{top}"]', f"team_dirs: {top} is listed twice")):
            with self.subTest(value=value):
                self.local(f"team_dirs = {value}\n")
                with self.assertRaises(compose.ConfigError) as cm:
                    compose.team_dirs(self.root)
                self.assertEqual(str(cm.exception), msg)

    def test_team_dirs_in_config_toml_or_a_role_table(self):
        self.config('team_dirs = ["/x"]\n' + CONFIG)
        with self.assertRaisesRegex(compose.ConfigError,
                                    r"^team_dirs: set it in ~/\.agent-pm/core\.local\.toml, not config\.toml$"):
            compose.team_dirs(self.root)
        self.config(CONFIG)
        self.local('[roles.writer]\nteam_dirs = ["/x"]\n')
        self.fails("unknown key 'team_dirs' in roles.writer")
        self.local('team_dirs = []\n')
        self.assertEqual(compose.team_dirs(self.root), ())
        self.compose()

    def test_no_team_dir_is_found_from_the_cwd(self):
        cwd = os.path.join(self.home, "cwd")
        self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE}, "cwd/.agent-pm/team")
        here = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, here)
        self.assertEqual(compose.team_dirs(self.root), ())
        self.assertIsNone(compose.team(self.root, compose.team_dirs(self.root)).lookup("roles", "poet"))
        self.fails("unknown role 'poet'", "poet")

    # FR-2
    def test_a_new_role_without_a_table_takes_the_global_keys(self):
        team = self.team({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        prompt, run = self.go(team, "poet")
        self.assertEqual((run.tier, run.effort, run.output, run.templates, run.role_title),
                         (2, "high", {"type": "local"}, [], "Poet"))
        self.assertIn("\n# Poet\n\nRhyme.\n\n## Tasks\n\n" + INTRO.format("verse"), prompt)
        self.assertIn("- `verse` (`<tasks>/verse.md`): a verse; always; light.\n", prompt)
        self.assertEqual(compose.index(self.root, "poet", team), {"verse": "a verse; always; light."})
        self.assertEqual(self.go(team, "poet", "verse")[1].task, "verse")
        self.fails("unknown role 'poet'", "poet")

    # FR-3
    def test_an_extension_appends_task_lines_to_a_roles_index_in_team_dirs_order(self):
        team = self.team({"roles/writer.md": EXT, "tasks/tiny-note.md": TINY})
        self.assertEqual(list(compose.index(self.root, "writer", team)), ["short-note", "long-note", "tiny-note"])
        prompt, _ = self.go(team, task="tiny-note")
        self.assertIn(INTRO.format("short-note"), prompt)
        self.assertIn("- `long-note` (`<tasks>/long-note.md`): a long note; when asked; heavy.\n"
                      "- `tiny-note` (`<tasks>/tiny-note.md`): a tiny note; rarely; light.\n\n## Style\n", prompt)
        second = self.team_dir({"roles/writer.md": EXT.replace("tiny-note", "wee-note"),
                                "tasks/wee-note.md": TINY}, "team2")
        team = compose.team(self.root, (self.path(""), second))
        self.assertEqual(list(compose.index(self.root, "writer", team)),
                         ["short-note", "long-note", "tiny-note", "wee-note"])
        self.assertIn("a tiny note; rarely; light.\n- `wee-note` (`<tasks>/wee-note.md`)", self.go(team)[0])

    def test_a_task_listed_twice(self):
        team = self.team({"roles/writer.md": EXT.replace("tiny-note", "long-note")})
        self.refused(team, f"{self.path('roles/writer.md')}: lists 'long-note', already in writer's index")

    def test_an_extension_holds_only_task_lines(self):
        for text in (EXT + "\nMore words.\n", EXT + "\n## Style\n", EXT.replace("(`<tasks>/tiny-note.md`)", ""),
                     EXT.replace("<tasks>/tiny-note.md", "<tasks>/x.md"), "## Tasks\n"):
            with self.subTest(text=text):
                team = self.team({"roles/writer.md": text, "tasks/tiny-note.md": TINY})
                self.refused(team, f"{self.path('roles/writer.md')}: an extension holds only a ## Tasks section of "
                                   "- `<task>` (`<tasks>/<task>.md`): … lines")

    # FR-4
    def test_a_custom_role_runs_a_built_in_task_and_a_custom_template_keeps_its_heading(self):
        team = self.team({"roles/poet.md": POET.replace("verse", "long-note"), "templates/card.md": "# Card\n"})
        self.assertEqual(list(compose.index(self.root, "poet", team)), ["long-note"])
        self.go(team, "poet", "long-note")
        self.local('[roles.writer]\ntemplates = ["note", "card"]\n')
        prompt, _ = self.go(team)
        self.assertIn("# Template: `templates/note.md`\n", prompt)
        self.assertIn("# Template: `templates/card.md`\n\n```markdown\n# Card\n```\n", prompt)

    # FR-5
    def test_a_name_defined_twice_names_both_files(self):
        builtin = os.path.join(self.root, "team")
        for files, role, kind, name, first in (
                ({"roles/editor.md": "# Editor\n"}, "editor", "role", "editor", f"{builtin}/roles/editor.md"),
                ({"tasks/long-note.md": VERSE}, "writer", "task", "long-note", f"{builtin}/tasks/long-note.md"),
                ({"templates/note.md": "# N\n"}, "writer", "template", "note", f"{builtin}/templates/note.md")):
            with self.subTest(kind=kind):
                team = self.team(files, kind)
                rel = next(iter(files))
                self.refused(team, f"{self.path(rel, kind)}: {kind} {name!r} is also defined by {first}; "
                                   "team dir files only add", role)
                with self.assertRaisesRegex(compose.ConfigError, "is also defined by"):
                    team.check_all(self.cfg())
        a = self.team_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE}, "a")
        b = self.team_dir({"roles/poet.md": POET}, "b")
        team = compose.team(self.root, (a, b))
        self.refused(team, f"{b}/roles/poet.md: role 'poet' is also defined by {a}/roles/poet.md", "poet")

    def test_an_extension_of_an_undefined_role_and_a_misnamed_file_only_stop_list(self):
        team = self.team({"roles/ghost.md": EXT, "tasks/tiny-note.md": TINY, "tasks/Big_Note.md": TINY})
        self.go(team)
        self.refused(team, "unknown role 'ghost'", "ghost")
        with self.assertRaises(compose.ConfigError) as cm:
            team.check_all(self.cfg())
        self.assertEqual(str(cm.exception), f"{self.path('tasks/Big_Note.md')}: name 'Big_Note': want [a-z0-9-]+")
        os.remove(self.path("tasks/Big_Note.md"))
        team = compose.team(self.root, (self.path(""),))
        with self.assertRaises(compose.ConfigError) as cm:
            team.check_all(self.cfg())
        self.assertEqual(str(cm.exception), f"{self.path('roles/ghost.md')}: extends role 'ghost', which neither core "
                                            "nor a team dir defines")
        self.team_dir({"roles/ghost.md": POET.replace("Poet", "Ghost").replace("verse", "long-note")}, "two")
        both = compose.team(self.root, (self.path(""), self.path("", "two")))
        both.check_all(self.cfg())
        self.assertEqual(list(compose.index(self.root, "ghost", both)), ["long-note", "tiny-note"])

    def test_a_defect_in_another_roles_file_leaves_this_role_loading(self):
        team = self.team({"roles/poet.md": FRONT + POET, "tasks/verse.md": "no title\n", "roles/editor.md": "# E\n",
                          "templates/card.md": "{{x}}\n"})
        self.go(team)
        with self.assertRaises(compose.ConfigError):
            team.check_all(self.cfg())

    def test_roles_without_a_charter(self):
        self.local("[roles.ghost]\ntier = 3\n")
        team = self.team({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        self.go(team, "editor", "long-note")
        self.refused(team, "roles.ghost: no charter roles/ghost.md in core or team_dirs", "ghost")
        with self.assertRaisesRegex(compose.ConfigError, "^roles.ghost: no charter roles/ghost.md in core or team_dirs$"):
            compose.roles(self.root, team, self.cfg())
        self.local("")
        self.assertEqual(compose.roles(self.root, team, self.cfg()), ["writer", "editor", "poet"])

    # FR-6
    def test_front_matter_in_any_team_dir_file(self):
        self.local('[roles.writer]\ntemplates = ["note", "card"]\n')
        for files, rel, role in (({"roles/poet.md": POET, "tasks/verse.md": FRONT + VERSE}, "tasks/verse.md", "poet"),
                                 ({"roles/poet.md": FRONT + POET, "tasks/verse.md": VERSE}, "roles/poet.md", "poet"),
                                 ({"roles/writer.md": FRONT + EXT, "tasks/tiny-note.md": TINY}, "roles/writer.md",
                                  "writer"),
                                 ({"templates/card.md": FRONT + "# Card\n"}, "templates/card.md", "writer")):
            with self.subTest(rel=rel):
                team = self.team(files, rel.replace("/", "-"))
                self.refused(team, f"{self.path(rel, rel.replace('/', '-'))}: front matter (---) is not read: team dir "
                                   "files hold text only; run keys go in ~/.agent-pm/core.local.toml", role)

    # FR-7
    def test_each_defect_of_a_team_dir_file_names_it(self):
        task, charter = self.path("tasks/verse.md"), self.path("roles/poet.md")
        for verse, poet, msg in (
                ("Verse\n" + STEPS, POET, f"{task}: must start with a '# ' heading"),
                (VERSE, POET.replace("# Poet", "Poet"), f"{charter}: must start with a '# ' heading"),
                (VERSE + "{{x}}\n", POET, f"{task}: unfilled placeholder {{{{x}}}}"),
                (VERSE, POET + "{{role}}\n", f"{charter}: unfilled placeholder {{{{role}}}}"),
                (VERSE, POET + "\n[x](#nope)\n", f"{charter}: no such anchor: [x](#nope)"),
                (VERSE + "\n[x](#nope)\n", POET, f"{task}: no such anchor: [x](#nope)"),
                (VERSE.replace("[agent-pm-progress:start] the topic.", "the topic."), POET,
                 f"{task}: has 0 [agent-pm-progress:start] lines, not 1"),
                (VERSE + "\n[agent-pm-progress:start] again.\n", POET,
                 f"{task}: has 2 [agent-pm-progress:start] lines, not 1"),
                (VERSE.replace("## Resume", "## Later"), POET, f"{task}: has no ## Resume section"),
                (VERSE.replace("## Resume", "```\n## Resume\n```"), POET, f"{task}: has no ## Resume section"),
                (VERSE + "\nUse `<methods>/nope.md`.\n", POET,
                 f"{task}: names <methods>/nope.md, which core has no file for")):
            with self.subTest(msg=msg):
                team = self.team({"roles/poet.md": poet, "tasks/verse.md": verse})
                with self.assertRaises(compose.ConfigError) as cm:
                    self.go(team, "poet")
                self.assertEqual(str(cm.exception), msg)

    def test_a_custom_task_lacking_a_path_a_built_in_reference_needs_leads(self):
        steps = "\n## Steps\n\n1. **Write:** it.\n"
        self.write({"team/roles/writer.md": WRITER.replace("## Style", "See `<tasks>/<task>.md` › Steps › Write.\n\n"
                                                                     "## Style"),
                    "team/tasks/short-note.md": FILES["team/tasks/short-note.md"] + steps,
                    "team/tasks/long-note.md": FILES["team/tasks/long-note.md"] + steps})
        self.compose()
        team = self.team({"roles/writer.md": EXT, "tasks/tiny-note.md": TINY})
        msg = self.refused(team, "breaks a reference in roles/writer.md: no such task file path: See")
        self.assertTrue(msg.startswith(f"{self.path('tasks/tiny-note.md')}: breaks a reference in "), msg)
        team = self.team({"roles/writer.md": EXT, "tasks/tiny-note.md": TINY.replace("## Resume", steps + "\n## Resume")},
                         "fixed")
        self.go(team)

    def test_a_custom_heading_a_built_in_text_names_in_parentheses_leads(self):
        self.write({"team/principles.md": "# Principles\n\nKeep it short (Rhyme).\n"})
        self.compose()
        team = self.team({"roles/poet.md": POET.replace("## Tasks", "## Rhyme\n\nWell.\n\n## Tasks"),
                          "tasks/verse.md": VERSE})
        msg = self.refused(team, "breaks a reference in principles.md: bare reference: Keep it short (Rhyme).", "poet")
        self.assertTrue(msg.startswith(f"{self.path('roles/poet.md')}: "), msg)

    # FR-8
    def test_view_files(self):
        self.assertEqual(compose.view_files(compose.team(self.root), "writer"), {})
        team = self.team({"roles/poet.md": POET, "tasks/verse.md": VERSE, "roles/editor.md": EXT,
                          "tasks/tiny-note.md": TINY})
        self.assertEqual(compose.view_files(team, "writer"), {})
        self.assertEqual(compose.view_files(team, "poet"), {"verse": self.path("tasks/verse.md")})
        builtin = os.path.join(self.root, "team", "tasks")
        self.assertEqual(list(compose.view_files(team, "editor").items()),
                         [("long-note", f"{builtin}/long-note.md"), ("tiny-note", self.path("tasks/tiny-note.md"))])

    # NFR-1
    def test_without_a_team_dir_for_this_role_every_prompt_is_unchanged(self):
        other = self.team({"roles/poet.md": POET, "tasks/verse.md": VERSE, "templates/card.md": "# Card\n"})
        self.assertEqual(compose.team(self.root, ()), compose.team(self.root))
        for role in ("writer", "editor"):
            for task in (None, *compose.index(self.root, role)):
                with self.subTest(role=role, task=task):
                    want = self.compose(role, task)[0]
                    for team in (compose.team(self.root), other):
                        self.assertEqual(self.go(team, role, task)[0], want)
                    run = compose.load_run(self.root, role, task)
                    self.assertEqual(compose.render(self.root, run, client=Plain(inline_workdir="a temp dir")),
                                     compose.render(self.root, run, client=Plain(inline_workdir="a temp dir"),
                                                    team=other, tasks=None))
        self.assertEqual(compose.team(self.root).used("writer", ["note"]), [])
        self.assertEqual(other.used("writer", ["note"]), [])
        self.assertEqual(other.used("poet", ["note", "card"]),
                         [self.path("roles/poet.md"), self.path("tasks/verse.md"), self.path("templates/card.md")])

    def test_tasks_names_the_tasks_dir(self):
        team = self.team({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        run = compose.load_run(self.root, "poet", team=team)
        prompt = compose.render(self.root, run, PARAMS, client=Plain(), team=team, tasks="/w/tasks")
        self.assertEqual(parameters(prompt)["<tasks>"], "`/w/tasks`")

    def test_labels_and_sources(self):
        team = self.team({"roles/poet.md": POET})
        builtin = os.path.join(self.root, "team", "roles", "writer.md")
        self.assertEqual((team.label("roles", "writer", builtin), team.source(builtin), team.custom(builtin)),
                         ("roles/writer.md", "built-in", False))
        poet = self.path("roles/poet.md")
        self.assertEqual((team.label("roles", "poet", poet), team.source(poet), team.custom(poet)), (poet, poet, True))
        self.assertEqual(team.read("tasks", "long-note"), ("# Long Note\n\nWrite a long note.\n", "tasks/long-note.md"))
        with self.assertRaisesRegex(compose.ConfigError, r"^missing file tasks/nope\.md$"):
            team.read("tasks", "nope")


class Anchors(Fake):
    def test_a_prompt_without_a_repeated_heading_has_no_id_line(self):
        for task in (None, "long-note"):
            self.assertNotIn("<a id", self.compose(task=task)[0], task)

    def test_a_repeated_heading_gets_its_top_level_titles_id(self):
        self.write({"output/output.md": "# Output\n\n## Style\n\nShort.\n"})
        prompt, _ = self.compose()
        self.assertEqual(prompt.count("<a id"), 1)
        self.assertIn('\n## Style\n\n- `draft`: not a task.\n', prompt)
        self.assertIn('\n<a id="output-style"></a>\n## Style\n\nShort.\n', prompt)
        anchors = heading_anchors(prompt)
        self.assertEqual(len(anchors), len(set(anchors)))
        self.assertIn("output-style", anchors)

    def test_a_third_repeat_gets_a_suffix_on_the_qualified_id(self):
        self.write({"output/output.md": "# Output\n\n## Style\n\nShort.\n",
                    "output/destinations/local.md": "## Style\n\nKeep it local.\n"})
        prompt, _ = self.compose()
        self.assertIn('<a id="output-style"></a>\n## Style\n\nShort.\n', prompt)
        self.assertIn('<a id="output-style-2"></a>\n## Style\n\nKeep it local.\n', prompt)
        anchors = heading_anchors(prompt)
        self.assertEqual(len(anchors), len(set(anchors)))

    def test_a_link_to_a_repeated_heading_resolves_by_its_qualified_id(self):
        self.write({"output/output.md": "# Output\n\n## Style\n\nShort.\n"})
        own = own_text(self.compose()[0])
        for text, want in (("[Output › Style](#output-style)", True), ("[Writer › Style](#style)", True),
                           ("[Output › Style](#style)", False), ("[Writer › Style](#output-style)", False)):
            self.assertEqual(compose.reference_errors(f"{own}{text}\n", {}, {}) == [], want, text)

    def test_a_role_heading_input_makes_the_input_heading_input_2(self):
        self.write({"team/roles/writer.md": WRITER + "\n## Input\n\nWhat you get.\n"})
        prompt, _ = self.compose()
        self.assertEqual(prompt.count("<a id"), 1)
        self.assertTrue(prompt.endswith('\n<a id="input-2"></a>\n# Input\n\nResearch X.\n'))
        self.assertEqual(heading_anchors(prompt).count("input"), 1)

    def test_the_input_text_is_verbatim_whatever_it_repeats(self):
        given = "## Style\n\n## Style\n\n# Input\n\n```\n## Style\n```\n"
        prompt, _ = self.compose(input=given)
        self.assertEqual(prompt.count("<a id"), 0)
        self.assertTrue(prompt.endswith(f"\n# Input\n\n{given}"))

    def test_a_heading_in_a_fenced_template_is_no_repeat(self):
        self.write({"team/templates/note.md": "# Output\n\n## Style\n"})
        prompt, _ = self.compose()
        self.assertIn("```markdown\n# Output\n\n## Style\n```", prompt)
        self.assertNotIn("<a id", prompt)

    def test_without_an_input_section_the_whole_prompt_is_walked(self):
        self.write({"output/output.md": "# Output\n\n## Style\n\nShort.\n"})
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, None, client=Plain())
        self.assertNotIn("# Input", prompt)
        self.assertIn('\n<a id="output-style"></a>\n## Style\n\nShort.\n', prompt)

    def test_a_handover_heading_repeating_one_is_made_unique_too(self):
        self.write({"team/roles/writer.md": WRITER + "\n## Return\n\nBack.\n"})
        run = compose.load_run(self.root, "writer")
        prompt = compose.render(self.root, run, PARAMS, client=Plain("Say it back."))
        self.assertIn('\n<a id="output-return"></a>\n## Return\n\nSay it back.\n', prompt)


class Parameters(Fake):
    GITHUB = FILES["output/destinations/github.md"]

    def test_the_rule_opens_the_section_and_with_params_and_no_handover_the_workdir_scripts_and_tasks(self):
        prompt = self.compose()[0]
        self.assertIn(f"\n# Parameters\n\n{compose.RULE}\n\n- `<Workdir>`: `/w`\n", prompt)
        self.assertEqual(list(parameters(prompt).items()), [
            ("<Workdir>", "`/w`"), ("<scripts>", f"`{self.root}/src`"), ("<tasks>", f"`{self.root}/team/tasks`")])

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
    def test_a_default_is_used_when_the_value_is_missing_and_a_value_beats_it(self):
        for text, values, want in (("https://{{host|github.com}}/{{repo}}", {"repo": "o/n"}, "https://github.com/o/n"),
                                   ("{{host|github.com}}", {"host": "ghe.example.com"}, "ghe.example.com")):
            with self.subTest(values=values):
                self.assertEqual(compose.fill(text, values, "t"), want)

    def test_missing_without_default_fails(self):
        with self.assertRaises(compose.ConfigError):
            compose.fill("{{repo}}", {}, "t")


class Anchor(unittest.TestCase):
    def test_github_style(self):
        self.assertEqual(compose.anchor("Light Research"), "light-research")
        self.assertEqual(compose.anchor("Template: `x.md`"), "template-xmd")


OUTLINED = """\
# Role

<a id="custom"></a>
## Steps
- **Which build:** x
1. **Done.** y
   * **Nested**: z
text **not** an item
```text
# Fenced
**Fenced item**
```
~~~
## Also fenced
~~~
### Deep
**Item**
## Other
**Other item**
"""


class Outline(unittest.TestCase):
    def test_unfenced_skips_fences_by_char_and_length_and_two_backticks_are_no_fence(self):
        for text, want in (("a\n```\nb\n~~~\n```\n````\n```\nc\n````\n~~~ js\nd\n~~~  \ne\n  ```\nf\n~~~\ng",
                            [(0, "a"), (12, "e")]),
                           ("``\nx", [(0, "``"), (1, "x")])):
            with self.subTest(text=text):
                self.assertEqual(compose.unfenced(text), want)

    def test_headings_and_bold_items_outside_fences_with_their_paths_anchors_and_lines(self):
        steps = ("Role", "Steps")
        self.assertEqual(compose.outline(OUTLINED), [
            (0, ("Role",), "role", True),
            (3, steps, "custom", True),
            (4, steps + ("Which build",), "custom", False),
            (5, steps + ("Done",), "custom", False),
            (6, steps + ("Nested",), "custom", False),
            (15, steps + ("Deep",), "deep", True),
            (16, steps + ("Deep", "Item"), "deep", False),
            (17, ("Role", "Other"), "other", True),
            (18, ("Role", "Other", "Other item"), "other", False)])

    def test_a_skipped_level_nests_under_the_last_shallower_heading(self):
        paths = [path for _, path, _, _ in compose.outline("# A\n### B\n## C\n### D\n# E\n")]
        self.assertEqual(paths, [("A",), ("A", "B"), ("A", "C"), ("A", "C", "D"), ("E",)])

    def test_only_a_heading_of_one_to_six_hashes_and_a_space_at_the_line_start(self):
        text = "#No space\n####### seven\n # indented\n#\n###### Six\n"
        self.assertEqual(compose.outline(text), [(4, ("Six",), "six", True)])

    def test_an_explicit_id_is_the_whole_previous_unfenced_line(self):
        cases = {'<a id="x"></a>\n## H': "x", 'a <a id="x"></a>\n## H': "h", '<a id="x"></a>\n\n## H': "h",
                 '```\n<a id="x"></a>\n```\n## H': "h"}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual([a for _, _, a, h in compose.outline(text) if h], [want])

    def test_a_bold_item_before_any_heading_has_no_anchor(self):
        self.assertEqual(compose.outline("**X:** y\n"), [(0, ("X",), "", False)])

    def test_a_bold_item_opens_its_line(self):
        for line in ("**X** y", "- **X**", "* **X.**", "12. **X:**", "  - **X**", "**X**: y"):
            with self.subTest(line=line):
                self.assertEqual([path for _, path, _, _ in compose.outline(f"# T\n{line}\n")][1:], [("T", "X")])
        for line in ("a **X**", "-**X**", "**X", "- a **X**"):
            with self.subTest(line=line):
                self.assertEqual(len(compose.outline(f"# T\n{line}\n")), 1)


with open(os.path.join(CORE, compose.CONFIG), "rb") as _f:
    ROLES = list(tomllib.load(_f)["roles"])
ALL = [(r, t) for r in ROLES for t in compose.index(CORE, r)]
RUNS = [(r, None) for r in ROLES] + ALL   # each role given no task, then each role and task
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
        "wins. Read that task's file (no other task's), `<tasks>/<task>.md`, and follow its steps in order; its links (`#…`) point to "
        "sections of this prompt.")
COMMANDS_RULE = ("- **Commands**: run each command this prompt gives exactly, written as Parameters says, as its own "
                 "command (no `cd`, pipe, redirect or `&&`).")
RETURN = """Report through `report`, never in a reply:

- **Progress:** at each `[agent-pm-progress:<name>] …` line in your steps, before calling the next tool, run `report progress <name> <your report>`, e.g. `report progress start <what that line asks you to report>`.
- **Outcome:** as your last action, after everything else is done and any background work you started has finished, run `report outcome --status <done|needs_input|failed> --title <one line> --summary <text> [--question <q>]... [--url <url>] [--file <path>]... [--deliverable <file>]`: `--question` and `--file` once per item; `--deliverable` a file holding the deliverable, which is read as its text. If it fails, fix it and run it again."""


def client_prompt(name, role, task=None):
    """The prompt of the client `name` for `role`, with RUN when that client runs, and its run."""
    client = clients.get(name, CORE)
    run = compose.load_run(CORE, role, task, layers=[client.config])
    return compose.render(CORE, run, RUN if client.runs else None, client=client), run


def claude_prompt(role, task=None):
    """The claude client's prompt for `role` with RUN, and its run."""
    return client_prompt("claude", role, task)


def role_sources(role, client="claude"):
    """What `reference_errors` takes for `role` and `client`: the prompt's own text, the role's task files and the
    method files these name."""
    own = own_text(client_prompt(client, role)[0])
    tasks = {t: task_text(t) for t in compose.index(CORE, role)}
    named = sorted(set(compose.METHOD.findall(own + "".join(tasks.values()))))
    return own, tasks, {f"methods/{m}.md": method(m) for m in named}


class GenericNames(unittest.TestCase):
    def test_a_plugin_skill_without_its_slash_or_the_x_skill_is_caught_and_slash_commands_pass(self):
        for text, want in (("run autopilot:build\nthen `agent-pm:tmux`\nor x/agent-pm:act-as",
                            ["run autopilot:build", "then `agent-pm:tmux`"]),
                           ("the act-as skill\nthe tmux skill's workers\nthe skill",
                            ["the act-as skill", "the tmux skill's workers"]),
                           ("/autopilot:build, `/agent-pm:tmux`'s workers, /deep-research", [])):
            with self.subTest(text=text):
                self.assertEqual(generic_names(text), want)


class References(unittest.TestCase):
    def errors(self, text):
        """The errors in `text` as a method file beside the real engineer prompt and task files."""
        own, tasks, methods = role_sources("engineer")
        return [e for e in compose.reference_errors(own, tasks, methods | {"fixture": text}) if e.startswith("fixture:")]

    def test_a_link_to_a_path_of_the_prompt_and_a_task_file_reference_pass(self):
        for text in ("[Engineer › Repo › Merge](#repo), then", "[Engineer › Repo](#repo) step 1",
                     "[Principles](#principles)", "[Principles › Work › Worktree](#work)",
                     "[Engineer › Finish › Failure](#finish)", "[Output › Return](#return)",
                     "([Engineer › Rules](#rules)' rule)",
                     "(`<tasks>/<task>.md` › Steps › Which build picks the build)", "`<tasks>/build.md` › Steps",
                     "[docs](https://example.com/a)", "[A › B](https://example.com/a)",
                     "`Engineer › Repo` and ``a ` › b``",
                     "```\nEngineer › Repo\n[x](../y.md)\n```"):
            self.assertEqual(self.errors(text), [], text)

    def test_a_wrong_reference_is_an_error(self):
        for text in ("[Engineer › Nope](#nope)", "Engineer › Repo", "[Engineer › Repo](#finish)",
                     "[x](../roles/engineer.md#repo)", "[x]()", "[Principles › Worktree](#work)",
                     "[Engineer › Repo step 1](#repo)", "[Engineer › Repo](#repo) › Merge",
                     "`<tasks>/<task>.md` › Steps › Nope", "`<tasks>/<task>.md` › Steps › Which builds",
                     "`<tasks>/<task>.md` › Steps › Which build › Nope", "`<tasks>/<task>.md` › Nope",
                     "`<tasks>/nope.md` › Steps", "Which build › Steps",
                     "```\n```\nEngineer › Repo"):
            self.assertTrue(self.errors(text), text)

    def test_errors_name_the_file_and_the_offender(self):
        errors = compose.reference_errors("# R\n\n## S\n\nR › S\n", {"x": "# X\n\n[A](b.md)\n"},
                                  {"m.md": "[A › B](#nope)\n"})
        self.assertEqual(errors, ["prompt: bare reference: R › S", "tasks/x.md: relative link target: [A](b.md)",
                                  "m.md: no such anchor: [A › B](#nope)"])

    def test_a_task_file_path_must_exist_in_each_task_file_named(self):
        tasks = {"a": "# A\n\n## Steps\n\n1. **Go:** x\n", "b": "# B\n\n## Steps\n"}
        for text, want in (("`<tasks>/<task>.md` › Steps", True), ("`<tasks>/<task>.md` › Steps › Go", False),
                           ("`<tasks>/a.md` › Steps › Go", True), ("`<tasks>/b.md` › Steps › Go", False)):
            self.assertEqual(compose.reference_errors("# R\n", tasks, {"m": text}) == [], want, text)

    def test_a_method_file_path_must_exist_in_that_method_file(self):
        methods = {"methods/a.md": "# A\n\n## Rules\n\n- **Voting**: x\n"}
        for text, want in (("`<methods>/a.md` › Rules › Voting", True), ("`<methods>/a.md` › Rules", True),
                           ("`<methods>/a.md` › Rules › Nope", False), ("`<methods>/b.md` › Rules", False),
                           ("`<methods>/<task>.md` › Rules", False)):
            self.assertEqual(compose.reference_errors("# R\n", {}, methods | {"m": text}) == [], want, text)

    def test_a_section_name_of_the_prompt_in_parentheses_is_a_bare_reference(self):
        for text in ("decide it (Repo).", "(Merge)", "see (Finish) first", "(Worktree)", "(Autopilot)"):
            self.assertTrue(self.errors(text), text)
        for text in ("(local, mixed)", "([Engineer › Repo](#repo))", "(`Repo`)", "(the Repo)", "(Repo step 1)",
                     "```\n(Repo)\n```"):
            self.assertEqual(self.errors(text), [], text)

    def test_texts_checks_only_the_given_texts_against_owns_anchors(self):
        own = "# R\n\n## S\n\nR › S\n"
        errors = compose.reference_errors(own, {}, {}, texts={"a": "[x](#s)\n", "b": "[y](#nope)\n"})
        self.assertEqual(errors, ["b: no such anchor: [y](#nope)"])

    def test_labels_relabel_a_tasks_errors(self):
        errors = compose.reference_errors("# R\n", {"x": "[A](b.md)\n", "y": "[B](c.md)\n"}, {},
                                          labels={"x": "/t/x.md"})
        self.assertEqual(errors, ["/t/x.md: relative link target: [A](b.md)",
                                  "tasks/y.md: relative link target: [B](c.md)"])

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
    def test_core_config_renders_the_language_and_progress_rules_once(self):
        for role in ROLES:
            prompt, run = composed(role)
            self.assertEqual(run.language, "Chinese")
            self.assertEqual(prompt.count(LANGUAGE_RULE), 1, role)
            self.assertIn("are in Chinese", prompt, role)
            self.assertEqual(prompt.count("is a point to tell the user your progress"), 1, role)

    def test_fixed_labels_are_translated_and_template_headings_say_so(self):
        prompt, _ = composed("researcher")
        self.assertIn("- Reports and PRDs, headings and fixed labels included, are in Chinese; a `<X, translated>` in a "
                      "template or task is X in Chinese.", prompt)
        for role, task in ALL:
            text = composed(role, task)[0]
            for literal in ("`Check:`", "Check:", "Confidence:", "`Light Research. Angles: "):
                self.assertNotIn(literal, text, (role, task, literal))
        for role, task in [(r, None) for r in ROLES] + ALL:
            g = guide(composed(role, task)[0])
            self.assertIn("Its headings are fixed apart from their language ([Principles › Writing](#writing))", g,
                          (role, task))

    def test_writing_has_the_one_gloss_and_diagram_rules_and_no_length_cap(self):
        for role, task in RUNS:
            prompt = composed(role, task)[0]
            writing = section(prompt, "Writing")
            self.assertNotIn("the first mention adds the", prompt, (role, task))
            self.assertIn("A term's first mention adds one gloss, its Chinese rendering (for an acronym, of its full "
                          "name); none when the term is common in Chinese, and none in a heading.", writing)
            diagrams = item(writing, "Diagrams and tables")
            for literal in ("a Mermaid diagram", "`flowchart`", "`sequenceDiagram`", "`timeline`", "short node labels",
                            "a table", "a list", "a caption", "delete the text the diagram replaces"):
                self.assertIn(literal, diagrams, (role, task, literal))
            for cap in ("3000 words", "5 sentences"):
                self.assertNotIn(cap, prompt, (role, task, cap))

    def test_templates_put_the_summary_first_one_fact_per_item_and_point_to_the_diagram_rule(self):
        pm, light = composed("pm", "product-design")[0], composed("researcher", "light-research")[0]
        point = "[Principles › Writing › Diagrams and tables](#writing)"
        for text in (pm, light):
            self.assertIn("One fact per list item", text)
            self.assertIn("never repeats `[Reference]`", text)
        self.assertIn("Opens with one sentence", section(pm, "Problem and goals"))
        self.assertIn(point, section(pm, "User flow"))
        self.assertIn(point, section(pm, "Approach and trade-offs"))
        self.assertIn("`- FR-<n>: <the requirement>` on one line, then one indented sub-item `  - <Check, translated>: ", pm)
        self.assertIn("Opens with one sentence", section(light, "Conclusion and recommendation"))
        self.assertIn(point, section(light, "Comparison table"))
        findings = section(light, "Findings")
        self.assertIn(point, findings)
        self.assertIn("`(<confidence: high, medium or low>; [n]…; <single-source or unverified, when so>)`", findings)
        self.assertIn("`1. <URL or code permalink> (primary|secondary|code)`", section(light, "Sources"))
        self.assertEqual(light.count("type line"), 1)   # the Template's, deep research only
        self.assertIn("Deep research's type line (light research gives its own)", light)
        self.assertEqual(task_text("light-research").count("`<Light Research type line, translated>`"), 1)

    def test_pm_and_researcher_point_to_writing_for_concision(self):
        for role, doc in (("pm", "the PRD"), ("researcher", "the report")):
            self.assertIn(f"- **Concise**: after writing {doc}, cut repetition and preamble "
                          "([Principles › Writing](#writing)); length follows content.", composed(role)[0])

    def test_echo_summary_overrides_outputs_length(self):
        with open(os.path.join(CORE, "team", "tasks", "echo.md")) as f:
            self.assertIn("`summary` the input's first line, not [Output](#output)'s 3–5 lines.", f.read())

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

    def test_a_local_users_trusted_dirs_workers_per_column_and_grid_retile_are_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("team", "output"):
                os.symlink(os.path.join(CORE, d), os.path.join(root, d))
            shutil.copy(os.path.join(CORE, compose.CONFIG), root)
            with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
                f.write('users = ["octocat"]\ntrusted_dirs = ["~/data-repo"]\nshow = ""\nworkers_per_column = 2\n'
                        'grid_retile = "off"\n')
            for role, task in ALL:
                run = compose.load_run(root, role, task)
                self.assertEqual((run.task, run.show), (task, ""))

    def test_local_lines_uncommented_are_a_valid_local_file(self):
        with open(os.path.join(CORE, compose.CONFIG)) as f:
            local = tomllib.loads("".join(line.removeprefix("# local: ") for line in f if line.startswith("# local: ")))
        self.assertLessEqual({"show", "cwd", "users", "trusted_dirs", "workers_per_column", "grid_retile", "team_dirs"},
                             set(local))
        self.assertLessEqual({"researcher", "pm"}, set(local["roles"]))
        for role, table in local["roles"].items():
            compose.check_old_keys(table, role)

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
            self.assertEqual(len(compose.START.findall(text)), 1, name)
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

    def test_every_reference_is_a_link_to_a_path_of_the_prompt(self):
        for role in ROLES:
            for client in clients.REGISTRY:
                self.assertEqual(compose.reference_errors(*role_sources(role, client)), [], (role, client))

    def test_every_prompts_own_headings_have_unique_anchors(self):
        for role, task in RUNS:
            for client in clients.REGISTRY:
                anchors = heading_anchors(client_prompt(client, role, task)[0])
                self.assertEqual(len(anchors), len(set(anchors)), (role, task, client))

    def test_every_run_compiles_without_placeholders_and_opens_with_the_guide_listing_parameters_first_input_last(self):
        for role, task in RUNS:
            with self.subTest(role=role, task=task):
                prompt, run = composed(role, task)
                self.assertNotIn("{{", prompt)
                self.assertEqual(run.task, task or "")
                g = guide(prompt)
                self.assertTrue(g.startswith("# Guide\n"))
                self.assertIn("`<tasks>/<task>.md`", g)
                self.assertIn(f"[{run.role_title} rules](#{compose.anchor(run.role_title)}) > your task file's rules", g)
                self.assertEqual(prompt.count("On conflict:"), 1)
                self.assertIn("On conflict:", g)
                items = re.findall(r"^- \*\*.+$", g, re.M)
                self.assertEqual(items[0], "- **Parameters**: the value of each name this prompt and your task's file use.")
                self.assertEqual(items[-1], "- **Input**: the last section; everything after its heading is the input "
                                            "text, verbatim (it may contain `#` or `---`).")
                self.assertNotIn("final `---`", g)
                self.assertNotIn("your Workdir and the Input text", g)

    def test_the_guide_names_a_given_task_else_the_run_picks_it(self):
        named = guide(composed("researcher", "light-research")[0])
        self.assertIn("**Your task**: `light-research` (`<tasks>/light-research.md`). Read that task's file "
                      "(no other task's)",
                      named)
        self.assertNotIn("pick it from", named)
        picked = guide(composed("researcher")[0])
        self.assertIn(PICK, picked)
        self.assertNotIn("unsure → the default it names", picked)
        self.assertIn("Your start progress report names the task and why.", picked)
        self.assertNotIn("light-research", picked)

    def test_a_prompt_has_its_roles_index_and_no_task_steps(self):
        for role, task in RUNS:
            prompt, _ = composed(role, task)
            for t, line in compose.index(CORE, role).items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", prompt, (role, task))
                for step in task_text(t).splitlines():
                    if len(step.strip()) >= 30 and not step.startswith("#"):
                        self.assertNotIn(step.strip(), prompt, (role, task, t))

    def test_a_task_from_another_roles_index_is_an_error(self):
        with self.assertRaisesRegex(compose.ConfigError, "task 'deep-research' is not one of engineer's tasks"):
            compose.load_run(CORE, "engineer", "deep-research")

    def test_pm_engineer_and_researcher_runs(self):
        researcher = (("[Researcher rules](#researcher) > your task file's rules",
                       "# Template: `templates/research-report.md`", "`ophis/private_docs`"),
                      lambda run: (run.tier, run.effort, run.read), (2, "high", ["{{methods}}"]))
        for role, task, literals, got, want in (
                ("pm", None, ("[PM rules](#pm) > your task file's rules", "# Template: `templates/prd.md`"),
                 lambda run: (run.output["dir"], run.read, run.write), ("Product Design/", [], [])),
                ("engineer", None, ("[Engineer rules](#engineer) > your task file's rules", "gh pr create"),
                 lambda run: (run.effort, run.write, run.output), ("xhigh", [], {"type": "pull-request"})),
                *(("researcher", task, *researcher) for task in (None, "deep-research", "light-research"))):
            with self.subTest(role=role, task=task):
                prompt, run = composed(role, task)
                for literal in literals:
                    self.assertIn(literal, prompt)
                self.assertEqual(got(run), want)

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
        for phrase in ("No such tool → follow the deep-research method ([Researcher › Methods](#methods)).",
                       "per the ultracode method ([Researcher › Methods](#methods))",
                       "No Workflow tool → follow that method.",
                       "Before the second, the brake ([Researcher › Methods](#methods)).",
                       "No subagents with a round's tools → skip that round, under Gaps."):
            self.assertIn(phrase, task_text("deep-research"))

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
        for role, task in RUNS:
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

    def test_literals_a_role_or_task_text_holds(self):
        engineer, researcher = composed("engineer")[0], composed("researcher")[0]
        for why, text, literal in (
                ("builds fail without push permission", engineer, "`push` false → `failed`"),
                ("light-build's cutoff skips merge commits", task_text("light-build"),
                 "`git -C <worktree> log -1 --first-parent --no-merges --format=%cI`"),
                ("research hand-off phrases", task_text("light-research"), "`<Light Research type line, translated>`"),
                ("research hand-off phrases", task_text("light-research"), "`Suggest upgrading to Deep Research: "),
                ("research hand-off phrases", task_text("deep-research"), "its Light Research type line"),
                # target.py leaves <id>-<task> branches of read-only tasks out of a build's branch check.
                ("a read-only task's branch is <id>-<task>", researcher,
                 "`<branch>` `<id>-<task>`, `<task>` this task, `deep-research` or `light-research`"),
                ("a read-only task's branch is <id>-<task>", task_text("product-design"),
                 "`<branch>` `<id>-product-design`")):
            with self.subTest(why, literal=literal):
                self.assertIn(literal, text)

    def test_builds_wait_for_the_prs_checks(self):
        for task in ("build", "light-build"):
            done = item(section(composed("engineer", task)[0], "Finish"), "Done")
            for literal in ("`gh pr checks <branch> --repo <host>/<owner>/<name> --watch --fail-fast`",
                            "`gh run view <run-id> --repo <host>/<owner>/<name> --log-failed`",
                            "`git -C <worktree> push -u origin <branch>`", "3 fixes", "20 minutes",
                            "[Engineer › Finish › Failure](#finish)"):
                self.assertIn(literal, done, task)

    def test_builds_merge_the_base_branch_before_autopilot_at_resume_and_before_the_pr(self):
        prompt, _ = composed("engineer")
        repo = section(prompt, "Repo")
        self.assertIn("JSON `base` → `<base>`: the PR's base branch, else `Base:`, else `<default>`.", repo)
        merge = item(repo, "Merge")
        for literal in ("[Engineer › Repo](#repo) step 1", "`git -C <worktree> status`", "a merge in progress",
                        "`git -C <worktree> merge --no-edit origin/<base>`", "`git -C <worktree> merge --abort`"):
            self.assertIn(literal, merge)
        for where, text in (("Autopilot", section(prompt, "Autopilot")), ("Finish", section(prompt, "Finish")),
                            ("Repo › Resume", item(repo, "Resume"))):
            self.assertIn("[Engineer › Repo › Merge](#repo)", text, where)
        self.assertIn("`origin/<base>`", section(prompt, "Autopilot"))
        for literal in ("--head <branch> --base <base> --title", "/compare/<base>...<branch>?expand=1"):
            self.assertIn(literal, prompt)
        for task in ("build", "light-build"):
            self.assertIn("[Engineer › Repo › Resume](#repo).", task_text(task), task)

    def test_the_engineer_charter_has_six_sections_and_autopilot_adds_its_lines_verbatim(self):
        with open(os.path.join(CORE, "team", "roles", "engineer.md")) as f:
            charter = f.read()
        heads = [path[1] for _, path, _, heading in compose.outline(charter) if heading and len(path) == 2]
        self.assertEqual(heads, ["Tasks", "Rules", "Input processing", "Repo", "Autopilot", "Finish"])
        lines = ("Work only in `<worktree>` on branch `<branch>`, with absolute paths; create no other clone, worktree "
                 "or branch.", "Commits merged from `origin/<base>` are not this build's work.",
                 "Skip S8; keep the commits. After <your task's push points>, run exactly "
                 "`git -C <worktree> push -u origin <branch>`.")
        self.assertIn("\nAdd verbatim, placeholders filled in:\n\n```\n" + "\n".join(lines) + "\n```\n",
                      section(charter, "Autopilot"))

    def test_merges_failing_checks_say_what_happens_in_finish_before_the_build_and_on_resume(self):
        merge = item(section(composed("engineer")[0], "Repo"), "Merge")
        self.assertIn("One failing: during [Engineer › Finish › Done](#finish) → [Engineer › Finish › Failure](#finish); "
                      "before the build → the failing checks go into the build's requirement; on [Engineer › Repo › Resume](#repo) → "
                      "into the requirement of the build left, none left → [Engineer › Finish › Failure](#finish).", merge)

    def test_the_pull_request_destination_reads_pr_from_repo_py_status(self):
        self.assertIn("3. By `repo.py status`'s `pr`: null → `gh pr create", composed("engineer")[0])

    def test_a_builds_files_are_its_spec_and_plan_doc(self):
        self.assertIn("adding `files`: this build's spec (its plan doc's `spec_file=`) and plan doc (on failure, those "
                      "that exist);", task_text("build"))

    def test_builds_report_each_s5_task_against_their_task_count(self):
        for task, doc in (("build", "the plan doc's"), ("light-build", "the state file's")):
            text = task_text(task)
            self.assertIn("**Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:", text, task)
            self.assertIn("[agent-pm-progress:step] the step and its result; an S5 task as "
                          f"`S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` {doc} task count "
                          "(its `### Task` headings)", text, task)

    def test_light_build_always_writes_a_state_file_with_a_task_list(self):
        self.assertIn('docs line "The state file goes where the repo keeps design docs, else in `docs/.autopilot/`; '
                      'never `git add -f` it."', task_text("light-build"))
        line = compose.index(CORE, "engineer")["light-build"]
        self.assertIn("(a task list in its state file, implementation, verification, a light review, no spec or plan docs)",
                      line)

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

    def test_drive_list_gives_descriptions_without_paths(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(drive.main(["--list"], root=CORE), 0)
        self.assertIn("\n  build: a PRD into a pull request with `/autopilot:build` (", out.getvalue())
        self.assertNotIn("<tasks>", out.getvalue())

    def test_both_skills_list_roles_with_drive_list(self):
        rule = "  - Bash(python3 ${CLAUDE_SKILL_DIR}/../../src/drive.py --list)\n"
        for skill in ("act-as", "tmux"):
            with open(os.path.join(CORE, "skills", skill, "SKILL.md")) as f:
                text = f.read()
            with self.subTest(skill):
                self.assertIn(rule, re.match(r"---\n(.*?\n)---\n", text, re.S).group(1))
                self.assertIn("`python3 ${CLAUDE_SKILL_DIR}/../../src/drive.py --list`", text)
        self.assertNotIn("python3 -c", text)

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
        self.assertIn("https://<out-host>/<out-repo>/blob/<out-branch>/<file>`, `<file>` URL-encoded", prompt)
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


def item(text, name):
    """The top-level bold item `- **name**` of `text`, up to the next top-level item."""
    m = re.search(rf"^- \*\*{re.escape(name)}\*\*(.*?)(?=^- |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


class Methods(unittest.TestCase):
    def test_voting_has_its_home_in_ultracode_and_pipeline_shares_its_start(self):
        deep, ultra = (method(m) for m in METHOD_NAMES)
        for m, text in zip(METHOD_NAMES, (deep, ultra)):
            for name in ("Voting", "Pipeline"):
                self.assertEqual(len(rule(text, name)), 1, f"{m}: {name}")
            self.assertTrue(rule(text, "Pipeline")[0].startswith(
                "- **Pipeline**: ≤ 10 subagents running at once. Each result of a stage goes to the next stage as soon "
                "as it arrives, never in batches;"), m)
        self.assertEqual(rule(deep, "Voting"), ["- **Voting**: `<methods>/ultracode.md` › Rules › Voting."])
        self.assertTrue(rule(ultra, "Pipeline")[0].endswith("only verification waits for all claims, to rank them."))
        for phrase in ("fetch selection waits for all search results", "verification waits for all claims"):
            self.assertIn(phrase, rule(deep, "Pipeline")[0])

    def test_each_states_voting_pipeline_restrictions_and_its_own_limits(self):
        voting = ("≥ 2 refutes → refuted", "else ≥ 2 valid votes → confirmed",
                  "else (agent errors, missing votes) → unverified", "votes that came back", "unsure → votes refuted")
        both = ("≤ 10 subagents running at once", "verification waits for all claims",
                "the calling task's restrictions for", "into every subagent prompt, voters included")
        own = {"deep-research": ("5 complementary web search angles", "after every search agent has returned",
                                 "rank the whole set by relevance (high → low)", "dispatch fetches for the first ≤ 15",
                                 "top 25", "≤ 100",
                                 "start each web agent with fresh context (no inherited conversation), so it sees "
                                 "only its prompt",
                                 "only from the brief and web results",
                                 "Dispatch fetches only for URLs a search agent returned", "**Page text**:"),
               "ultracode": voting + ("A fixed method for the ultracode round",
                                      "room in the cap for the key claims' votes", "while ≥ 3 cap slots remain",
                                      "undispatched subquestions and unverified claims go under Gaps")}
        self.assertEqual(set(own), set(METHOD_NAMES))
        for m in METHOD_NAMES:
            text = method(m)
            with self.subTest(m):
                self.assertTrue(text.startswith("# "))
                self.assertNotRegex(text, r"\b[Yy]ou\b")
            for phrase in both + own[m]:
                with self.subTest(m, phrase=phrase):
                    self.assertIn(phrase, text)
        self.assertNotIn("as each search agent's results arrive", method("deep-research"))
        for phrase in voting:  # the Voting rule's one home is ultracode
            with self.subTest("deep-research", not_in=phrase):
                self.assertNotIn(phrase, method("deep-research"))

    def test_private_detail_has_its_home_in_the_researcher_charter(self):
        home = "with no **private detail** in queries: internal names, paths, permalinks, private repo names, "
        with open(os.path.join(CORE, "team", "roles", "researcher.md")) as f:
            self.assertIn(home, f.read())
        link = "private detail ([Researcher › Type and target › Agents](#type-and-target))"
        self.assertIn(link, task_text("deep-research"))
        self.assertIn("private detail per [Researcher › Type and target › Agents](#type-and-target)",
                      method("deep-research"))
        for text in (task_text("deep-research"), method("deep-research")):
            self.assertNotIn("internal names", text)

    def test_the_researcher_charter_and_methods_are_harness_neutral(self):
        with open(os.path.join(CORE, "team", "roles", "researcher.md")) as f:
            texts = {"roles/researcher.md": (f.read(), ())} | {m: (method(m), ("{{",)) for m in METHOD_NAMES}
        for name, (text, more) in texts.items():
            for word in ("Read, Grep", "Glob", "Workflow tool", "journal.jsonl", *more):
                with self.subTest(name, word=word):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
