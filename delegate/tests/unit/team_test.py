import os
import re

from agent_pm.delegate.composer.base import TEAM, outline
from agent_pm.delegate.composer.teams import START
from agent_pm.utils import unfenced

from delegate.tests.case import Case
from delegate.tests.unit.composer.prompts import (
    composed,
    item,
    parameters,
    rule,
    section,
    task_text,
)
from delegate.tests.unit.composer.shipped import (
    CHECKOUT_RULE,
    PICK,
    REPO_ARGS,
    TASKS,
    RealConfig,
    catalog,
    guide,
    role_tasks,
    roles,
    runs,
    team_texts,
)
from delegate.tests.unit.composer.teams import INTRO


class Acceptance(Case):
    def test_each_requested_command_runs_the_teams_repo_tool(self):
        self.assertTrue(os.path.isfile(os.path.join(TEAM, "tools", "repo.py")))
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
                        "Write the deliverable to `<Workdir>/out.md` and name that file in the outcome's "
                        "`deliverable_file`"
                    ),
                    destination,
                )
                self.assertNotIn("outcome's `deliverable`", destination)
                self.assertIn("Leave `url` empty.", destination)
                self.assertEqual(prompt.count("`<Workdir>/out.md`"), 1)
                self.assertIn(
                    (
                        "\n- `deliverable_file`: a `.md` file in `<Workdir>` holding the deliverable, in place "
                        "of `deliverable`, when [Output › Destination](#destination) says so or the deliverable "
                        "is long.\n"
                    ),
                    prompt,
                )

    def test_each_task_file_has_one_start_mark_and_a_resume_section(self):
        for name in os.listdir(TASKS):
            text = task_text(name.removesuffix(".md"))
            self.assertEqual(len(START.findall(text)), 1, name)
            self.assertIn("## Resume", [line.rstrip() for _, line in unfenced(text)], name)


