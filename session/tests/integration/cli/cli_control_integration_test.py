"""agent-pm-session's batch commands through bin/agent-pm-session on a private tmux server: status
answers each name or stdin Record in order (Q11); a duplicate or bad input refuses the whole batch
before any action; show, hide, close and attach-command by name and by stdin Record, show placing a
session by its own display or by a Record's, close repeatable and only for the named (Q12), show
and close refusing a Record whose sid the session's @record does not carry; on a
Host that cannot display (session.tests.fakes.host:FakeHost, the module entry) show, hide, attach-command,
read and tile exit 1 saying so, status giving no attach command.
"""

import dataclasses
import os
import subprocess
import uuid

from agent_pm.session.engine.types import Launch
from agent_pm.utils import Record, Status

from session.tests import live_tmux
from session.tests.cli_support import COMMAND, MANAGER, CliCase
from session.tests.contracts.host import program
from session.tests.fakes.host import FakeHost

PROG = "agent-pm-session"
OTHER = "other"
SLEEP = "import time; time.sleep(120)"


def attach(name: str) -> str:
    return f"tmux attach -t '={name}'"


def absent(name: str) -> Record:
    return Record(
        v=1,
        kind="worker",
        name=name,
        sessions=(name,),
        sid=None,
        workdir=None,
        ctl=(PROG,),
        resume=(PROG, "resume", name),
        display=None,
        detail=None,
    )


def lines(*records: Record) -> str:
    return "".join(record.to_line() + "\n" for record in records)


def gone(name: str) -> Status:
    return Status(name=name, alive=False, state="gone", outcome=None, attach=None, hidden=False)


