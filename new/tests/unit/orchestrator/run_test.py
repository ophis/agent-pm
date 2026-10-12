"""orchestrator run.py, ported from the old run_test onto a fake delegate.run driving on_event, plus FR-43's TUI close
and --tui's show on a fake Driver api, each Linear call's identity and the usage-limit path (FR-59)."""

import io
import json
import os
import signal
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime
from unittest import mock

from agent_pm import delegate
from agent_pm.delegate import Begun, Finished, Limit, Outcome, Progress, Result
from agent_pm.driver import api
from agent_pm_orchestrator import config, linear, run, sessions, writeback

from new.tests.unit.orchestrator.board_ids import STATES
from new.tests.unit.orchestrator.fake_api import FakeApi
from new.tests.unit.orchestrator.run_fixtures import (
    ENGINEER,
    HARNESS,
    ID,
    KEY,
    PROJECT,
    RESEARCHER,
    SESSION,
    SID,
    UUID,
    Base,
    Gql,
)

RUNTIME = "claude"
PR = "https://github.com/Ophis/Agent-PM/pull/7"
TITLE = "TASK-7: Session registry"
DONE = Outcome("done", TITLE, "Opened the PR.", url=PR)
DONE_RESULT = Result(0, DONE)
NOW = "2026-10-04T10:00:00+08:00"
CMD = f"resume {SID}"
TUI = f"engineer-{ID}-{SID[:8]}"
T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()
NO_OUTCOME = Result(0, None, "the agent run returned no outcome")
RUNNING = f"running · #1 · {NOW}"
REPAIR = ("find", HARNESS, {"c": SESSION})


def text(line):
    return f"Run {SID} · {line}\n\n```\n{CMD}\n```"


def ended(status, rc, seq=2):
    return f"{status} · #{seq} · {NOW} → {NOW} · exit {rc}"


def created(line):
    return [
        ("update", HARNESS, {"c": SESSION, "b": text(line)}),
        ("comment", HARNESS, {"c": SESSION, "i": ID, "b": text(line)}),
    ]


def updated(line):
    return [("update", HARNESS, {"c": SESSION, "b": text(line)})]


def cid(step):
    return linear.client_id(SID, step)


class FakeRun:
    """delegate.run: records its call, raises `error`, else sends Begun (unless `begun` is False), then each step (an
    event, or a callable it calls), then Finished when the result has an outcome, and returns the result."""

    def __init__(self, order, result=NO_OUTCOME, steps=(), error=None, begun=True):
        self.order, self.result, self.steps, self.error = order, result, steps, error
        self.begun, self.calls, self.dirs = begun, [], []

    def __call__(self, run, task, input, workdir, **kw):
        self.order.append("run")
        self.calls.append(((run, task, input, workdir), kw))
        self.dirs.append(os.path.isdir(workdir))
        if self.error:
            raise self.error
        if self.begun:
            kw["on_event"](Begun(kw["sid"]))
        for step in self.steps:
            if callable(step):
                step()
            else:
                kw["on_event"](step)
        if self.result.outcome is not None:
            kw["on_event"](Finished(self.result))
        return self.result


class Case(Base):
    def setUp(self):
        super().setUp()
        self.handlers, self.order = {}, []
        self.delegate = FakeRun(self.order)
        self.api = FakeApi(self.order, lambda: self.gql)
        for p in (
            mock.patch.object(signal, "signal", lambda s, h: self.handlers.__setitem__(s, h)),
            mock.patch.object(sessions, "now", return_value=NOW),
            self.api.patch(),
        ):
            p.start()
            self.addCleanup(p.stop)

    def argv(
        self,
        *extra,
        assignee=ENGINEER,
        task=None,
        mode="new",
        target="Ophis/Agent-PM",
        uuid=UUID,
        runtime=RUNTIME,
    ):
        return [
            "--uuid",
            uuid,
            *(["--target", target] if target else []),
            f"--issue={ID}",
            f"--project={PROJECT}",
            f"--assignee={assignee}",
            f"--sid={SID}",
            f"--runtime={runtime}",
            *([f"--task={task}"] if task else []),
            f"--mode={mode}",
            *extra,
            "--input=Do it.\n",
        ]

    def main(self, *extra, **kw):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run.main(self.argv(*extra, **kw), gql=self.gql, run=self.delegate)
        self.err = err.getvalue()
        return rc

    def done(self, *steps, result=DONE_RESULT):
        self.delegate.result, self.delegate.steps = result, steps

    def call(self):
        ((args, kw),) = self.delegate.calls
        return args, {k: v for k, v in kw.items() if k != "on_event"}

    def nothing_ran(self):
        self.assertEqual((self.gql.calls, self.delegate.calls), ([], []))
        self.assertFalse(os.path.exists(self.logs))


