"""Execute opaque starter, control and resume commands while Roster owns their bookkeeping."""

import datetime
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import replace

from agent_pm.manager.engine.roster import LOCK_TIMEOUT, Roster
from agent_pm.manager.engine.types import Entry, Lease, RosterError, Spec, State, StatusError
from agent_pm.utils import LEASE_ENV, Record, Status, StatusState, one_line, printable

TIMEOUT = 30
RECOVERY_MARGIN = 5
# Events, receipt open/read and commit may each wait; margin covers timestamps and cleanup.
RECOVERY_AGE = TIMEOUT + 4 * LOCK_TIMEOUT + RECOVERY_MARGIN
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
HERE = ("agent-pm-session", "ls", "--here", "--records")
DESCENDANTS = (*HERE, "--descendants")
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tattach"
HARNESS_SUFFIX = "-drive"


def _owner(session: str, entries: dict[str, Entry]) -> str | None:
    return next(
        (
            name
            for name, entry in entries.items()
            if entry.record is not None and session in entry.record.sessions
        ),
        None,
    )


class Crew:
    def __init__(self, roster: Roster, *, lease: Lease | None = None):
        self.roster = roster
        self._listed: list[Record] | None = None
        roster.bind(lease)

    def _env(self, *, events: bool) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"AGENT_PM_SID", "AGENT_PM_EVENTS", *LEASE_ENV}
        }
        if events:
            env["AGENT_PM_EVENTS"] = self.roster.events
        return env

    def _call(self, argv: tuple[str, ...], text: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                input=text,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env=self._env(events=False),
            )
        except OSError as error:
            raise RuntimeError(f"{shlex.join(argv)}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"{shlex.join(argv)}: no response within {TIMEOUT} seconds"
            ) from error

    def _execute(self, argv: tuple[str, ...], text: str = "") -> str:
        result = self._call(argv, text)
        if result.returncode:
            reason = result.stderr.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"{shlex.join(argv)}: {reason}")
        return result.stdout

    def _listing(self, argv: tuple[str, ...]) -> list[Record] | None:
        try:
            return [Record.parse(line) for line in self._execute(argv).splitlines()]
        except ValueError as error:
            print(f"crew: {shlex.join(argv)}: invalid Record: {error}", file=sys.stderr)
        except RuntimeError as error:
            print(f"crew: {error}", file=sys.stderr)
        return None

    def _here(self) -> list[Record] | None:
        if self._listed is None:
            self._listed = self._listing(HERE)
        return self._listed

    def _start(self, argv: tuple[str, ...], name: str, sid: str) -> Record:
        env = {**self._env(events=True), "AGENT_PM_SID": sid}
        try:
            with self.roster.open_receipt(name) as receipt:
                result = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=receipt,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=TIMEOUT,
                    env=env,
                )
                if result.returncode:
                    reason = result.stderr.strip() or f"exit {result.returncode}"
                    raise RuntimeError(f"{shlex.join(argv)}: {reason}")
                sys.stderr.write(result.stderr)
                return self.roster.receipt_record(name)
        except OSError as error:
            raise RuntimeError(f"{shlex.join(argv)}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"{shlex.join(argv)}: no response within {TIMEOUT} seconds"
            ) from error

    def run(self, spec: Spec) -> Record:
        self._listed = None
        sid = str(uuid.uuid4())
        intent = self.roster.begin(spec, sid=sid)
        try:
            record = self._start(spec.argv, spec.name, sid)
            self.roster.commit(spec.name, record)
        except (RosterError, RuntimeError, ValueError) as error:
            try:
                self.roster.mark_if(spec.name, intent, State.LOST)
            except RosterError as mark_error:
                error.add_note(f"{spec.name}: could not mark lost: {mark_error}")
            raise
        return record

    def _statuses(self, entries: dict[str, Entry]) -> tuple[dict[str, Status], dict[str, str]]:
        """Each entry's Status, else why it has none: one `<ctl> status` per ctl, whose exit 1
        drops only the keys it answered no Status for (utils answer_keys).
        """
        groups: dict[tuple[str, ...], dict[str, Record]] = {}
        owners: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        answers: dict[str, Status] = {}
        failures: dict[str, str] = {}
        for name, entry in entries.items():
            record = entry.record
            if record is None:
                word = StatusState.STARTING if entry.state == State.STARTING else StatusState.GONE
                answers[name] = Status(
                    name=name, alive=False, state=word, outcome=None, attach=None, hidden=False
                )
                continue
            owners.setdefault((record.name, record.ctl), []).append(name)
            groups.setdefault(record.ctl, {}).setdefault(record.name, record)
        for (key, ctl), names in owners.items():
            if any(entries[name].record != groups[ctl][key] for name in names):
                groups[ctl].pop(key)
                for name in names:
                    failures[name] = f"ambiguous Record name {key!r}"
        for ctl, records in groups.items():
            if not records:
                continue
            argv = (*ctl, "status")
            text = "".join(record.to_line() + "\n" for record in records.values())
            try:
                result = self._call(argv, text)
                if result.returncode not in (0, 1):
                    reason = result.stderr.strip() or f"exit {result.returncode}"
                    raise RuntimeError(f"{shlex.join(argv)}: {reason}")
                try:
                    statuses = [Status.parse(line) for line in result.stdout.splitlines()]
                except ValueError as error:
                    raise RuntimeError(f"{shlex.join(argv)}: invalid Status: {error}") from None
                returned = {status.name: status for status in statuses}
                if len(returned) != len(statuses) or not returned.keys() <= records.keys():
                    raise RuntimeError(f"{shlex.join(argv)}: expected one Status per Record")
            except RuntimeError as error:
                for key in records:
                    for name in owners[key, ctl]:
                        failures[name] = str(error)
                continue
            reason = result.stderr.strip() or f"exit {result.returncode}"
            for key in records:
                for name in owners[key, ctl]:
                    if key in returned:
                        answers[name] = replace(returned[key], name=name)
                    else:
                        failures[name] = f"{shlex.join(argv)}: no Status: {reason}"
        return answers, failures

    def status(self, names: Iterable[str] = ()) -> list[Status]:
        """The selected entries' Statuses; StatusError when any failed, holding the others'."""
        entries = self.roster.entries()
        selected = tuple(names) or tuple(sorted(entries))
        missing = set(selected) - entries.keys()
        if missing:
            raise ValueError(f"not in roster: {', '.join(sorted(missing))}")
        answers, failures = self._statuses({name: entries[name] for name in selected})
        statuses = [answers[name] for name in selected if name in answers]
        if failures:
            raise StatusError(
                "\n".join(f"{name}: {failures[name]}" for name in selected if name in failures),
                statuses,
            )
        return statuses

    def _control(self, verb: str, records: Iterable[Record]) -> dict[tuple[str, ...], str]:
        groups: dict[tuple[str, ...], list[Record]] = {}
        failures = {}
        for record in records:
            group = groups.setdefault(record.ctl, [])
            if record not in group:
                group.append(record)
        for ctl, group in groups.items():
            try:
                self._execute((*ctl, verb), "".join(record.to_line() + "\n" for record in group))
            except RuntimeError as error:
                failures[ctl] = str(error)
        return failures

    def attach(self, *, after: int | None = None, gen: int | None = None) -> str:
        self._listed = None
        self.roster.check_lease()
        here = self._here()
        if gen is not None:
            self.roster.advance(after, gen)
        instant = datetime.datetime.now(datetime.UTC)
        for name, entry in self.roster.entries().items():
            age = (instant - datetime.datetime.fromisoformat(entry.started)).total_seconds()
            if age <= RECOVERY_AGE:
                continue
            if entry.state == State.STARTING:
                self.roster.mark_if(name, entry, State.LOST)
            elif entry.state == State.RESUMING and here is not None:
                live = entry.sid is not None and any(entry.sid == found.sid for found in here)
                if not live:
                    self.roster.mark_if(name, entry, State.GONE)
        for record in here or ():
            try:
                adopted = self.roster.adopt(record)
                if adopted is None:
                    print(f"crew: adopt {record.name}: no matching launch sid", file=sys.stderr)
            except RosterError as error:
                print(f"crew: adopt {record.name}: {error}", file=sys.stderr)
        entries = self.roster.entries()
        answers, failures = self._statuses(entries)
        for name, failure in failures.items():
            print(f"crew: {name}: {failure}", file=sys.stderr)
        shown: list[Record] = []
        for name, entry in tuple(entries.items()):
            if name not in answers:
                continue
            status = answers[name]
            state = entry.state
            if state in (State.LIVE, State.GONE):
                if status.alive:
                    state = State.LIVE
                elif status.outcome in ("done", "failed"):
                    state = State.DONE
                elif status.outcome == "needs_input":
                    state = State.WAITING
                else:
                    state = State.GONE
                if state != entry.state and not self.roster.mark_if(name, entry, state):
                    current = self.roster.entries().get(name)
                    if current is None:
                        entries.pop(name)
                    else:
                        entries[name] = current
                    answers.pop(name)
                    continue
            entries[name] = replace(entry, state=state)
            if state == State.LIVE and status.attach is not None and entry.record is not None:
                shown.append(entry.record)
        for failure in self._control("show", shown).values():
            print(f"crew: {failure}", file=sys.stderr)
        self.roster.rotate_events()
        cursor, generation, backlog = self.roster.backlog()
        output = [HEADER]
        for name, entry in sorted(entries.items()):
            record = entry.record
            status = answers.get(name)
            fields = (
                name,
                "-" if record is None else record.kind,
                "-" if record is None else record.sid or "-",
                "unknown"
                if status is None
                else status.state
                if entry.state == State.LIVE
                else entry.state,
                entry.note or "-",
                "-" if status is None else status.cwd or "-",
                "-" if status is None else status.attach or "-",
            )
            output.append("\t".join(printable(field).replace("\t", " ") for field in fields))
        output.extend(
            f"{cursor + i} {printable(line)}"
            for i, line in enumerate(backlog, 1)
            if EVENT.fullmatch(line)
        )
        output.append(f"N={cursor} GEN={generation}")
        return "\n".join(output) + "\n"

    def resume(self, name: str) -> Record | None:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        if name not in entries:
            raise ValueError(f"{name}: not in roster")
        entry = entries[name]
        if entry.state == State.RESUMING:
            return None
        if entry.record is None:
            raise RuntimeError(f"{name}: {entry.state}: no Record to resume")
        status = self.status((name,))[0]
        if status.alive:
            return entry.record
        if entry.state != State.GONE:
            raise RuntimeError(f"{name}: only gone entries can resume (state {entry.state})")
        claimed = self.roster.claim_resume(name)
        if claimed is None:
            return None
        record = claimed.record
        if record is None or claimed.sid is None:
            raise RuntimeError(f"{name}: no Record or sid to resume")
        try:
            resumed = self._start(record.resume, name, claimed.sid)
            self.roster.commit(name, resumed)
        except (RosterError, RuntimeError, ValueError) as error:
            try:
                self.roster.mark_if(name, claimed, State.GONE)
            except RosterError as mark_error:
                error.add_note(f"{name}: could not mark gone: {mark_error}")
            raise
        return resumed

    def close(self, name: str) -> None:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        key = name if name in entries else _owner(name, entries)
        if key is None:
            raise ValueError(f"{name}: not in roster")
        record = entries[key].record
        if record is not None:
            failures = self._control("close", (record,))
            if failures:
                raise RuntimeError("\n".join(failures.values()))
        self.roster.remove(key)

    def close_all(self, *, dry_run: bool = False) -> list[str]:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        records = [entry.record for entry in entries.values() if entry.record is not None]
        here = self._listing(DESCENDANTS)
        records.extend(here or ())
        names = sorted(set(entries) | {record.name for record in records})
        if not dry_run:
            failures = self._control("close", records)
            for name, entry in entries.items():
                if entry.record is None or entry.record.ctl not in failures:
                    self.roster.remove(name)
            if failures:
                raise RuntimeError("\n".join(failures.values()))
        if here is None:
            raise RuntimeError("close-all partial: unlisted work may still be running")
        return names

    def forget(self, name: str, *, force: bool = False) -> None:
        self._listed = None
        self.roster.check_lease()
        if name not in self.roster.entries():
            raise ValueError(f"{name}: not in roster")
        if not force and self.status((name,))[0].alive:
            raise RuntimeError(f"{name}: still alive; close it before forgetting")
        self.roster.remove(name)

    def note(self, name: str, text: str) -> None:
        self._listed = None
        self.roster.note(name, text)

    def release(self) -> None:
        self._listed = None
        self.roster.release()

    def next_event(self, *, after: int | None, gen: int) -> tuple[int, str]:
        self._listed = None
        cursor, identity = self.roster.watch_events(after, gen)
        while True:
            if identity is None:
                identity = self.roster.event_identity()
            line, event = self.roster.next_event(cursor, gen, identity=identity)
            if event is not None:
                cursor = line
                if EVENT.fullmatch(event):
                    return line, printable(event)
            else:
                time.sleep(0.5)

    def event_content(self, event: str, session_command: tuple[str, ...]) -> str:
        """What a next_event line is about, newline-terminated: a harness's (`<tui>-drive`, its
        entry's Record naming the tui) entry Status, else by session_command a `done` session's
        reply or the session's screen; else why it is unreadable.
        """
        try:
            _, session, word = event.split(maxsplit=3)[:3]
            entries = self.roster.entries()
            name = _owner(session, entries)
            record = None if name is None else entries[name].record
            tui = session.removesuffix(HARNESS_SUFFIX)
            if (
                name is not None
                and record is not None
                and tui != session
                and tui in (record.name, *record.sessions)
            ):
                content = "".join(status.to_line() + "\n" for status in self.status((name,)))
            else:
                verb = "reply" if word == "done" else "read"
                content = self._execute((*session_command, verb, session))
        except (OSError, RosterError, RuntimeError, ValueError) as error:
            content = f"content unavailable: {printable(one_line(str(error)))}"
        return content if content.endswith("\n") else content + "\n"
