"""Crew.run: the starter command's final Record becomes the live entry; a failed start is lost.
Crew.attach shows each live entry but a hidden one, whose view column says so.
"""

import contextlib
import io
import os
import uuid
from dataclasses import replace
from unittest import mock

from agent_pm.manager.engine.crew import HEADER, Crew
from agent_pm.manager.engine.types import RosterError, Spec, State
from agent_pm.utils import Record, Status

from manager.tests.roster_case import RosterCase
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired

ARGV = ("fake-ctl", "start", "x")


class Run(RosterCase):
    def start(self, run, name="x"):
        stderr = io.StringIO()
        with (
            mock.patch("subprocess.run", side_effect=run) as called,
            contextlib.redirect_stderr(stderr),
        ):
            try:
                return Crew(self.roster).run(Spec(name, ARGV)), called, stderr.getvalue()
            except RuntimeError as error:
                return error, called, stderr.getvalue()

    def test_the_final_record_on_stdout_becomes_the_live_entry(self):
        def run(_argv, **options):
            record = replace(self.record, sid=options["env"]["AGENT_PM_SID"])
            os.write(options["stdout"].fileno(), f"progress\n{record.to_line()}\n".encode())
            return mock.Mock(returncode=0, stderr="warning\n")

        record, called, stderr = self.start(run)
        entry = self.roster.entries()["x"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, record))
        self.assertEqual(record, replace(self.record, sid=entry.sid))
        self.assertEqual(called.call_args.args, (ARGV,))
        env = called.call_args.kwargs["env"]
        self.assertEqual(env["AGENT_PM_EVENTS"], self.roster.events)
        self.assertEqual(env["AGENT_PM_SID"], entry.sid)
        self.assertIs(called.call_args.kwargs["stdin"], DEVNULL)
        self.assertEqual(stderr, "warning\n")

    def test_a_failed_start_raises_and_marks_the_entry_lost(self):
        for name, effect, message in (
            ("stderr", mock.Mock(returncode=2, stderr=" boom \n"), "boom"),
            ("exit", mock.Mock(returncode=2, stderr=""), "exit 2"),
            ("missing", FileNotFoundError(2, "No such file"), "[Errno 2] No such file"),
            ("timeout", TimeoutExpired(ARGV, 30), "no response within 30 seconds"),
        ):
            with self.subTest(name=name):
                error, _, _ = self.start(mock.Mock(side_effect=[effect]), name)
                self.assertEqual(str(error), f"fake-ctl start x: {message}")
                self.assertIs(self.roster.entries()[name].state, State.LOST)