class Inner(Case):
    def test_build_run(self):
        pick = "light-build: a template wording tweak, like TASK-226."
        self.done(Progress("start", pick))
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said(),
            [
                f"run launch TASK-7 mode=new sid={SID}",
                f"writeback step TASK-7 sid={SID} step=start",
                f"writeback step TASK-7 sid={SID} step=comment",
                f"writeback step TASK-7 sid={SID} step=attach:{PR}",
                f"writeback step TASK-7 sid={SID} step=move:in_review",
                f"run end TASK-7 sid={SID} exit=0",
            ],
        )
        self.assertEqual(os.listdir(self.logs), ["orchestrator.jsonl"])
        self.assertEqual(
            self.gql.calls,
            [
                *created(RUNNING),
                ("comment", KEY, {"c": cid("start"), "i": UUID, "b": f"Build started: {pick}"}),
                ("read", KEY, {"i": UUID}),
                ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                (
                    "comment",
                    KEY,
                    {"c": cid("comment"), "i": UUID, "b": "Build ready: Opened the PR."},
                ),
                ("attach", KEY, {"i": UUID, "u": PR, "t": TITLE}),
                ("reread", KEY, {"i": UUID}),
                ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
                *updated(ended("done", 0)),
                REPAIR,
            ],
        )
        args, kw = self.call()
        self.assertEqual(args, (config.load().roles["engineer"].run, None, "Do it.\n", self.rd))
        self.assertEqual(
            kw,
            dict(
                runtime=RUNTIME,
                mode="headless",
                sid=SID,
                resume=None,
                name=None,
                events=None,
                out=os.path.join(self.rd, "out.md"),
                cwd=self.rd,
            ),
        )
        self.assertEqual(self.delegate.dirs, [True])
        self.assertEqual(self.api.info, [(RUNTIME, SID, self.rd)] * 2)
        self.assertEqual(os.environ["PATH"], config.path())
        self.assertEqual([o for o in self.order if o[0] == "show"], [])

    def test_no_outcome_leaves_the_issue(self):
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[-2:],
            [
                f"run end TASK-7 sid={SID} exit=0",
                "run no-outcome TASK-7 error=the agent run returned no outcome",
            ],
        )
        self.assertEqual(self.gql.calls, [*created(RUNNING), *updated(ended("done", 0)), REPAIR])

    def test_nonzero_exit_keeps_a_valid_reported_outcome(self):
        self.done(result=Result(1, DONE, "the client exited 1"))
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said(),
            [
                f"run launch TASK-7 mode=new sid={SID}",
                f"writeback step TASK-7 sid={SID} step=comment",
                f"writeback step TASK-7 sid={SID} step=attach:{PR}",
                f"writeback step TASK-7 sid={SID} step=move:in_review",
                f"run end TASK-7 sid={SID} exit=1",
            ],
        )
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("done", 1)), REPAIR])

    def test_sighup_stops_the_run(self):
        self.delegate.steps = [lambda: self.handlers[signal.SIGHUP](signal.SIGHUP, None)]
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said(),
            [
                f"run launch TASK-7 mode=new sid={SID}",
                f"run end TASK-7 sid={SID} exit=129",
                "run no-outcome TASK-7 error=no result",
            ],
        )
        self.assertEqual(
            self.gql.calls, [*created(RUNNING), *updated(ended("interrupted", 129)), REPAIR]
        )
        self.assertEqual(set(self.handlers), {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.assertRaises(SystemExit) as cm:
                self.handlers[sig](sig, None)
            self.assertEqual(cm.exception.code, code)

    def test_signals_are_unblocked_once_their_handlers_are_in(self):
        seen = []

        def sigmask(how, sigs):
            seen.append((how, sigs, set(self.handlers)))

        with mock.patch.object(signal, "pthread_sigmask", sigmask):
            self.assertEqual(self.main(), 0)
        self.assertEqual(
            seen,
            [(signal.SIG_UNBLOCK, api.SIGNALS, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})],
        )

    def test_a_task_reaches_the_delegate(self):
        self.assertEqual(self.main(task="light-build"), 0)
        self.assertEqual(self.call()[0][1], "light-build")

    def test_a_researcher_run_has_the_brake(self):
        self.assertEqual(self.main(assignee=RESEARCHER, target=None), 0)
        (researcher, *_), _ = self.call()
        self.assertEqual(researcher, config.load().roles["researcher"].run)
        self.assertEqual(
            researcher.gate, "python3 -P -m agent_pm_orchestrator.router --brake researcher"
        )
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=new sid={SID}")

    def test_resume(self):
        self.assertEqual(self.main(mode="resume"), 0)
        _, kw = self.call()
        self.assertEqual((kw["sid"], kw["resume"]), (SID, SID))
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=resume sid={SID}")

    def test_a_failed_session_comment_is_a_registry_error(self):
        self.gql = Gql(lambda name, v: RuntimeError("down") if name == "update" else None)
        self.assertEqual(self.main(), 0)
        error = f"run registry-error TASK-7 sid={SID} error=RuntimeError: down"
        self.assertEqual(
            self.said(),
            [
                f"run launch TASK-7 mode=new sid={SID}",
                error,
                f"run end TASK-7 sid={SID} exit=0",
                "run no-outcome TASK-7 error=the agent run returned no outcome",
                error,
                error,
            ],
        )

    def test_run_error(self):
        cases = [
            (
                dict(result=Result(1, None, "headless: spawn failed"), begun=False),
                [
                    "run end TASK-7 sid={} exit=1",
                    "run no-outcome TASK-7 error=headless: spawn failed",
                ],
            ),
            (
                dict(error=delegate.ConfigError("runtime 'x' is not configured")),
                [
                    "run run-error TASK-7 error=ConfigError: runtime 'x' is not configured",
                    "run end TASK-7 sid={} exit=1",
                    "run no-outcome TASK-7 error=no result",
                ],
            ),
        ]
        for kw, lines in cases:
            with self.subTest(kw=kw):
                self.gql, self.delegate = Gql(), FakeRun(self.order, **kw)
                n = len(self.said())
                self.assertEqual(self.main(), 0)
                self.assertEqual(self.said()[n + 1 :], [x.format(SID) for x in lines])
                self.assertEqual(self.gql.calls, [*created(ended("interrupted", 1, seq=1)), REPAIR])

    def test_a_config_role_or_task_error_logs_config_error_and_end(self):
        def bad_config():
            with open(self.local) as f:
                good = f.read()
            self.write(self.local, good.replace(f'harness_key = "{HARNESS}"\n', ""))

        cases = [
            (None, dict(assignee="x@y.com"), "'x@y.com' is not a role account"),
            (
                None,
                dict(assignee=RESEARCHER, task="build", target=None),
                "task 'build' is not one of researcher's tasks (deep-research, light-research)",
            ),
            (
                bad_config,
                {},
                "orchestrator/config.toml: missing harness_key: set them in "
                "~/.agent-pm/orchestrator-new.local.toml (orchestrator/config.toml's `# local:` lines)",
            ),
        ]  # last: it changes the config
        for setup, kw, reason in cases:
            with self.subTest(reason=reason):
                if setup:
                    setup()
                n = len(self.said())
                self.assertEqual(self.main(**kw), 1)
                self.assertEqual(
                    self.said()[n:],
                    [
                        f"run config-error TASK-7 reason={reason}",
                        f"run end TASK-7 sid={SID} exit=1",
                    ],
                )
                self.assertEqual(self.err, "")
                self.assertFalse(os.path.exists(self.runs))
        self.assertEqual((self.gql.calls, self.delegate.calls, self.order), ([], [], []))

    def test_a_bad_work_dir_is_said_on_stderr_and_logs_nothing(self):
        with open(self.local) as f:
            good = f.read()
        self.write(self.local, good.replace(f'work_dir = "{self.work}"', 'work_dir = "w"'))
        self.assertEqual(self.main(), 1)
        self.assertEqual(
            self.err,
            "orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n",
        )
        self.nothing_ran()

    def test_bad_uuid_or_target(self):
        for kw in (dict(uuid="nope"), dict(target="a/b/c")):
            with self.subTest(kw=kw):
                self.assertEqual(self.main(**kw), 2)
                self.assertTrue(self.err.startswith("run.py: bad issue uuid or target: "), self.err)
        self.nothing_ran()


class Tui(Case):
    MINE = [f"engineer-{ID}-aaaaaaaa", f"engineer-{ID}-bbbbbbbb"]
    OTHER = [
        f"engineer-{ID}-aaaaaaaa-drive",
        f"pm-{ID}-cccccccc",
        "engineer-TASK-70-dddddddd",
        f"agent-pm-engineer-{ID}",
    ]

    def test_old_tuis_of_this_role_and_issue_close_before_the_run(self):
        for extra in ((), ("--tui",)):
            with self.subTest(extra=extra):
                self.gql, self.api.live = Gql(), [*self.MINE, *self.OTHER]
                self.order.clear()
                n = len(self.said())
                self.assertEqual(self.main(*extra), 0)
                self.assertEqual(
                    self.order[:4],
                    [
                        ("sessions", f"engineer-{ID}-"),
                        *(("close", x) for x in self.MINE),
                        "run",
                    ],
                )
                self.assertEqual(
                    self.said()[n : n + 3],
                    [
                        f"run launch TASK-7 mode=new sid={SID}",
                        *(f"run tui-closed TASK-7 session={x}" for x in self.MINE),
                    ],
                )

    def test_an_unknown_listing_is_logged_and_the_run_goes_on(self):
        self.api.live = None
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.said()[1], "run tui-error TASK-7 msg=tmux sessions unknown")
        self.assertEqual(self.order[:2], [("sessions", f"engineer-{ID}-"), "run"])

    def test_a_close_error_is_logged_and_the_others_still_close(self):
        self.api.live = self.MINE
        self.api.errors = {self.MINE[0]: api.DriverError("kill failed")}
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[1:3],
            [
                f"run tui-error TASK-7 session={self.MINE[0]} msg=DriverError: kill failed",
                f"run tui-closed TASK-7 session={self.MINE[1]}",
            ],
        )

    def test_tui_runs_in_the_delegates_tui_mode_and_shows_its_session_once_begun(self):
        self.api.shown = "attach-command"
        self.done()
        extra = ("--tui", "--split=below", "--split-from=dev", "--opener=w0t0p0:ABC")
        self.assertEqual(self.main(*extra, "--events=/x/ev.log"), 0)
        _, kw = self.call()
        self.assertEqual(
            (kw["mode"], kw["name"], kw["events"], kw["sid"]), ("tui", TUI, "/x/ev.log", SID)
        )
        self.assertEqual(
            [o for o in self.order if o[0] == "show"],
            [("show", TUI, "below", "dev", "w0t0p0:ABC", 2)],
        )
        self.assertEqual(self.err, "attach-command\n")
        self.assertIn(f"writeback step TASK-7 sid={SID} step=move:in_review", self.said())

    def test_a_show_error_is_logged_never_raised(self):
        self.api.shown = api.DriverError(f"no tmux session {TUI}")
        self.done()
        self.assertEqual(self.main("--tui"), 0)
        said = self.said()
        self.assertIn(
            f"run tui-error TASK-7 session={TUI} msg=DriverError: no tmux session {TUI}", said
        )
        self.assertEqual(
            said[-2:],
            [
                f"writeback step TASK-7 sid={SID} step=move:in_review",
                f"run end TASK-7 sid={SID} exit=0",
            ],
        )
        self.assertEqual(self.err, "")