def live(
    name: str,
    state: str,
    *,
    showable: bool = True,
    cwd: str | None = None,
    hidden: bool = False,
) -> Status:
    """A live session's Status: its tmux attach command, none when its Host cannot display; a
    worker's cwd.
    """
    command = attach(name) if showable else None
    return Status(
        name=name, alive=True, state=state, outcome=None, attach=command, cwd=cwd, hidden=hidden
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ControlCase(CliCase):
    def sessions(self) -> list[str]:
        result = self.server.tmux("list-sessions", "-F", "#{session_name}")
        self.assertEqual(result.returncode, 0, result.stderr)
        return sorted(result.stdout.split())

    def tmux(self, *args: str) -> None:
        result = self.server.tmux(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def done(self, name: str) -> Record:
        record = self.record(self.start(name))
        self.wait_event(name, "done")
        return record

    def assert_printed(self, result, stdout: str) -> None:
        self.ok(result)
        self.assertEqual(result.stdout, stdout)


class StatusTest(ControlCase):
    def test_status_answers_each_name_or_record_in_input_order(self):
        w1 = self.done("w1")
        done = live("w1", "done", cwd=self.cwd)
        for argv, stdin, expected in (
            (("w1", "nope"), None, [done, gone("nope")]),
            (("nope", "w1"), None, [gone("nope"), done]),
            ((), lines(w1, absent("nope")), [done, gone("nope")]),
            ((), lines(absent("nope"), w1), [gone("nope"), done]),
        ):
            with self.subTest(argv=argv, stdin=stdin):
                result = self.session("status", *argv, stdin=stdin)
                self.assert_printed(result, "".join(s.to_line() + "\n" for s in expected))
                self.assertEqual(
                    [Status.parse(line) for line in result.stdout.splitlines()], expected
                )
        self.assertEqual(self.sessions(), [MANAGER, "w1"])
        self.assertEqual(len(self.calls("w1")), 1)

    def test_a_duplicate_or_a_bad_stdin_line_refuses_the_whole_batch_before_any_action(self):
        w1 = self.done("w1")
        pane = self.server.pane_of("w1")
        line = w1.to_line() + "\n"
        nowhere = dataclasses.replace(w1, name="w9", sessions=()).to_line() + "\n"
        for cmd in ("status", "show", "close", "attach-command"):
            for argv, stdin, stderr in (
                (("w1", "w1"), None, f"{PROG}: {cmd}: w1 given twice\n"),
                ((), line + line, f"{PROG}: {cmd}: w1 given twice\n"),
                ((), line + "not json\n", f"{PROG}: {cmd}: stdin: Record line: not JSON"),
                ((), line + '{"v": 1}\n', f"{PROG}: {cmd}: stdin: kind: missing\n"),
                ((), line + nowhere, f"{PROG}: w9: names no session\n"),
            ):
                with self.subTest(cmd=cmd, argv=argv, stdin=stdin):
                    result = self.session(cmd, *argv, stdin=stdin)
                    self.assertEqual((result.returncode, result.stdout), (2, ""), result.stderr)
                    self.assertTrue(result.stderr.startswith(stderr), result.stderr)
                    self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertEqual(self.sessions(), [MANAGER, "w1"])
        self.assertEqual(self.server.pane_of("w1"), pane)
        self.assertEqual(self.status("w1"), [live("w1", "done", cwd=self.cwd)])
        self.assertEqual(len(self.calls("w1")), 1)

    def test_stdin_that_is_not_utf_8_is_a_usage_error(self):
        result = subprocess.run(
            [COMMAND, "status"],
            input=b"\xff\n",
            capture_output=True,
            env=self.env(),
            cwd=self.cwd,
            timeout=60,
        )
        self.assertEqual((result.returncode, result.stdout), (2, b""), result.stderr)
        self.assertTrue(result.stderr.startswith(f"{PROG}: status: stdin: ".encode()))
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)


class ControlTest(ControlCase):
    def test_attach_command_prints_each_sessions_attach_command_in_input_order(self):
        w1, w2 = self.record(self.start("w1")), self.record(self.start("w2"))
        both = f"{attach('w1')}\n{attach('w2')}\n"
        self.assert_printed(self.session("attach-command", "w1", "w2"), both)
        self.assert_printed(self.session("attach-command", stdin=lines(w1, w2)), both)
        self.assert_printed(
            self.session("attach-command", stdin=lines(w2, w1)), f"{attach('w2')}\n{attach('w1')}\n"
        )

    def test_close_closes_only_the_named_sessions_and_again_is_fine(self):
        records = {name: self.done(name) for name in ("w1", "w2", "w3")}
        pids = {name: self.calls(name)[0]["pid"] for name in records}
        for _ in range(2):
            self.assert_printed(self.session("close", "w1"), "")
            self.assert_printed(self.session("close", stdin=lines(records["w2"])), "")
        self.assertEqual(self.sessions(), [MANAGER, "w3"])
        for name in ("w1", "w2"):
            live_tmux.wait(lambda name=name: not pid_alive(pids[name]), what=f"{name} to end")
        self.assertTrue(pid_alive(pids["w3"]))
        self.assertEqual(
            self.status("w1", "w2", "w3"),
            [gone("w1"), gone("w2"), live("w3", "done", cwd=self.cwd)],
        )

    def test_close_show_and_hide_refuse_a_record_whose_sid_its_session_does_not_carry(self):
        w1 = self.done("w1")
        pid = self.calls("w1")[0]["pid"]
        pane = self.server.pane_of("w1")
        self.tmux("kill-pane", "-t", pane)
        reused = dataclasses.replace(w1, sid=str(uuid.uuid4()))
        for verb in ("show", "hide", "close"):
            with self.subTest(verb=verb):
                res = self.session(verb, stdin=lines(reused))
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                self.assertEqual(
                    res.stderr,
                    f"{PROG}: w1: session w1 carries sid {w1.sid}, not the Record's {reused.sid}\n",
                )
        self.assertEqual(self.sessions(), [MANAGER, "w1"])
        self.assertTrue(pid_alive(pid))
        self.assertEqual(self.server.pane_of("w1"), pane)
        self.assert_printed(self.session("close", stdin=lines(w1)), "")
        self.assertEqual(self.sessions(), [MANAGER])

    def shows(self, name: str, window: str) -> None:
        """name's @pane is in `window` and runs a client of name."""
        pane = self.server.pane_of(name)
        self.assertIn(pane, self.server.panes(window))
        tty = self.server.tmux("display-message", "-p", "-t", pane, "#{pane_tty}").stdout.strip()

        def attached():
            found = self.server.tmux("list-clients", "-t", f"={name}", "-F", "#{client_tty}")
            return tty in found.stdout.split()

        live_tmux.wait(attached, what=f"{pane} to attach {name}")

    def test_show_places_a_session_by_its_own_display_or_by_a_records(self):
        self.tmux("new-session", "-d", "-s", OTHER, "-x", "200", "-y", "50", *live_tmux.IDLE)
        self.server.attach(OTHER)
        w1 = self.done("w1")
        self.assertEqual(w1.display, MANAGER)
        self.shows("w1", f"={MANAGER}:")
        self.tmux("kill-pane", "-t", self.server.pane_of("w1"))
        moved = dataclasses.replace(w1, display=OTHER)
        self.assert_printed(self.session("show", stdin=lines(moved)), "")
        self.assertEqual(self.server.option("w1", "@opener"), OTHER)
        self.shows("w1", f"={OTHER}:")
        self.assert_printed(self.session("show", "w1"), "")
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)
        self.shows("w1", f"={MANAGER}:")
        self.assertEqual(len(self.calls("w1")), 1)

    def test_hide_closes_the_view_keeps_the_session_and_show_reopens_it(self):
        w1 = self.done("w1")
        self.shows("w1", f"={MANAGER}:")
        pane = self.server.pane_of("w1")
        for names, stdin in ((("w1",), None), ((), lines(w1))):
            self.assert_printed(self.session("hide", *names, stdin=stdin), "")
            self.assertNotIn(pane, self.server.panes(f"={MANAGER}:"))
            self.assertEqual(self.sessions(), [MANAGER, "w1"])
            self.assertEqual(self.server.option("w1", "@hidden"), "1")
            self.assertEqual(
                self.status(stdin=lines(w1)), [live("w1", "done", cwd=self.cwd, hidden=True)]
            )
        self.assert_printed(self.session("show", stdin=lines(w1)), "")
        self.shows("w1", f"={MANAGER}:")
        self.assertEqual(self.server.option("w1", "@hidden"), "0")
        self.assertEqual(self.status("w1"), [live("w1", "done", cwd=self.cwd)])
        self.assertEqual(len(self.calls("w1")), 1)

    def test_hide_of_an_absent_session_fails_with_its_reason(self):
        res = self.session("hide", "nope")
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        self.assertEqual(res.stderr, f"{PROG}: nope: no tmux session nope\n")

    def test_a_key_its_host_refuses_fails_alone_and_the_batch_goes_on(self):
        w1, w2 = self.done("w1"), self.done("w2")
        self.tmux("kill-pane", "-t", self.server.pane_of("w2"))
        bad = dataclasses.replace(w1, display="a/b/c/d")
        res = self.session("show", stdin=lines(bad, w2))
        self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
        self.assertEqual(
            res.stderr, f"{PROG}: w1: display 'a/b/c/d': not <opener>[/<split>[/<split_from>]]\n"
        )
        self.shows("w2", f"={MANAGER}:")


