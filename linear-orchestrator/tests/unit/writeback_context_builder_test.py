import os
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit import run_fixtures
from orchestrator_test_helpers.unit.writeback.context_builder_fixtures import (
    ContextBuilder as ContextBuilderBase,
)
from orchestrator_test_helpers.unit.writeback.data import ID, PROJECT, SID, TARGET, UUID


class ContextBuilder1(ContextBuilderBase):
    def test_the_one_context_of_a_role_from_the_config(self):
        cfg = config.load()
        gql = mock.Mock(return_value={})
        for resume, target in ((False, TARGET), (True, None)):
            with self.subTest(resume=resume):
                ctx = writeback.context(
                    cfg,
                    "engineer",
                    gql,
                    ident=ID,
                    issue_id=UUID,
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    target=target,
                )
                want = writeback.Context(
                    ident=ID,
                    issue_id=UUID,
                    role="engineer",
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    workdir=os.path.join(self.work, "work", ID),
                    gql=ctx.gql,
                    humans=("me@x.com",),
                    states=cfg.states,
                    repos={PROJECT: "ophis/agent-pm"},
                    team=cfg.team,
                    target=target,
                )
                self.assertEqual(ctx, want)
                ctx.gql("q", i=UUID)
                gql.assert_called_with("q", service=run_fixtures.KEY, i=UUID)