class Identity(Case):
    def test_session_comments_go_as_the_harness_and_write_back_as_the_role(self):
        self.done(Progress("start", "x"))
        self.assertEqual(self.main(mode="resume"), 0)
        self.assertEqual(config.load().harness_key, HARNESS)
        by = {}
        for (_, service, v), timeout in zip(self.gql.calls, self.gql.timeouts, strict=True):
            by.setdefault(v.get("c") == SESSION, set()).add((service, timeout))
        self.assertEqual(by, {True: {(HARNESS, sessions.LIMIT)}, False: {(KEY, 30)}})
        for step in ("start", "comment"):
            self.assertIn(("find", KEY, {"c": cid(step)}), self.gql.calls)


class Limits(Case):
    def test_a_usage_limit_writes_back_and_notes_it_in_the_runs_log(self):
        for at, said in (
            (T, f"resets at {writeback.local_time(T)}."),
            (None, "reset time unknown."),
        ):
            with self.subTest(at=at):
                self.gql = Gql()
                ledger = os.path.join(self.rd, writeback.LEDGER)
                if os.path.exists(ledger):
                    os.remove(ledger)
                self.done(result=Result(-9, None, "usage limit", Limit(at)))
                n = len(self.said())
                self.assertEqual(self.main(), 0)
                self.assertEqual(
                    self.said()[n:],
                    [
                        f"run launch TASK-7 mode=new sid={SID}",
                        f"run end TASK-7 sid={SID} exit=-9",
                        f"writeback step TASK-7 sid={SID} step=limit",
                    ],
                )
                self.assertEqual(
                    self.gql.calls,
                    [
                        *created(RUNNING),
                        ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                        (
                            "comment",
                            KEY,
                            {"c": cid("limit"), "i": UUID, "b": f"Usage limit, {said}"},
                        ),
                        ("reread", KEY, {"i": UUID}),
                        ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
                        *updated(ended("limit", -9)),
                        REPAIR,
                    ],
                )
                with open(self.runs) as f:
                    last = json.loads(f.read().splitlines()[-1])
                self.assertEqual(
                    {k: v for k, v in last.items() if k != "ts"},
                    {
                        "kind": "limit",
                        "issue": ID,
                        "sid": SID,
                        "runtime": RUNTIME,
                        "resets_at": writeback.local_time(at),
                    },
                )

    def test_a_failed_runs_note_is_a_run_error_and_the_session_comment_still_ends(self):
        os.makedirs(self.runs)
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[1:],
            [
                f"run end TASK-7 sid={SID} exit=-9",
                f"writeback step TASK-7 sid={SID} step=limit",
                f"run run-error TASK-7 error=IsADirectoryError: [Errno 21] Is a directory: '{self.runs}'",
            ],
        )
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])

    def test_any_runs_note_exception_is_a_run_error_and_the_session_comment_still_ends(self):
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        with mock.patch.object(run.runs, "note", side_effect=ValueError("bad line")):
            self.assertEqual(self.main(), 0)
        self.assertEqual(self.said()[-1], "run run-error TASK-7 error=ValueError: bad line")
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])


