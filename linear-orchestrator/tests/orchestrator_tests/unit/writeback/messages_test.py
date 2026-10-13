from dataclasses import replace

from agent_pm.delegate import STATUSES, Outcome
from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_tests.unit.run.support import logged
from orchestrator_tests.unit.writeback.data import (
    APPROVE,
    FOOTER,
    ID,
    PLAN_TEXT,
    PROJECT,
    SID,
    SPEC_TEXT,
    TARGET,
    TREE,
    WritebackEffects,
    expect,
    plan_comment,
    sha,
    spec_comment,
)
from orchestrator_tests.unit.writeback.fakes import Gql
from orchestrator_tests.unit.writeback.fixtures import Base
from orchestrator_tests.unit.writeback.support import APPROVE_MAPPED, DOC, PR, PRD


class ApprovalMessages(Base):
    def test_say(self):
        self.assertEqual(writeback.say("", "just text"), "just text")
        self.assertEqual(writeback.say("Build started:", ""), "Build started")
        self.assertEqual(
            writeback.say("Build started:", "first build of the PRD"),
            "Build started: first build of the PRD",
        )
        self.assertEqual(writeback.say("Build ready:", "a\nb"), "Build ready:\na\nb")

    def test_approve_line(self):
        self.assertEqual(writeback.approve_line(None), APPROVE)
        self.assertEqual(writeback.approve_line("ophis/agent-pm"), APPROVE_MAPPED)

    def test_queries_verbatim(self):
        self.assertEqual(
            writeback.M_TITLE,
            (
                "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { "
                "title: $t }) { success } }"
            ),
        )
        self.assertEqual(
            writeback.M_ATTACH,
            (
                "mutation($i: String!, $u: String!, $t: String) { "
                "attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
            ),
        )
        self.assertEqual(
            writeback.M_UNARCHIVE, "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
        )
        self.assertEqual(
            writeback.Q_ATTACHED,
            "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }",
        )
        self.assertEqual(writeback.Q_ID, "query($i: String!) { issue(id: $i) { id team { id } } }")


class CompletionMessages(Base):
    def outcomes(self, role, spec, plan):
        kind, files = config.TASKS[role].kind, [plan, spec]
        if kind == "research":
            return {
                "done": Outcome(
                    status="done",
                    summary="Three queues compared.\nRedis streams fit best.",
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                ),
                "needs_input": Outcome(
                    status="needs_input",
                    questions=list(["Which repo?", "Which version?"]),
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                    summary="",
                ),
                "failed": Outcome(
                    status="failed",
                    summary="gh api failed",
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                ),
            }
        if kind == "design":
            return {
                "done": Outcome(
                    status="done",
                    title="Session Registry",
                    summary="PRD for the registry.",
                    url=PRD,
                    files=list(files),
                ),
                "needs_input": Outcome(
                    status="needs_input",
                    title="Session Registry",
                    questions=list(["Who uses it?", "Web or CLI?"]),
                    files=list(files),
                    summary="",
                ),
                "failed": Outcome(
                    status="failed",
                    title="Session Registry",
                    summary="Could not publish.",
                    files=list(files),
                ),
            }
        return {
            "done": Outcome(
                status="done",
                title="ENG-7: Session registry",
                summary="Adds the registry.\nVerify: python3 -m unittest",
                url=PR,
                files=list(files),
            ),
            "needs_input": Outcome(
                status="needs_input",
                title="ENG-7: Session registry",
                questions=list(["Keep the old API?"]),
                files=list(files),
                summary="",
            ),
            "failed": Outcome(
                status="failed",
                title="ENG-7: Session registry",
                summary="push not permitted",
                url=TREE,
                files=list(files),
            ),
        }

    def golden(self, role, status, resume):
        research = (
            "1. Which repo?\n2. Which version?\n\n"
            + FOOTER
            + " To change the target repo, edit the description's `Repo:` line."
        )
        files = [spec_comment(), plan_comment()] if config.TASKS[role].files else []
        table = {
            ("research", "done"): expect(
                WritebackEffects(
                    resume=resume,
                    body=f"Three queues compared.\nRedis streams fit best.\n\n{DOC}",
                    state="in_review",
                    attach=DOC,
                    attach_title="Queue options",
                )
            ),
            ("research", "needs_input"): expect(
                WritebackEffects(resume=resume, body=research, state="in_review")
            ),
            ("research", "failed"): (
                expect(
                    WritebackEffects(
                        resume=resume, body=f"gh api failed\n\n{DOC}", state="in_review"
                    )
                )
                if resume
                else expect(
                    WritebackEffects(
                        resume=resume, body=f"gh api failed\n\n{DOC}", state="todo", subscribe=False
                    )
                )
            ),
            ("design", "done"): expect(
                WritebackEffects(
                    resume=resume,
                    title="PRD: Session Registry",
                    body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE}",
                    state="in_review",
                    attach=PRD,
                    attach_title="Session Registry",
                )
            ),
            ("design", "needs_input"): expect(
                WritebackEffects(
                    resume=resume,
                    body=f"1. Who uses it?\n2. Web or CLI?\n\n{FOOTER}",
                    state="in_review",
                )
            ),
            ("design", "failed"): expect(
                WritebackEffects(resume=resume, body="Could not publish.", state="in_review")
            ),
            ("build", "done"): expect(
                WritebackEffects(
                    resume=resume,
                    files=files,
                    state="in_review",
                    body="Build ready:\nAdds the registry.\nVerify: python3 -m unittest",
                    attach=PR,
                    attach_title="ENG-7: Session registry",
                )
            ),
            ("build", "needs_input"): expect(
                WritebackEffects(
                    resume=resume,
                    body=f"Question:\n1. Keep the old API?\n\n{FOOTER}",
                    state="in_review",
                )
            ),
            ("build", "failed"): expect(
                WritebackEffects(
                    resume=resume,
                    files=files,
                    state="in_review",
                    body=f"Build failed: push not permitted\n\n{TREE}",
                )
            ),
        }
        return table[(config.TASKS[role].kind, status)]

    def test_every_role_status_and_mode(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        for role in config.TASKS:
            for status in STATUSES:
                for resume in (False, True):
                    with self.subTest(role=role, status=status, resume=resume):
                        gql = Gql()
                        build = config.TASKS[role].kind == "build"
                        ctx = replace(
                            self.ctx(role, gql=gql), resume=resume, target=TARGET if build else None
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
        ctx = replace(self.ctx("pm", gql=gql), target=None, project=PROJECT)
        writeback.finish(
            ctx,
            Outcome(
                status="done", title="Session Registry", summary="PRD for the registry.", url=PRD
            ),
        )
        self.assertEqual(
            gql.calls,
            expect(
                facts=WritebackEffects(
                    title="PRD: Session Registry",
                    body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}",
                    state="in_review",
                    attach=PRD,
                    attach_title="Session Registry",
                )
            ),
        )
        self.assertEqual(
            self.ledger(ctx), {SID: ["retitle", "comment", f"attach:{PRD}", "move:in_review"]}
        )
