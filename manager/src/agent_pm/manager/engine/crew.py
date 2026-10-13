"""Execute opaque starter, control and resume commands while Roster owns their bookkeeping."""

import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Entry, Lease, RosterError, Spec, State
from agent_pm.utils import Record, Status, printable

TIMEOUT = 30
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
HERE = ("agent-pm-session", "ls", "--here", "--records")
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tattach"
BIN = str(Path(__file__).parents[5] / "bin")


class Crew:
    def __init__(self, roster: Roster, *, lease: Lease | None = None):
        self.roster = roster
        self._listed: list[Record] | None = None
        roster.bind(lease)

    def _env(self, *, events: bool) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "AGENT_PM_SID",
                "AGENT_PM_MANAGER",
                "AGENT_PM_HOLDER_ID",
                "AGENT_PM_HOLDER_GENERATION",
            }
        }
        env["PATH"] = os.pathsep.join((BIN, os.environ.get("PATH", os.defpath)))
        if events:
            env["AGENT_PM_EVENTS"] = self.roster.events
        return env

    def _execute(self, argv: tuple[str, ...], text: str = "", *, events: bool = False) -> str:
        try:
            result = subprocess.run(
                argv,
                input=text,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                env=self._env(events=events),
            )
        except OSError as error:
            raise RuntimeError(f"{shlex.join(argv)}: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"{shlex.join(argv)}: no response within {TIMEOUT} seconds"
            ) from error
        if result.returncode:
            reason = result.stderr.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"{shlex.join(argv)}: {reason}")
        return result.stdout

    def _here(self) -> list[Record]:
        if self._listed is None:
            raw = self._execute(HERE)
            try:
                self._listed = [Record.parse(line) for line in raw.splitlines()]
            except ValueError as error:
                raise RuntimeError(f"{shlex.join(HERE)}: invalid Record: {error}") from error
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
        except (RosterError, RuntimeError, ValueError):
            self.roster.mark_if(spec.name, intent, State.LOST)
            raise
        return record

    def _statuses(self, entries: dict[str, Entry]) -> tuple[dict[str, Status], list[str]]:
        groups: dict[tuple[str, ...], dict[str, Record]] = {}
        answers: dict[str, Status] = {}
        failures: dict[tuple[str, ...], str] = {}
        for name, entry in entries.items():
            record = entry.record
            if record is None:
                answers[name] = Status(
                    name, alive=False, state=entry.state, outcome=None, attach=None
                )
                continue
            group = groups.setdefault(record.ctl, {})
            if record.name in group and group[record.name] != record:
                failures[record.ctl] = f"{name}: ambiguous Record name {record.name!r}"
            group[record.name] = record
        for ctl, records in groups.items():
            if ctl in failures:
                continue
            try:
                raw = self._execute(
                    (*ctl, "status"), "".join(r.to_line() + "\n" for r in records.values())
                )
                statuses = [Status.parse(line) for line in raw.splitlines()]
            except ValueError as error:
                failures[ctl] = f"{shlex.join(ctl)} status: invalid Status: {error}"
                continue
            except RuntimeError as error:
                failures[ctl] = str(error)
                continue
            returned = {status.name: status for status in statuses}
            if len(returned) != len(statuses) or returned.keys() != records.keys():
                failures[ctl] = f"{shlex.join(ctl)} status: expected one Status per Record"
                continue
            for name, entry in entries.items():
                record = entry.record
                if record is not None and record.ctl == ctl:
                    answers[name] = replace(returned[record.name], name=name)
        return answers, list(failures.values())

    def status(self, names: Iterable[str] = ()) -> list[Status]:
        entries = self.roster.entries()
        selected = tuple(names) or tuple(sorted(entries))
        missing = set(selected) - entries.keys()
        if missing:
            raise ValueError(f"not in roster: {', '.join(sorted(missing))}")
        answers, failures = self._statuses({name: entries[name] for name in selected})
        if failures:
            raise RuntimeError("\n".join(failures))
        return [answers[name] for name in selected]

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
        try:
            here = self._here()
        except RuntimeError as error:
            print(f"crew: {error}", file=sys.stderr)
            here = None
        if gen is not None:
            self.roster.advance(after, gen)
        instant = datetime.now(UTC)
        for name, entry in self.roster.entries().items():
            age = (instant - datetime.fromisoformat(entry.started)).total_seconds()
            if age <= TIMEOUT:
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
        for failure in failures:
            print(f"crew: {failure}", file=sys.stderr)
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
                "-" if record is None else record.workdir or "-",
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
        except (RosterError, RuntimeError, ValueError):
            self.roster.mark_if(name, claimed, State.GONE)
            raise
        return resumed

    def stop(self, name: str) -> None:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        key = (
            name
            if name in entries
            else next(
                (
                    alias
                    for alias, entry in entries.items()
                    if entry.record is not None and name in entry.record.sessions
                ),
                None,
            )
        )
        if key is None:
            raise ValueError(f"{name}: not in roster")
        record = entries[key].record
        if record is not None:
            failures = self._control("close", (record,))
            if failures:
                raise RuntimeError("\n".join(failures.values()))
        self.roster.remove(key)

    def stop_all(self, *, dry_run: bool = False) -> list[str]:
        self._listed = None
        self.roster.check_lease()
        entries = self.roster.entries()
        records = [entry.record for entry in entries.values() if entry.record is not None]
        records.extend(self._here())
        names = sorted(set(entries) | {record.name for record in records})
        if not dry_run:
            failures = self._control("close", records)
            for name, entry in entries.items():
                if entry.record is None or entry.record.ctl not in failures:
                    self.roster.remove(name)
            if failures:
                raise RuntimeError("\n".join(failures.values()))
        return names

    def forget(self, name: str, *, force: bool = False) -> None:
        self._listed = None
        self.roster.check_lease()
        if not force and self.status((name,))[0].alive:
            raise RuntimeError(f"{name}: still alive; stop it before forgetting")
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
