from agent_pm.linear_orchestrator.engine import inputs, target
from agent_pm.linear_orchestrator.linear import issues
from orchestrator_tests.case import Case
from orchestrator_tests.unit.inputs_data import (
    ANN,
    BASE,
    DESIGN_PRECEDENCE,
    DOCS,
    HUMANS,
    RESEARCH_PRECEDENCE,
    fenced,
    issue,
    lines,
    note,
    research_issue,
)


class RenderResearch(Case):
    def test_golden_full(self):
        iss = research_issue()
        src = inputs.Sources(
            (
                inputs.Doc(
                    BASE + "Research/2026-08-01-RES-2-queues.md",
                    "Research/2026-08-01-RES-2-queues.md",
                    "# Queues\n",
                ),
            ),
            inputs.Doc(
                BASE + "Research/2026-09-01-RES-4-q.md",
                "Research/2026-09-01-RES-4-q.md",
                "# Earlier",
            ),
        )
        got = inputs.render(
            iss,
            "researcher",
            src,
            humans=HUMANS,
            target=target.Target("ophis", "agent-pm"),
            docs=DOCS,
        )
        self.assertEqual(
            got,
            lines(
                "Reference: RES-4",
                "Repo: ophis/agent-pm",
                "Checkout: RES-4",
                "",
                RESEARCH_PRECEDENCE,
                "",
                "## Question",
                "",
                "Compare queues",
                "",
                f"Which queue fits?\nBackground: {BASE}Research/2026-08-01-RES-2-queues.md",
                "",
                "## The user's comments",
                "",
                "Ann, 2026-09-02T00:00:00.000Z:\nFocus on cost.",
                "",
                "## Other comments (context)",
                "",
                "- Researcher, 2026-09-01T10:00:00.000Z:",
                "  > Research started: https://x/y",
                "  > second line",
                "- integration, 2026-09-01T11:00:00.000Z:",
                "  > Deploy ok",
                "",
                "## Linked documents (context)",
                "",
                "### `Research/2026-08-01-RES-2-queues.md`",
                "",
                fenced("# Queues"),
                "",
                "## Earlier version: `Research/2026-09-01-RES-4-q.md`",
                "",
                fenced("# Earlier"),
            ),
        )

    def test_golden_without_optional_sections(self):
        for description, t, repo_line in (
            ("Which?", None, ()),
            (
                "Which?",
                target.Target("ophis", "agent-pm", clone="/x/agent-pm"),
                ("Repo: /x/agent-pm",),
            ),
            ("", None, ()),
        ):
            with self.subTest(description=description, target=t):
                got = inputs.render(
                    issue(description),
                    "researcher",
                    inputs.Sources((), None),
                    humans=HUMANS,
                    target=t,
                    docs=DOCS,
                )
                self.assertEqual(
                    got,
                    lines(
                        "Reference: RES-4",
                        *repo_line,
                        "Checkout: RES-4",
                        "",
                        RESEARCH_PRECEDENCE,
                        "",
                        "## Question",
                        "",
                        "Compare queues",
                        *(("", description) if description else ()),
                    ),
                )

    def test_user_notes_are_matched_case_insensitively_and_use_email_without_name(self):
        ns = (
            issues.Note("Hi", "2026-09-02T00:00:00.000Z", "ANN@example.com", None),
            issues.Note("Bye", "2026-09-03T00:00:00.000Z", "bob@example.com", "Bob"),
        )
        got = inputs.render(
            issue("Q", notes=ns),
            "researcher",
            inputs.Sources((), None),
            humans=HUMANS,
            target=None,
            docs=DOCS,
        )
        self.assertIn(
            "## The user's comments\n\nANN@example.com, 2026-09-02T00:00:00.000Z:\nHi\n\nBob, 2026-09-03T00:00:00.000Z:\nBye",
            got,
        )
        self.assertNotIn("## Other comments", got)

    def test_not_found_doc_names_the_branch(self):
        src = inputs.Sources((inputs.Doc(BASE + "Research/a.md", "Research/a.md", None),), None)
        got = inputs.render(issue("Q"), "researcher", src, humans=HUMANS, target=None, docs=DOCS)
        self.assertTrue(
            got.endswith(
                "## Linked documents (context)\n\n### `Research/a.md`\n\n(not found on main)"
            )
        )


class Fence(Case):
    def render_doc(self, text):
        src = inputs.Sources((inputs.Doc(BASE + "Research/a.md", "Research/a.md", text),), None)
        return inputs.render(issue("Q"), "researcher", src, humans=HUMANS, target=None, docs=DOCS)

    def test_fence_is_longer_than_any_backtick_run_and_trailing_newlines_add_no_blank_lines(self):
        for text, fence, shown in (
            ("plain", "```", "plain"),
            ("a ``` b", "````", "a ``` b"),
            ("`` and ```` and `", "`````", "`` and ```` and `"),
            ("`x`", "```", "`x`"),
            ("a\n\n", "```", "a"),
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    self.render_doc(text).endswith(
                        f"### `Research/a.md`\n\n{fence}markdown\n{shown}\n{fence}"
                    )
                )


