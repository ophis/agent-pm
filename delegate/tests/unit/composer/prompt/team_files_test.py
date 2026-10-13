"""Team files behavior."""

import os

from agent_pm.delegate.composer.prompt import (
    reference_errors,
)
from agent_pm.delegate.composer.teams import index, team

from delegate.tests.unit.composer.prompt.case import Fake
from delegate.tests.unit.composer.prompts import (
    heading_anchors,
    own_text,
    parameters,
)
from delegate.tests.unit.composer.teams import FILES, INTRO, POET, RUN_CONFIG, VERSE, WRITER


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
                self.configure(RUN_CONFIG.replace(old, new))
                self.assertEqual(self.fails(role), msg)

    def test_a_missing_guide(self):
        os.remove(path := os.path.join(self.root, "guide.md"))
        self.assertEqual(self.fails(), f"{path}: No such file or directory")


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
