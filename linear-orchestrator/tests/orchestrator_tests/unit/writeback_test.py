import functools
import json
import os
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import STATUSES
from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_tests.unit.run_fixtures import logged
from orchestrator_tests.unit.writeback_data import (
    APPROVE,
    FOOTER,
    HUMANS,
    ID,
    OTHER_ISSUE,
    OTHER_SID,
    PLAN_TEXT,
    PROJECT,
    READ,
    SID,
    SPEC_TEXT,
    START,
    TARGET,
    TREE,
    UUID,
    cid,
    expect,
    find,
    mine,
    move,
    outcome,
    plan_comment,
    post,
    reread,
    sha,
    spec_comment,
    sub,
)
from orchestrator_tests.unit.writeback_fakes import Gql
from orchestrator_tests.unit.writeback_scenarios import Base
from orchestrator_tests.unit.writeback_support import APPROVE_MAPPED, DOC, PR, PRD


class SayAndApprove(Base):
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
            "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }",
        )
        self.assertEqual(
            writeback.M_ATTACH,
            "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }",
        )
        self.assertEqual(
            writeback.M_UNARCHIVE, "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
        )
        self.assertEqual(
            writeback.Q_ATTACHED,
            "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }",
        )
        self.assertEqual(writeback.Q_ID, "query($i: String!) { issue(id: $i) { id team { id } } }")


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