class NotShowableTest(ControlCase):
    def setUp(self):
        super().setUp()
        host = FakeHost("fake", {"root": self.fake_host(default="fake")})
        self.f1 = dataclasses.replace(absent("f1"), display=MANAGER)
        process = host.terminal(
            "f1", Launch(tuple(program(SLEEP)), cwd=self.cwd), display=MANAGER, record=self.f1
        )
        self.addCleanup(process.close)

    def test_show_attach_command_read_and_tile_exit_1_saying_the_host_cannot_display(self):
        refused = "session f1: host fake cannot display\n"
        for argv, stdin, stderr in (
            (("show", "f1"), None, refused),
            (("show",), lines(self.f1), refused),
            (("hide", "f1"), None, refused),
            (("attach-command", "f1"), None, refused),
            (("attach-command",), lines(self.f1), refused),
            (("read", "f1"), None, refused),
            (("read", "f1", "--lines", "5"), None, refused),
            (("tile", MANAGER), None, f"session {MANAGER}: host fake cannot display\n"),
        ):
            with self.subTest(argv=argv, stdin=stdin):
                result = self.session(*argv, stdin=stdin, module=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", stderr))
        self.assertEqual(self.status("f1", module=True), [live("f1", "working", showable=False)])

    def test_status_on_a_host_that_cannot_display_gives_no_attach_command(self):
        self.assertEqual(
            self.status("f1", "nope", module=True),
            [live("f1", "working", showable=False), gone("nope")],
        )
        self.assertEqual(
            self.status(stdin=lines(absent("nope"), self.f1), module=True),
            [gone("nope"), live("f1", "working", showable=False)],
        )