class RenderDesign(Case):
    HANDOFF = lines(
        "Handoff from RES-4: https://linear.app/t/issue/RES-4",
        "",
        "## Source",
        f"- Spec: {BASE}Research/2026-09-01-RES-4-queues.md",
        "",
        "## Instructions",
        "Ann, 2026-09-01T00:00:00.000Z:",
        "Write the PRD.",
        "",
        "## Comments",
        "- Ann, 2026-09-01T00:00:00.000Z:",
        "  > hello",
        "- integration, 2026-09-01T01:00:00.000Z:",
        "  > bot",
    )

    def test_golden_handoff(self):
        ns = (
            note("PRD started: https://x", "2026-09-06T00:00:00.000Z"),
            note("Also mobile.", "2026-09-07T00:00:00.000Z", ANN),
        )
        iss = issue(
            self.HANDOFF,
            ident="PM-9",
            title="PRD: Compare queues",
            notes=ns,
            linked=(
                issues.Linked("RES-4", "Compare\nqueues  now", "Done"),
                issues.Linked("ENG-1", "Other", "Todo"),
            ),
        )
        src = inputs.Sources(
            (
                inputs.Doc(
                    BASE + "Research/2026-09-01-RES-4-queues.md",
                    "Research/2026-09-01-RES-4-queues.md",
                    "# Report\n",
                ),
            ),
            inputs.Doc(
                BASE + "Product%20Design/2026-09-05-PM-9-q.md",
                "Product Design/2026-09-05-PM-9-q.md",
                "# Mine",
            ),
        )
        got = inputs.render(iss, "pm", src, humans=HUMANS, target=None, docs=DOCS)
        self.assertEqual(
            got,
            lines(
                "Reference: PM-9",
                "Checkout: PM-9",
                "",
                DESIGN_PRECEDENCE,
                "",
                "## Brief",
                "",
                "PRD: Compare queues",
                "",
                "Ann, 2026-09-01T00:00:00.000Z:\nWrite the PRD.",
                "",
                "## The user's later words",
                "",
                "Ann, 2026-09-07T00:00:00.000Z:\nAlso mobile.",
                "",
                "## Reports",
                "",
                "### `Research/2026-09-01-RES-4-queues.md`",
                "",
                fenced("# Report"),
                "",
                "## Linked issues (context)",
                "",
                "- RES-4 Compare queues now (Done)",
                "- ENG-1 Other (Todo)",
                "",
                "## Comments on RES-4 (context)",
                "",
                "- Ann, 2026-09-01T00:00:00.000Z:",
                "  > hello",
                "- integration, 2026-09-01T01:00:00.000Z:",
                "  > bot",
                "",
                "## Other comments (context)",
                "",
                "- Researcher, 2026-09-06T00:00:00.000Z:",
                "  > PRD started: https://x",
                "",
                "## Earlier version: `Product Design/2026-09-05-PM-9-q.md`",
                "",
                fenced("# Mine"),
            ),
        )

    def test_golden_direct_issue(self):
        for t, repo_line in (
            (None, ()),
            (target.Target("ophis", "agent-pm"), ("Repo: ophis/agent-pm",)),
            (target.Target("ophis", "agent-pm", clone="/x/agent-pm"), ("Repo: /x/agent-pm",)),
        ):
            with self.subTest(target=t):
                got = inputs.render(
                    issue("Build a PRD for X.", ident="PM-9", title="Queues PRD"),
                    "pm",
                    inputs.Sources((), None),
                    humans=HUMANS,
                    target=t,
                    docs=DOCS,
                )
                self.assertEqual(
                    got,
                    lines(
                        "Reference: PM-9",
                        *repo_line,
                        "Checkout: PM-9",
                        "",
                        DESIGN_PRECEDENCE,
                        "",
                        "## Brief",
                        "",
                        "Queues PRD",
                        "",
                        "Build a PRD for X.",
                    ),
                )

    def test_direct_issue_user_words_and_prd_fallback_earlier(self):
        ns = (note("More.", "2026-09-02T00:00:00.000Z", ANN),)
        src = inputs.Sources(
            (), inputs.Doc(BASE + "Product%20Design/p.md", "Product Design/p.md", None)
        )
        got = inputs.render(
            issue("Desc", ident="PM-9", notes=ns), "pm", src, humans=HUMANS, target=None, docs=DOCS
        )
        self.assertTrue(
            got.endswith(
                lines(
                    "## The user's later words",
                    "",
                    "Ann, 2026-09-02T00:00:00.000Z:\nMore.",
                    "",
                    "## Earlier version: `Product Design/p.md`",
                    "",
                    "(not found on main)",
                )
            )
        )

    def test_handoff_without_comments_has_no_source_comments_section(self):
        d = lines("Handoff from RES-4: u", "", "## Instructions", "Ann, t:", "Go.")
        got = inputs.render(
            issue(d, ident="PM-9"),
            "pm",
            inputs.Sources((), None),
            humans=HUMANS,
            target=None,
            docs=DOCS,
        )
        self.assertNotIn("## Comments on", got)
        self.assertIn("## Brief\n\nCompare queues\n\nAnn, t:\nGo.", got)