class Cli(Case):
    def test_tui_is_off_by_default_and_main_hands_inner_the_config_and_its_fakes(self):
        seen = []
        with mock.patch.object(
            run, "inner", side_effect=lambda a, cfg, **kw: seen.append((a, cfg, kw)) or 0
        ):
            self.assertEqual(self.main(), 0)
        ((a, cfg, kw),) = seen
        self.assertEqual((a.tui, a.runtime, cfg.harness_key), (False, RUNTIME, HARNESS))
        self.assertEqual(kw, {"gql": self.gql, "run": self.delegate})
        self.assertEqual(linear.LOGS_DIR, self.logs)
        self.assertEqual(self.err, "")

    def test_tui_options_need_tui(self):
        for extra in (
            ["--split", "right"],
            ["--split-from", "dev"],
            ["--events", os.path.join(self.work, "events.log")],
            ["--opener", "mine"],
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.main(*extra), 2)
                self.assertEqual(
                    self.err, "run.py: --split, --split-from, --opener and --events need --tui\n"
                )
        self.nothing_ran()

    def test_argparse_errors(self):
        argv = self.argv()
        no_runtime = [x for x in argv if not x.startswith("--runtime")]
        for bad in (
            argv[:-1],
            argv[2:],
            no_runtime,
            argv + ["--k", "1"],
            argv + ["--runner", "tui"],
            argv + ["--tui=1"],
            argv + ["--mode", "later"],
        ):
            with (
                self.subTest(argv=bad),
                self.assertRaises(SystemExit) as cm,
                redirect_stderr(io.StringIO()),
            ):
                run.main(bad, gql=self.gql, run=self.delegate)
            self.assertEqual(cm.exception.code, 2)
        self.nothing_ran()

    def test_bad_issue_or_session_id(self):
        argv = self.argv()
        for flag, value in (("--issue", "task-7"), ("--sid", "not-a-sid"), ("--sid", "z" * 36)):
            with self.subTest(flag=flag, value=value):
                bad = [f"{flag}={value}" if x.startswith(flag + "=") else x for x in argv]
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(run.main(bad, gql=self.gql, run=self.delegate), 2)
                issue, sid = (value, SID) if flag == "--issue" else (ID, value)
                self.assertEqual(
                    err.getvalue(), f"run.py: bad issue or session id: {issue} {sid}\n"
                )
        self.nothing_ran()

    def test_bad_runtime(self):
        for runtime in ("", "cl\x07aude"):
            with self.subTest(runtime=runtime):
                self.assertEqual(self.main(runtime=runtime), 2)
                self.assertEqual(self.err, f"run.py: bad runtime: {runtime!r}\n")
        self.nothing_ran()


if __name__ == "__main__":
    unittest.main()
