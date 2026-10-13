"""composer/prompt.py: render on a fake team (ported from the old composer's Prompt, Anchors, Parameters and
Deliverable tests and the TeamDirs cases that render), RULE alone, the neutral Return, the resume prefix, and
reference_errors and the method files on the real team (its References and Methods tests)."""

import inspect
import os
import re
import tempfile
import unittest
from dataclasses import replace

from agent_pm.delegate.composer.base import METHOD, TEAM, ConfigError, outline
from agent_pm.delegate.composer.prompt import (
    OUTCOME,
    PARAMETERS,
    RESUME,
    RETURN,
    RULE,
    reference_errors,
    render,
)
from agent_pm.delegate.composer.run import RunParams, resolve
from agent_pm.delegate.composer.teams import index, team

from delegate.tests.case import Case
from delegate.tests.unit.composer.run_test import CONFIG
from delegate.tests.unit.composer.team_test import FILES, INTRO, POET, VERSE, WRITER

TEXTS = {
    "guide.md": (
        "# Guide\n\nYou are {{role}}. Task: {{task|pick one}}; files in `<tasks>`.\n\n"
        "On conflict: [Principles](#principles) > [{{role}} rules](#{{role_anchor}}) > your task's.\n"
    ),
    "principles.md": "# Principles\n",
    "output/output.md": "# Output\n\nWrite `Output:`.\n",
    "output/destinations/local.md": "## Destination\n\nKeep it local.\n",
    "output/destinations/github.md": "## Destination\n\nPush to `<out-repo>` on `<out-branch>`.\n",
}
PARAMS = RunParams(input="Research X.", out="/w/out.md", workdir="/w")
RUN = RunParams(
    input="Research X.", out="/runs/W-1/out.md", workdir="/runs/W-1"
)  # a workdir no other text holds
METHOD_NAMES = ("deep-research", "ultracode")


def parameters(prompt):
    """{name: value} of the prompt's # Parameters lines after RULE, in order; a line of another shape fails."""
    section_ = re.search(r"^# Parameters\n\n(.*?)\n(?=# )", prompt, re.M | re.S).group(1)
    lines = section_.split("\n\n", 1)[1].splitlines()
    return dict(re.fullmatch(r"- `([^`]+)`: (.+)", line).groups() for line in lines)


def own_text(prompt):
    """`prompt` up to and including its # Input heading line: the input text, always last, is dropped."""
    return prompt.split("\n# Input\n\n", 1)[0] + "\n# Input\n"


def heading_anchors(prompt):
    """The anchor of each heading of the prompt's own text, in order."""
    return [a for _, _, a, heading in outline(own_text(prompt)) if heading]


def composed(role, task=None, overlays=(), **changes):
    """The real core's prompt for `role` (and `task`) with RUN, its run changed by `changes`, and that run."""
    run = replace(resolve(role, task, overlays=overlays), **changes)
    return render(run, RUN), run


def task_text(task):
    with open(os.path.join(TEAM, "tasks", f"{task}.md")) as f:
        return f.read()


def method(name):
    with open(os.path.join(TEAM, "methods", f"{name}.md")) as f:
        return f.read()


def role_sources(role, task=None):
    """What `reference_errors` takes for `role` (and `task`): the real prompt's own text, the role's task files and the
    method files these name."""
    own = own_text(composed(role, task)[0])
    tasks = {t: task_text(t) for t in index(role)}
    named = sorted(set(METHOD.findall(own + "".join(tasks.values()))))
    return own, tasks, {f"methods/{m}.md": method(m) for m in named}


def rule(text, name):
    return [line for line in text.splitlines() if line.startswith(f"- **{name}**:")]


def section(text, name):
    m = re.search(rf"^## {name}\n(.*?)(?=^#)", text, re.M | re.S)
    return m.group(1) if m else ""


def item(text, name):
    """The top-level bold item `- **name**` of `text`, up to the next top-level item."""
    m = re.search(rf"^- \*\*{re.escape(name)}\*\*(.*?)(?=^- |\Z)", text, re.M | re.S)
    return m.group(1) if m else ""


