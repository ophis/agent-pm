import os
from dataclasses import replace

from agent_pm.delegate import Begun, Finished, Progress, Result
from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import issues, writeback
from orchestrator_test_helpers.unit.clock import logs_at
from orchestrator_test_helpers.unit.writeback.data import (
    ID,
    OTHER_SID,
    SID,
    START,
    outcome,
    post,
    sha,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback.progress_events_fixtures import (
    ProgressEvents as ProgressEventsBase,
)


class ProgressEvents1(ProgressEventsBase):
    def test_start_once_per_sid(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        start = Progress("start", "first build of the PRD")
        writeback.progress(ctx, start)
        writeback.progress(ctx, start)
        self.assertEqual(gql.calls, [post("start", "Build started: first build of the PRD")])
        self.assertEqual(self.ledger(ctx), {SID: ["start"]})
        self.assertEqual(self.lines(), [f"step {ID} step=start"])
        writeback.progress(replace(ctx, sid=OTHER_SID), start)
        self.assertEqual(
            gql.calls[1:], [post("start", "Build started: first build of the PRD", OTHER_SID)]
        )

    def test_start_comment_is_the_roles_lead_and_the_runs_pick(self):
        pick = "light-build: a template wording tweak, like TASK-226."
        for role, text, body in (
            ("researcher", "budget 2 rounds", "Research started: budget 2 rounds"),
            ("pm", "budget 2 rounds", "PRD started: budget 2 rounds"),
            ("engineer", pick, f"Build started: {pick}"),
        ):
            with self.subTest(role=role, text=text):
                gql = Gql()
                writeback.progress(self.ctx(role, gql=gql), Progress("start", text))
                self.assertEqual(gql.calls, [post("start", body)])
                if role == "engineer":
                    self.assertTrue(issues.BUILD_STARTED.match(body))

    def test_ignores_begun_and_finished(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, Begun(SID))
        writeback.progress(ctx, Finished(Result(0, outcome("done", summary="x"))))
        self.assertEqual(gql.calls, [])
        self.assertEqual(self.lines(), [])

    def test_posts_every_other_mark_once_per_text(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        for text in ("12 passing", "12 passing", "13 passing"):
            writeback.progress(ctx, Progress("tests", text))
        steps = [f"progress:tests:{sha('12 passing')}", f"progress:tests:{sha('13 passing')}"]
        self.assertEqual(
            gql.calls,
            [
                post(steps[0], "Progress (tests): 12 passing"),
                post(steps[1], "Progress (tests): 13 passing"),
            ],
        )
        self.assertEqual(self.ledger(ctx), {SID: steps})

    def test_swallows_system_exit_and_retries_later(self):
        gql = Gql(fail=lambda _name, _v: LinearError("linear api error: down"))
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=start error=LinearError: linear api error: down"]
        )
        ok = Gql()
        writeback.progress(replace(ctx, gql=ok), START)
        self.assertEqual(ok.calls, [post("start", "Build started: x")])

    def test_signal_exit_passes_through(self):
        gql = Gql(fail=lambda _name, _v: Stopped(129))
        ctx = self.ctx(gql=gql)
        with self.assertRaises(Stopped) as cm:
            writeback.progress(ctx, START)
        self.assertEqual(cm.exception.code, 129)
        self.no_ledger(ctx)
        self.assertEqual(self.lines(), [])

    def test_never_raises(self):
        gql = Gql(fail=lambda _name, _v: RuntimeError("boom"))
        open(os.path.join(self.tmp, "file"), "w").close()
        with logs_at(os.path.join(self.tmp, "file", "logs")):
            writeback.progress(self.ctx(gql=gql), START)
        writeback.progress(replace(self.ctx(), role="nope"), START)
        self.assertEqual(self.lines(), [f"step-error {ID} step=sink error=KeyError: 'nope'"])
