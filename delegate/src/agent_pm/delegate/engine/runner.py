"""One role's run to its Result: an invalid outcome gets one fix of the same session, a usage limit
ends the run, the deliverable is saved to `out`; records go to <workdir>/run.jsonl. A Mode's policy
(POLICIES) starts the session, sends the fix and answers each turn end and IdleTimeout: go on
(None), END or GiveUp.
"""

import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.plan import plan
from agent_pm.delegate.engine.types import (
    AttemptState,
    Begun,
    ConfigError,
    Event,
    Finished,
    Limit,
    Plan,
    Progress,
    Result,
    Spec,
    Stopped,
)
from agent_pm.delegate.event.outcome import SAVES_DELIVERABLE
from agent_pm.delegate.event.outcome import read as read_outcome
from agent_pm.delegate.event.reply import OUTCOME, marks, outcome_block
from agent_pm.delegate.event.types import InvalidOutcome, Outcome
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import (
    Driver,
    DriverError,
    Exited,
    Handle,
    IdleTimeout,
    LimitHit,
    Reply,
    StartOptions,
    Stderr,
    TurnEnd,
    holds,
    note,
)
from agent_pm.utils import printable, save

IDLE_TIMEOUT = 2 * 60 * 60  # seconds
STOP_LIMIT = 3
FIX = (
    f"The outcome block in your last reply is invalid: {{error}}. Fix it: end your reply "
    f"with one corrected "
    f"{OUTCOME}"
    f" block; change nothing else."
)
NUDGE = (
    f"Finish your task, then end your last reply with your outcome block "
    f"(`"
    f"{OUTCOME}"
    f"`), as your instructions' Return says. If you are waiting for background work, "
    f"wait for it first."
)
END = "end"


@dataclass(frozen=True)
class GiveUp:
    reason: str


class Headless:
    def __init__(self, note: Callable[..., None]):
        self.note = note

    def start(self, p: Plan, name: str | None = None, events: str | None = None) -> Handle:
        s = p.spec
        return p.driver.start(
            p.runtime,
            s.mode,
            p.prompt,
            replace(p.session, sid=s.sid, name=name),
            options=StartOptions(
                resume=s.sid if s.resume else None,
                idle_timeout=IDLE_TIMEOUT,
                events=events,
            ),
        )

    def fix(self, _handle: Handle, p: Plan, text: str, /) -> Handle:
        s = p.spec
        return p.driver.start(
            p.runtime,
            s.mode,
            text,
            p.session,
            options=StartOptions(resume=s.sid, idle_timeout=IDLE_TIMEOUT),
        )

    def turn_end(self, _handle: Handle, _reply: str, _pending: int, /) -> str | GiveUp | None:
        return None

    def progress(self) -> None:
        pass

    def idle(self, _seconds: float, /) -> str | GiveUp | None:
        return None


class Tui(Headless):
    """FR-23: the first turn end without an outcome block gets NUDGE, STOP_LIMIT more give up; a
    progress mark resets that count; a turn end with work pending is ignored. FR-24: an IdleTimeout
    gives up. The session is left open.
    """

    def __init__(self, note: Callable[..., None]):
        super().__init__(note)
        self.nudged, self.count = False, 0

    def start(self, p: Plan) -> Handle:
        handle = super().start(p, p.spec.name, p.spec.events)
        name = handle.name
        assert name is not None
        say(f"delegate: tui session {name}: {p.driver.attach_command(name)}")
        return handle

    def fix(self, handle: Handle, _plan: Plan, text: str, /) -> Handle:
        handle.send(text)
        return handle

    def turn_end(self, handle: Handle, reply: str, pending: int) -> str | GiveUp | None:
        if outcome_block(reply) is not None:
            return END
        if pending:
            return None
        if not self.nudged:
            self.nudged = True
            try:
                handle.send(NUDGE)
            except DriverError as e:
                say(f"delegate: tui: {e}")
            self.note("nudge")
            return None
        self.count += 1
        return (
            GiveUp(f"no outcome after {self.count} turn ends") if self.count >= STOP_LIMIT else None
        )

    def progress(self) -> None:
        self.count = 0

    def idle(self, seconds: float) -> str | GiveUp | None:
        return GiveUp(f"no event for {seconds / 3600:g} h")


