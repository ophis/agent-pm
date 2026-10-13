from agent_pm.linear_orchestrator.engine import inputs, target
from agent_pm.linear_orchestrator.linear import issues
from orchestrator_test_helpers.unit.inputs.data import (
    ANN,
    BASE,
    DESIGN_PRECEDENCE,
    DOCS,
    HUMANS,
    fenced,
    issue,
    lines,
    note,
)
from orchestrator_test_helpers.unit.inputs.render_design_fixtures import (
    RenderDesign as RenderDesignBase,
)


class RenderDesign1(RenderDesignBase):
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
