"""One role's run to its Result (FR-17 to FR-22, FR-58): its plan, then the Driver session, each event handled as it
comes: a reply printed, its mark lines recorded (`progress`), printed and passed to on_event; the last reply's outcome
block read and validated, an invalid one fixed by one resume of the same session; a usage limit stopping the session
and ending the run; the deliverable saved to `out`. Records `input`, `progress`, `idle`, `limit`, `invalid`, `missing`,
then `result` and `end` however the run ends, in <workdir>/run.jsonl beside the Driver's. A Mode's policy (POLICIES)
starts the session and its fix resume; the rest is mode-neutral. Ported from the old core's driver (start, _drive,
terminal, printable)."""
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from composer import OUTCOME, RunConfig, Team
from delegate.outcome import SAVES_DELIVERABLE, InvalidOutcome, Outcome, save
from delegate.outcome import read as read_outcome
from delegate.plan import ConfigError, Plan, plan
from delegate.reply import marks, outcome_block
from driver import api

IDLE_TIMEOUT = 2 * 60 * 60   # seconds: each span without an event is an `idle` record
FIX = ("The outcome block in your last reply is invalid: {error}. Fix it: end your reply with one corrected "
       f"{OUTCOME} block; change nothing else.")


@dataclass(frozen=True)
class Limit:
    """The usage limit that ended a run; resets_at: epoch seconds, None when not given."""
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


class Headless:
    """The headless Mode's policy: each attempt is a process; the fix resume starts another on the same session."""

    def start(self, p: Plan) -> api.Handle:
        return api.start(p.runtime, p.mode, p.prompt, p.spec, resume=p.sid if p.resume else None,
                         sid=None if p.resume else p.sid, idle_timeout=IDLE_TIMEOUT)

    def fix(self, handle: api.Handle, p: Plan, text: str) -> api.Handle:
        return api.start(p.runtime, p.mode, text, p.spec, resume=p.sid, idle_timeout=IDLE_TIMEOUT)


POLICIES = {"headless": Headless}


def printable(text: str) -> str:
    """`text` without its control characters but tabs, so an agent run's output can't steer the terminal."""
    return "".join(c for c in text if c.isprintable() or c == "\t")


def _say(text: str) -> None:
    print(printable(text), file=sys.stderr, flush=True)


def run(run: RunConfig, task: str | None, input: str, workdir: str, *, runtime: str, mode: str,
        sid: str | None = None, resume: str | None = None, name: str | None = None, events: str | None = None,
        on_event: Callable[[Event], None] | None = None, out: str | None = None, repo: str | None = None,
        cwd: str | None = None, team: Team | None = None) -> Result:
    """Runs `run` on `task` and `input` (plan's arguments) to its Result. on_event gets Begun once, a Progress per mark
    line and Finished only with a valid outcome; an exception from it stops the session and propagates. A refusal is a
    ConfigError before anything starts."""
    p = plan(run, task, input, workdir, runtime=runtime, mode=mode, sid=sid, resume=resume, name=name, events=events,
             out=out, repo=repo, cwd=cwd, team=team)
    if p.out and not p.resume and not os.path.isdir(p.out):   # a resume keeps its session's draft
        Path(p.out).unlink(missing_ok=True)   # an earlier deliverable never stands for this run's
    attempts = _Attempts(p, on_event or (lambda event: None))
    try:
        result = attempts.result()
    except BaseException as e:
        if not attempts.refused:
            text = " ".join(str(e).split())
            attempts.close({"error": f"stopped: {type(e).__name__}" + (f": {text}" if text else "")},
                           e.code if isinstance(e, SystemExit) and isinstance(e.code, int) else 1)
        raise
    closing = {"error": result.error}
    if result.outcome is not None:
        closing = {"outcome": {k: v for k, v in asdict(result.outcome).items() if k != "deliverable"},
                   **({"error": result.error} if result.error else {})}
    attempts.close(closing, result.returncode)
    return result


def _resets(at: float | None) -> str:
    try:
        return f"resets at {datetime.fromtimestamp(at).astimezone().isoformat(timespec='seconds')}"
    except (TypeError, ValueError, OverflowError, OSError):
        return "reset time unknown"


class _Attempts:
    """One run's attempts: the first, then at most one fix resume (FR-21)."""

    def __init__(self, p: Plan, on_event: Callable[[Event], None]):
        self.plan, self.on_event, self.policy = p, on_event, POLICIES[p.mode]()
        self.channel = api.records.path(p.spec.workdir)
        self.seen: set[str] = set()   # mark names, every attempt's
        self.refused = False          # the Driver refused the first start: nothing to close

    def note(self, kind: str, **fields) -> None:
        api.records.note(self.channel, kind, **fields)

    def close(self, result: dict, rc: int) -> None:
        """The `result` and `end` records, each best effort: an OSError is printed."""
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
            self.note("input", text=p.params.input)
            return Result(1, None, f"{p.mode}: {e}")
        try:
            self.note("input", text=p.params.input)
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
            handle.stop()
            raise

    def attempt(self, handle: api.Handle) -> tuple[int, str | None, Limit | None]:
        """Each event of `handle` handled as it comes, up to Exited: (its returncode, the last reply, a limit)."""
        rc = last = limit = None
        for event in handle.events():
            if isinstance(event, api.Reply):
                last = self.reply(event.text)
            elif isinstance(event, api.TurnEnd) and event.reply and event.reply != last:
                last = self.reply(event.reply)
            elif isinstance(event, api.Stderr):
                _say(event.text)
            elif isinstance(event, api.IdleTimeout):
                self.note("idle", seconds=event.seconds)
            elif isinstance(event, api.LimitHit):
                self.note("limit", resets_at=event.resets_at)
                handle.stop()
                limit = Limit(event.resets_at)
            elif isinstance(event, api.Exited):
                rc = event.returncode
        return rc, last, limit

    def reply(self, text: str) -> str:
        """Prints `text`'s non-blank lines, then records, prints and passes on each of its marks; returns `text`."""
        for line in text.splitlines():
            if line.strip():
                _say(line)
        for name, said in marks(text):
            self.note("progress", name=name, text=said)
            _say(f"Progress ({name}): {said}")
            self.seen.add(name)
            self.on_event(Progress(name, said))
        return text

    def finish(self, outcome: Outcome, rc: int) -> Result:
        """A valid outcome's Result: its deliverable saved to `out`, a missing start mark noted, Finished passed on."""
        p, kind = self.plan, self.plan.run.output["type"]
        if kind in SAVES_DELIVERABLE and outcome.deliverable and p.out:
            if not api.records.holds(p.out, outcome.deliverable):   # else the agent run wrote it there
                save(p.out, outcome.deliverable)
            if kind == "local":
                outcome = replace(outcome, url=p.out)
        if not p.resume and outcome.status != "needs_input" and "start" not in self.seen:
            print("delegate: missing progress mark: start", file=sys.stderr, flush=True)
            self.note("missing", name="start")
        result = Result(rc, outcome, f"the client exited {rc}" if rc else "")
        self.on_event(Finished(result))
        return result