class Fake(Case):
    """A team root, config and local file in a temp dir; a HOME of the test's own for team dirs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.root = os.path.join(self.tmp, "team")
        self.config = os.path.join(self.tmp, "config.toml")
        self.local_file = os.path.join(self.tmp, "local.toml")
        self.write(FILES | TEXTS)
        with open(self.config, "w") as f:
            f.write(CONFIG)

    def write(self, files, top=None):
        for rel, text in files.items():
            path = os.path.join(top or self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def configure(self, text):
        with open(self.config, "w") as f:
            f.write(text)

    def local(self, text):
        with open(self.local_file, "w") as f:
            f.write(text)

    def run_of(self, role="writer", task=None, overlays=(), found=None):
        return resolve(
            role,
            task,
            overlays=overlays,
            team=self.found(found),
            config=self.config,
            local=self.local_file,
        )

    def found(self, given=None):
        return given if given is not None else team(self.root)

    def compose(self, role="writer", task=None, *, overlays=(), found=None, **params):
        run = self.run_of(role, task, overlays, found)
        return render(run, replace(PARAMS, **params), team=self.found(found)), run

    def fails(self, role="writer", task=None, overlays=(), found=None) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.compose(role, task, overlays=overlays, found=found)
        return str(cm.exception)


class Prompt(Fake):
    def test_sections_in_order(self):
        for task in (None, "long-note"):
            prompt, _ = self.compose(task=task)
            prose = re.sub(r"```markdown\n.*?\n```\n", "", prompt, flags=re.S)
            heads = [line for line in prose.splitlines() if line.startswith(("# ", "## "))]
            self.assertEqual(
                heads,
                [
                    "# Guide",
                    "# Parameters",
                    "# Principles",
                    "# Writer",
                    "## Tasks",
                    "## Style",
                    "# Template: `templates/note.md`",
                    "# Output",
                    "## Destination",
                    "## Return",
                    "# Input",
                ],
                task,
            )

    def test_the_guide_names_the_task_only_when_one_is_named(self):
        prompt, _ = self.compose()
        guide = prompt.split("# Principles", 1)[0]
        self.assertIn("You are Writer. Task: pick one; files in `<tasks>`.", guide)
        self.assertIn(
            "On conflict: [Principles](#principles) > [Writer rules](#writer) > your task's.", guide
        )
        self.assertEqual(prompt.count("On conflict:"), 1)
        prompt, _ = self.compose(task="long-note")
        self.assertIn(
            "You are Writer. Task: `long-note` (`<tasks>/long-note.md`); files in",
            prompt.split("# Principles", 1)[0],
        )

    def test_no_task_text_reaches_the_prompt(self):
        for task in (None, "short-note", "long-note"):
            prompt, _ = self.compose(task=task)
            for text in ("Write a note.", "Write a long note.", "# Short Note", "# Long Note"):
                self.assertNotIn(text, prompt, task)

    def test_destination_keeps_its_names(self):
        prompt, _ = self.compose("editor")
        self.assertIn("Push to `<out-repo>` on `<out-branch>`.", prompt)

    def test_template_is_fenced_and_the_fence_outgrows_its_backticks(self):
        for template, want in (
            ("# Note: [Title]\n", "```markdown\n# Note: [Title]\n```"),
            ("```js\nx\n```\n", "````markdown\n```js\nx\n```\n````"),
        ):
            with self.subTest(template=template):
                self.write({"templates/note.md": template})
                self.assertIn(want, self.compose()[0])

    def test_an_input_naming_a_file_is_text(self):
        self.write({"input.md": "question"}, self.tmp)
        here = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, here)
        for given in ("input.md", os.path.join(self.tmp, "input.md")):
            prompt, _ = self.compose(input=given, out="o.md", workdir="wd")
            self.assertTrue(prompt.endswith(f"\n# Input\n\n{given}\n"), given)
            self.assertEqual(parameters(prompt)["<Workdir>"], f"`{os.path.abspath('wd')}`")
            self.assertNotIn("question", prompt)

    def test_input_last_with_free_text(self):
        prompt, _ = self.compose(input="Compare cmux and tmux.\nKeep it short.")
        self.assertTrue(
            prompt.endswith(
                f"Keep it local.\n\n{RETURN}\n# Input\n\nCompare cmux and tmux.\nKeep it short.\n"
            )
        )

    def test_an_input_with_headings_and_rules_is_verbatim_after_the_one_input_heading(self):
        given = "# Heading\n\nText.\n\n---\n\n# Input\n\nMore."
        prompt, _ = self.compose(input=f"\n{given}\n\n")
        self.assertEqual(prompt.split("\n# Input\n\n", 1)[1], f"{given}\n")
        self.assertEqual(prompt.count("\n---\n"), 1)

    def test_the_return_section_closes_the_output_section(self):
        body = self.compose()[0].rsplit("\n# Input\n", 1)[0]
        self.assertTrue(body.endswith(f"Keep it local.\n\n{RETURN}"))

    def test_the_return_reports_in_replies_and_names_no_runtime(self):
        self.assertTrue(RETURN.startswith("## Return\n\n"))
        self.assertEqual(OUTCOME, "agent-pm-outcome")
        for literal in (
            "at each `[agent-pm-progress:<name>] …` line in your steps, before calling the next tool, "
            "write that line in a reply as a line of its own, outside any code block",
            "`[agent-pm-progress:start] <what that line asks you to report>`",
            "all background work and every agent you spawned have finished",
            "end your last reply with one fenced code block whose info string is `agent-pm-outcome`",
            "one JSON object of the fields in [Output](#output)",
            "`deliverable_file`",
        ):
            self.assertIn(literal, RETURN)
        self.assertNotRegex(RETURN, re.compile("claude|codex", re.I))
        for word in ("report.py", "report progress"):
            self.assertNotIn(word, RETURN)

    def test_the_rule_alone_opens_parameters(self):
        self.assertEqual(
            RULE,
            "Each name below stands for its value. Run every command with each name, nested "
            "ones included, replaced by its value and written out in full, no name or shell "
            "variable left.",
        )
        first = self.compose()[0].split("\n# Parameters\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(first, RULE)

    def test_texts_keep_the_names_and_parameters_give_the_composers_paths(self):
        self.write(
            {
                "principles.md": "# Principles\n\nRun `<Workdir>/x.py`; tasks in `<tasks>`.\n",
                "roles/writer.md": WRITER + "\nKnow `<team>` and `<tasks>/short-note.md`.\n",
            }
        )
        prompt, _ = self.compose()
        self.assertIn("Run `<Workdir>/x.py`; tasks in `<tasks>`.", prompt)
        self.assertIn("Know `<team>` and `<tasks>/short-note.md`.", prompt)
        self.assertEqual(
            parameters(prompt),
            {
                "<Workdir>": "`/w`",
                "<tasks>": f"`{self.root}/tasks`",
                "<team>": f"`{self.root}`",
            },
        )

    def test_resolve_then_render_with_another_output(self):
        run = self.run_of("editor")
        prompt = render(replace(run, output={"type": "local"}), PARAMS, team=self.found())
        self.assertIn("Keep it local.", prompt)
        self.assertNotIn("Push to", prompt)

    def test_params_are_required(self):
        self.assertEqual(list(inspect.signature(render).parameters), ["run", "params", "team"])
        with self.assertRaises(TypeError):
            render(self.run_of(), team=self.found())

    def test_resume_starts_with_resumed_run_names_no_task_and_its_input_adds_to_the_earlier_one(
        self,
    ):
        self.assertIn(
            "Continue the task this session already picked or was given, following your task file's "
            "`## Resume` section; never pick it again.",
            RESUME,
        )
        self.assertIn(
            "The input below is current: it adds to this session's earlier input.", RESUME
        )
        prompt, _ = self.compose(resume=True, input="Answers:\n- Use B.")
        self.assertTrue(prompt.startswith(RESUME + "# Guide"))
        self.assertIn("Task: pick one;", prompt)
        self.assertTrue(prompt.endswith("\n# Input\n\nAnswers:\n- Use B.\n"))
        self.assertTrue(self.compose()[0].startswith("# Guide"))

    def test_a_leftover_or_run_value_placeholder_in_a_rule_file(self):
        for name in (
            "tool",
            "scripts",
            "tasks",
            "team",
            "gate",
            "repo",
            "branch",
            "dir",
            "host",
            "report",
        ):
            with self.subTest(name=name):
                self.write({"roles/writer.md": WRITER + f"\nUse `{{{{{name}}}}}`.\n"})
                self.assertEqual(
                    self.fails(), f"unfilled placeholder {{{{{name}}}}} in roles/writer.md"
                )


class Errors(Fake):
    def test_a_destination_template_or_output_key_the_run_names_is_missing(self):
        for old, new, msg, role in (
            (
                'type = "github"',
                'type = "s3"',
                "no destination 's3' (output/destinations/s3.md)",
                "editor",
            ),
            (
                'repo = "o/docs"\n',
                "",
                "<out-repo> is used in destinations/github.md but Parameters doesn't define "
                "it (output.repo is not set)",
                "editor",
            ),
            (
                'templates = ["note"]',
                'templates = ["memo"]',
                "missing file templates/memo.md",
                "writer",
            ),
        ):
            with self.subTest(new=new):
                self.configure(CONFIG.replace(old, new))
                self.assertEqual(self.fails(role), msg)

    def test_a_missing_guide(self):
        os.remove(os.path.join(self.root, "guide.md"))
        self.assertEqual(self.fails(), "missing file guide.md")


class Anchors(Fake):
    def test_a_prompt_without_a_repeated_heading_has_no_id_line(self):
        for task in (None, "long-note"):
            self.assertNotIn("<a id", self.compose(task=task)[0], task)

    def test_a_repeated_heading_gets_its_top_level_titles_id(self):
        self.write({"output/output.md": "# Output\n\n## Style\n\nShort.\n"})
        prompt, _ = self.compose()
        self.assertEqual(prompt.count("<a id"), 1)
        self.assertIn("\n## Style\n\n- `draft`: not a task.\n", prompt)
        self.assertIn('\n<a id="output-style"></a>\n## Style\n\nShort.\n', prompt)
        anchors = heading_anchors(prompt)
        self.assertEqual(len(anchors), len(set(anchors)))
        self.assertIn("output-style", anchors)

    def test_a_third_repeat_gets_a_suffix_on_the_qualified_id(self):
        self.write(
            {
                "output/output.md": "# Output\n\n## Style\n\nShort.\n",
                "output/destinations/local.md": "## Style\n\nKeep it local.\n",
            }
        )
        prompt, _ = self.compose()
        self.assertIn('<a id="output-style"></a>\n## Style\n\nShort.\n', prompt)
        self.assertIn('<a id="output-style-2"></a>\n## Style\n\nKeep it local.\n', prompt)
        anchors = heading_anchors(prompt)
        self.assertEqual(len(anchors), len(set(anchors)))

    def test_a_link_to_a_repeated_heading_resolves_by_its_qualified_id(self):
        self.write({"output/output.md": "# Output\n\n## Style\n\nShort.\n"})
        own = own_text(self.compose()[0])
        for text, want in (
            ("[Output › Style](#output-style)", True),
            ("[Writer › Style](#style)", True),
            ("[Output › Style](#style)", False),
            ("[Writer › Style](#output-style)", False),
        ):
            self.assertEqual(reference_errors(f"{own}{text}\n", {}, {}) == [], want, text)

    def test_a_role_heading_input_makes_the_input_heading_input_2(self):
        self.write({"roles/writer.md": WRITER + "\n## Input\n\nWhat you get.\n"})
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
        self.write({"templates/note.md": "# Output\n\n## Style\n"})
        prompt, _ = self.compose()
        self.assertIn("```markdown\n# Output\n\n## Style\n```", prompt)
        self.assertNotIn("<a id", prompt)

    def test_a_role_heading_the_return_repeats_makes_the_returns_unique(self):
        self.write({"roles/writer.md": WRITER + "\n## Return\n\nBack.\n"})
        prompt, _ = self.compose()
        self.assertIn(
            '\n<a id="output-return"></a>\n## Return\n\nYour replies carry your reports:\n', prompt
        )


class Parameters(Fake):
    GITHUB = TEXTS["output/destinations/github.md"]

    def test_the_names_in_order_and_no_report(self):
        self.assertEqual(
            list(PARAMETERS),
            [
                "<Workdir>",
                "<tasks>",
                "<team>",
                "<gate>",
                "<out-repo>",
                "<out-branch>",
                "<out-dir>",
                "<out-host>",
            ],
        )

    def test_the_rule_opens_the_section_then_the_workdir_and_tasks(self):
        prompt = self.compose()[0]
        self.assertIn(f"\n# Parameters\n\n{RULE}\n\n- `<Workdir>`: `/w`\n", prompt)
        self.assertEqual(
            list(parameters(prompt).items()),
            [("<Workdir>", "`/w`"), ("<tasks>", f"`{self.root}/tasks`")],
        )

    def test_scripts_is_no_name(self):
        self.write({"principles.md": "# Principles\n\nRun `<scripts>/x.py`.\n"})
        prompt = self.compose()[0]
        self.assertIn("Run `<scripts>/x.py`.", prompt)
        self.assertEqual(list(parameters(prompt)), ["<Workdir>", "<tasks>"])

    def test_a_task_file_using_a_name_its_run_cannot_define(self):
        self.write({"tasks/long-note.md": "# Long Note\n\nOpen `<out-repo>`.\n"})
        self.assertEqual(
            self.fails(),
            "<out-repo> is used in tasks/long-note.md but Parameters doesn't define it "
            "(output.repo is not set)",
        )

    def test_team_used_by_name_in_the_role(self):
        self.write({"roles/writer.md": WRITER + "\nKnow `<team>`.\n"})
        names = parameters(self.compose()[0])
        self.assertEqual(list(names), ["<Workdir>", "<tasks>", "<team>"])
        self.assertEqual(names["<team>"], f"`{self.root}`")

    def test_gate_used_by_name_in_an_indexed_task_file_only(self):
        self.write({"tasks/short-note.md": "# Short Note\n\nRun `<gate>` first.\n"})
        self.assertEqual(parameters(self.compose()[0])["<gate>"], "`none`")
        self.assertEqual(
            parameters(self.compose(overlays=[{"gate": "make gate"}])[0])["<gate>"], "`make gate`"
        )
        self.assertNotIn("<gate>", parameters(self.compose("editor")[0]))

    def test_gate_used_in_a_method_file_a_text_names(self):
        self.write(
            {
                "tasks/long-note.md": "# Long Note\n\nFollow `<team>/methods/m.md`, `<team>/methods/gone.md`.\n",
                "methods/m.md": "# M\n\nRun `<gate>`.\n",
                "methods/other.md": "# O\n\n`<out-dir>`\n",
            }
        )
        names = parameters(self.compose()[0])
        self.assertEqual(list(names), ["<Workdir>", "<tasks>", "<team>", "<gate>"])
        self.write({"tasks/long-note.md": "# Long Note\n\nFollow `{{team}}/methods/m.md`.\n"})
        self.assertNotIn("<gate>", parameters(self.compose()[0]))

    def test_github_output_its_used_keys(self):
        names = parameters(self.compose("editor")[0])
        self.assertEqual(
            list(names.items())[2:], [("<out-repo>", "`o/docs`"), ("<out-branch>", "`main`")]
        )

    def test_an_unset_host_is_github_com(self):
        self.write(
            {"output/destinations/github.md": self.GITHUB + "Clone `<out-host>/<out-repo>`.\n"}
        )
        self.assertEqual(parameters(self.compose("editor")[0])["<out-host>"], "`github.com`")
        overlay = {
            "roles": {
                "editor": {
                    "output": {"type": "github", "repo": "o/d", "branch": "b", "host": "ghe.x"}
                }
            }
        }
        self.assertEqual(
            parameters(self.compose("editor", overlays=[overlay])[0])["<out-host>"], "`ghe.x`"
        )

    def test_another_used_output_key_unset(self):
        self.write({"output/destinations/github.md": self.GITHUB + "Into `<out-dir>`.\n"})
        self.assertIn("output.dir is not set", self.fails("editor"))

    def test_a_value_holding_backticks_is_inline_code(self):
        run = self.run_of("editor")
        prompt = render(
            replace(run, output={**run.output, "repo": "o/`d``x"}), PARAMS, team=self.found()
        )
        self.assertEqual(parameters(prompt)["<out-repo>"], "``` o/`d``x ```")


class Deliverable(Fake):
    DEST = {
        "output/destinations/local.md": "## Destination\n\nKeep it local.\nWrite it to `{{deliverable}}`.\n"
    }

    def test_a_destination_names_it_under_the_workdirs_name(self):
        self.write(self.DEST)
        prompt, _ = self.compose()
        self.assertIn("Keep it local.\nWrite it to `<Workdir>/out.md`.\n", prompt)
        prompt, _ = self.compose(out="/elsewhere/out.md")
        self.assertIn("Write it to `<Workdir>/tmp/deliverable.md`.", prompt)
        wd = os.path.join(os.path.realpath(self.tmp), "wd")
        os.makedirs(wd)
        os.symlink(wd, os.path.join(self.tmp, "via"))
        prompt, _ = self.compose(
            workdir=os.path.join(self.tmp, "via"), out=os.path.join(wd, "sub", "out.md")
        )
        self.assertIn("Write it to `<Workdir>/sub/out.md`.", prompt)

    def test_an_unfilled_placeholder_in_another_destination_line_fails(self):
        self.write(
            {
                "output/destinations/local.md": "## Destination\n\nWrite it to `{{deliverable}}`.\nSee {{dir}}.\n"
            }
        )
        self.assertEqual(self.fails(), "unfilled placeholder {{dir}} in destinations/local.md")


class TeamDirs(Fake):
    """Team dirs under the test's HOME, passed as a Team."""

    def with_dir(self, files, name="team"):
        """The Team of the team root and one team dir of `files`, ~/.agent-pm/<name>."""
        top = os.path.realpath(os.path.join(self.agent_pm, name))
        self.write(files, top)
        return team(self.root, (top,))

    def path(self, rel, name="team"):
        return os.path.join(os.path.realpath(os.path.join(self.agent_pm, name)), rel)

    def test_a_new_roles_charter_and_its_tasks_dir(self):
        found = self.with_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        prompt, _ = self.compose("poet", found=found)
        self.assertIn("\n# Poet\n\nRhyme.\n\n## Tasks\n\n" + INTRO.format("verse"), prompt)
        self.assertIn("- `verse` (`<tasks>/verse.md`): a verse; always; light.\n", prompt)
        self.assertEqual(parameters(prompt)["<tasks>"], f"`{self.path('tasks')}`")
        self.assertEqual(
            parameters(self.compose(found=found)[0])["<tasks>"], f"`{self.root}/tasks`"
        )

    def test_a_custom_template_keeps_its_heading(self):
        found = self.with_dir({"templates/card.md": "# Card\n"})
        self.local('[roles.writer]\ntemplates = ["note", "card"]\n')
        prompt, _ = self.compose(found=found)
        self.assertIn("# Template: `templates/note.md`\n", prompt)
        self.assertIn("# Template: `templates/card.md`\n\n```markdown\n# Card\n```\n", prompt)

    def test_a_reference_error_in_a_custom_file_names_it(self):
        task, charter = self.path("tasks/verse.md"), self.path("roles/poet.md")
        for verse, poet, msg in (
            (VERSE, POET + "\n[x](#nope)\n", f"{charter}: no such anchor: [x](#nope)"),
            (VERSE + "\n[x](#nope)\n", POET, f"{task}: no such anchor: [x](#nope)"),
        ):
            with self.subTest(msg=msg):
                found = self.with_dir({"roles/poet.md": poet, "tasks/verse.md": verse})
                self.assertEqual(self.fails("poet", found=found), msg)

    def test_a_custom_task_lacking_a_path_a_built_in_reference_needs_leads(self):
        steps = "\n## Steps\n\n1. **Write:** it.\n"
        self.write(
            {
                "principles.md": "# Principles\n\nSee `<tasks>/<task>.md` › Steps › Write.\n",
                "tasks/short-note.md": FILES["tasks/short-note.md"] + steps,
                "tasks/long-note.md": FILES["tasks/long-note.md"] + steps,
            }
        )
        self.compose()
        found = self.with_dir({"roles/poet.md": POET, "tasks/verse.md": VERSE})
        msg = self.fails("poet", found=found)
        self.assertIn("breaks a reference in principles.md: no such task file path: See", msg)
        self.assertTrue(
            msg.startswith(f"{self.path('tasks/verse.md')}: breaks a reference in "), msg
        )
        verse = VERSE.replace("## Resume", steps + "\n## Resume")
        found = self.with_dir({"roles/poet.md": POET, "tasks/verse.md": verse}, "fixed")
        self.compose("poet", found=found)

    def test_a_custom_heading_a_built_in_text_names_in_parentheses_leads(self):
        self.write({"principles.md": "# Principles\n\nKeep it short (Rhyme).\n"})
        self.compose()
        found = self.with_dir(
            {
                "roles/poet.md": POET.replace("## Tasks", "## Rhyme\n\nWell.\n\n## Tasks"),
                "tasks/verse.md": VERSE,
            }
        )
        self.assertEqual(
            self.fails("poet", found=found),
            f"{self.path('roles/poet.md')}: breaks a reference in "
            "principles.md: bare reference: Keep it short (Rhyme).",
        )

    def test_an_input_heading_in_a_custom_text_leaves_the_check_the_whole_prompt(self):
        found = self.with_dir(
            {
                "roles/poet.md": POET + "\n# Input\n\nSee [the output](#output).\n",
                "tasks/verse.md": VERSE,
            }
        )
        self.assertIn("See [the output](#output).\n", self.compose("poet", found=found)[0])
        found = self.with_dir(
            {"roles/poet.md": POET + "\n# Input\n\nSee [x](#nope).\n", "tasks/verse.md": VERSE},
            "bad",
        )
        self.assertEqual(
            self.fails("poet", found=found),
            f"{self.path('roles/poet.md', 'bad')}: no such anchor: [x](#nope)",
        )

    def test_without_a_team_dir_for_this_role_every_prompt_is_unchanged(self):
        other = self.with_dir(
            {"roles/poet.md": POET, "tasks/verse.md": VERSE, "templates/card.md": "# Card\n"}
        )
        for role in ("writer", "editor"):
            for task in (None, *index(role, team(self.root), self.config, self.local_file)):
                with self.subTest(role=role, task=task):
                    self.assertEqual(
                        self.compose(role, task, found=other)[0], self.compose(role, task)[0]
                    )


