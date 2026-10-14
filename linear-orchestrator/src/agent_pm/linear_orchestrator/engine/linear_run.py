"""A Linear issue's run, owned and recovered through its recorded Delegate implementation."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import replace

from agent_pm.delegate import Delegate, Spec
from agent_pm.linear_orchestrator.engine import runs
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
        runtime: str | None = None,
        mode: str = "headless",
        display: Display = None,
        delegate_kind: str | None = None,
        delegate_cls: type | None = None,
        callback: Sequence[str] | None = None,
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
                runtime=runtime,
                sid=sid,
                mode=mode,
                name=f"{role}-{issue}-{sid[:8]}",
                out=os.path.join(workdir, "out.md"),
                cwd=workdir,
                protect=cfg.protect,
            ),
            display=display,
            callback=callback,
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
        view.delegate = provider.attach(cfg.run_dir(issue))
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

    def attach_command(self) -> str | None:
        return self.delegate.attach_command()

    def session_info(self):
        return self.delegate.session_info()

    def resume(self) -> Record:
        return self._record(self.delegate.resume())
