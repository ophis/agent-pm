"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import io
import os
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from agent_pm.manager import Spec, State
from agent_pm.manager.cli import main
from agent_pm.manager.engine.pr import VIEW_FIELDS
from agent_pm.utils import Record, Status, StatusState

from manager.tests.cli_case import CliCase
from utils.tests.fakes import ctl


class CliTest(CliCase):
    def test_invalid_or_partial_holder_environment_is_usage_error_without_writes(self):
        identity = str(uuid.uuid4())
        for env in (
            {"AGENT_PM_HOLDER_ID": identity},
            {"AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": "", "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": "invalid", "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": str(uuid.uuid1()), "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": identity.upper(), "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "0"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "-1"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "01"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "text"},
        ):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                result, stdout, stderr = self.invoke("attach", "--manager", "mgr")
                self.assertEqual(result, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("crew: "))
                self.assertFalse(os.path.exists(self.directory))

    def test_attach_uses_holder_generation_and_does_not_take_new_lease(self):
        with self.roster.hold("label") as lease:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_PM_HOLDER_ID": lease.id,
                        "AGENT_PM_HOLDER_GENERATION": str(lease.generation),
                    },
                ),
                self.listed(),
            ):
                result, _, stderr = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result, 0, stderr)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_PM_HOLDER_ID": lease.id,
                        "AGENT_PM_HOLDER_GENERATION": str(lease.generation + 1),
                    },
                ),
                self.listed(),
            ):
                result, _, stderr = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result, 1, stderr)

    def test_unleased_attach_cannot_access_live_holder(self):
        with self.roster.hold("label"), self.listed():
            result, _, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual(result, 1, stderr)

    def test_removed_owner_and_missing_holder_command_are_usage_errors(self):
        for argv in (
            ("attach", "--manager", "mgr", "--owner", "label"),
            ("hold", "--manager", "mgr"),
            ("hold", "--manager", "mgr", "--"),
            ("attach", "--manager", "mgr", "--", "unused"),
            ("hide", "--manager", "mgr"),
            ("show", "--manager", "mgr", "a", "--all"),
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(argv)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(os.path.exists(self.directory))

    def test_missing_entry_is_usage_error_without_controller_or_writes(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing ctl")) as run:
            for argv in (
                ("close",),
                ("status",),
                ("forget",),
                ("resume",),
                ("forget", "--force"),
                ("hide",),
                ("show",),
            ):
                with self.subTest(argv=argv):
                    command, *extra = argv
                    result, stdout, stderr = self.invoke(
                        command, "missing", "--manager", "mgr", *extra
                    )
                    self.assertEqual(result, 2, stderr)
                    self.assertEqual(stdout, "")
                    self.assertIn("not in roster", stderr)
                    self.assertFalse(os.path.exists(self.directory))
        run.assert_not_called()

    def test_empty_close_all_reports_nothing_without_creating_a_manager(self):
        with self.listed():
            result, stdout, stderr = self.invoke("close-all", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, "nothing to close\n")
        self.assertFalse(os.path.exists(self.directory))


class Launch(CliCase):
    def start(self, *arguments, code=0, output=None):
        self.calls = []

        def run(argv, **options):
            self.calls.append((tuple(argv), options["env"]))
            record = self.fake_record(
                "returned-" + options["env"]["AGENT_PM_SID"][:8],
                sid=options["env"]["AGENT_PM_SID"],
            )
            text = record.to_line() + "\n" if output is None else output
            os.write(options["stdout"].fileno(), text.encode())
            return SimpleNamespace(returncode=code, stderr="layer message\n")

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke(*arguments)

    def test_worker_and_issue_forward_every_word_after_the_key(self):
        for verb, key, prefix, tail in (
            (
                "worker",
                "w1",
                ("agent-pm-session", "start", "w1"),
                ("--client", "claude", "--prompt", "hi", "--", "--permission-mode", "auto"),
            ),
            (
                "issue",
                "TASK-12",
                ("agent-pm-linear", "router", "--issue", "TASK-12", "--tui"),
                ("--display", "D", "--note", "belongs to layer", "--manager", "also layer"),
            ),
        ):
            with self.subTest(verb=verb):
                result, stdout, stderr = self.start(
                    verb, "--manager", "mgr", "--note", "Crew note", key, *tail
                )
                self.assertEqual((result, stderr), (0, "layer message\n"))
                self.assertEqual(self.calls[0][0], (*prefix, *tail))
                entry = self.roster.entries()[key]
                self.assertEqual(entry.note, "Crew note")
                self.assertEqual(entry.record, Record.parse(stdout))

    def test_role_names_share_the_generated_launch_sid(self):
        for explicit in ((), ("--name", "chosen")):
            with self.subTest(explicit=explicit):
                result, stdout, _ = self.start(
                    "role",
                    "--manager",
                    "mgr",
                    *explicit,
                    "pm",
                    "--task",
                    "product-design",
                    "--input",
                    "x",
                )
                self.assertEqual(result, 0)
                sid = Record.parse(stdout).sid
                name = "chosen" if explicit else f"pm-{sid[:8]}"
                self.assertEqual(self.roster.entries()[name].sid, sid)
                self.assertEqual(
                    self.calls[0][0],
                    (
                        "agent-pm-delegate",
                        "run",
                        "--role",
                        "pm",
                        "--detach",
                        "--name",
                        name,
                        "--task",
                        "product-design",
                        "--input",
                        "x",
                    ),
                )

    def test_refusals_remove_only_the_shorthand_intent_and_allow_name_reuse(self):
        for code, output in ((2, ""), (0, "dry run\n"), (0, "")):
            with self.subTest(code=code, output=output):
                result, stdout, stderr = self.start(
                    "worker",
                    "--manager",
                    "mgr",
                    "w1",
                    "--note",
                    "layer option",
                    code=code,
                    output=output,
                )
                self.assertEqual((result, stdout), (2, ""))
                self.assertIn("layer message", stderr)
                self.assertEqual(self.roster.entries(), {})

    def test_other_failures_keep_lost_and_generic_run_keeps_exit_two(self):
        for arguments, code, output in (
            (("worker", "--manager", "mgr", "a"), 1, ""),
            (("worker", "--manager", "mgr", "b"), 2, "not empty\n"),
            (("run", "--manager", "mgr", "c", "--", "fake"), 2, ""),
        ):
            with self.subTest(arguments=arguments):
                result, stdout, _ = self.start(*arguments, code=code, output=output)
                self.assertEqual((result, stdout), (1, ""))
        self.assertEqual({e.state.value for e in self.roster.entries().values()}, {"lost"})


class ResumeInput(CliCase):
    def test_multiple_names_with_input_fail_before_any_roster_write(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["resume", "--manager", "mgr", "a", "b", "--input", "answer"])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(os.path.exists(self.directory))

    def test_refusal_returns_two_and_preserves_waiting(self):
        record = self.fake_record("r1")
        self.roster.begin(Spec("r1", ("start",)), sid=record.sid)
        self.roster.commit("r1", record)
        self.roster.mark("r1", State.WAITING)
        with mock.patch(
            "subprocess.run", return_value=SimpleNamespace(returncode=2, stderr="refused")
        ):
            code, stdout, stderr = self.invoke("resume", "--manager", "mgr", "r1", "--input", "")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("refused", stderr)
        self.assertIs(self.roster.entries()["r1"].state, State.WAITING)


class NextEventContent(CliCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.answers = {}
        self.inputs = []

    def launched(self, name, *sessions, record_name=None):
        sid = str(uuid.uuid4())
        record = replace(
            self.fake_record(record_name or name, sid=sid), sessions=sessions or (name,)
        )
        self.roster.begin(Spec(name, ("start",)), sid=sid)
        self.roster.commit(name, record)
        return record

    def next_event(self, event, *extra):
        Path(self.roster.events).write_text(f"{event}\n")

        def run(argv, **options):
            self.calls.append(tuple(argv))
            self.inputs.append(options["input"])
            code, out, err = self.answers.get(tuple(argv), (1, "", "no such session"))
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke(
                "next-event", "--manager", "mgr", "--after", "0", "--gen", "0", *extra
            )

    def test_without_content_prints_only_the_event_line(self):
        self.launched("w1")
        self.assertEqual(self.next_event("12:00:00 w1 done"), (0, "1 12:00:00 w1 done\n", ""))
        self.assertEqual(self.calls, [])

    def test_each_event_uses_its_owners_ctl_content(self):
        for sessions, event, answer, expected in (
            (("w1",), "w1 done", "the answer\n", "the answer\n"),
            (
                ("test-drive",),
                "test-drive done",
                "the answer\n",
                "the answer\n",
            ),
            (
                ("w1",),
                "w1 blocked",
                "Allow this edit?\n 1. Yes",
                "Allow this edit?\n 1. Yes\n",
            ),
        ):
            with self.subTest(sessions=sessions, event=event):
                record = self.launched("w1", *sessions)
                self.answers = {(*record.ctl, "content"): (0, answer, "")}
                self.assertEqual(
                    self.next_event(f"12:00:00 {event}", "--content"),
                    (0, f"1 12:00:00 {event}\n{expected}", ""),
                )
                self.assertEqual(self.calls[-1], (*record.ctl, "content"))
                self.assertEqual(self.inputs[-1], record.to_line() + "\n")
                self.roster.remove("w1")

    def test_a_harness_event_prints_its_entry_status(self):
        for sessions, record_name in (
            (("engineer-ab12-drive",), "engineer-ab12"),
            (("engineer-ab12", "engineer-ab12-drive"), None),
        ):
            with self.subTest(sessions=sessions):
                record = self.launched("r1", *sessions, record_name=record_name)
                status = Status(
                    name=record.name,
                    alive=False,
                    state="dead",
                    outcome="done",
                    attach=None,
                    hidden=False,
                )
                self.answers = {(*record.ctl, "content"): (0, status.to_line() + "\n", "")}
                self.assertEqual(
                    self.next_event("12:00:00 engineer-ab12-drive outcome done", "--content"),
                    (
                        0,
                        f"1 12:00:00 engineer-ab12-drive outcome done\n{status.to_line()}\n",
                        "",
                    ),
                )
                self.assertNotIn(("agent-pm-session", "read", "engineer-ab12-drive"), self.calls)
                self.roster.remove("r1")

    def test_a_ctl_failure_is_reported_without_falling_back_to_session_commands(self):
        record = self.launched("r1", "tui", "harness")
        self.answers = {(*record.ctl, "content"): (1, "", "not available")}
        code, stdout, _ = self.next_event("12:00:00 tui done", "--content")
        self.assertEqual(code, 0)
        self.assertIn("content unavailable:", stdout)
        self.assertIn("not available", stdout)
        self.assertEqual(self.calls, [(*record.ctl, "content")])

    def test_an_event_naming_only_control_characters_still_exits_0(self):
        self.launched("w1")
        result, stdout, _ = self.next_event("12:00:00 \x01 done", "--content")
        self.assertEqual(result, 0)
        head, content = stdout.split("\n", 1)
        self.assertEqual(head, "1 12:00:00  done")
        self.assertTrue(content.startswith("content unavailable: "), content)
        self.assertEqual(self.calls, [])

    def test_unreadable_content_still_prints_the_event_and_says_why(self):
        self.launched("w1")
        for event in ("12:00:00 gone dead", "12:00:00 gone done", "12:00:00 gone-drive exited 0"):
            with self.subTest(event=event):
                result, stdout, _ = self.next_event(event, "--content")
                self.assertEqual(result, 0)
                head, content = stdout.split("\n", 1)
                self.assertEqual(head, f"1 {event}")
                self.assertTrue(content.startswith("content unavailable: "), content)
                self.assertTrue(content.endswith("\n") and content.count("\n") == 1, content)

    def test_follow_prints_each_event_block_and_keeps_going(self):
        record = self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\nnoise\n12:00:01 w1 blocked\n")
        stdout = io.StringIO()
        writes = []
        stdout.write = lambda text: writes.append(text) or len(text)

        answers = iter(((0, "the answer\nline 2\n", ""), (1, "", "no such session")))

        def run(argv, **options):
            self.assertEqual(tuple(argv), (*record.ctl, "content"))
            self.assertEqual(options["input"], record.to_line() + "\n")
            code, out, err = next(answers)
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        argv = ("next-event", "--manager", "mgr", "--after", "0", "--gen", "0")
        with (
            mock.patch("subprocess.run", side_effect=run),
            mock.patch("time.sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(KeyboardInterrupt),
        ):
            main([*argv, "--content", "--follow"])
        self.assertEqual(
            [text for text in writes if text],
            [
                "1 12:00:00 w1 done\nthe answer\nline 2\n",
                f"3 12:00:01 w1 blocked\ncontent unavailable: {ctl.COMMAND} content: "
                "no such session\n",
            ],
        )
        self.assertEqual(self.roster.backlog()[0], 0)
        self.assertEqual(len(self.roster.backlog()[2]), 3)

    def test_read_and_pending_retain_events_until_explicit_ack(self):
        self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\n12:00:01 w1 blocked\n")
        result, stdout, stderr = self.invoke(
            "next-event", "--manager", "mgr", "--after", "1", "--gen", "0"
        )
        self.assertEqual((result, stdout, stderr), (0, "2 12:00:01 w1 blocked\n", ""))
        self.assertEqual(self.roster.backlog()[0], 0)
        result, stdout, stderr = self.invoke("pending", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertIn("1 12:00:00 w1 done", stdout)
        self.assertIn("2 12:00:01 w1 blocked", stdout)
        self.assertIn("N=0 GEN=0", stdout)
        for _ in range(2):
            result, _, stderr = self.invoke(
                "ack", "--manager", "mgr", "--through", "1", "--gen", "0"
            )
            self.assertEqual(result, 0, stderr)
            self.assertEqual(self.roster.backlog(), (1, 0, ["12:00:01 w1 blocked"]))
        result, _, _ = self.invoke("ack", "--manager", "mgr", "--through", "2", "--gen", "9")
        self.assertEqual(result, 1)
        self.assertEqual(self.roster.backlog()[0], 1)

    def test_follow_exits_1_on_stale(self):
        self.launched("w1")
        Path(self.roster.events).write_text("12:00:00 w1 done\n")
        result, stdout, stderr = self.invoke(
            "next-event", "--manager", "mgr", "--after", "0", "--gen", "5", "--follow"
        )
        self.assertEqual((result, stdout), (1, ""))
        self.assertIn("crew: stale line numbers", stderr)


URL = "https://github.com/o/r/pull/7"
HEADER = "name\tkind\tstate\tnote\treply\tpr\tchecks\tmergeable\treview\n"
VIEW = (
    '{"number":7,"mergeable":"MERGEABLE","reviewDecision":"APPROVED",'
    '"statusCheckRollup":[{"status":"COMPLETED","conclusion":"SUCCESS"}]}'
)


class Report(CliCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.statuses = {}
        self.answers: dict[tuple[str, ...], Any] = {}

    def launched(self, name, *, note=None, record=None, alive=True, outcome=None):
        sid = str(uuid.uuid4())
        record = replace(record or self.fake_record(name), sid=sid)
        self.roster.begin(Spec(name, ("start",), note), sid=sid)
        self.roster.commit(name, record)
        self.statuses[record.name] = Status(
            name=record.name,
            alive=alive,
            state=StatusState.WORKING if alive else StatusState.GONE,
            outcome=outcome,
            attach=None,
            hidden=False,
        )
        self.answers[(*record.ctl, "status")] = self.status_answer
        return record

    def status_answer(self, text):
        lines = (Record.parse(line).name for line in text.splitlines())
        return 0, "".join(self.statuses[name].to_line() + "\n" for name in lines), ""

    def report(self):
        def run(argv, **kwargs):
            self.calls.append(tuple(argv))
            key = (
                (*argv, Record.parse(kwargs["input"]).name)
                if argv[-1] == "content"
                else tuple(argv)
            )
            answer = self.answers.get(key, (1, "", "no such session"))
            code, out, err = answer if isinstance(answer, tuple) else answer(kwargs.get("input"))
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke("report", "--manager", "mgr")

    def gh_calls(self):
        return [call for call in self.calls if call[0] == "gh"]

    def delegate_record(self, name, workdir="/w"):
        return Record(
            1,
            "role",
            name,
            (f"{name}-drive", name),
            None,
            workdir,
            ("agent-pm-delegate",),
            ("agent-pm-delegate", "resume", workdir),
            None,
            "engineer",
        )

    def test_one_row_per_entry_sorted_with_header(self):
        self.launched("b", note="fix the parser")
        self.launched("a", alive=False, outcome="done")
        self.roster.begin(Spec("c", ("start",)), sid=str(uuid.uuid4()))
        self.answers[(ctl.COMMAND, "content", "b")] = (0, "\n  first line  \nsecond\n", "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(
            stdout,
            HEADER
            + "a\tfake\tdone\t-\t-\t-\t-\t-\t-\n"
            + "b\tfake\tworking\tfix the parser\tfirst line\t-\t-\t-\t-\n"
            + "c\t-\tstarting\t-\t-\t-\t-\t-\t-\n",
        )

    def test_reply_is_first_nonblank_line_cut_and_dash_when_unavailable(self):
        self.launched("w1")
        self.launched("w2")
        self.launched("w3")
        self.answers[(ctl.COMMAND, "content", "w1")] = (0, "x" * 100 + "\n", "")
        self.answers[(ctl.COMMAND, "content", "w2")] = (0, " \n\n", "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        replies = [line.split("\t")[4] for line in stdout.splitlines()[1:]]
        self.assertEqual(replies, ["x" * 80, "-", "-"])

    def test_pr_from_note_url_fills_pr_columns(self):
        self.launched("w1", note=f"TASK-1 {URL}")
        self.answers[("gh", "pr", "view", URL, "--json", VIEW_FIELDS)] = (0, VIEW, "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        self.assertTrue(stdout.endswith("\t#7\tpass\tmergeable\tapproved\n"), stdout)

    def test_pr_from_role_outcome_url_when_note_has_none(self):
        record = self.launched(
            "r1", record=self.delegate_record("engineer-ab12"), alive=False, outcome="done"
        )
        self.launched("r2", record=self.delegate_record("engineer-cd34", "/v"))
        self.launched("p1", alive=False, outcome="done")
        outcome = ("agent-pm-delegate", "outcome")
        inputs = []

        def answer(text):
            inputs.append(text)
            return 0, f'{{"status":"done","url":"{URL}"}}\n', ""

        self.answers[outcome] = answer
        self.answers[("gh", "pr", "view", URL, "--json", VIEW_FIELDS)] = (0, VIEW, "")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        rows = {line.split("\t")[0]: line.split("\t")[5:] for line in stdout.splitlines()[1:]}
        self.assertEqual(rows["r1"], ["#7", "pass", "mergeable", "approved"])
        self.assertEqual(rows["r2"], ["-"] * 4)
        self.assertEqual(rows["p1"], ["-"] * 4)
        self.assertEqual(inputs, [record.to_line() + "\n"])
        self.assertEqual(self.calls.count(outcome), 1)

    def test_no_pr_url_makes_no_gh_call(self):
        for note, env, called in (
            (None, {}, False),
            ("https://evil.example/o/r/pull/1", {}, False),
            ("http://github.com/o/r/pull/1", {}, False),
            ("https://x@github.com/o/r/pull/1", {}, False),
            ("https://[x/o/r/pull/1", {}, False),
            ("https://ghe.corp/o/r/pull/1", {"GH_HOST": "ghe.corp"}, True),
        ):
            with self.subTest(note=note), mock.patch.dict(os.environ, env):
                self.launched("w1", note=note)
                self.calls = []
                result, stdout, _ = self.report()
                self.assertEqual(result, 0)
                self.assertEqual(bool(self.gh_calls()), called, stdout)
                if not called:
                    self.assertTrue(stdout.endswith("\t-\t-\t-\t-\n"), stdout)
                self.roster.remove("w1")

    def test_pipeline_outcome_status_does_not_query_an_unsupported_outcome_command(self):
        pipeline = replace(
            self.delegate_record("TASK-12"),
            kind="pipeline",
            ctl=("agent-pm-linear",),
            resume=("agent-pm-linear", "resume", "TASK-12"),
        )
        self.launched("TASK-12", record=pipeline, alive=False, outcome="done")
        result, stdout, stderr = self.report()
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("\tpipeline\tdone\t", stdout)
        self.assertNotIn(("agent-pm-linear", "outcome"), self.calls)

    def test_status_gh_and_outcome_failures_go_to_stderr_and_exit_0(self):
        self.launched("bad-gh", note=URL)
        self.launched("bad-json", note=URL.replace("7", "8"))
        self.launched("bad-outcome", record=self.delegate_record("engineer-ab12"), outcome="done")
        self.answers[("gh", "pr", "view", URL, "--json", VIEW_FIELDS)] = (1, "", "HTTP 404")
        self.answers[("gh", "pr", "view", URL.replace("7", "8"), "--json", VIEW_FIELDS)] = (
            0,
            "not json",
            "",
        )
        self.answers[("agent-pm-delegate", "outcome")] = (0, "[]", "")
        result, stdout, stderr = self.report()
        self.assertEqual(result, 0)
        rows = {line.split("\t")[0]: line.split("\t")[5:] for line in stdout.splitlines()[1:]}
        self.assertEqual(rows["bad-gh"], ["#7", "?", "?", "?"])
        self.assertEqual(rows["bad-json"], ["#8", "?", "?", "?"])
        self.assertEqual(rows["bad-outcome"], ["-"] * 4)
        self.assertIn("crew: bad-gh: ", stderr)
        self.assertIn("HTTP 404", stderr)
        self.assertIn("crew: bad-json: ", stderr)
        self.assertNotIn("bad-outcome", stderr)
        self.answers[("agent-pm-delegate", "outcome")] = (1, "", "no run")
        self.answers[("agent-pm-delegate", "status")] = self.status_answer
        self.answers[(ctl.COMMAND, "status")] = (2, "", "ctl broke")
        result, stdout, stderr = self.report()
        self.assertEqual(result, 0)
        states = {line.split("\t")[0]: line.split("\t")[2] for line in stdout.splitlines()[1:]}
        self.assertEqual(
            states, {"bad-gh": "unknown", "bad-json": "unknown", "bad-outcome": "working"}
        )
        self.assertIn("crew: bad-gh: ", stderr)
        self.assertIn("ctl broke", stderr)
        self.assertIn("crew: bad-outcome: ", stderr)
        self.assertIn("no run", stderr)
        self.answers[("agent-pm-delegate", "outcome")] = (0, "not json", "")
        result, _, stderr = self.report()
        self.assertEqual(result, 0)
        self.assertIn("crew: bad-outcome: Expecting value", stderr)

    def test_empty_roster_prints_header_only(self):
        self.assertEqual(self.report(), (0, HEADER, ""))

    def test_tabs_and_control_characters_are_flattened(self):
        self.launched("w1")
        self.answers[(ctl.COMMAND, "content", "w1")] = (0, "a\tb\x1b[31mc\n", "")
        _, stdout, _ = self.report()
        self.assertEqual(stdout.splitlines()[1].split("\t")[4], "a b[31mc")

    def test_report_leaves_roster_unchanged(self):
        self.launched("w1", alive=False)
        self.launched(
            "r1", record=self.delegate_record("engineer-ab12"), alive=False, outcome="needs_input"
        )
        self.answers[("agent-pm-delegate", "outcome")] = (0, "null\n", "")
        with self.roster.hold("another commander"):
            before = Path(self.roster.path).read_bytes()
            result, stdout, stderr = self.report()
            after = Path(self.roster.path).read_bytes()
        self.assertEqual((result, stderr), (0, ""))
        self.assertIn("\tgone\t", stdout)
        self.assertIn("\twaiting\t", stdout)
        self.assertEqual(after, before)


class View(CliCase):
    def setUp(self):
        super().setUp()
        for name in ("a", "b"):
            record = self.fake_record(name)
            self.roster.begin(Spec(name, ("start",)), sid=record.sid)
            self.roster.commit(name, record)

    def view(self, *argv, answers):
        def run(_argv, **kwargs):
            name = Record.parse(kwargs["input"]).name
            return SimpleNamespace(returncode=answers[name][0], stdout=answers[name][1], stderr="")

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke(*argv, "--manager", "mgr")

    def test_show_prints_each_layers_attach_command_and_exits_0(self):
        answers = {"a": (0, "attach a\n"), "b": (0, "")}
        self.assertEqual(self.view("show", "a", "b", answers=answers), (0, "attach a\n", ""))

    def test_a_failed_entry_is_named_on_stderr_and_exits_1(self):
        answers = {"a": (1, ""), "b": (0, "")}
        result, stdout, stderr = self.view("hide", "a", "b", answers=answers)
        self.assertEqual((result, stdout), (1, ""))
        self.assertEqual(stderr, f"crew: a: {ctl.COMMAND} hide: exit 1\n")


class InsideAnAgentSession(CliCase):
    def test_run_hold_and_resume_exit_2_and_write_nothing(self):
        for argv in (
            ("run", "--manager", "mgr", "w1", "--", "true"),
            ("hold", "--manager", "mgr", "--", "true"),
            ("resume", "--manager", "mgr", "w1"),
            ("worker", "--manager", "mgr", "w1", "--client", "claude"),
            ("issue", "--manager", "mgr", "TASK-12"),
            ("role", "--manager", "mgr", "pm", "--input", "x"),
        ):
            with (
                self.subTest(argv=argv),
                mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}),
            ):
                result, stdout, stderr = self.invoke(*argv)
                self.assertEqual((result, stdout), (2, ""))
                self.assertTrue(stderr.startswith("crew: refused inside an agent session"), stderr)
                self.assertFalse(os.path.exists(self.directory))

    def test_help_and_control_commands_still_run(self):
        with mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}):
            with (
                self.assertRaises(SystemExit) as raised,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertNotIn("refused", self.invoke("status", "--manager", "mgr")[2])