class References(Case):
    def errors(self, text):
        """The errors in `text` as a method file beside the real engineer prompt and task files."""
        own, tasks, methods = role_sources("engineer")
        return [
            e
            for e in reference_errors(own, tasks, methods | {"fixture": text})
            if e.startswith("fixture:")
        ]

    def test_a_link_to_a_path_of_the_prompt_and_a_task_file_reference_pass(self):
        for text in (
            "[Engineer › Repo › Merge](#repo), then",
            "[Engineer › Repo](#repo) step 1",
            "[Principles](#principles)",
            "[Principles › Work › Worktree](#work)",
            "[Engineer › Finish › Failure](#finish)",
            "[Output › Return](#return)",
            "([Engineer › Rules](#rules)' rule)",
            "(`<tasks>/<task>.md` › Steps › Which build picks the build)",
            "`<tasks>/build.md` › Steps",
            "[docs](https://example.com/a)",
            "[A › B](https://example.com/a)",
            "`Engineer › Repo` and ``a ` › b``",
            "```\nEngineer › Repo\n[x](../y.md)\n```",
        ):
            self.assertEqual(self.errors(text), [], text)

    def test_a_wrong_reference_is_an_error(self):
        for text in (
            "[Engineer › Nope](#nope)",
            "Engineer › Repo",
            "[Engineer › Repo](#finish)",
            "[x](../roles/engineer.md#repo)",
            "[x]()",
            "[Principles › Worktree](#work)",
            "[Engineer › Repo step 1](#repo)",
            "[Engineer › Repo](#repo) › Merge",
            "`<tasks>/<task>.md` › Steps › Nope",
            "`<tasks>/<task>.md` › Steps › Which builds",
            "`<tasks>/<task>.md` › Steps › Which build › Nope",
            "`<tasks>/<task>.md` › Nope",
            "`<tasks>/nope.md` › Steps",
            "Which build › Steps",
            "```\n```\nEngineer › Repo",
        ):
            self.assertTrue(self.errors(text), text)

    def test_errors_name_the_file_and_the_offender(self):
        errors = reference_errors(
            "# R\n\n## S\n\nR › S\n", {"x": "# X\n\n[A](b.md)\n"}, {"m.md": "[A › B](#nope)\n"}
        )
        self.assertEqual(
            errors,
            [
                "prompt: bare reference: R › S",
                "tasks/x.md: relative link target: [A](b.md)",
                "m.md: no such anchor: [A › B](#nope)",
            ],
        )

    def test_a_task_file_path_must_exist_in_each_task_file_named(self):
        tasks = {"a": "# A\n\n## Steps\n\n1. **Go:** x\n", "b": "# B\n\n## Steps\n"}
        for text, want in (
            ("`<tasks>/<task>.md` › Steps", True),
            ("`<tasks>/<task>.md` › Steps › Go", False),
            ("`<tasks>/a.md` › Steps › Go", True),
            ("`<tasks>/b.md` › Steps › Go", False),
        ):
            self.assertEqual(reference_errors("# R\n", tasks, {"m": text}) == [], want, text)

    def test_a_method_file_path_must_exist_in_that_method_file(self):
        methods = {"methods/a.md": "# A\n\n## Rules\n\n- **Voting**: x\n"}
        for text, want in (
            ("`<team>/methods/a.md` › Rules › Voting", True),
            ("`<team>/methods/a.md` › Rules", True),
            ("`<team>/methods/a.md` › Rules › Nope", False),
            ("`<team>/methods/b.md` › Rules", False),
            ("`<team>/methods/<task>.md` › Rules", False),
        ):
            self.assertEqual(reference_errors("# R\n", {}, methods | {"m": text}) == [], want, text)

    def test_a_section_name_of_the_prompt_in_parentheses_is_a_bare_reference(self):
        for text in (
            "decide it (Repo).",
            "(Merge)",
            "see (Finish) first",
            "(Worktree)",
            "(Autopilot)",
        ):
            self.assertTrue(self.errors(text), text)
        for text in (
            "(local, mixed)",
            "([Engineer › Repo](#repo))",
            "(`Repo`)",
            "(the Repo)",
            "(Repo step 1)",
            "```\n(Repo)\n```",
        ):
            self.assertEqual(self.errors(text), [], text)

    def test_texts_checks_only_the_given_texts_against_owns_anchors(self):
        own = "# R\n\n## S\n\nR › S\n"
        errors = reference_errors(own, {}, {}, texts={"a": "[x](#s)\n", "b": "[y](#nope)\n"})
        self.assertEqual(errors, ["b: no such anchor: [y](#nope)"])

    def test_labels_relabel_a_tasks_errors(self):
        errors = reference_errors(
            "# R\n", {"x": "[A](b.md)\n", "y": "[B](c.md)\n"}, {}, labels={"x": "/t/x.md"}
        )
        self.assertEqual(
            errors,
            [
                "/t/x.md: relative link target: [A](b.md)",
                "tasks/y.md: relative link target: [B](c.md)",
            ],
        )