POLICIES = {"headless": Headless, "tui": Tui, "in_process": Headless}


def say(text: str) -> None:
    print(printable(text), file=sys.stderr, flush=True)


def run(
    run: RunConfig,
    spec: Spec,
    *,
    on_event: Callable[[Event], None] | None = None,
    driver: Driver | None = None,
) -> Result:
    """on_event gets Begun once, a Progress per mark line and Finished only with a valid outcome;
    its exception stops the session and propagates.
    """
    p = plan(run, spec, driver=driver)
    attempts = _Attempts(p, on_event or (lambda _event: None))
    try:
        result = attempts.result()
    except BaseException as e:
        if not attempts.refused:
            text = " ".join(str(e).split())
            attempts.close(
                {"error": f"stopped: {type(e).__name__}" + (f": {text}" if text else "")},
                e.code if isinstance(e, (Stopped, SystemExit)) and isinstance(e.code, int) else 1,
            )
        raise
    closing = {"error": result.error}
    if result.outcome is not None:
        closing = {
            "outcome": {k: v for k, v in asdict(result.outcome).items() if k != "deliverable"},
            **({"error": result.error} if result.error else {}),
        }
    attempts.close(closing, result.returncode)
    return result


def _resets(at: float | None) -> str:
    if at is None:
        return "reset time unknown"
    try:
        return f"resets at {datetime.fromtimestamp(at).astimezone().isoformat(timespec='seconds')}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "reset time unknown"


