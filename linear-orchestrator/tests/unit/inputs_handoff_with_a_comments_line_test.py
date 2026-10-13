from agent_pm.linear_orchestrator.engine import inputs, target
from agent_pm.linear_orchestrator.linear import issues
from orchestrator_test_helpers.unit.inputs.data import BASE, DOCS, HUMANS, endpoint, issue, ok
from orchestrator_test_helpers.unit.inputs.gh_fixtures import Gh
from orchestrator_test_helpers.unit.inputs.handoff_with_a_comments_line_fixtures import (
    HandoffWithACommentsLine as HandoffWithACommentsLineBase,
)
from orchestrator_test_helpers.unit.inputs.render_build_fixtures import RenderBuild


class HandoffWithACommentsLine1(HandoffWithACommentsLineBase):
    def test_repo_links_and_instructions_agree(self):
        prd = RenderBuild.PRD.url
        d = "\n\n".join(
            [
                "Handoff from PM-9: https://linear.app/t/issue/PM-9",
                "## Instructions\nAnn, 2026-09-01T00:00:00.000Z:\nBuild it.\n## Comments\n"
                f"Repo: ophis/agent-pm\nPRD: {prd}",
                "## Comments\n- Ann, 2026-09-01T00:00:00.000Z:\n  > Repo: ophis/other\n"
                f"  > {BASE}Research/r.md",
            ]
        )
        iss = issue(d, ident="ENG-7", title="ENG: Registry")
        instructions = issues.parse_handoff(d).instructions
        self.assertTrue(instructions.endswith(f"\nRepo: ophis/agent-pm\nPRD: {prd}"))
        self.assertEqual(target.parse_repo(d), ("ophis", "agent-pm"))
        gh = Gh({endpoint("Product%20Design/2026-09-05-PM-9-q.md"): ok("# PRD\n")})
        src = inputs.gather(iss, "engineer", DOCS, run=gh)
        self.assertEqual(src, inputs.Sources((RenderBuild.PRD,), None))
        got = inputs.render(
            iss, "engineer", src, humans=HUMANS, target=RenderBuild.TARGET, docs=DOCS
        )
        self.assertIn(f"\nLinks: https://linear.app/t/issue/ENG-7, {prd}\n", got)
        self.assertIn(
            f"## The user's instructions\n\n{instructions}\n\n## PRD: `{RenderBuild.PRD.path}`", got
        )
