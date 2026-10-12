"""One role's run to its Result: the Driver session's events handled as they come; an invalid outcome gets one fix
of the same session, a usage limit ends the run; the deliverable is saved to `out`. Records go to <workdir>/run.jsonl;
`result` and `end` however the run ends. A Mode's policy (POLICIES) starts the session, sends the fix and answers each
turn end and IdleTimeout: go on (None), END the attempt or GiveUp."""

import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from agent_pm.delegate.composer import OUTCOME, RunConfig, Team
from agent_pm.delegate.engine.plan import ConfigError, Plan, plan
from agent_pm.delegate.event.outcome import SAVES_DELIVERABLE, InvalidOutcome, Outcome
from agent_pm.delegate.event.outcome import read as read_outcome
from agent_pm.delegate.event.reply import marks, outcome_block
from agent_pm.session import api
from agent_pm.utils.files import save
from agent_pm.utils.text import printable

IDLE_TIMEOUT = 2 * 60 * 60  # seconds
STOP_LIMIT = 3
FIX = (
    "The outcome block in your last reply is invalid: {error}. Fix it: end your reply with one corrected "
    f"{OUTCOME} block; change nothing else."
)
NUDGE = (
    f"Finish your task, then end your last reply with your outcome block (`{OUTCOME}`), as your instructions' "
    "Return says. If you are waiting for background work, wait for it first."
)
END = "end"


@dataclass(frozen=True)
class Limit:
    """resets_at: epoch seconds, None when not given."""

    resets_at: float | None


@dataclass(frozen=True)
class Result:
    returncode: int
    outcome: Outcome | None
    error: str = ""
    limit: Limit | None = None


@dataclass(frozen=True)
class Begun:
    sid: str


@dataclass(frozen=True)
class Progress:
    name: str
    text: str


@dataclass(frozen=True)
class Finished:
    result: Result


Event = Begun | Progress | Finished


@dataclass(frozen=True)
class GiveUp:
    reason: str


class Headless:
    def __init__(self, note: Callable[..., None]):
        self.note = note

    def start(self, p: Plan, **tui) -> api.Handle:
        return api.start(
            p.runtime,
            p.mode,
            p.prompt,
            p.spec,
            resume=p.sid if p.resume else None,
            sid=None if p.resume else p.sid,
            idle_timeout=IDLE_TIMEOUT,
            **tui,
        )

    def fix(self, handle: api.Handle, p: Plan, text: str) -> api.Handle:
        return api.start(p.runtime, p.mode, text, p.spec, resume=p.sid, idle_timeout=IDLE_TIMEOUT)

    def turn_end(self, handle: api.Handle, reply: str, pending: int) -> str | GiveUp | None:
        return None

    def progress(self) -> None:
        pass

    def idle(self, seconds: float) -> str | GiveUp | None:
        return None


class Tui(Headless):
    """FR-23: the first turn end without an outcome block gets NUDGE, STOP_LIMIT more give up; a progress mark resets
    that count; a turn end with work pending is ignored. FR-24: an IdleTimeout gives up. The session is left open."""

    def __init__(self, note: Callable[..., None]):
        super().__init__(note)
        self.nudged, self.count = False, 0

    def start(self, p: Plan) -> api.Handle:
        handle = super().start(p, name=p.name, events=p.events)
        _say(f"delegate: tui session {handle.name}: {api.attach_command(handle.name)}")
        return handle

    def fix(self, handle: api.Handle, p: Plan, text: str) -> api.Handle:
        handle.send(text)
        return handle

    def turn_end(self, handle: api.Handle, reply: str, pending: int) -> str | GiveUp | None:
        if outcome_block(reply) is not None:
            return END
        if pending:
            return None
        if not self.nudged:
            self.nudged = True
            try:
                handle.send(NUDGE)
            except api.DriverError as e:
                _say(f"delegate: tui: {e}")
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


def _say(text: str) -> None:
    print(printable(text), file=sys.stderr, flush=True)


