import functools

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.writeback.base_fixtures import Base
from orchestrator_test_helpers.unit.writeback.data import (
    APPROVE,
    FOOTER,
    TREE,
    expect,
    outcome,
    plan_comment,
    spec_comment,
)
from orchestrator_test_helpers.unit.writeback_support import DOC, PR, PRD


class FinishGolden(Base):
    def outcomes(self, role, spec, plan):
        kind, files = config.TASKS[role].kind, [plan, spec]
        if kind == "research":
            return {
                "done": outcome(
                    "done",
                    summary="Three queues compared.\nRedis streams fit best.",
                    url=DOC,
                    files=files,
                ),
                "needs_input": outcome(
                    "needs_input", questions=["Which repo?", "Which version?"], url=DOC, files=files
                ),
                "failed": outcome("failed", summary="gh api failed", url=DOC, files=files),
            }
        if kind == "design":
            return {
                "done": outcome(
                    "done", "Session Registry", "PRD for the registry.", url=PRD, files=files
                ),
                "needs_input": outcome(
                    "needs_input",
                    "Session Registry",
                    questions=["Who uses it?", "Web or CLI?"],
                    files=files,
                ),
                "failed": outcome("failed", "Session Registry", "Could not publish.", files=files),
            }
        return {
            "done": outcome(
                "done",
                "ENG-7: Session registry",
                "Adds the registry.\nVerify: python3 -m unittest",
                url=PR,
                files=files,
            ),
            "needs_input": outcome(
                "needs_input",
                "ENG-7: Session registry",
                questions=["Keep the old API?"],
                files=files,
            ),
            "failed": outcome(
                "failed", "ENG-7: Session registry", "push not permitted", url=TREE, files=files
            ),
        }

    def golden(self, role, status, resume):
        research = (
            "1. Which repo?\n2. Which version?\n\n"
            + FOOTER
            + " To change the target repo, edit the description's `Repo:` line."
        )
        files = [spec_comment(), plan_comment()] if config.TASKS[role].files else []
        e = functools.partial(expect, resume=resume)
        table = {
            ("research", "done"): e(
                body=f"Three queues compared.\nRedis streams fit best.\n\n{DOC}",
                state="in_review",
                attach=DOC,
                attach_title="Queue options",
            ),
            ("research", "needs_input"): e(body=research, state="in_review"),
            ("research", "failed"): (
                e(body=f"gh api failed\n\n{DOC}", state="in_review")
                if resume
                else e(body=f"gh api failed\n\n{DOC}", state="todo", subscribe=False)
            ),
            ("design", "done"): e(
                title="PRD: Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
            ("design", "needs_input"): e(
                body=f"1. Who uses it?\n2. Web or CLI?\n\n{FOOTER}", state="in_review"
            ),
            ("design", "failed"): e(body="Could not publish.", state="in_review"),
            ("build", "done"): e(
                files=files,
                state="in_review",
                body="Build ready:\nAdds the registry.\nVerify: python3 -m unittest",
                attach=PR,
                attach_title="ENG-7: Session registry",
            ),
            ("build", "needs_input"): e(
                body=f"Question:\n1. Keep the old API?\n\n{FOOTER}", state="in_review"
            ),
            ("build", "failed"): e(
                files=files, state="in_review", body=f"Build failed: push not permitted\n\n{TREE}"
            ),
        }
        return table[(config.TASKS[role].kind, status)]
