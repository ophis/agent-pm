"""FakeSession, a Session in the caller's process with no Host (it cannot display). Its sessions yield the class's
script (FakeSession's is empty; scripted(*events) makes a subclass with those), then Exited(0) unless it ends with
Exited. FakeSession and each subclass have their own started (start's arguments), live (name → session; stop and
close remove it) and configured."""

from __future__ import annotations

import re
import uuid

from agent_pm.session import DriverError, NotDisplayable, Session
from agent_pm.session.engine.api import (
    Exited,
    Reply,
    SessionInfo,
    SessionRow,
    SessionStatus,
    TurnEnd,
)

from tests.fakes.host import FakeHost


class FakeSession(Session):
    script: tuple = ()
    started: list[dict] = []
    live: dict[str, FakeSession] = {}
    configured: dict | None = None
    usage_result = None

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.started, cls.live, cls.configured = [], {}, None

    @classmethod
    def scripted(cls, *events) -> type[FakeSession]:
        return type(cls.__name__, (cls,), {"script": events})

    def __init__(self, runtime, mode, spec, sid, name, display) -> None:
        self.runtime, self.mode, self.spec, self.sid, self.name = runtime, mode, spec, sid, name
        self.display_value = display
        self.sent: list[str] = []
        self._state, self._reply = "live", None
        ended = self.script and isinstance(self.script[-1], Exited)
        self._pending = iter(self.script if ended else (*self.script, Exited(0)))

    def events(self):
        for event in self._pending:
            if isinstance(event, Reply):
                self._reply = event.text
            elif isinstance(event, TurnEnd) and event.reply:
                self._reply = event.reply
            yield event

    def send(self, text: str) -> None:
        self.sent.append(text)

    def state(self) -> str:
        return self._state

    def stop(self) -> None:
        self._state = "absent"
        if self.live.get(self.name) is self:
            del self.live[self.name]

    def close(self) -> None:
        self.stop()

    def info(self) -> SessionInfo:
        return SessionInfo(
            self.sid, self.sid, self.spec.access.cwd or self.spec.workdir, False, "", ""
        )

    @property
    def reply(self) -> str | None:
        return self._reply

    def session_info(self) -> SessionStatus:
        return SessionStatus("yes", None, "")

    def record(self) -> None:
        return None

    @property
    def displayable(self) -> bool:
        return False

    def show(self, display=None):
        raise NotDisplayable("host none cannot display")

    def attach_command(self):
        raise NotDisplayable("host none cannot display")

    def screen(self, lines=None):
        raise NotDisplayable("host none cannot display")

    @classmethod
    def configure(cls, *, adapters, hosts, default_host) -> None:
        cls.configured = {"adapters": adapters, "hosts": hosts, "default_host": default_host}

    @classmethod
    def start(
        cls,
        runtime,
        mode,
        prompt,
        spec,
        *,
        sid=None,
        resume=None,
        name=None,
        events=None,
        idle_timeout=None,
        display=None,
        host=None,
    ) -> FakeSession:
        cls.started.append(
            {
                "runtime": runtime,
                "mode": mode,
                "prompt": prompt,
                "spec": spec,
                "sid": sid,
                "resume": resume,
                "name": name,
                "events": events,
                "idle_timeout": idle_timeout,
                "display": display,
                "host": host,
            }
        )
        sid = resume or sid or str(uuid.uuid4())
        name = name or f"{runtime}-{sid[:8]}"
        if name in cls.live:
            raise DriverError(f"session {name} is live")
        cls.live[name] = cls(runtime, mode, spec, sid, name, display)
        return cls.live[name]

    @classmethod
    def attach(cls, name, *, host=None, idle_timeout=None) -> FakeSession:
        if name not in cls.live:
            raise DriverError(f"no session {name}")
        return cls.live[name]

    @classmethod
    def list(cls, match=None, *, display=None) -> list[SessionRow]:
        return [
            SessionRow(name, s.sid, s.runtime, "working", None, None, None, None, False)
            for name, s in sorted(cls.live.items())
            if (match is None or re.fullmatch(match, name))
            and (display is None or display != "" and s.display_value == display)
        ]

    @classmethod
    def usage(cls, runtime):
        return cls.usage_result

    @classmethod
    def layout(cls, display=None) -> None:
        return None

    @classmethod
    def writable_dirs(cls, runtime, spec, resume=None) -> tuple[str, ...]:
        return ()

    @classmethod
    def display(cls, value=None, *, host=None):
        return value

    @classmethod
    def host(cls, name=None) -> FakeHost:
        return FakeHost(name or "fake")
