"""Shipped behavior."""

import glob
import hashlib
import os
import re
import tempfile
import tomllib
import types

from agent_pm.delegate import composer
from agent_pm.delegate.composer import ConfigSource
from agent_pm.delegate.composer.base import outline
from agent_pm.delegate.composer.prompt import RETURN, RULE, reference_errors
from agent_pm.delegate.composer.run import check_old_keys
from agent_pm.delegate.composer.teams import START
from agent_pm.delegate.engine.config import CONFIG
from agent_pm.delegate.event.outcome import SCHEMA
from agent_pm.utils import unfenced

from delegate.tests.case import REPO, Case
from delegate.tests.unit.composer.prompts import (
    RUN,
    composed,
    role_sources,
    section,
    task_text,
)
from delegate.tests.unit.composer.real.case import (
    API,
    DROPPED,
    LANGUAGE_RULE,
    RUNTIME,
    SCHEMA_SHA256,
    TASKS,
    TICKET,
    TICKET_EXAMPLES,
    WORDS,
    catalog,
    generic_names,
    role_tasks,
    roles,
    runs,
    team_texts,
    tickets,
    words,
)
from delegate.tests.unit.composer.teams import INTRO


class Package(Case):
    def test_the_api_is_the_modules_names(self):
        self.assertEqual(composer.__all__, API)
        self.assertEqual([name for name in DROPPED if hasattr(composer, name)], [])
        self.assertIs(composer.render, composer.prompt.render)
        self.assertIs(composer.resolve, composer.run.resolve)
        self.assertIsInstance(composer.teams, types.ModuleType)
        self.assertIs(composer.team, composer.teams.team)
        self.assertIs(composer.team_paths, composer.teams.team_paths)
        self.assertEqual(CONFIG, os.path.join(REPO, "delegate", "config.toml"))


class Acceptance(Case):
    """The shipped config alone (no local file)."""

    def test_every_role_and_task_composes_with_no_reference_error(self):
        self.assertEqual(roles(), ["researcher", "pm", "engineer", "dummy-tester"])
        for role, task in runs():
            with self.subTest(role=role, task=task):
                self.assertEqual(reference_errors(*role_sources(role, task)), [])

    def test_each_prompt_returns_in_replies_with_neutral_return_and_parameters(self):
        self.assertNotRegex(RETURN, RUNTIME)
        for role, task in runs():
            with self.subTest(role=role, task=task):
                prompt = words(composed(role, task)[0])
                self.assertIn("agent-pm-outcome", prompt)
                self.assertNotIn("report.py", prompt)
                # The report command was a code span opening with `report`; guide.md says how to
                # report progress.
                self.assertNotRegex(prompt, r"`report[` ]|(?<!how to )report progress")
                self.assertIn(f"\n{RETURN}\n# Input\n", prompt)
                block = re.search(r"^# Parameters\n(.*?)(?=^# )", prompt, re.M | re.S)[1]
                self.assertTrue(block.startswith(f"\n{RULE}\n\n"))
                self.assertNotRegex(block, RUNTIME)

    def test_each_requested_command_runs_the_teams_repo_tool(self):
        self.assertTrue(os.path.isfile(os.path.join(composer.TEAM, "tools", "repo.py")))
        for role in roles():
            for command in composed(role)[1].commands:
                self.assertTrue(
                    command.startswith("python3 -P {{team}}/tools/repo.py "), (role, command)
                )

    def test_local_and_orchestrator_destinations_name_deliverable_file(self):
        for dest in ("local", "orchestrator"):
            with self.subTest(dest=dest):
                prompt = composed(
                    "researcher", overlays=[{"roles": {"researcher": {"output": {"type": dest}}}}]
                )[0]
                destination = section(prompt, "Destination")
                self.assertIn(
                    (
                        "Write the deliverable to `<Workdir>/out.md` and name that file in "
                        "the outcome's `deliverable_file`"
                    ),
                    destination,
                )
                self.assertNotIn("outcome's `deliverable`", destination)
                self.assertIn("Leave `url` empty.", destination)
                self.assertEqual(prompt.count("`<Workdir>/out.md`"), 1)
                self.assertIn(
                    (
                        "\n- `deliverable_file`: a `.md` file in `<Workdir>` holding the "
                        "deliverable, in place of `deliverable`, when [Output › "
                        "Destination](#destination) says so or the deliverable is "
                        "long.\n"
                    ),
                    prompt,
                )

    def test_each_task_file_has_one_start_mark_and_a_resume_section(self):
        for name in os.listdir(TASKS):
            text = task_text(name.removesuffix(".md"))
            self.assertEqual(len(START.findall(text)), 1, name)
            self.assertIn("## Resume", [line.rstrip() for _, line in unfenced(text)], name)

    def test_the_outcome_schema_is_mains(self):
        with open(SCHEMA, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), SCHEMA_SHA256)

    def test_no_language_no_language_rule(self):
        with open(CONFIG) as f:
            cfg = f.read()
        self.assertIn('\nlanguage = "English"\n', cfg)
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.toml")
            with open(config, "w") as f:
                f.write(cfg.replace('\nlanguage = "English"\n', "\n"))
            for role in roles():
                run = composer.resolve(role, source=ConfigSource(path=config, local=os.devnull))
                prompt = composer.render(run, RUN)
                self.assertEqual(run.language, "", role)
                self.assertNotIn(LANGUAGE_RULE, prompt, role)
                self.assertNotIn("are in English", prompt, role)


