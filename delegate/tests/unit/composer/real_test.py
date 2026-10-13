"""On the real team root (composer.TEAM) and delegate/config.toml: every built-in role × task composes with no reference
error, the neutral Return and Parameters, the FR-31 destinations, the package API, the schema left as on main, and the
shipped text's rules (ported from the old composer's RealCore, Tickets and GenericNames tests, less the client-specific
ones)."""

import functools
import glob
import hashlib
import os
import re
import tempfile
import tomllib
import types
import unittest

from agent_pm.delegate import composer
from agent_pm.delegate.composer.base import anchor, fill, outline
from agent_pm.delegate.composer.prompt import RETURN, RULE, reference_errors
from agent_pm.delegate.composer.run import check_old_keys
from agent_pm.delegate.composer.teams import START
from agent_pm.delegate.engine.config import CONFIG
from agent_pm.delegate.event.outcome import SCHEMA
from agent_pm.utils.markdown import unfenced

from delegate.tests.case import REPO, ROOTS, Case
from delegate.tests.unit.composer.prompts import (
    RUN,
    composed,
    heading_anchors,
    item,
    parameters,
    role_sources,
    rule,
    section,
    task_text,
)
from delegate.tests.unit.composer.teams import INTRO

TASKS = os.path.join(composer.TEAM, "tasks")
# outcome.schema.json at main 3e5f9f3
SCHEMA_SHA256 = "748847763f62d05e4412f1627a029ad88e42f004162361561d7556ec3877c366"
RUNTIME = re.compile(r"claude|codex", re.I)
# The old tests' local file: reports in Chinese, the researcher's and pm's deliverables on GitHub.
FIXTURE = """\
language = "Chinese"

[roles.researcher.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Research/"

[roles.pm.output]
type = "github"
repo = "ophis/private_docs"
branch = "main"
dir = "Product Design/"
"""
API = [
    "ConfigError",
    "TEAM",
    "DELIVERABLE",
    "RunConfig",
    "RunParams",
    "Team",
    "team",
    "team_dirs",
    "index",
    "lookup",
    "check_all",
    "catalog",
    "check_team_dirs",
    "check_overlap",
    "team_paths",
    "resolve",
    "trusted_dirs",
    "access",
    "render",
]
DROPPED = [
    "SRC",
    "SCHEMA",
    "PROGRESS",
    "OUTCOME",
    "CONFIG",
    "Intent",
    "roles",
    "tasks_path",
    "reference_errors",
    "bind",
    "fill",
    "outline",
    "unfenced",
    "anchor",
]
TICKET = re.compile(r"TASK-\d+")  # an internal ticket id
# The format examples the team's text may hold: path in the team root → its examples, each allowed once.
TICKET_EXAMPLES = {"principles.md": ["(e.g. `TASK-142`)"]}
WORDS = (
    "router.py",
    "research.py",
    "usage.py",
    "eng.py",
    "Linear",
    "In Review",
    "Todo",
    "Handoff",
    "issue",
)
GENERIC_NAME = re.compile(r"(?<![/\w])(agent-pm|autopilot):[\w-]+|\bthe [\w-]+ skill\b")
LANGUAGE_RULE = "headings and fixed labels included"
REPO_ARGS = {
    "worktree": "--dir <Workdir>/src --branch <branch> [--name <checkout>] [--base <Base:>] <repo>",
    "status": "--dir <Workdir>/src --branch <branch> [--name <checkout>] [--base <Base:>] <repo>",
}
CHECKOUT_RULE = (
    "- **Checkout**: `[--name <checkout>]` in a command → `--name <checkout>`, `<checkout>` the input's "
    "`Checkout:`; no `Checkout:` → drop it."
)
PICK = (
    "**Your task**: pick it from your charter's Tasks section as its opening sentence says; a task the input names "
    "wins. Read that task's file (no other task's), `<tasks>/<task>.md`, and follow its steps in order; its links "
    "(`#…`) point to sections of this prompt."
)
COMMANDS_RULE = (
    "- **Commands**: run each command this prompt gives exactly, written as Parameters says, as its own "
    "command (no `cd`, pipe, redirect or `&&`)."
)


