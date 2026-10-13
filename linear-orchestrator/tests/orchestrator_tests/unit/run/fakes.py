import os

from agent_pm.delegate import Begun, Finished
from orchestrator_tests.unit.run.data import NO_OUTCOME


class FakeRun:
    """delegate.run: records its call, raises `error`, else sends Begun (unless `begun` is False),
    then each step (an event, or a callable it calls), then Finished when the result has an outcome,
    and returns the result.
    """

    def __init__(self, order, result=NO_OUTCOME, steps=(), error=None, *, begun=True):
        self.order, self.result, self.steps, self.error = order, result, steps, error
        self.begun, self.calls, self.dirs = begun, [], []

    def __call__(self, run, spec, *, driver, on_event):
        self.driver = driver
        self.order.append("run")
        self.calls.append((run, spec))
        self.dirs.append(os.path.isdir(spec.workdir))
        if self.error:
            raise self.error
        if self.begun:
            on_event(Begun(spec.sid))
        for step in self.steps:
            if callable(step):
                step()
            else:
                on_event(step)
        if self.result.outcome is not None:
            on_event(Finished(self.result))
        return self.result