class Shipped(Case):
    """The team's text, the core config's comments and the composer's strings."""

    def test_tickets_finds_an_id_beyond_the_examples(self):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in {
                "roles/x.md": "when asked, e.g. a wording tweak like TASK-300; light.",
                "principles.md": "the id (e.g. `TASK-142`); twice (e.g. `TASK-142`)",
                "output/output.md": "the id (e.g. `TASK-142`)",
            }.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w") as f:
                    f.write(text)
            self.assertEqual(
                tickets(root),
                {
                    "roles/x.md": ["TASK-300"],
                    "principles.md": ["TASK-142"],
                    "output/output.md": ["TASK-142"],
                },
            )

    def test_no_ticket_id_but_the_format_examples(self):
        self.assertEqual(tickets(composer.TEAM), {})
        for rel, examples in TICKET_EXAMPLES.items():
            with open(os.path.join(composer.TEAM, rel)) as f:
                text = f.read()
            for example in examples:
                self.assertEqual(text.count(example), 1, (rel, example))
        with open(CONFIG) as f:
            self.assertEqual(TICKET.findall(f.read()), [])
        for role, task in runs():
            prompt = words(composed(role, task)[0])
            self.assertEqual(
                TICKET.findall(prompt.replace("(e.g. `TASK-142`)", "", 1)), [], (role, task)
            )

    def test_no_orchestration_or_tracker_words(self):
        texts = team_texts() | {
            f"{role} {task}": words(composed(role, task)[0]) for role, task in runs()
        }
        composer_py = glob.glob(os.path.join(os.path.dirname(composer.__file__), "*.py"))
        for path in [CONFIG, SCHEMA, *composer_py]:
            with open(path) as f:
                texts[path] = f.read()
        for label, text in texts.items():
            for word in WORDS:
                self.assertNotIn(word, text, f"{label}: {word}")

    def test_a_plugin_skill_without_its_slash_or_the_x_skill_is_caught_and_slash_commands_pass(
        self,
    ):
        for text, want in (
            (
                "run autopilot:build\nthen `agent-pm:tmux`\nor x/agent-pm:act-as",
                ["run autopilot:build", "then `agent-pm:tmux`"],
            ),
            (
                "the act-as skill\nthe tmux skill's workers\nthe skill",
                ["the act-as skill", "the tmux skill's workers"],
            ),
            ("/autopilot:build, `/agent-pm:tmux`'s workers, /deep-research", []),
        ):
            with self.subTest(text=text):
                self.assertEqual(generic_names(text), want)

    def test_config_comments_name_skills_by_slash_command(self):
        with open(CONFIG) as f:
            self.assertEqual(generic_names("\n".join(re.findall(r"#.*", f.read()))), [])

    def test_text_holds_only_the_placeholders_render_fills(self):
        inline = {"role", "role_anchor", "task", "language", "deliverable"}
        for rel, text in team_texts().items():
            if rel.endswith(".md"):
                self.assertLessEqual(set(re.findall(r"\{\{(\w+)", text)), inline, rel)

    def test_local_lines_uncommented_are_a_valid_local_file(self):
        with open(CONFIG) as f:
            local = tomllib.loads(
                "".join(
                    line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
                )
            )
        self.assertLessEqual({"show", "cwd", "users", "trusted_dirs", "team_dirs"}, set(local))
        self.assertLessEqual({"researcher", "pm"}, set(local["roles"]))
        for role, table in local["roles"].items():
            check_old_keys(table, role)

    def test_each_charter_opens_with_its_index_naming_a_listed_default(self):
        defaults = {
            "researcher": "deep-research",
            "pm": "product-design",
            "engineer": "build",
            "dummy-tester": "echo",
        }
        self.assertEqual(set(roles()), set(defaults))
        for role, default in defaults.items():
            with open(os.path.join(composer.TEAM, "roles", f"{role}.md")) as f:
                text = f.read()
            self.assertTrue(
                text.split("\n## ")[1].startswith(f"Tasks\n\n{INTRO.format(default)}- `"), role
            )
            self.assertIn(default, catalog()[role], role)
            for t, line in catalog()[role].items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", text, role)
                self.assertRegex(line, r"; (light|heavy)\.$", role)
        self.assertEqual(
            sorted(t for _, t in role_tasks()),
            sorted(f.removesuffix(".md") for f in os.listdir(TASKS)),
        )

    def test_task_files_have_no_frontmatter_and_no_placeholder(self):
        for name in os.listdir(TASKS):
            text = task_text(name.removesuffix(".md"))
            self.assertTrue(text.startswith("# "), name)
            self.assertNotIn("{{", text, name)
            self.assertNotIn("agent-pm-progress:budget", text, name)

    def test_engineers_tasks_name_their_skill_by_slash_command(self):
        for t in ("build", "light-build"):
            self.assertIn(f" with `/autopilot:{t}` (", catalog()["engineer"][t])
            self.assertIn(f"`/autopilot:{t}`", task_text(t))
            self.assertNotRegex(task_text(t), r"(?<!/)autopilot:")

    def test_echo_summary_overrides_outputs_length(self):
        self.assertIn(
            "`summary` the input's first line, not [Output](#output)'s 3–5 lines.",
            task_text("echo"),
        )

    def test_the_engineer_charter_has_six_sections_and_autopilot_adds_its_lines_verbatim(self):
        with open(os.path.join(composer.TEAM, "roles", "engineer.md")) as f:
            charter = f.read()
        heads = [path[1] for _, path, _, heading in outline(charter) if heading and len(path) == 2]
        self.assertEqual(
            heads, ["Tasks", "Rules", "Input processing", "Repo", "Autopilot", "Finish"]
        )
        lines = (
            (
                "Work only in `<worktree>` on branch `<branch>`, with absolute paths; "
                "create no other clone, worktree or branch."
            ),
            "Commits merged from `origin/<base>` are not this build's work.",
            "Skip S7; keep the commits. After <your task's push points>, run exactly "
            "`git -C <worktree> push -u origin <branch>`.",
        )
        self.assertIn(
            "\nAdd verbatim, placeholders filled in:\n\n```\n" + "\n".join(lines) + "\n```\n",
            section(charter, "Autopilot"),
        )

    def test_a_builds_files_are_its_spec_and_plan_doc(self):
        self.assertIn(
            (
                "adding `files`: this build's spec (its plan doc's `spec_file=`) and plan "
                "doc (on failure, those that exist);"
            ),
            task_text("build"),
        )

    def test_builds_report_each_s5_task_against_their_task_count(self):
        for task, doc in (("build", "the plan doc's"), ("light-build", "the state file's")):
            text = task_text(task)
            self.assertIn(
                (
                    "**Report progress** at the end of each build step (`S<i>`) you run "
                    "and of each S5 task:"
                ),
                text,
                task,
            )
            self.assertIn(
                (
                    f"[agent-pm-progress:step] the step and its result; an S5 task as `S5 "
                    f"task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` "
                    f"{doc}"
                    f" task count (its `### Task` headings)"
                ),
                text,
                task,
            )

    def test_light_build_always_writes_a_state_file_with_a_task_list(self):
        self.assertIn(
            (
                'docs line "The state file goes where the repo keeps design docs, else in '
                '`docs/.autopilot/`; never `git add -f` it."'
            ),
            task_text("light-build"),
        )
        self.assertIn(
            (
                "(a task list in its state file, implementation, verification, a light "
                "review, no spec or plan docs)"
            ),
            catalog()["engineer"]["light-build"],
        )

    def test_light_research_prepares_before_its_start_mark(self):
        text = task_text("light-research")
        self.assertLess(text.index("**Prepare**"), text.index("[agent-pm-progress:start]"))
