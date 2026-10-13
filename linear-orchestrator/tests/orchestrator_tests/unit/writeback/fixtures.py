import json
import os
import shutil
import tempfile

from agent_pm.linear_orchestrator.linear import linear, writeback
from orchestrator_tests.case import Case
from orchestrator_tests.unit import board_ids
from orchestrator_tests.unit.run.support import logged, show
from orchestrator_tests.unit.writeback.data import (
    HUMANS,
    ID,
    PLAN_TEXT,
    REPOS,
    SID,
    SPEC_TEXT,
    STATES,
    TARGET,
    UUID,
)
from orchestrator_tests.unit.writeback.fakes import Gql


class Base(Case):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        linear.use_logs(os.path.join(self.tmp, "logs"))

    def ctx(
        self,
        role="engineer",
        *,
        gql=None,
        resume=False,
        target=TARGET,
        project=None,
        sid=SID,
        workdir=None,
        humans=HUMANS,
    ):
        if workdir is None:
            workdir = tempfile.mkdtemp(dir=self.tmp)
        return writeback.Context(
            ident=ID,
            issue_id=UUID,
            role=role,
            sid=sid,
            resume=resume,
            project=project,
            workdir=workdir,
            gql=gql or Gql(),
            humans=humans,
            states=STATES,
            repos=REPOS,
            team=board_ids.TEAM,
            target=target,
        )

    def write(self, ctx, rel, text):
        path = os.path.join(ctx.workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def files(self, ctx):
        return (
            self.write(ctx, "src/agent-pm/docs/ENG-7-spec.md", SPEC_TEXT),
            self.write(ctx, "src/agent-pm/docs/ENG-7-plan.md", PLAN_TEXT),
        )

    def lines(self):
        events = logged()
        self.assertEqual(
            {(e["src"], e.get("sid", SID)) for e in events} - {("writeback", SID)}, set()
        )
        return [show({k: v for k, v in e.items() if k != "sid"}) for e in events]

    def ledger(self, ctx):
        with open(os.path.join(ctx.workdir, "writeback.json")) as f:
            return json.load(f)

    def no_ledger(self, ctx):
        self.assertFalse(os.path.exists(os.path.join(ctx.workdir, "writeback.json")))