class ResumeInput(RosterCase):
    def setUp(self):
        super().setUp()
        self.live()
        self.roster.mark("x", State.WAITING)

    def resume(self, *, code=0, output=None, input="answer", during=None):
        def run(argv, **options):
            self.assertEqual(argv, (*self.record.resume, "--input", input))
            self.assertEqual(options["env"]["AGENT_PM_SID"], self.record.sid)
            self.assertEqual(options["env"]["AGENT_PM_EVENTS"], self.roster.events)
            self.assertIs(self.roster.entries()["x"].state, State.RESUMING)
            if during is not None:
                during()
            text = self.record.to_line() + "\n" if output is None else output
            os.write(options["stdout"].fileno(), text.encode())
            return mock.Mock(returncode=code, stderr="refused" if code else "")

        with mock.patch("subprocess.run", side_effect=run):
            return Crew(self.roster).resume("x", input=input)

    def test_input_and_empty_input_resume_the_same_identity(self):
        for text in ("answer", ""):
            with self.subTest(text=text):
                self.assertEqual(self.resume(input=text), self.record)
                self.assertIs(self.roster.entries()["x"].state, State.LIVE)
                self.roster.mark("x", State.WAITING)

    def test_only_an_empty_exit_two_restores_waiting(self):
        with self.assertRaisesRegex(ValueError, "refused"):
            self.resume(code=2, output="")
        self.assertIs(self.roster.entries()["x"].state, State.WAITING)
        for code, output in ((1, ""), (2, "nonempty"), (0, "not a Record")):
            with self.subTest(code=code, output=output):
                with self.assertRaises((RosterError, RuntimeError)):
                    self.resume(code=code, output=output)
                self.assertIs(self.roster.entries()["x"].state, State.GONE)
                self.roster.mark("x", State.WAITING)

    def test_cached_adoption_cannot_promote_the_current_input_attempt(self):
        def adopt():
            self.roster.adopt(self.record, resuming_before="2000-01-01T00:00:00+00:00")
            self.assertIs(self.roster.entries()["x"].state, State.RESUMING)

        with self.assertRaises(ValueError):
            self.resume(code=2, output="", during=adopt)
        self.assertIs(self.roster.entries()["x"].state, State.WAITING)

    def test_stale_claims_can_still_be_adopted(self):
        self.roster.claim_resume("x", state=State.WAITING)
        self.roster.adopt(self.record, resuming_before="9999-01-01T00:00:00+00:00")
        self.assertIs(self.roster.entries()["x"].state, State.LIVE)

    def test_waiting_without_input_is_rejected_without_a_controller_call(self):
        with mock.patch("subprocess.run") as run, self.assertRaisesRegex(RuntimeError, "waiting"):
            Crew(self.roster).resume("x")
        run.assert_not_called()

    def test_input_rejects_other_states_without_queries_or_changes(self):
        for state in (State.LIVE, State.GONE, State.DONE, State.RESUMING):
            with self.subTest(state=state):
                self.roster.remove("x")
                self.live()
                if state != State.LIVE:
                    self.roster.mark("x", State.GONE if state == State.RESUMING else state)
                    if state == State.RESUMING:
                        self.roster.claim_resume("x")
                before = self.roster.entries()
                with mock.patch("subprocess.run") as run, self.assertRaises(ValueError):
                    Crew(self.roster).resume("x", input="answer")
                run.assert_not_called()
                self.assertEqual(self.roster.entries(), before)


class RecordlessStatus(RosterCase):
    def test_a_launch_without_a_record_is_starting_then_gone_once_lost(self):
        starting = self.roster.begin(Spec("s", ARGV), sid=str(uuid.uuid4()))
        lost = self.roster.begin(Spec("l", ARGV), sid=str(uuid.uuid4()))
        self.assertTrue(self.roster.mark_if("l", lost, State.LOST))
        self.assertIsNone(starting.record)
        self.assertEqual(
            Crew(self.roster).status(),
            [
                Status(
                    name="l", alive=False, state="gone", outcome=None, attach=None, hidden=False
                ),
                Status(
                    name="s", alive=False, state="starting", outcome=None, attach=None, hidden=False
                ),
            ],
        )


class Attach(RosterCase):
    def test_a_hidden_live_entry_stays_closed_and_its_view_says_hidden(self):
        shown, hidden = (
            replace(self.record, name=name, sessions=(name,), sid=str(uuid.uuid4()))
            for name in ("v", "h")
        )
        self.live("v", record=shown)
        self.live("h", record=hidden)
        shows = []

        def run(argv, **options):
            records = [Record.parse(line) for line in options["input"].splitlines()]
            if argv[1:] == ("show",):
                shows.append(records)
            statuses = (
                Status(
                    name=record.name,
                    alive=True,
                    state="working",
                    outcome=None,
                    attach=f"attach {record.name}",
                    hidden=record.name == "h",
                )
                for record in records
            )
            stdout = "".join(s.to_line() + "\n" for s in statuses) if argv[1] == "status" else ""
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with mock.patch("subprocess.run", side_effect=run):
            table = Crew(self.roster).attach().splitlines()
        self.assertEqual(shows, [[shown]])
        self.assertEqual(table[0], HEADER)
        self.assertIn(f"h\tcustom\t{hidden.sid}\tworking\thidden\t-\t", table[1])
        self.assertIn(f"v\tcustom\t{shown.sid}\tworking\t-\t-\t", table[2])


