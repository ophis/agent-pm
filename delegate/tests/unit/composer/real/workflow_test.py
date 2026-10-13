"""Workflow behavior."""

import os
import re

from agent_pm.delegate import composer
from agent_pm.delegate.composer.base import anchor, fill
from agent_pm.delegate.composer.prompt import RULE, reference_errors

from delegate.tests.case import ROOTS, Case
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
from delegate.tests.unit.composer.real.case import (
    CHECKOUT_RULE,
    COMMANDS_RULE,
    FIXTURE,
    LANGUAGE_RULE,
    PICK,
    REPO_ARGS,
    catalog,
    expanded,
    guide,
    parameter_names,
    role_tasks,
    roles,
    runs,
)


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
            (
                "- Reports and PRDs, headings and fixed labels included, are in Chinese; a "
                "`<X, translated>` in a template or task is X in "
                "Chinese."
            ),
            composed("researcher")[0],
        )
        for role, task in role_tasks():
            text = composed(role, task)[0]
            for literal in ("`Check:`", "Check:", "Confidence:", "`Light Research. Angles: "):
                self.assertNotIn(literal, text, (role, task, literal))
        for role, task in runs():
            self.assertIn(
                (
                    "Its headings are fixed apart from their language ([Principles › "
                    "Writing](#writing))"
                ),
                guide(composed(role, task)[0]),
                (role, task),
            )

    def test_writing_has_the_one_gloss_and_diagram_rules_and_no_length_cap(self):
        for role, task in runs():
            prompt = composed(role, task)[0]
            writing = section(prompt, "Writing")
            self.assertNotIn("the first mention adds the", prompt, (role, task))
            self.assertIn(
                (
                    "A term's first mention adds one gloss, its Chinese rendering (for an "
                    "acronym, of its full name); none when the term is common in Chinese, "
                    "and none in a heading."
                ),
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
            'users = ["octocat"]\ntrusted_dirs = ["~/data-repo"]\nshow = '
            '"none"\n[hosts.tmux]\nworkers_per_column = 2\ngrid_retile = "off"\nteam = '
            '"T"\n[roles.pm]\naccount = "pm@x"\n'
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

    def test_runs_fill_placeholders_and_list_parameters_before_input(
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
            (
                "One failing: during [Engineer › Finish › Done](#finish) → [Engineer › "
                "Finish › Failure](#finish); before the build → the failing checks go into "
                "the build's requirement; on [Engineer › Repo › Resume](#repo) → into the "
                "requirement of the build left, none left → [Engineer › Finish › "
                "Failure](#finish)."
            ),
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
