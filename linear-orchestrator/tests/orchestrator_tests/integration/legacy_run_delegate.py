"""Test bridge for legacy run.py state until TASK-374: run.py builds and runs the HarnessDelegate;
attach's view reads the run from runs.jsonl and its liveness from the legacy session names.
"""

import os

from agent_pm import delegate
from agent_pm.delegate import Delegate, NotDisplayable
from agent_pm.linear_orchestrator.engine import runs
from agent_pm.session import NoRecordedSession, RunLog, Session, SessionStatus
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import Record, Status, load_class

# run.py loads this class in its own process, whose import roots hold no utils.tests.
HARNESS = load_class("agent_pm.delegate.engine.harness_delegate:HarnessDelegate", Delegate)


class LegacyRunDelegate(HARNESS):
    @classmethod
    def attach(cls, record_or_workdir):
        return LegacyRunView(record_or_workdir)


class LegacyRunView:
    def __init__(self, record_or_workdir):
        self.workdir = (
            record_or_workdir.workdir
            if isinstance(record_or_workdir, Record)
            else record_or_workdir
        )
        self.issue = os.path.basename(self.workdir)
        path = os.path.join(os.path.dirname(os.path.dirname(self.workdir)), "logs", "runs.jsonl")
        entries = runs.parse_log(path)
        entry = runs.latest(entries)[self.issue]
        self.sid, self.role = entry.sid, entry.role
        self.client = runs.client_of(entries, entry.sid)
        Session.configure_from(delegate.read_config())
        self.names = (
            f"agent-pm-{self.role}-{self.issue}",
            f"{self.role}-{self.issue}-{self.sid[:8]}",
        )

    def alive(self):
        return any(row.name == self.names[0] for row in Session.list())

    def status(self):
        alive = self.alive()
        return Status(self.issue, alive, "working" if alive else "gone", None, None)

    def outcome(self):
        for row in reversed(RunLog(self.workdir).read()):
            if row.get("sid") == self.sid and row.get("kind") == "result" and row.get("outcome"):
                return delegate.Outcome(**row["outcome"])
        return None

    def close(self):
        for row in Session.list():
            if row.name in self.names:
                Session.host().attach(row.name).close()

    def session_info(self):
        try:
            return Session.inspect(
                SessionSpec(workdir=self.workdir, client=self.client, sid=self.sid)
            )
        except NoRecordedSession:
            return SessionStatus("no", None, "")

    def detach(self, *, outer=None):
        raise AssertionError("legacy fixture launch must use run.py")

    def run(self, on_event=None):
        raise AssertionError("legacy fixture execution must use run.py")

    def show(self):
        raise NotDisplayable("legacy query fixture cannot display")

    def attach_command(self):
        return None

    def resume(self):
        raise AssertionError("legacy fixture resume must use router's launch path")