class Methods(Case):
    def test_voting_has_its_home_in_ultracode_and_pipeline_shares_its_start(self):
        deep, ultra = (method(m) for m in METHOD_NAMES)
        for m, text in zip(METHOD_NAMES, (deep, ultra), strict=False):
            for name in ("Voting", "Pipeline"):
                self.assertEqual(len(rule(text, name)), 1, f"{m}: {name}")
            self.assertTrue(
                rule(text, "Pipeline")[0].startswith(
                    "- **Pipeline**: ≤ 10 subagents running at once. Each result of a stage goes to the next stage as soon "
                    "as it arrives, never in batches;"
                ),
                m,
            )
        self.assertEqual(
            rule(deep, "Voting"), ["- **Voting**: `<team>/methods/ultracode.md` › Rules › Voting."]
        )
        self.assertTrue(
            rule(ultra, "Pipeline")[0].endswith(
                "only verification waits for all claims, to rank them."
            )
        )
        for phrase in (
            "fetch selection waits for all search results",
            "verification waits for all claims",
        ):
            self.assertIn(phrase, rule(deep, "Pipeline")[0])

    def test_each_states_voting_pipeline_restrictions_and_its_own_limits(self):
        voting = (
            "≥ 2 refutes → refuted",
            "else ≥ 2 valid votes → confirmed",
            "else (agent errors, missing votes) → unverified",
            "votes that came back",
            "unsure → votes refuted",
        )
        both = (
            "≤ 10 subagents running at once",
            "verification waits for all claims",
            "the calling task's restrictions for",
            "into every subagent prompt, voters included",
        )
        own = {
            "deep-research": (
                "5 complementary web search angles",
                "after every search agent has returned",
                "rank the whole set by relevance (high → low)",
                "dispatch fetches for the first ≤ 15",
                "top 25",
                "≤ 100",
                "start each web agent with fresh context (no inherited conversation), so it sees "
                "only its prompt",
                "only from the brief and web results",
                "Dispatch fetches only for URLs a search agent returned",
                "**Page text**:",
            ),
            "ultracode": voting
            + (
                "A fixed method for the ultracode round",
                "room in the cap for the key claims' votes",
                "while ≥ 3 cap slots remain",
                "undispatched subquestions and unverified claims go under Gaps",
            ),
        }
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
        with open(os.path.join(TEAM, "roles", "researcher.md")) as f:
            self.assertIn(home, f.read())
        link = "private detail ([Researcher › Type and target › Agents](#type-and-target))"
        self.assertIn(link, task_text("deep-research"))
        self.assertIn(
            "private detail per [Researcher › Type and target › Agents](#type-and-target)",
            method("deep-research"),
        )
        for text in (task_text("deep-research"), method("deep-research")):
            self.assertNotIn("internal names", text)

    def test_the_researcher_charter_and_methods_are_harness_neutral(self):
        with open(os.path.join(TEAM, "roles", "researcher.md")) as f:
            texts = {"roles/researcher.md": (f.read(), ())} | {
                m: (method(m), ("{{",)) for m in METHOD_NAMES
            }
        for name, (text, more) in texts.items():
            for word in ("Read, Grep", "Glob", "Workflow tool", "journal.jsonl", *more):
                with self.subTest(name, word=word):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
