"""A Linear issue's run, owned and recovered through its recorded Delegate implementation; its
harness calls `agent-pm-linear writeback --issue ID --workdir <workdir>` on each event.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace

from agent_pm.delegate import Delegate, Spec
from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.linear_orchestrator.engine.types import ISSUE_ID, UUID_RE, Config, ConfigError
from agent_pm.utils import Display, Record, Status


class LinearRun:
    def __init__(
        self,
        issue: str,
        input: str,
        workdir: str,
        *,
        cfg: Config,
        role: str,
        sid: str,
        task: str | None = None,
        client: str | None = None,
        mode: str = "headless",
        resume: bool = False,
        events: str | None = None,
        display: Display = None,
        delegate_kind: str | None = None,
        delegate_cls: type | None = None,
        read: tuple[str, ...] = (),
    ):
        self._identity(issue, sid)
        self.issue, self.sid = issue, sid
        self.delegate_kind = delegate_kind or cfg.delegate_kind
        provider = delegate_cls or self._provider(cfg, self.delegate_kind)
        self.delegate: Delegate = provider(
            role,
            Spec(
                input,
                workdir,
                task=task,
                client=client,
                sid=sid,
                mode=mode,
                resume=resume,
                name=config.tui_name(role, issue, sid),
                events=events,
                out=os.path.join(workdir, "out.md"),
                read=read,
                cwd=workdir,
            ),
            display=display,
            callback=["agent-pm-linear", "writeback", "--issue", issue, "--workdir", workdir],
            overlays=(cfg.core,),
        )

    @staticmethod
    def _identity(issue: str, sid: str | None = None) -> None:
        if not re.fullmatch(ISSUE_ID, issue) or sid is not None and not UUID_RE.fullmatch(sid):
            raise ConfigError(f"bad issue or session id: {issue} {sid}")

    @staticmethod
    def _provider(cfg: Config, kind: str) -> type:
        if kind not in cfg.delegates:
            raise ConfigError(f"delegate kind {kind!r} is no longer configured")
        return cfg.delegates[kind]

    @classmethod
    def check_tui(cls, *, cfg: Config, display: Display, events: str) -> None:
        """ConfigError unless an attended run of the configured Delegate kind could show at
        display and write events (Delegate.check_tui).
        """
        cls._provider(cfg, cfg.delegate_kind).check_tui(display=display, events=events)

    @classmethod
    def attach(
        cls,
        issue: str,
        *,
        cfg: Config,
        delegate_cls: type | None = None,
        sid: str | None = None,
    ) -> LinearRun:
        cls._identity(issue, sid)
        entries = [entry for entry in runs.parse_log(cfg.runs_log) if entry.issue == issue]
        if not entries:
            raise ConfigError(f"{issue}: no recorded run")
        latest = runs.latest(entries)[issue]
        if sid is not None and sid != latest.sid:
            raise ConfigError(f"{issue}: session {sid} is no longer the latest run")
        view = cls.__new__(cls)
        view.issue, view.sid = issue, latest.sid
        view.delegate_kind = runs.delegate_of(entries, latest.sid)
        provider = delegate_cls or cls._provider(cfg, view.delegate_kind)
        view.delegate = provider.attach(cfg.run_dir(issue), sid=latest.sid)
        return view

    def _record(self, record: Record) -> Record:
        return replace(
            record,
            kind="pipeline",
            name=self.issue,
            ctl=("agent-pm-linear",),
            resume=("agent-pm-linear", "resume", self.issue),
        )

    def detach(self) -> Record:
        return self._record(
            self.delegate.detach(
                outer={
                    "kind": "pipeline",
                    "name": self.issue,
                    "ctl": ("agent-pm-linear",),
                    "resume": ("agent-pm-linear", "resume", self.issue),
                }
            )
        )

    def alive(self) -> bool:
        return self.delegate.alive()

    def status(self) -> Status:
        return replace(self.delegate.status(), name=self.issue)

    def outcome(self):
        return self.delegate.outcome()

    def show(self) -> str | None:
        return self.delegate.show()

    def close(self) -> None:
        self.delegate.close()

    def forget(self) -> None:
        self.delegate.forget()

    def attach_command(self) -> str | None:
        return self.delegate.attach_command()

    def session_info(self):
        return self.delegate.session_info()

    def resume(self) -> Record:
        return self._record(self.delegate.resume())
