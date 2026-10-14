"""Configured fake run fixtures, shared by LinearRun and CLI tests."""

from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.session import Usage
from orchestrator_tests.case import Case

from delegate.tests.fakes.delegate import FakeDelegate

SID = "1234abcd-0000-4000-8000-000000000000"


class ReadyDelegate(FakeDelegate):
    usage_result = Usage(None, None)
    usage_calls = []

    @classmethod
    def usage(cls, role):
        cls.usage_calls.append(role)
        return super().usage(role)


class RunCase(Case):
    def setUp(self):
        ReadyDelegate.usage_calls = []
        self.cfg = config.Config(
            team="team",
            states={},
            human_members=(),
            harness_key="fake",
            task_label_group="labels",
            task_labels={},
            roles={},
            project_repos={},
            local_clones={},
            work_dir=self.home,
            protect=("/protected/package", "/protected/checkout"),
            docs=config.Docs("", "", {}),
            stages={},
            delegate_kind="fake",
            delegates={"fake": ReadyDelegate, "harness": ReadyDelegate},
        )

    def start(self, issue="TASK-1", *, sid=SID, delegate_cls=None, display=None):
        owner = LinearRun(
            issue,
            "Echo input.",
            self.cfg.run_dir(issue),
            cfg=self.cfg,
            role="dummy",
            sid=sid,
            task="echo",
            runtime="fake",
            display=display,
            delegate_cls=delegate_cls,
        )
        runs.note(
            self.cfg.runs_log,
            "start",
            issue=issue,
            sid=sid,
            role="dummy",
            runtime="fake",
            delegate_kind="fake",
        )
        return owner, owner.detach()