def run(
    run: RunConfig,
    task: str | None,
    input: str,
    workdir: str,
    *,
    runtime: str,
    mode: str,
    sid: str | None = None,
    resume: str | None = None,
    name: str | None = None,
    events: str | None = None,
    on_event: Callable[[Event], None] | None = None,
    out: str | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    team: Team | None = None,
) -> Result:
    """on_event gets Begun once, a Progress per mark line and Finished only with a valid outcome; its exception stops
    the session and propagates."""
    p = plan(
        run,
        task,
        input,
        workdir,
        runtime=runtime,
        mode=mode,
        sid=sid,
        resume=resume,
        name=name,
        events=events,
        out=out,
        repo=repo,
        cwd=cwd,
        team=team,
    )
    attempts = _Attempts(p, on_event or (lambda event: None))
    try:
        result = attempts.result()
    except BaseException as e:
        if not attempts.refused:
            text = " ".join(str(e).split())
            attempts.close(
                {"error": f"stopped: {type(e).__name__}" + (f": {text}" if text else "")},
                e.code if isinstance(e, SystemExit) and isinstance(e.code, int) else 1,
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
    try:
        return f"resets at {datetime.fromtimestamp(at).astimezone().isoformat(timespec='seconds')}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "reset time unknown"


class _Attempts:
    def __init__(self, p: Plan, on_event: Callable[[Event], None]):
        self.plan, self.on_event, self.policy = p, on_event, POLICIES[p.mode](self.note)
        self.channel = api.records.path(p.spec.workdir)
        self.seen: set[str] = set()
        self.refused = False  # the Driver refused the start: nothing to close

    def note(self, kind: str, **fields) -> None:
        api.records.note(self.channel, kind, **fields)

    def close(self, result: dict, rc: int) -> None:
        """Best effort: an OSError is printed."""
        for kind, fields in (("result", result), ("end", {"sid": self.plan.sid, "rc": rc})):
            try:
                self.note(kind, **fields)
            except OSError as e:
                print(f"delegate: {e}", file=sys.stderr)

    def result(self) -> Result:
        p = self.plan
        try:
            handle = self.policy.start(p)
        except api.ConfigError as e:
            self.refused = True
            raise ConfigError(str(e)) from None
        except api.DriverError as e:
            self.drop_stale_out()
            self.note("input", text=p.params.input)
            return Result(1, None, f"{p.mode}: {e}")
        try:
            stale = self.drop_stale_out()
            self.note("input", text=p.params.input)
            if stale:
                self.stop(handle)
                return Result(1, None, stale)
            self.on_event(Begun(p.sid))
            fixed = False
            while True:
                rc, last, limit = self.attempt(handle)
                diagnostic = f"the client exited {rc}" if rc else ""
                if limit:
                    return Result(rc, None, f"usage limit; {_resets(limit.resets_at)}", limit)
                if (block := outcome_block(last or "")) is None:
                    return Result(rc, None, diagnostic or "the agent run returned no outcome")
                try:
                    outcome = read_outcome(block, p.run, p.spec.workdir)
                except InvalidOutcome as e:
                    if rc or fixed:
                        return Result(rc, None, diagnostic or f"invalid outcome: {e}")
                    self.note("invalid", error=str(e))
                    _say(f"delegate: invalid outcome: {e}; resuming once")
                    try:
                        handle = self.policy.fix(handle, p, FIX.format(error=e))
                    except (api.ConfigError, api.DriverError) as failed:
                        return Result(1, None, f"invalid outcome: {e}; resume failed: {failed}")
                    fixed = True
                    continue
                return self.finish(outcome, rc)
        except BaseException:
            self.stop(handle)
            raise

    def stop(self, handle: api.Handle) -> bool:
        """A DriverError is printed, never the run's result or exception; False then, the session maybe running."""
        try:
            handle.stop()
        except api.DriverError as e:
            _say(f"delegate: {self.plan.mode}: {e}")
            return False
        return True

    def drop_stale_out(self) -> str:
        """Removes a new run's earlier `out`, only once the Driver took the run; returns why it could not, else ""."""
        p = self.plan
        if (
            p.out and not p.resume and not os.path.isdir(p.out)
        ):  # a resume keeps its session's draft
            try:
                Path(p.out).unlink(missing_ok=True)
            except OSError as e:
                return f"cannot remove the stale out: {e}"
        return ""

    def attempt(self, handle: api.Handle) -> tuple[int, str | None, Limit | None]:
        """rc 0 when the policy ended it, the session running on."""
        rc = last = limit = replied = None
        for event in handle.events():
            answer = None
            if isinstance(event, api.Reply):
                last = replied = self.reply(event.text)
            elif isinstance(event, api.TurnEnd):
                if (
                    event.reply and event.reply != replied
                ):  # a headless turn's Reply comes again at its end
                    last = self.reply(event.reply)
                replied = None
                if not limit:
                    answer = self.policy.turn_end(handle, last or "", event.pending)
            elif isinstance(event, api.Stderr):
                _say(event.text)
            elif isinstance(event, api.IdleTimeout):
                self.note("idle", seconds=event.seconds)
                if not limit:
                    answer = self.policy.idle(event.seconds)
            elif isinstance(event, api.LimitHit):
                self.note("limit", resets_at=event.resets_at)
                limit = Limit(event.resets_at)
                if not self.stop(handle):
                    break
            elif isinstance(event, api.Exited):
                rc = event.returncode
            if isinstance(answer, GiveUp):
                self.note("give_up", reason=answer.reason)
                _say(
                    f"delegate: {answer.reason}; session {handle.name} left open: {api.attach_command(handle.name)}"
                )
                return 0, None, None
            if answer == END:
                break
        return rc or 0, last, limit

    def reply(self, text: str) -> str:
        for line in text.splitlines():
            if line.strip():
                _say(line)
        for name, said in marks(text):
            self.note("progress", name=name, text=said)
            _say(f"Progress ({name}): {said}")
            self.seen.add(name)
            self.on_event(Progress(name, said))
            self.policy.progress()
        return text

    def finish(self, outcome: Outcome, rc: int) -> Result:
        p, kind = self.plan, self.plan.run.output["type"]
        if outcome.deliverable and p.out:
            if not api.records.holds(
                p.out, outcome.deliverable
            ):  # else the agent run wrote it there
                try:
                    save(p.out, outcome.deliverable)
                except OSError as e:
                    if kind in SAVES_DELIVERABLE:
                        raise
                    _say(f"delegate: cannot save the deliverable to {p.out}: {e}")
                    self.note("save_error", error=str(e))
            if kind == "local":
                outcome = replace(outcome, url=p.out)
        if not p.resume and outcome.status != "needs_input" and "start" not in self.seen:
            print("delegate: missing progress mark: start", file=sys.stderr, flush=True)
            self.note("missing", name="start")
        result = Result(rc, outcome, f"the client exited {rc}" if rc else "")
        self.on_event(Finished(result))
        return result