class Shipped(Case):
    def test_text_holds_only_the_placeholders_render_fills(self):
        inline = {"role", "role_anchor", "task", "language", "deliverable"}
        for rel, text in team_texts().items():
            if rel.endswith(".md"):
                self.assertLessEqual(set(re.findall(r"\{\{(\w+)", text)), inline, rel)

    def test_each_charter_opens_with_its_index_naming_a_listed_default(self):
        defaults = {
            "researcher": "research",
            "pm": "product-design",
            "engineer": "build",
            "dummy-tester": "echo",
        }
        self.assertEqual(set(roles()), set(defaults))
        for role, default in defaults.items():
            with open(os.path.join(TEAM, "roles", f"{role}.md")) as f:
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

    def test_the_engineer_charter_has_six_sections_and_autopilot_adds_its_lines_verbatim(self):
        with open(os.path.join(TEAM, "roles", "engineer.md")) as f:
            charter = f.read()
        heads = [path[1] for _, path, _, heading in outline(charter) if heading and len(path) == 2]
        self.assertEqual(
            heads, ["Tasks", "Rules", "Input processing", "Repo", "Autopilot", "Finish"]
        )
        lines = (
            (
                "Work only in `<worktree>` on branch `<branch>`, with absolute paths; create no other clone, worktree "
                "or branch."
            ),
            "Commits merged from `origin/<base>` are not this build's work.",
            "Your and your producers' local checks, in every phase: only lint and the unit tests of the projects "
            "you changed; never integration tests or full suites: the verification reviewer runs them, on the PR's "
            "CI when the repo has a `pull_request` workflow, else locally.",
            "Ask for no pull request: the caller opens it, onto `<base>`.",
            "After <your task's push points>, run exactly `git -C <worktree> push -u origin <branch>`.",
        )
        self.assertIn(
            "\nAdd verbatim, placeholders filled in:\n\n```\n" + "\n".join(lines) + "\n```\n",
            section(charter, "Autopilot"),
        )

    def test_builds_report_each_s5_task_against_their_task_count(self):
        for task, doc in (("build", "the plan doc's"), ("light-build", "the plan doc's")):
            text = task_text(task)
            self.assertIn(
                (
                    "**Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:"
                ),
                text,
                task,
            )
            self.assertIn(
                (
                    "[agent-pm-progress:step] the step and its result; an S5 task as "
                    f"`S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` {doc} task count "
                    "(its `### Task` headings)"
                ),
                text,
                task,
            )

    def test_builds_leave_their_docs_where_autopilot_puts_them(self):
        self.assertIn(
            (
                'docs line "Spec and plan doc stay where autopilot puts them; S7 copies them to where the repo '
                'keeps design docs; never `git add -f` them."'
            ),
            task_text("build"),
        )
        self.assertIn(
            (
                'docs line "The plan doc stays where autopilot puts it; S7 copies it to where the repo keeps '
                'design docs; never `git add -f` it."'
            ),
            task_text("light-build"),
        )
        self.assertIn(
            "(a task list in its plan doc, implementation, verification, a light review, no spec doc)",
            catalog()["engineer"]["light-build"],
        )

    def test_builds_continue_the_newest_unfinished_plan_doc_by_the_engineers_one_rule(self):
        continue_ = item(section(composed("engineer")[0], "Repo"), "Continue")
        self.assertIn(
            (
                " a plan doc: [Engineer › Repo › Merge](#repo), then [Engineer › Repo › Draft](#repo), then "
                "your task's slash command "
                "([Engineer › Tasks](#tasks)) with the plan doc's verbatim input as its requirement; "
                "autopilot's resolver `mode` not `resume` → `needs_input` naming the plan doc; stop. Once it "
                "resumes, give it the rest as mid-run input, each logged in its progress log: the user's "
                "requirements, the review input (marked as [Engineer › Autopilot](#autopilot) says), failing "
                "merge checks, and with a user requirement a review cap 2 rounds beyond its RESUME "
                "`review_round`, never below the skill's cap (a capped plan doc stops again at once). Added to "
                "its input, they would start a new plan doc at HEAD, leaving its commits unreviewed."
            ),
            continue_,
        )
        for task in ("build", "light-build"):
            text = task_text(task)
            self.assertIn(
                "2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.", text
            )
            self.assertIn(
                "\n   - The newest plan doc (`plan_docs`' last) not at `done` → continue it.\n"
                "   - Else a plan doc or a `pr` (a **finished build**) and a user requirement → a new build",
                text,
                task,
            )
            self.assertIn(
                "\n5. **Build**: continuing → [Engineer › Repo › Continue](#repo); else "
                f"`/autopilot:{task}` ([Engineer › Autopilot](#autopilot)), the requirements being",
                text,
                task,
            )
            self.assertNotIn("mid-run", text, task)

    def test_engineer_lists_non_blocking_items_commits_nothing_after_and_reports_autopilot(self):
        with open(os.path.join(TEAM, "roles", "engineer.md")) as f:
            charter = f.read()
        self.assertIn(
            (
                "- **Done**, once the build converges, committing nothing more: [Engineer › Repo](#repo) step 2 "
                "again (its `pr` now the draft PR the build may have opened), then `status: done`; "
                "`deliverable` what changed, the input's `Links:`, how to verify, and each of this run's builds' "
                "deferred non-blocking review items verbatim, for the manager or the user to decide on (never "
                "fix them or start a build for them); `autopilot` the `status` and `head` of this run's last "
                "`autopilot-result`; `title`"
            ),
            charter,
        )
        self.assertNotIn("leftover", charter)
        self.assertNotIn("Merge](#repo)", section(composed("engineer")[0], "Finish"))
        self.assertIn(
            '- `autopilot`: `{"status", "head"}`, when your task asks for it.\n',
            composed("engineer")[0],
        )

    def test_engineer_prs_are_drafts_until_the_delegate_readies_them(self):
        prompt = composed("engineer")[0]
        destination = section(prompt, "Destination")
        self.assertIn(
            "null → `gh pr create --draft --repo <host>/<owner>/<name> --head", destination
        )
        self.assertNotIn("gh pr ready", destination)
        repo_ = section(prompt, "Repo")
        self.assertIn(
            (
                ": before each autopilot start, by the latest `repo.py status`: `pr.state` `OPEN` and "
                "`pr.isDraft` false → `gh pr ready --undo <pr.number> --repo <host>/<owner>/<name>`. Only the "
                "Delegate marks a PR ready."
            ),
            item(repo_, "Draft"),
        )
        for where, text in (
            ("Autopilot", section(prompt, "Autopilot")),
            ("Repo › Continue", item(repo_, "Continue")),
        ):
            self.assertIn(
                "[Engineer › Repo › Merge](#repo), then [Engineer › Repo › Draft](#repo), then",
                text,
                where,
            )

    def test_research_tasks_prepare_before_their_start_mark(self):
        for task in ("research", "light-research"):
            text = task_text(task)
            self.assertLess(
                text.index("**Prepare**"), text.index("[agent-pm-progress:start]"), task
            )

    def test_light_research_is_one_pass(self):
        light = task_text("light-research")
        self.assertIn("in one pass: no subagents, no workflow", light)
        self.assertIn("one pass by one agent, no subagents, no verification stage", light)
        self.assertNotIn("angles", light)


