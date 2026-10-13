from agent_pm.linear_orchestrator.engine import inputs, target
from agent_pm.linear_orchestrator.linear import issues
from orchestrator_test_helpers.unit.inputs.data import (
    BASE,
    DOCS,
    HUMANS,
    RESEARCH_PRECEDENCE,
    fenced,
    issue,
    lines,
    research_issue,
)
from orchestrator_test_helpers.unit.inputs.render_research_fixtures import (
    RenderResearch as RenderResearchBase,
)


class RenderResearch1(RenderResearchBase):
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
