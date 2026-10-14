"""A process-free Delegate boundary for retained orchestrator scheduling scenarios."""

import os
from dataclasses import replace

from agent_pm.delegate import ConfigError, Delegate, SessionError
from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import NoRecordedSession, SessionStatus
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import Record, Status


def provider(api, cfg):
    class ScenarioDelegate(Delegate):
        @classmethod
        def attach(cls, record_or_workdir):
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
                raise SessionError("agent runs unknown")
            return config.driver_name(self.entry.role, self.issue) in api.live

        def status(self):
            live = self.alive()
            return Status(
                self.issue, live, "working" if live else "gone", None, self.attach_command()
            )

        def outcome(self):
            return None

        def show(self):
            return api.show(config.driver_name(self.entry.role, self.issue))

        def close(self):
            return api.close(config.driver_name(self.entry.role, self.issue))

        def attach_command(self):
            return api.attach_command(config.driver_name(self.entry.role, self.issue))

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
            raise AssertionError("scenario must not launch")

        def run(self, on_event=None):
            raise AssertionError("scenario must not launch")

        def resume(self):
            raise AssertionError("legacy launcher owns this scenario")

    return ScenarioDelegate