class Real(RealConfig):
    def test_fixed_labels_are_translated_and_template_headings_say_so(self):
        self.assertIn(
            (
                "- Reports and PRDs, headings and fixed labels included, are in Chinese; a `<X, translated>` in "
                "a template or task is X in Chinese."
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
                    "Its headings are fixed apart from their language ([Principles › Writing](#writing))"
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
                    "A term's first mention adds one gloss, its Chinese rendering (for an acronym, of its full "
                    "name); none when the term is common in Chinese, and none in a heading."
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

    def test_every_run_judges_related_work_first_and_only_proposes_board_changes(self):
        for role, task in runs():
            prompt = composed(role, task)[0]
            overlap = section(prompt, "Overlap")
            for literal in (
                "Input has a `## Related work` section",
                "before your task's steps",
                "the user's later words say to go on",
                *(f"| `{v}` |" for v in ("covered", "supersedes", "intersects", "partial", "none")),
                "Backlog or Todo: never `covered`; the same scope is `supersedes`",
                "No **evidence** → at most `partial`.",
                "`covered` beats every other verdict",
                "return `needs_input`, its `questions` naming the candidate's id and **evidence**",
                "Canceled: its last comment, as the cancel reason",
                "suggest closing this issue as a Duplicate of it",
                "| `supersedes` | Backlog, Todo, In Review | all of this issue | cancel |",
                "| `intersects` | Backlog, Todo, In Review | all of this issue, the shared part included "
                "| split residue, then cancel |",
                "| `supersedes` | Done |",
                "lists its output as an earlier version",
                "| `intersects` | Done | as `partial` |",
                "| `supersedes`, `intersects` | In Progress, Canceled |",
                "only the part not yet answered; the deliverable cites the candidate and its output as sources",
                "`<old id>: <cancel | split residue | cross-link>: <reason>`",
                "the residue issue's title and description",
                "`none` → no mention.",
            ):
                self.assertIn(literal, overlap, (role, task, literal))
            self.assertIn(
                "or `covered` related work ([Principles › Overlap](#overlap))", prompt, (role, task)
            )

    def test_templates_put_the_summary_first_one_fact_per_item_and_point_to_the_diagram_rule(self):
        pm, light = composed("pm", "product-design")[0], composed("researcher", "research")[0]
        point = "[Principles › Writing › Diagrams and tables](#writing)"
        for text in (pm, light):
            self.assertIn("One fact per list item", text)
            self.assertIn("never repeats `[Reference]`", text)
        self.assertIn("Opens with one sentence", section(pm, "Problem and goals"))
        self.assertIn(point, section(pm, "User flow"))
        self.assertIn(point, section(pm, "Approach and trade-offs"))
        self.assertIn(point, section(pm, "System diagram"))
        requirements = section(pm, "Requirements")
        self.assertIn("Each subsection is one table", requirements)
        self.assertIn("fills the Module cell of its first row only", requirements)
        for row in ("`| FR-<n> | <module> | <the requirement> | ", "`| NFR-<n> | <module> | "):
            self.assertIn(row, pm)
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
        self.assertIn(
            "Deep research's type line (research and light research give their own)", light
        )
        for task, line in (("research", "Research"), ("light-research", "Light Research")):
            self.assertEqual(task_text(task).count(f"`<{line} type line, translated>`"), 1, task)

    def test_the_guide_names_a_given_task_else_the_run_picks_it(self):
        named = guide(composed("researcher", "research")[0])
        self.assertIn(
            "**Your task**: `research` (`<tasks>/research.md`). Read that task's file "
            "(no other task's)",
            named,
        )
        self.assertNotIn("pick it from", named)
        picked = guide(composed("researcher")[0])
        self.assertIn(PICK, picked)
        self.assertIn("Your start progress report names the task and why.", picked)
        self.assertNotIn("`research`", picked)

    def test_a_prompt_has_its_roles_index_and_no_task_steps(self):
        for role, task in runs():
            prompt, _ = composed(role, task)
            for t, line in catalog()[role].items():
                self.assertIn(f"\n- `{t}` (`<tasks>/{t}.md`): {line}\n", prompt, (role, task))
                for step in task_text(t).splitlines():
                    if len(step.strip()) >= 30 and not step.startswith("#"):
                        self.assertNotIn(step.strip(), prompt, (role, task, t))

    def test_deep_research_falls_back_to_both_method_files_the_researcher_names(self):
        prompt, _ = composed("researcher")
        self.assertEqual(parameters(prompt)["<team>"], f"`{TEAM}`")
        for name in ("deep-research", "ultracode"):
            self.assertTrue(os.path.isfile(os.path.join(TEAM, "methods", f"{name}.md")), name)
            self.assertIn(f"`<team>/methods/{name}.md`", prompt)
        for phrase in (
            "No such tool → follow the deep-research method ([Researcher › Methods](#methods)).",
            "per the ultracode method ([Researcher › Methods](#methods))",
            "No Workflow tool → follow that method.",
            "Before the second, the brake ([Researcher › Methods](#methods)).",
            "No subagents with a round's tools → skip that round, under Gaps.",
        ):
            self.assertIn(phrase, task_text("deep-research"))

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
                "the pull request destination reads pr from repo status",
                engineer,
                "3. By `repo.py status`'s `pr`: null → `gh pr create",
            ),
            *(
                (
                    "pm and researcher point to writing for concision",
                    composed(role)[0],
                    f"- **Concise**: after writing {doc}, cut repetition and preamble "
                    "([Principles › Writing](#writing)); length follows content.",
                )
                for role, doc in (("pm", "the PRD"), ("researcher", "the report"))
            ),
            (
                "echo's summary overrides Output's length",
                task_text("echo"),
                "`summary` the input's first line, not [Output](#output)'s 3–5 lines.",
            ),
            (
                "a build's files are its spec and plan doc",
                task_text("build"),
                "adding `files`: this build's spec (the `-spec.md` beside its `-plan.md` plan doc) and plan doc "
                "(on failure, those that exist);",
            ),
            (
                "research hand-off phrases",
                task_text("research"),
                "`<Research type line, translated>`",
            ),
            (
                "research hand-off phrases",
                task_text("research"),
                "`Suggest upgrading to Deep Research: ",
            ),
            (
                "research hand-off phrases",
                task_text("light-research"),
                "`<Light Research type line, translated>`",
            ),
            (
                "research hand-off phrases",
                task_text("light-research"),
                "`Suggest upgrading to Research: ",
            ),
            (
                "research hand-off phrases",
                task_text("research"),
                "Revising a Light Research report (its Light Research type line marks it",
            ),
            (
                "research hand-off phrases",
                task_text("deep-research"),
                "Revising a Research or Light Research report (its type line marks it",
            ),
            (
                "a read-only task's branch is <id>-<task>",
                researcher,
                "`<branch>` `<id>-<task>`, `<task>` this task, `deep-research`, `research` or `light-research`",
            ),
            (
                "a read-only task's branch is <id>-<task>",
                task_text("product-design"),
                "`<branch>` `<id>-product-design`",
            ),
            (
                "a run stopped in Prepare starts it over",
                task_text("light-research"),
                "Step 4 not started → step 2. Else reuse this session's findings",
            ),
            (
                "an unlabelled Light Research upgrade is Research",
                researcher,
                "or no task is named and the input revises a Light Research report (an upgrade); light.",
            ),
            (
                "an unlabelled Light Research upgrade is Research",
                researcher,
                "when the answer must be reliable, or a Research report left gaps; heavy.",
            ),
        ):
            with self.subTest(why, literal=literal):
                self.assertIn(literal, text)
        self.assertNotIn("a quick pass left gaps", researcher)

    def test_builds_wait_for_the_prs_checks(self):
        for task in ("build", "light-build"):
            done = item(section(composed("engineer", task)[0], "Finish"), "Done")
            for literal in (
                "`gh pr checks <branch> --repo <host>/<owner>/<name> --watch --fail-fast`",
                "`gh run view <run-id> --repo <host>/<owner>/<name> --log-failed`",
                "a new build of your task's slash command ([Engineer › Autopilot](#autopilot))",
                "redo Done's `deliverable` and `autopilot`, then [Output › Destination](#destination) updates "
                "the PR, then step 1",
                "3 fix builds",
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

    def test_merges_failing_checks_go_into_the_next_build_and_resume_merges_only_before_one(self):
        repo_ = section(composed("engineer")[0], "Repo")
        self.assertIn(
            (
                "One failing → the failing checks go into the next build's requirements (mid-run for a "
                "continued plan doc: [Engineer › Repo › Continue](#repo))."
            ),
            item(repo_, "Merge"),
        )
        self.assertIn(
            "steps 1–2 again, then, with a build left, [Engineer › Repo › Merge](#repo), then",
            item(repo_, "Resume"),
        )
