from agent_pm.delegate import STATUSES
from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.run_fixtures import logged
from orchestrator_test_helpers.unit.writeback.data import (
    ID,
    PLAN_TEXT,
    PROJECT,
    SID,
    SPEC_TEXT,
    TARGET,
    expect,
    outcome,
    sha,
)
from orchestrator_test_helpers.unit.writeback.finish_golden_fixtures import (
    FinishGolden as FinishGoldenBase,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback_support import APPROVE_MAPPED, PR, PRD


class FinishGolden1(FinishGoldenBase):
    def test_every_role_status_and_mode(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        for role in config.TASKS:
            for status in STATUSES:
                for resume in (False, True):
                    with self.subTest(role=role, status=status, resume=resume):
                        gql = Gql()
                        build = config.TASKS[role].kind == "build"
                        ctx = self.ctx(
                            role, gql=gql, resume=resume, target=TARGET if build else None
                        )
                        spec, plan = self.files(ctx)
                        self.assertTrue(
                            writeback.finish(ctx, self.outcomes(role, spec, plan)[status])
                        )
                        self.assertEqual(gql.calls, self.golden(role, status, resume))

    def test_log_and_ledger(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        writeback.finish(ctx, self.outcomes("engineer", spec, plan)["done"])
        steps = [
            f"file:ENG-7-spec.md:{sha(SPEC_TEXT)}",
            f"file:ENG-7-plan.md:{sha(PLAN_TEXT)}",
            "comment",
            f"attach:{PR}",
            "move:in_review",
        ]
        self.assertEqual(self.lines(), [f"step {ID} step={s}" for s in steps])
        self.assertEqual([e["sid"] for e in logged()], [SID] * len(steps))
        self.assertEqual(self.ledger(ctx), {SID: steps})

    def test_design_retitle_and_mapped_approve_line(self):
        gql = Gql()
        ctx = self.ctx("pm", gql=gql, target=None, project=PROJECT)
        writeback.finish(ctx, outcome("done", "Session Registry", "PRD for the registry.", url=PRD))
        self.assertEqual(
            gql.calls,
            expect(
                title="PRD: Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
        )
        self.assertEqual(
            self.ledger(ctx), {SID: ["retitle", "comment", f"attach:{PRD}", "move:in_review"]}
        )
