"""CtlContract, what every Record ctl answers (FR-7, FR-42): utils/tests/fakes/ctl.py, and
agent-pm-session for its workers. A subclass also inherits its Case and defines `command`,
call(*argv, stdin="", sid=None) running the command (sid: AGENT_PM_SID, None unset),
start(name, sid=None) -> the Record a start printed, and end(name), its session gone.
"""

import uuid
from dataclasses import replace

from agent_pm.utils.record import Record, Status


def lines(*records: Record) -> str:
    return "".join(record.to_line() + "\n" for record in records)


class CtlContract:
    command: str

    def call(self, *argv: str, stdin: str = "", sid: str | None = None): ...

    def start(self, name: str, sid: str | None = None) -> Record: ...

    def end(self, name: str) -> None: ...

    def statuses(self, *records: Record) -> list[Status]:
        done = self.call("status", stdin=lines(*records))
        self.assertEqual(done.returncode, 0, done.stderr)
        return [Status.parse(line) for line in done.stdout.splitlines()]

    def test_start_prints_a_record_its_command_answers_and_resumes(self):
        sid = str(uuid.uuid4())
        started = self.start("w1", sid=sid)
        self.assertEqual((started.name, started.sessions, started.sid), ("w1", ("w1",), sid))
        self.assertEqual(
            (started.ctl, started.resume), ((self.command,), (self.command, "resume", "w1"))
        )
        self.assertEqual(Record.parse(started.to_line()), started)

    def test_status_answers_each_stdin_record_in_order_an_unknown_one_gone(self):
        started = self.start("w1")
        other = replace(started, name="w9", sessions=("w9",))
        found = self.statuses(started, other)
        self.assertEqual([(s.name, s.alive) for s in found], [("w1", True), ("w9", False)])
        self.assertEqual((found[1].state, found[1].outcome, found[1].attach), ("gone", None, None))

    def test_resume_of_a_live_one_prints_its_record_and_another_sid_exits_2(self):
        started = self.start("w1")
        again = self.call(*started.resume[1:], sid=started.sid)
        self.assertEqual((again.returncode, Record.parse(again.stdout)), (0, started))
        refused = self.call(*started.resume[1:], sid=str(uuid.uuid4()))
        self.assertEqual((refused.returncode, refused.stdout), (2, ""))
        self.assertTrue(self.statuses(started)[0].alive)

    def test_resume_of_an_ended_one_starts_it_again_with_its_sid(self):
        started = self.start("w1")
        self.end("w1")
        self.assertFalse(self.statuses(started)[0].alive)
        again = self.call(*started.resume[1:], sid=started.sid)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(Record.parse(again.stdout).sid, started.sid)
        self.assertTrue(self.statuses(started)[0].alive)

    def test_close_of_stdin_records_ends_each(self):
        records = [self.start(name) for name in ("w1", "w2")]
        done = self.call("close", stdin=lines(*records))
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
        self.assertEqual([s.alive for s in self.statuses(*records)], [False, False])

    def test_a_bad_stdin_line_exits_2_and_closes_nothing(self):
        started = self.start("w1")
        for verb in ("status", "close", "show"):
            with self.subTest(verb=verb):
                done = self.call(verb, stdin=lines(started) + "not json\n")
                self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertTrue(self.statuses(started)[0].alive)