class FinishLedger(Base):
    def test_second_finish_skips_ledgered_steps(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        done = outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again), done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_stops_at_first_error_and_resumes_there(self):
        broken = Gql(
            fail=lambda name, v: (
                False if name == "comment" and v["b"].startswith("Build ready") else None
            )
        )
        ctx = self.ctx(gql=broken)
        spec, plan = self.files(ctx)
        done = outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])
        self.assertFalse(writeback.finish(ctx, done))
        self.assertEqual(broken.calls[-1], post("comment", "Build ready: Ready."))
        self.assertEqual(
            self.lines()[-1],
            f"step-error {ID} step=comment error=LinearError: commentCreate: success: false",
        )
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again), done))
        self.assertEqual(
            again.calls,
            expect(
                body="Build ready: Ready.", state="in_review", attach=PR, attach_title="ENG-7: x"
            ),
        )

    def test_read_error_stops(self):
        gql = Gql(
            fail=lambda name, v: LinearError("linear api error: down") if name == "read" else None
        )
        ctx = self.ctx(gql=gql)
        self.assertFalse(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
        self.assertEqual(gql.calls, [READ])
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=read error=LinearError: linear api error: down"]
        )

    def test_other_sid_does_not_skip(self):
        ctx = self.ctx()
        self.write(ctx, "writeback.json", json.dumps({OTHER_SID: ["comment", "move:in_review"]}))
        gql = Gql()
        writeback.finish(replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted"))
        self.assertEqual(
            gql.calls, expect(body="Build failed: push not permitted", state="in_review")
        )
        self.assertEqual(
            self.ledger(ctx),
            {OTHER_SID: ["comment", "move:in_review"], SID: ["comment", "move:in_review"]},
        )

    def test_unreadable_ledger_is_empty(self):
        for text in (
            "not json",
            json.dumps([1]),
            json.dumps({SID: "comment move:in_review"}),
            json.dumps({SID: [1]}),
        ):
            with self.subTest(text=text):
                gql = Gql()
                ctx = self.ctx(gql=gql)
                self.write(ctx, "writeback.json", text)
                writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
                self.assertEqual(
                    gql.calls, expect(body="Build failed: push not permitted", state="in_review")
                )

    def test_fifo_ledger_does_not_block(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        os.mkfifo(os.path.join(ctx.workdir, "writeback.json"))
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertEqual(self.ledger(ctx), {SID: ["comment", "move:in_review"]})

    def test_symlinked_ledger_is_empty_and_replaced(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        elsewhere = os.path.join(self.tmp, "elsewhere.json")
        with open(elsewhere, "w") as f:
            f.write(json.dumps({SID: ["comment", "move:in_review"]}))
        os.symlink(elsewhere, os.path.join(ctx.workdir, "writeback.json"))
        writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
        self.assertEqual(
            gql.calls, expect(body="Build failed: push not permitted", state="in_review")
        )
        self.assertFalse(os.path.islink(os.path.join(ctx.workdir, "writeback.json")))
        with open(elsewhere) as f:
            self.assertEqual(json.load(f), {SID: ["comment", "move:in_review"]})

    def test_ledger_write_failure_leaves_no_temp(self):
        ctx = self.ctx()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            err = writeback._step(ctx, "comment", lambda: None)
        self.assertIsInstance(err, OSError)
        self.assertEqual(os.listdir(ctx.workdir), [])


class CommentIds(Base):
    def test_a_new_run_never_queries(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertTrue(writeback.limit(ctx, None))
        self.assertNotIn("find", [c[0] for c in gql.calls])
        self.assertEqual(
            [c for c in gql.calls if c[0] == "comment"],
            [
                post("start", "Build started: x"),
                post("comment", "Build failed: push not permitted"),
                post("limit", "Usage limit, reset time unknown."),
            ],
        )

    def test_a_resume_skips_ledgered_steps_without_a_query(self):
        ctx = self.ctx()
        done = outcome("failed", "ENG-7: x", "push not permitted")
        writeback.progress(ctx, START)
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        resumed = replace(ctx, gql=again, resume=True)
        writeback.progress(resumed, START)
        self.assertTrue(writeback.finish(resumed, done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_a_resume_finds_a_post_whose_ledger_write_was_lost(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            writeback.progress(ctx, START)
        self.assertEqual(gql.calls, [post("start", "Build started: x")])
        self.assertEqual(self.lines(), [f"step-error {ID} step=start error=OSError: disk full"])
        self.no_ledger(ctx)
        gql.calls.clear()
        writeback.progress(replace(ctx, resume=True), START)
        self.assertEqual(gql.calls, [find("start")])
        self.assertEqual(self.ledger(ctx), {SID: ["start"]})
        self.assertEqual(self.lines()[-1], f"step {ID} step=start found=true")

    def test_a_resumed_finish_counts_a_found_comment_as_posted(self):
        body = "Build failed: push not permitted"
        gql = Gql(comments={cid("comment"): mine(body)})
        ctx = self.ctx(gql=gql, resume=True)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertEqual(
            gql.calls,
            [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment"), reread(), move("in_review")],
        )
        self.assertEqual(self.ledger(ctx), {SID: ["comment", "move:in_review"]})
        self.assertEqual(
            self.lines(), [f"step {ID} step=comment found=true", f"step {ID} step=move:in_review"]
        )

    def test_a_resume_after_a_failed_post_finds_then_posts_once(self):
        broken = Gql(fail=lambda name, v: False if name == "comment" else None)
        ctx = self.ctx(gql=broken)
        done = outcome("failed", "ENG-7: x", "push not permitted")
        self.assertFalse(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again, resume=True), done))
        self.assertEqual(
            again.calls,
            expect(body="Build failed: push not permitted", state="in_review", resume=True),
        )
        self.assertEqual(len([c for c in again.calls if c[0] == "comment"]), 1)

    def test_a_find_error_is_the_steps_error(self):
        squatted = f"LinearError: comment {cid('comment')} is not ours"
        for name, comments, fail, error in (
            ("another issue's", {cid("comment"): mine("x", OTHER_ISSUE)}, None, squatted),
            (
                "another author's",
                {cid("comment"): {"body": "x", "issue": UUID, "mine": False}},
                None,
                squatted,
            ),
            (
                "unreadable",
                {},
                LinearError("linear api error: down"),
                "LinearError: linear api error: down",
            ),
        ):
            with self.subTest(name):
                gql = Gql(comments=comments, fail=lambda n, v: fail if n == "find" else None)
                ctx = self.ctx(gql=gql, resume=True)
                self.assertFalse(
                    writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
                )
                self.assertEqual(gql.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment")])
                self.assertEqual(self.lines()[-1], f"step-error {ID} step=comment error={error}")
                self.no_ledger(ctx)

