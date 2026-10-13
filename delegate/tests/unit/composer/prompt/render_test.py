"""Render behavior."""

import inspect
import os
import re
from dataclasses import replace

from agent_pm.delegate.composer.prompt import (
    OUTCOME,
    PARAMETERS,
    RESUME,
    RETURN,
    RULE,
    render,
)

from delegate.tests.unit.composer.prompt.case import PARAMS, TEXTS, Fake
from delegate.tests.unit.composer.prompts import (
    parameters,
)
from delegate.tests.unit.composer.teams import WRITER


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
