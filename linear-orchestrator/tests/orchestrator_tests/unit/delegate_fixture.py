"""A process-free Delegate boundary for orchestrator scheduling and launch scenarios: a run is
alive while api.live holds its harness name `agent-pm-<role>-<ID>`; a constructed run records
(role, spec, options) in api.constructed (construct_error raises), its detach in api.detached
(detach_error raises) with runs.jsonl's text then in api.runs_at_detach, and puts its name in
api.live.
"""

import os
from dataclasses import replace

from agent_pm.delegate import ConfigError, Delegate, DriverError
from agent_pm.linear_orchestrator.engine import runs
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import NoRecordedSession, SessionStatus
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import Record, Status


def harness_name(role, issue):
    return f"agent-pm-{role}-{issue}"


def provider(api, cfg):
    class ScenarioDelegate(Delegate):
        def __init__(self, role, spec, **options):
            api.constructed.append((role, spec, options))
            if api.construct_error is not None:
                raise api.construct_error
            self.role, self.spec = role, spec

        @classmethod
        def attach(cls, record_or_workdir, *, sid=None):
            workdir = (
                record_or_workdir.workdir
                if isinstance(record_or_workdir, Record)
                else record_or_workdir
            )
            view = cls.__new__(cls)
            view.workdir = workdir
            view.issue = os.path.basename(workdir)
            entries = [e for e in runs.parse_log(cfg.runs_log) if e.issue == view.issue]
            if not entries:
                raise ConfigError(f"{view.issue}: no recorded run")
            view.entry = entries[-1]
            if sid is not None and sid != view.entry.sid:
                raise ConfigError(f"sid {sid} is not the run's")
            return view

        @classmethod
        def usage(cls, role, *, overlays=()):
            try:
                return api.usage(cls.resolve(role, overlays=overlays).client)
            except SessionConfigError as error:
                raise ConfigError(str(error)) from None

        @classmethod
        def roles(cls):
            return {role: {task: task for task in data.tasks} for role, data in cfg.roles.items()}

        @classmethod
        def resolve(cls, role, task=None, *, overlays=()):
            return replace(cfg.roles[role].run, task=task or "")

        @classmethod
        def trusted_dirs(cls):
            return frozenset()

        def alive(self):
            if api.live is None:
                raise DriverError("agent runs unknown")
            return harness_name(self.entry.role, self.issue) in api.live

        def status(self):
            live = self.alive()
            return Status(
                self.issue, live, "working" if live else "gone", None, self.attach_command()
            )

        def outcome(self):
            return None

        def show(self):
            return api.show(harness_name(self.entry.role, self.issue))

        def close(self):
            return api.close(harness_name(self.entry.role, self.issue))

        def attach_command(self):
            return api.attach_command(harness_name(self.entry.role, self.issue))

        def session_info(self):
            client = runs.client_of(runs.parse_log(cfg.runs_log), self.entry.sid)
            try:
                return api.inspect(
                    SessionSpec(workdir=self.workdir, client=client, sid=self.entry.sid)
                )
            except NoRecordedSession:
                return SessionStatus("no", None, "")
            except SessionConfigError as error:
                raise ConfigError(str(error)) from None

        def detach(self, *, outer=None):
            issue = os.path.basename(self.spec.workdir)
            name = harness_name(self.role, issue)
            api.order.append(("detach", name))
            api.detached.append((self.role, self.spec, api.constructed[-1][2]))
            with open(cfg.runs_log, encoding="utf-8") as f:
                api.runs_at_detach.append(f.read())
            if api.detach_error is not None:
                raise api.detach_error
            if api.live is not None:
                api.live.append(name)
            return Record(
                1,
                (outer or {}).get("kind", "role"),
                issue,
                (name,),
                self.spec.sid,
                self.spec.workdir,
                tuple((outer or {}).get("ctl", ("agent-pm-delegate",))),
                tuple((outer or {}).get("resume", ("agent-pm-delegate", "resume", issue))),
                None,
                None,
            )

        def run(self, on_event=None):
            raise AssertionError("scenario must not launch")

        def resume(self):
            raise AssertionError("legacy launcher owns this scenario")

    return ScenarioDelegate