@functools.cache
def catalog() -> dict[str, dict[str, str]]:
    """The built-in team's catalog with the shipped config alone."""
    return composer.catalog(config=CONFIG, local=os.devnull)


def roles() -> list[str]:
    return list(catalog())


def role_tasks() -> list[tuple[str, str]]:
    return [(r, t) for r in roles() for t in catalog()[r]]


def runs() -> list[tuple[str, str | None]]:
    """Each role given no task, then each role and task."""
    return [(r, None) for r in roles()] + role_tasks()


def tickets(root):
    """{path relative to `root`: its ticket ids beyond TICKET_EXAMPLES} of each file under `root`."""
    found = {}
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in (n for n in files if not n.endswith(".pyc")):
            rel = os.path.relpath(os.path.join(top, name), root)
            with open(os.path.join(top, name), errors="replace") as f:
                text = f.read()
            for example in TICKET_EXAMPLES.get(rel, ()):
                text = text.replace(example, "", 1)
            if ids := TICKET.findall(text):
                found[rel] = ids
    return found


def generic_names(text):
    return [line for line in text.splitlines() if GENERIC_NAME.search(line)]


def expanded(prompt):
    """`prompt` with each # Parameters name written as its value, as the rule says a run writes a command."""
    for name, value in parameters(prompt).items():
        prompt = prompt.replace(name, value.strip("`"))
    return prompt


def words(prompt):
    """`prompt` with this checkout's path, the machine's and not the team's text, written as <root>."""
    return prompt.replace(REPO, "<root>")


def guide(prompt):
    return prompt.split("\n# Principles\n", 1)[0]


def parameter_names(role, task=None, **changes):
    return list(parameters(composed(role, task, **changes)[0]))


def team_texts():
    """{path relative to the team root: text} of each .md file of the real team root (its tools/ are code)."""
    out = {}
    for path in glob.glob(os.path.join(composer.TEAM, "**", "*.md"), recursive=True):
        with open(path) as f:
            out[os.path.relpath(path, composer.TEAM)] = f.read()
    return out


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
                # The report command was a code span opening with `report`; guide.md says how to report progress.
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
                    "Write the deliverable to `<Workdir>/out.md` and name that file in the outcome's "
                    "`deliverable_file`",
                    destination,
                )
                self.assertNotIn("outcome's `deliverable`", destination)
                self.assertIn("Leave `url` empty.", destination)
                self.assertEqual(prompt.count("`<Workdir>/out.md`"), 1)
                self.assertIn(
                    "\n- `deliverable_file`: a `.md` file in `<Workdir>` holding the deliverable, in place "
                    "of `deliverable`, when [Output › Destination](#destination) says so or the deliverable "
                    "is long.\n",
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
                run = composer.resolve(role, config=config, local=os.devnull)
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
            "Work only in `<worktree>` on branch `<branch>`, with absolute paths; create no other clone, worktree "
            "or branch.",
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
            "adding `files`: this build's spec (its plan doc's `spec_file=`) and plan doc (on failure, those "
            "that exist);",
            task_text("build"),
        )

    def test_builds_report_each_s5_task_against_their_task_count(self):
        for task, doc in (("build", "the plan doc's"), ("light-build", "the state file's")):
            text = task_text(task)
            self.assertIn(
                "**Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:",
                text,
                task,
            )
            self.assertIn(
                "[agent-pm-progress:step] the step and its result; an S5 task as "
                f"`S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` {doc} task count "
                "(its `### Task` headings)",
                text,
                task,
            )

    def test_light_build_always_writes_a_state_file_with_a_task_list(self):
        self.assertIn(
            'docs line "The state file goes where the repo keeps design docs, else in `docs/.autopilot/`; '
            'never `git add -f` it."',
            task_text("light-build"),
        )
        self.assertIn(
            "(a task list in its state file, implementation, verification, a light review, no spec or plan "
            "docs)",
            catalog()["engineer"]["light-build"],
        )

    def test_light_research_prepares_before_its_start_mark(self):
        text = task_text("light-research")
        self.assertLess(text.index("**Prepare**"), text.index("[agent-pm-progress:start]"))


