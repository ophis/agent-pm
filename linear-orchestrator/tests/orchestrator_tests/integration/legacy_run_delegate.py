"""Test bridge for legacy run.py state until TASK-374 writes durable Delegate state."""

import os

from agent_pm import delegate
from agent_pm.delegate import Delegate, NotDisplayable
from agent_pm.linear_orchestrator.engine import runs
from agent_pm.session import NoRecordedSession, RunLog, SessionStatus
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import Record, Status


class LegacyRunDelegate(Delegate):
    @classmethod
    def roles(cls):
        return {role: delegate.task_index(role) for role in delegate.read_config()["roles"]}

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        return delegate.resolve_run(role, task, overlays=overlays)

    @classmethod
    def trusted_dirs(cls):
        return delegate.trusted_dirs()

    @classmethod
    def usage(cls, role, *, overlays=()):
        return delegate.configure().session_cls.usage(cls.resolve(role, overlays=overlays).client)

    @classmethod
    def attach(cls, record_or_workdir):
        view = cls.__new__(cls)
        view.workdir = (
            record_or_workdir.workdir
            if isinstance(record_or_workdir, Record)
            else record_or_workdir
        )
        view.issue = os.path.basename(view.workdir)
        path = os.path.join(os.path.dirname(os.path.dirname(view.workdir)), "logs", "runs.jsonl")
        entries = runs.parse_log(path)
        entry = runs.latest(entries)[view.issue]
        view.sid, view.role = entry.sid, entry.role
        view.client = runs.client_of(entries, entry.sid)
        view.session_cls = delegate.configure().session_cls
        view.names = (
            f"agent-pm-{view.role}-{view.issue}",
            f"{view.role}-{view.issue}-{view.sid[:8]}",
        )
        return view

    def alive(self):
        return any(row.name == self.names[0] for row in self.session_cls.list())

    def status(self):
        alive = self.alive()
        return Status(self.issue, alive, "working" if alive else "gone", None, None)

    def outcome(self):
        for row in reversed(RunLog(self.workdir).read()):
            if row.get("sid") == self.sid and row.get("kind") == "result" and row.get("outcome"):
                return delegate.Outcome(**row["outcome"])
        return None

    def close(self):
        for row in self.session_cls.list():
            if row.name in self.names:
                self.session_cls.host().attach(row.name).close()

    def session_info(self):
        try:
            return self.session_cls.inspect(
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