class View(RosterCase):
    def setUp(self):
        super().setUp()
        self.records = {}
        for name in ("a", "b", "c"):
            self.records[name] = replace(
                self.record, name=name, sessions=(name,), sid=str(uuid.uuid4())
            )
            self.live(name, record=self.records[name])
        self.calls = []
        self.alive = {"a": True, "b": True, "c": False}
        self.attach = {"a": "attach a", "b": None, "c": "attach c"}
        self.answers = {}

    def answer(self, argv, **options):
        records = [Record.parse(line) for line in options["input"].splitlines()]
        if argv[-1] == "status":
            stdout = "".join(
                Status(
                    name=r.name,
                    alive=self.alive[r.name],
                    state="working",
                    outcome=None,
                    attach=self.attach[r.name],
                    hidden=False,
                ).to_line()
                + "\n"
                for r in records
            )
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        self.calls.append((argv, records))
        return self.answers.get(records[0].name, mock.Mock(returncode=0, stdout="", stderr=""))

    def view(self, verb, names=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch("subprocess.run", side_effect=self.answer),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            failures = Crew(self.roster).view(verb, names)
        return failures, stdout.getvalue(), stderr.getvalue()

    def test_each_named_entry_goes_once_to_its_ctl_in_the_given_order(self):
        failures, _, _ = self.view("hide", ("c", "a", "c"))
        self.assertEqual(failures, {})
        self.assertEqual(
            self.calls,
            [
                (("agent-pm-session", "hide"), [self.records["c"]]),
                (("agent-pm-session", "hide"), [self.records["a"]]),
            ],
        )

    def test_all_takes_the_alive_showable_entries(self):
        self.view("show")
        self.assertEqual(self.calls, [(("agent-pm-session", "show"), [self.records["a"]])])

    def test_the_layers_output_is_relayed(self):
        self.answers["a"] = mock.Mock(returncode=0, stdout="tmux attach -t a\n", stderr="note\n")
        _, stdout, stderr = self.view("show", ("a",))
        self.assertEqual((stdout, stderr), ("tmux attach -t a\n", "note\n"))

    def test_a_failure_leaves_the_rest_and_names_its_reason(self):
        self.answers["a"] = mock.Mock(returncode=1, stdout="", stderr=" boom \n")
        self.answers["b"] = mock.Mock(returncode=1, stdout="", stderr="")
        failures, _, _ = self.view("hide", ("a", "b", "c"))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(
            failures,
            {"a": "agent-pm-session hide: boom", "b": "agent-pm-session hide: exit 1"},
        )

    def test_all_reports_an_entry_whose_status_failed(self):
        def run(argv, **options):
            if argv[-1] == "status":
                return mock.Mock(returncode=2, stdout="", stderr="down")
            return self.answer(argv, **options)

        stdout = io.StringIO()
        with mock.patch("subprocess.run", side_effect=run), contextlib.redirect_stdout(stdout):
            failures = Crew(self.roster).view("hide")
        self.assertEqual(failures, dict.fromkeys(("a", "b", "c"), "agent-pm-session status: down"))
        self.assertEqual(self.calls, [])

    def test_a_named_entry_without_a_record_fails(self):
        self.roster.begin(Spec("s", ARGV), sid=str(uuid.uuid4()))
        failures, _, _ = self.view("show", ("s",))
        self.assertEqual(failures, {"s": "starting: no Record"})
        self.assertEqual(self.calls, [])

    def test_an_unknown_name_raises_before_any_call(self):
        with self.assertRaisesRegex(ValueError, "not in roster: x, y"):
            self.view("hide", ("a", "y", "x"))
        self.assertEqual(self.calls, [])