class RealCore(Case):
    """The real core with FIXTURE as the local file."""

    def setUp(self):
        self.local(FIXTURE)

    def local(self, text):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(text)

    def test_the_config_renders_the_language_and_progress_rules_once(self):
        for role in roles():
            prompt, run = composed(role)
            self.assertEqual(run.language, "Chinese")
            self.assertEqual(prompt.count(LANGUAGE_RULE), 1, role)
            self.assertIn("are in Chinese", prompt, role)
            self.assertEqual(prompt.count("is a point to tell the user your progress"), 1, role)

    def test_fixed_labels_are_translated_and_template_headings_say_so(self):
        self.assertIn(
            "- Reports and PRDs, headings and fixed labels included, are in Chinese; a `<X, translated>` in "
            "a template or task is X in Chinese.",
            composed("researcher")[0],
        )
        for role, task in role_tasks():
            text = composed(role, task)[0]
            for literal in ("`Check:`", "Check:", "Confidence:", "`Light Research. Angles: "):
                self.assertNotIn(literal, text, (role, task, literal))
        for role, task in runs():
            self.assertIn(
                "Its headings are fixed apart from their language ([Principles › Writing](#writing))",
                guide(composed(role, task)[0]),
                (role, task),
            )

    def test_writing_has_the_one_gloss_and_diagram_rules_and_no_length_cap(self):
        for role, task in runs():
            prompt = composed(role, task)[0]
            writing = section(prompt, "Writing")
            self.assertNotIn("the first mention adds the", prompt, (role, task))
            self.assertIn(
                "A term's first mention adds one gloss, its Chinese rendering (for an acronym, of its full "
                "name); none when the term is common in Chinese, and none in a heading.",
                writing,
            )
            diagrams = item(writing, "Diagrams and tables")
            for literal in (
                "a Mermaid diagram",
                "`flowchart`",
                "`sequenceDiagram`",
                "`timeline`",
                "short node labels",
                "a table",
                "a list",
                "a caption",
                "delete the text the diagram replaces",
            ):
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
        self.assertIn(
            "`- FR-<n>: <the requirement>` on one line, then one indented sub-item "
            "`  - <Check, translated>: ",
            pm,
        )
        self.assertIn("Opens with one sentence", section(light, "Conclusion and recommendation"))
        self.assertIn(point, section(light, "Comparison table"))
        findings = section(light, "Findings")
        self.assertIn(point, findings)
        self.assertIn(
            "`(<confidence: high, medium or low>; [n]…; <single-source or unverified, when so>)`",
            findings,
        )
        self.assertIn(
            "`1. <URL or code permalink> (primary|secondary|code)`", section(light, "Sources")
        )
        self.assertEqual(light.count("type line"), 1)  # the Template's, deep research only
        self.assertIn("Deep research's type line (light research gives its own)", light)
        self.assertEqual(
            task_text("light-research").count("`<Light Research type line, translated>`"), 1
        )

    def test_pm_and_researcher_point_to_writing_for_concision(self):
        for role, doc in (("pm", "the PRD"), ("researcher", "the report")):
            self.assertIn(
                f"- **Concise**: after writing {doc}, cut repetition and preamble "
                "([Principles › Writing](#writing)); length follows content.",
                composed(role)[0],
            )

    def test_a_local_files_other_layers_keys_are_left_alone(self):
        self.local(
            'users = ["octocat"]\ntrusted_dirs = ["~/data-repo"]\nshow = "none"\n[hosts.tmux]\n'
            'workers_per_column = 2\ngrid_retile = "off"\nteam = "T"\n[roles.pm]\naccount = "pm@x"\n'
        )
        for role, task in role_tasks():
            run = composer.resolve(role, task)
            self.assertEqual((run.task, run.show), (task, "none"))

    def test_every_reference_is_a_link_to_a_path_of_the_prompt(self):
        for role, task in runs():
            self.assertEqual(reference_errors(*role_sources(role, task)), [], (role, task))

    def test_every_prompts_own_headings_have_unique_anchors(self):
        for role, task in runs():
            anchors = heading_anchors(composed(role, task)[0])
            self.assertEqual(len(anchors), len(set(anchors)), (role, task))

    def test_every_run_composes_without_placeholders_and_opens_with_the_guide_listing_parameters_first_input_last(
        self,
    ):
        for role, task in runs():
            with self.subTest(role=role, task=task):
                prompt, run = composed(role, task)
                self.assertNotIn("{{", prompt)
                self.assertEqual(run.task, task or "")
                g = guide(prompt)
                self.assertTrue(g.startswith("# Guide\n"))
                self.assertIn("`<tasks>/<task>.md`", g)
                self.assertIn(
                    f"[{run.role_title} rules](#{anchor(run.role_title)}) > your task file's rules",
                    g,
                )
                self.assertEqual(prompt.count("On conflict:"), 1)
                self.assertIn("On conflict:", g)
                items = re.findall(r"^- \*\*.+$", g, re.M)
                self.assertEqual(
                    items[0],
                    "- **Parameters**: the value of each name this prompt and your task's file "
                    "use.",
                )
                self.assertEqual(
                    items[-1],
                    "- **Input**: the last section; everything after its heading is the input "
                    "text, verbatim (it may contain `#` or `---`).",
                )

    def test_the_guide_names_a_given_task_else_the_run_picks_it(self):
        named = guide(composed("researcher", "light-research")[0])
        self.assertIn(
            "**Your task**: `light-research` (`<tasks>/light-research.md`). Read that task's file "
            "(no other task's)",
            named,
        )
        self.assertNotIn("pick it from", named)
        picked = guide(composed("researcher")[0])
        self.assertIn(PICK, picked)
        self.assertIn("Your start progress report names the task and why.", picked)
        self.assertNotIn("light-research", picked)

    def test_a_prompt_has_its_roles_index_and_no_task_steps(self):
        for role, task in runs():
            prompt, _ = composed(role, task)
            for t, line in catalog()[role].items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", prompt, (role, task))
                for step in task_text(t).splitlines():
                    if len(step.strip()) >= 30 and not step.startswith("#"):
                        self.assertNotIn(step.strip(), prompt, (role, task, t))

    def test_a_task_from_another_roles_index_is_an_error(self):
        with self.assertRaisesRegex(
            composer.ConfigError, "task 'deep-research' is not one of engineer's tasks"
        ):
            composer.resolve("engineer", "deep-research")

    def test_pm_engineer_and_researcher_runs(self):
        researcher = (
            (
                "[Researcher rules](#researcher) > your task file's rules",
                "# Template: `templates/research-report.md`",
                "`ophis/private_docs`",
            ),
            lambda run: (run.tier, run.effort, run.read),
            (2, "high", ["{{team}}"]),
        )
        for role, task, literals, got, want in (
            (
                "pm",
                None,
                ("[PM rules](#pm) > your task file's rules", "# Template: `templates/prd.md`"),
                lambda run: (run.output["dir"], run.read, run.write),
                ("Product Design/", [], []),
            ),
            (
                "engineer",
                None,
                ("[Engineer rules](#engineer) > your task file's rules", "gh pr create"),
                lambda run: (run.effort, run.write, run.output),
                ("xhigh", [], {"type": "pull-request"}),
            ),
            *(
                ("researcher", task, *researcher)
                for task in (None, "deep-research", "light-research")
            ),
        ):
            with self.subTest(role=role, task=task):
                prompt, run = composed(role, task)
                for literal in literals:
                    self.assertIn(literal, prompt)
                self.assertEqual(got(run), want)

    def test_each_roles_parameters_follow_what_its_texts_use(self):
        base, out = (
            ["<Workdir>", "<tasks>", "<team>"],
            ["<out-repo>", "<out-branch>", "<out-dir>", "<out-host>"],
        )
        want = {
            "engineer": base,
            "pm": [*base, *out],
            "researcher": [*base, "<gate>", *out],
        }
        for role, task in [(r, None) for r in want] + [
            (r, t) for r, t in role_tasks() if r in want
        ]:
            self.assertEqual(parameter_names(role, task), want[role], (role, task))
        self.assertEqual(parameter_names("researcher", output={"type": "local"}), [*base, "<gate>"])

    def test_deep_research_falls_back_to_both_method_files_the_researcher_names(self):
        prompt, _ = composed("researcher")
        self.assertEqual(parameters(prompt)["<team>"], f"`{composer.TEAM}`")
        for name in ("deep-research", "ultracode"):
            self.assertTrue(
                os.path.isfile(os.path.join(composer.TEAM, "methods", f"{name}.md")), name
            )
            self.assertIn(f"`<team>/methods/{name}.md`", prompt)
        for phrase in (
            "No such tool → follow the deep-research method ([Researcher › Methods](#methods)).",
            "per the ultracode method ([Researcher › Methods](#methods))",
            "No Workflow tool → follow that method.",
            "Before the second, the brake ([Researcher › Methods](#methods)).",
            "No subagents with a round's tools → skip that round, under Gaps.",
        ):
            self.assertIn(phrase, task_text("deep-research"))

    def test_names_written_as_their_values_each_requested_command_starts_a_command(self):
        for role in roles():
            prompt, run = composed(role)
            prompt = expanded(prompt)
            for cmd in run.commands:
                cmd = fill(cmd, {"workdir": RUN.workdir, "team": composer.TEAM}, role)
                cmd = cmd.removesuffix(" *")
                self.assertIn(f"`{cmd}", prompt, role)

    def test_a_prompt_defines_each_run_value_once_under_the_rule(self):
        for role, task in runs():
            prompt, _ = composed(role, task)
            for value in (RUN.workdir, RULE):
                self.assertEqual(prompt.count(value), 1, (role, task, value))
            for root in ROOTS:
                self.assertNotIn(root, prompt.replace(composer.TEAM, "<team>"), (role, task))
            self.assertEqual(rule(prompt, "Commands"), [COMMANDS_RULE], (role, task))

    def test_every_repo_command_takes_the_inputs_checkout(self):
        found = {}
        for role in roles():
            prompt, _ = composed(role)
            text = prompt + "".join(task_text(t) for t in catalog()[role])
            cmds = re.findall(r"`python3 -P <team>/tools/repo\.py (worktree|status) ([^`]*)`", text)
            self.assertNotIn("<scripts>", text, role)
            found[role] = [cmd for cmd, _ in cmds]
            for cmd, args in cmds:
                self.assertEqual(args, REPO_ARGS[cmd], role)
            self.assertEqual(rule(prompt, "Checkout"), [CHECKOUT_RULE], role)
        self.assertEqual(
            found,
            {
                "researcher": ["worktree"],
                "pm": ["worktree"],
                "engineer": ["worktree", "status"],
                "dummy-tester": ["worktree"],
            },
        )

    def test_every_repo_named_by_url_may_be_a_local_clone_path(self):
        for role in roles():
            text = composed(role)[0] + "".join(task_text(t) for t in catalog()[role])
            self.assertIn("repo URL", text, role)
            self.assertEqual(
                text.count("repo URL"),
                len(re.findall(r"repo URLs? or local clone paths?", text)),
                role,
            )

    def test_literals_a_role_or_task_text_holds(self):
        engineer, researcher = composed("engineer")[0], composed("researcher")[0]
        for why, text, literal in (
            ("builds fail without push permission", engineer, "`push` false → `failed`"),
            (
                "light-build's cutoff skips merge commits",
                task_text("light-build"),
                "`git -C <worktree> log -1 --first-parent --no-merges --format=%cI`",
            ),
            (
                "research hand-off phrases",
                task_text("light-research"),
                "`<Light Research type line, translated>`",
            ),
            (
                "research hand-off phrases",
                task_text("light-research"),
                "`Suggest upgrading to Deep Research: ",
            ),
            (
                "research hand-off phrases",
                task_text("deep-research"),
                "its Light Research type line",
            ),
            (
                "a read-only task's branch is <id>-<task>",
                researcher,
                "`<branch>` `<id>-<task>`, `<task>` this task, `deep-research` or `light-research`",
            ),
            (
                "a read-only task's branch is <id>-<task>",
                task_text("product-design"),
                "`<branch>` `<id>-product-design`",
            ),
        ):
            with self.subTest(why, literal=literal):
                self.assertIn(literal, text)

    def test_builds_wait_for_the_prs_checks(self):
        for task in ("build", "light-build"):
            done = item(section(composed("engineer", task)[0], "Finish"), "Done")
            for literal in (
                "`gh pr checks <branch> --repo <host>/<owner>/<name> --watch --fail-fast`",
                "`gh run view <run-id> --repo <host>/<owner>/<name> --log-failed`",
                "`git -C <worktree> push -u origin <branch>`",
                "3 fixes",
                "20 minutes",
                "[Engineer › Finish › Failure](#finish)",
            ):
                self.assertIn(literal, done, task)

    def test_builds_merge_the_base_branch_before_autopilot_at_resume_and_before_the_pr(self):
        prompt, _ = composed("engineer")
        repo_ = section(prompt, "Repo")
        self.assertIn(
            "JSON `base` → `<base>`: the PR's base branch, else `Base:`, else `<default>`.", repo_
        )
        merge = item(repo_, "Merge")
        for literal in (
            "[Engineer › Repo](#repo) step 1",
            "`git -C <worktree> status`",
            "a merge in progress",
            "`git -C <worktree> merge --no-edit origin/<base>`",
            "`git -C <worktree> merge --abort`",
        ):
            self.assertIn(literal, merge)
        for where, text in (
            ("Autopilot", section(prompt, "Autopilot")),
            ("Finish", section(prompt, "Finish")),
            ("Repo › Resume", item(repo_, "Resume")),
        ):
            self.assertIn("[Engineer › Repo › Merge](#repo)", text, where)
        self.assertIn("`origin/<base>`", section(prompt, "Autopilot"))
        for literal in (
            "--head <branch> --base <base> --title",
            "/compare/<base>...<branch>?expand=1",
        ):
            self.assertIn(literal, prompt)
        for task in ("build", "light-build"):
            self.assertIn("[Engineer › Repo › Resume](#repo).", task_text(task), task)

    def test_merges_failing_checks_say_what_happens_in_finish_before_the_build_and_on_resume(self):
        merge = item(section(composed("engineer")[0], "Repo"), "Merge")
        self.assertIn(
            "One failing: during [Engineer › Finish › Done](#finish) → "
            "[Engineer › Finish › Failure](#finish); before the build → the failing checks go into the build's requirement; on "
            "[Engineer › Repo › Resume](#repo) → into the requirement of the build left, none left → "
            "[Engineer › Finish › Failure](#finish).",
            merge,
        )

    def test_the_pull_request_destination_reads_pr_from_repo_status(self):
        self.assertIn(
            "3. By `repo.py status`'s `pr`: null → `gh pr create", composed("engineer")[0]
        )

    def test_a_run_reads_the_team_root_when_its_text_names_a_method(self):
        named = []
        for role in roles():
            with open(os.path.join(composer.TEAM, "roles", f"{role}.md")) as f:
                text = f.read() + "".join(task_text(t) for t in catalog()[role])
            if "<team>/methods/" in text:
                named.append(role)
                self.assertIn("{{team}}", composer.resolve(role).read, role)
        self.assertEqual(named, ["researcher"])

    def test_every_researcher_run_names_the_gate_default_none(self):
        gate = "python3 /u/usage.py --below 80"
        for task in (None, "deep-research", "light-research"):
            prompt = composed("researcher", task)[0]
            self.assertIn("the gate is `<gate>`", prompt, task)
            self.assertEqual(parameters(prompt)["<gate>"], "`none`", task)
            prompt = composed("researcher", task, overlays=[{"gate": gate}])[0]
            self.assertEqual(parameters(prompt)["<gate>"], f"`{gate}`", task)

    def test_github_host_defaults_and_overrides(self):
        prompt, run = composed("researcher")
        self.assertIn("gh repo clone <out-host>/<out-repo> <pub>", prompt)
        self.assertIn(
            "https://<out-host>/<out-repo>/blob/<out-branch>/<file>`, `<file>` URL-encoded", prompt
        )
        self.assertEqual(parameters(prompt)["<out-host>"], "`github.com`")
        prompt = composed("researcher", output={**run.output, "host": "ghe.example.com"})[0]
        self.assertEqual(parameters(prompt)["<out-host>"], "`ghe.example.com`")


if __name__ == "__main__":
    unittest.main()