class _Attempts:
    def __init__(self, p: Plan, on_event: Callable[[Event], None]):
        self.plan, self.on_event, self.policy = p, on_event, POLICIES[p.spec.mode](self.note)
        self.workdir = p.spec.workdir
        self.seen: set[str] = set()
        self.refused = False  # the Driver refused the start: nothing to close

    def note(self, kind: str, **fields) -> None:
        note(self.workdir, kind, **fields)

    def close(self, result: dict, rc: int) -> None:
        """Best effort: an OSError is printed."""
        for kind, fields in (("result", result), ("end", {"sid": self.plan.spec.sid, "rc": rc})):
            try:
                self.note(kind, **fields)
            except OSError as e:
                print(f"delegate: {e}", file=sys.stderr)

    def result(self) -> Result:
        p = self.plan
        try:
            handle = self.policy.start(p)
        except SessionConfigError as e:
            self.refused = True
            raise ConfigError(str(e)) from None
        except DriverError as e:
            self.drop_stale_out()
            self.note("input", text=p.params.input)
            return Result(1, None, f"{p.spec.mode}: {e}")
        try:
            stale = self.drop_stale_out()
            self.note("input", text=p.params.input)
            if stale:
                self.stop(handle)
                return Result(1, None, stale)
            self.on_event(Begun(p.sid))
            return self._outcome(handle)
        except BaseException:
            self.stop(handle)
            raise

    def _outcome(self, handle: Handle) -> Result:
        p = self.plan
        fixed = False
        while True:
            rc, last, limit = self.attempt(handle)
            diagnostic = f"the client exited {rc}" if rc else ""
            if limit:
                return Result(rc, None, f"usage limit; {_resets(limit.resets_at)}", limit)
            if (block := outcome_block(last or "")) is None:
                return Result(rc, None, diagnostic or "the agent run returned no outcome")
            try:
                outcome = read_outcome(block, p.run.output, p.spec.workdir)
            except InvalidOutcome as e:
                if rc or fixed:
                    return Result(rc, None, diagnostic or f"invalid outcome: {e}")
                self.note("invalid", error=str(e))
                say(f"delegate: invalid outcome: {e}; resuming once")
                try:
                    handle = self.policy.fix(handle, p, FIX.format(error=e))
                except (SessionConfigError, DriverError) as failed:
                    return Result(1, None, f"invalid outcome: {e}; resume failed: {failed}")
                fixed = True
                continue
            return self.finish(outcome, rc)

    def stop(self, handle: Handle) -> bool:
        """A DriverError is printed, not raised; False then, the session maybe running."""
        try:
            handle.stop()
        except DriverError as e:
            say(f"delegate: {self.plan.spec.mode}: {e}")
            return False
        return True

    def drop_stale_out(self) -> str:
        """Removes a new run's earlier `out`, only once the Driver took the run; returns why it
        could not, else "".
        """
        s = self.plan.spec
        if (
            s.out and not s.resume and not os.path.isdir(s.out)
        ):  # a resume keeps its session's draft
            try:
                Path(s.out).unlink(missing_ok=True)
            except OSError as e:
                return f"cannot remove the stale out: {e}"
        return ""

    def _turn_end(self, handle: Handle, event: TurnEnd, state: AttemptState):
        if event.reply and event.reply != state.replied:
            # A headless turn repeats its Reply at turn end.
            state.last = self.reply(event.reply)
        state.replied = None
        return (
            None if state.limit else self.policy.turn_end(handle, state.last or "", event.pending)
        )

    def _event(self, event, handle: Handle, state: AttemptState):
        if isinstance(event, Reply):
            state.last = state.replied = self.reply(event.text)
        elif isinstance(event, TurnEnd):
            return self._turn_end(handle, event, state)
        elif isinstance(event, Stderr):
            say(event.text)
        elif isinstance(event, IdleTimeout):
            self.note("idle", seconds=event.seconds)
            if not state.limit:
                return self.policy.idle(event.seconds)
        elif isinstance(event, LimitHit):
            self.note("limit", resets_at=event.resets_at)
            state.limit = Limit(event.resets_at)
            if not self.stop(handle):
                return END
        elif isinstance(event, Exited):
            state.returncode = event.returncode
        return None

    def attempt(self, handle: Handle) -> tuple[int, str | None, Limit | None]:
        """rc 0 when the policy ended it, the session running on."""
        state = AttemptState()
        for event in handle.events():
            answer = self._event(event, handle, state)
            if isinstance(answer, GiveUp):
                self.note("give_up", reason=answer.reason)
                name = handle.name
                assert name is not None
                say(
                    f"delegate: {answer.reason}; session {name} left open: "
                    f"{self.plan.driver.attach_command(name)}"
                )
                return 0, None, None
            if answer == END:
                break
        return state.returncode or 0, state.last, state.limit

    def reply(self, text: str) -> str:
        for line in text.splitlines():
            if line.strip():
                say(line)
        for name, said in marks(text):
            self.note("progress", name=name, text=said)
            say(f"Progress ({name}): {said}")
            self.seen.add(name)
            self.on_event(Progress(name, said))
            self.policy.progress()
        return text

    def finish(self, outcome: Outcome, rc: int) -> Result:
        s, kind = self.plan.spec, self.plan.run.output["type"]
        if outcome.deliverable and s.out:
            if not holds(s.out, outcome.deliverable):  # else the agent run wrote it there
                try:
                    save(s.out, outcome.deliverable)
                except OSError as e:
                    if kind in SAVES_DELIVERABLE:
                        raise
                    say(f"delegate: cannot save the deliverable to {s.out}: {e}")
                    self.note("save_error", error=str(e))
            if kind == "local":
                outcome = replace(outcome, url=s.out)
        if not s.resume and outcome.status != "needs_input" and "start" not in self.seen:
            print("delegate: missing progress mark: start", file=sys.stderr, flush=True)
            self.note("missing", name="start")
        result = Result(rc, outcome, f"the client exited {rc}" if rc else "")
        self.on_event(Finished(result))
        return result
