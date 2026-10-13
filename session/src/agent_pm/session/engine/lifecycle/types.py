"""A live session handle, its mode protocol and caller-supplied state and services."""

import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Protocol

from agent_pm.session.adapters.types import Adapter
from agent_pm.session.engine import records
from agent_pm.session.engine.types import (
    Blocked,
    Event,
    Exited,
    IdleTimeout,
    Reply,
    SessionBound,
    SessionInfo,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
)

POLL = 0.5


class Mode(Protocol):
    """A running session as one mode hosts it; a Handle's only way to it. Its own failure raises
    from poll (headless: its parse); the session's is its returncode (an in_process run that raised:
    1).
    """

    def poll(self, timeout: float) -> tuple[list[Event | Stderr], bool]:
        """The events that came, waiting up to `timeout` s for one, and whether no more will come."""
        ...

    def returncode(self, timeout: float) -> int | None:
        """The exit status, waiting at most `timeout` s; None while it runs; negative: killed by
        that signal.
        """
        ...

    def state(self) -> str:
        """One of live, absent or unknown."""
        ...

    def send(self, text: str, /) -> None:
        """Submits `text` to the session."""
        ...

    def screen(self, lines: int | None = None, /) -> str:
        """The session's visible text."""
        ...

    def stop(self) -> None:
        """Ends the session; idempotent."""
        ...


@dataclass(frozen=True, kw_only=True)
class HandleState:
    info: SessionInfo
    idle_timeout: float | None
    reply: str | None = None
    blocked: bool = False


def _diagnostics() -> logging.Logger:
    logger = logging.Logger("driver")
    logger.parent = logging.getLogger()
    return logger


@dataclass(frozen=True, kw_only=True)
class HandleServices:
    host: Mode
    adapter: Adapter | None = None
    usage: Callable[[], Usage | None] | None = None
    clock: Callable[[], float] | None = None
    logger: logging.Logger = field(default_factory=_diagnostics)


class Handle:
    def __init__(
        self, runtime: str, mode: str, spec: Spec, *, state: HandleState, services: HandleServices
    ):
        """spec: its sid, name (a tui session's) and workdir."""
        self.sid, self.runtime, self.mode, self.name = spec.sid, runtime, mode, spec.name
        self._host, self._info, self._idle = services.host, state.info, state.idle_timeout
        self._adapter = services.adapter
        self._usage = services.usage
        self._logger = services.logger
        self.clock = time.monotonic if services.clock is None else services.clock
        self._channel = records.path(spec.workdir)
        self._reply = state.reply
        self._queue: deque = deque()
        self._closed = False
        self._ended = False
        self._blocked = state.blocked

    def events(self) -> Iterator[Event | Stderr | IdleTimeout]:
        last = self.clock()
        try:
            while not self._ended:
                if self._queue:
                    event = self._queue.popleft()
                    self._record(event)
                    last = self.clock()
                    self._blocked = (
                        isinstance(event, Blocked) or self._blocked and isinstance(event, Stderr)
                    )
                    yield event
                    continue
                wait = POLL
                if self._idle is not None and not self._blocked:
                    wait = min(POLL, last + self._idle - self.clock())
                if self._closed:
                    code = self._host.returncode(max(wait, 0))
                    if code is not None:
                        self._ended = True
                        yield Exited(code)
                        continue
                elif wait > 0:
                    found, self._closed = self._host.poll(wait)
                    self._queue.extend(e for e in found if not isinstance(e, Exited))
                    continue
                if wait <= 0 and self._idle is not None:
                    last = self.clock()
                    yield IdleTimeout(self._idle)
        except GeneratorExit:  # the caller closed this iterator; the session goes on
            raise
        except BaseException:
            self._ended = True
            self._host.stop()
            raise

    def _record(self, event: Event | Stderr) -> None:
        if isinstance(event, SessionBound):
            records.note(
                self._channel,
                "client_session",
                sid=self.sid,
                client_id=event.native_id,
                durable=True,
            )
            self._info = replace(self._info, native_id=event.native_id)
            self._refresh_metadata()
        elif isinstance(event, Stderr):
            records.note(self._channel, "stderr", best_effort=True, text=event.text)
        elif isinstance(event, Reply):
            self._reply = event.text
        elif isinstance(event, TurnEnd) and event.reply:
            self._reply = event.reply

    def send(self, text: str) -> None:
        self._host.send(text)

    def state(self) -> str:
        return self._host.state()

    def screen(self, lines: int | None = None) -> str:
        return self._host.screen(lines)

    def stop(self) -> None:
        self._host.stop()

    def _refresh_metadata(self) -> None:
        if self._adapter is None or not self._info.native_id:
            return
        try:
            resolved = self._adapter.session_metadata(self._info)
            info = replace(self._info, transcript=resolved.transcript, resume=resolved.resume)
        except Exception:
            self._logger.debug("Session metadata resolution failed", exc_info=True)
            return
        if info != self._info:
            self._info = info
            records.note(
                self._channel,
                "session_metadata",
                best_effort=True,
                sid=self.sid,
                transcript=info.transcript,
                resume=info.resume,
            )

    def info(self) -> SessionInfo:
        self._refresh_metadata()
        return self._info

    @property
    def reply(self) -> str | None:
        return self._reply

    def usage(self) -> Usage | None:
        return self._usage() if self._usage is not None else None
