"""Orchestrator run behavior at its dependency boundaries."""

import io
import json
import os
import signal
from contextlib import redirect_stderr
from datetime import UTC, datetime
from unittest import mock

from agent_pm import delegate
from agent_pm.delegate import Begun, Finished, Limit, Outcome, Progress, Result, Spec
from agent_pm.linear_orchestrator.engine import (
    config,
    ops,
    router,
    run,
    run_comments,
    runs,
    writeback,
)
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.types import Spec as RunSpec
from agent_pm.linear_orchestrator.engine.types import Stopped
from agent_pm.session import SIGNALS, DriverError
from agent_pm.session import Spec as SessionSpec
from orchestrator_tests.config_fixtures import HARNESS_DELEGATE
from orchestrator_tests.unit.board_ids import STATES
from orchestrator_tests.unit.config_fixtures import GATE
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.run_fixtures import (
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
    logged,
)

from delegate.tests.fakes.delegate import FakeDelegate

CLIENT = "claude"

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
    return ops.client_id(SID, step)


class FakeRun:
    """The Delegate run.py builds (its delegate_cls) and runs: records each build's (role, spec,
    display, overlays) and each run's (the role's run its overlays resolve, spec); a run raises
    `error`, else sends Begun (unless `begun` is False), then each step (an event, or a callable it
    calls), then Finished when the result has an outcome, and returns the result. Its session_info
    is `api`'s inspect of the run's session.
    """

    def __init__(self, order, api, result=NO_OUTCOME, steps=(), error=None, *, begun=True):
        self.order, self.api, self.result, self.steps, self.error = order, api, result, steps, error
        self.begun, self.built, self.calls, self.dirs = begun, [], [], []

    def __call__(self, role, spec, *, display=None, callback=None, overlays=()):
        self.built.append((role, spec, display, tuple(overlays)))
        return self

    def session_info(self):
        spec = self.built[-1][1]
        return self.api.inspect(SessionSpec(workdir=spec.workdir, client=spec.client, sid=spec.sid))

    def run(self, on_event):
        role, spec, _, overlays = self.built[-1]
        self.order.append("run")
        self.calls.append((HARNESS_DELEGATE.resolve(role, overlays=overlays), spec))
        self.dirs.append(os.path.isdir(spec.workdir))
        if self.error:
            raise self.error
        if self.begun:
            on_event(Begun(spec.sid))
        for step in self.steps:
            if callable(step):
                step()
            else:
                on_event(step)
        if self.result.outcome is not None:
            on_event(Finished(self.result))
        return self.result


class CleanupDelegate(FakeDelegate):
    owners = {}
    ROLES = {
        "engineer": {"build": "", "light-build": ""},
        "researcher": {"deep-research": "", "light-research": ""},
        "pm": {"product-design": ""},
    }

    @classmethod
    def resolve(cls, role, task=None, *, overlays=()):
        return HARNESS_DELEGATE.resolve(role, task, overlays=overlays)

    def close(self):
        owner = self.owners[self.workdir]
        failed = None
        for name in owner.cleanup_sessions:
            try:
                owner.api.close(name)
            except DriverError as error:
                failed = error
        if failed:
            raise failed
        super().close()


class Case(Base):
    def setUp(self):
        super().setUp()
        self.handlers, self.order = {}, []
        self.api = FakeApi(self.order)
        self.delegate = FakeRun(self.order, self.api)
        self.api.records[SID] = {"cwd": self.rd}
        for p in (
            mock.patch.object(signal, "signal", lambda s, h: self.handlers.__setitem__(s, h)),
            mock.patch.object(signal, "pthread_sigmask"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def record_cleanup(self, sessions, *, issue=ID):
        self.cleanup_sessions = sessions
        workdir = os.path.join(self.work, "work", issue)
        CleanupDelegate.owners[workdir] = self
        self.addCleanup(CleanupDelegate.owners.pop, workdir, None)
        CleanupDelegate("engineer", Spec("", workdir, sid=SID)).detach()
        runs.note(self.runs, "start", issue=issue, sid=SID, role="engineer", client=CLIENT)
        with open(self.local) as stream:
            text = stream.read()
        self.write(
            self.local,
            text.replace(
                "agent_pm.delegate.engine.harness_delegate:HarnessDelegate",
                "orchestrator_tests.unit.engine.run_test:CleanupDelegate",
            ),
        )

    def argv(
        self,
        *extra,
        assignee=ENGINEER,
        task=None,
        mode="new",
        target="Ophis/Agent-PM",
        uuid=UUID,
        client=CLIENT,
    ):
        return [
            "--uuid",
            uuid,
            *(["--target", target] if target else []),
            f"--issue={ID}",
            f"--project={PROJECT}",
            f"--assignee={assignee}",
            f"--sid={SID}",
            f"--client={client}",
            *([f"--task={task}"] if task else []),
            f"--mode={mode}",
            *extra,
            "--input=Do it.\n",
        ]

    def main(
        self,
        *extra,
        assignee=ENGINEER,
        task=None,
        mode="new",
        target="Ophis/Agent-PM",
        uuid=UUID,
        client=CLIENT,
    ):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run.main(
                self.argv(
                    *extra,
                    assignee=assignee,
                    task=task,
                    mode=mode,
                    target=target,
                    uuid=uuid,
                    client=client,
                ),
                gql=self.gql,
                delegate_cls=self.delegate,
                now=lambda: NOW,
            )
        self.err = err.getvalue()
        return rc

    def done(self, *steps, result=DONE_RESULT):
        self.delegate.result, self.delegate.steps = result, steps

    def call(self):
        (found,) = self.delegate.calls
        return found

    def nothing_ran(self):
        self.assertEqual((self.gql.calls, self.delegate.calls), ([], []))
        self.assertFalse(os.path.exists(self.logs))


class RunExecution(Case):
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
                f"writeback step TASK-7 sid={SID} step=outcome",
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
        self.assertEqual(
            self.call(),
            (
                config.load().roles["engineer"].run,
                Spec(
                    "Do it.\n",
                    self.rd,
                    client=CLIENT,
                    sid=SID,
                    name=TUI,
                    out=os.path.join(self.rd, "out.md"),
                    cwd=self.rd,
                ),
            ),
        )
        self.assertEqual(self.delegate.dirs, [True])
        self.assertEqual(self.api.info, [(CLIENT, SID, self.rd)] * 2)
        self.assertEqual(os.environ["PATH"], config.path())

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
                f"writeback step TASK-7 sid={SID} step=outcome",
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
            with self.assertRaises(Stopped) as cm:
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
            [(signal.SIG_UNBLOCK, SIGNALS, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})],
        )

    def test_a_task_reaches_the_delegate(self):
        self.assertEqual(self.main(task="light-build"), 0)
        self.assertEqual(self.call()[1].task, "light-build")

    def test_a_researcher_run_has_the_brake(self):
        self.assertEqual(self.main(assignee=RESEARCHER, target=None), 0)
        researcher, _ = self.call()
        self.assertEqual(researcher, config.load().roles["researcher"].run)
        self.assertEqual(researcher.gate, GATE)
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=new sid={SID}")

    def test_resume(self):
        self.assertEqual(self.main(mode="resume"), 0)
        _, spec = self.call()
        self.assertEqual((spec.sid, spec.resume), (SID, True))
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=resume sid={SID}")

    def test_a_failed_session_comment_is_a_registry_error(self):
        self.gql = Gql(lambda name, _v: RuntimeError("down") if name == "update" else None)
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

    def test_a_run_error_records_its_original_cause_once(self):
        error = RuntimeError("failed run")
        error.__cause__ = ValueError("original cause")
        self.gql, self.delegate = Gql(), FakeRun(self.order, self.api, error=error)
        self.assertEqual(self.main(), 0)
        records = [e for e in logged(self.logs) if e["kind"] == "run-error"]
        self.assertEqual(len(records), 1)
        self.assertIn("ValueError: original cause", records[0]["traceback"])
        self.assertIn("direct cause", records[0]["traceback"])
        self.assertIn("RuntimeError: failed run", records[0]["traceback"])
        self.assertEqual(self.gql.calls, [*created(ended("interrupted", 1, seq=1)), REPAIR])

    def test_an_unrecorded_launch_failure_has_no_resume_command(self):
        self.api.records.clear()
        self.delegate = FakeRun(self.order, self.api, error=delegate.ConfigError("launch failed"))
        self.assertEqual(self.main(), 0)
        body = f"Run {SID} · {ended('interrupted', 1, seq=1)}"
        self.assertEqual(
            self.gql.calls,
            [
                ("update", HARNESS, {"c": SESSION, "b": body}),
                ("comment", HARNESS, {"c": SESSION, "i": ID, "b": body}),
                REPAIR,
            ],
        )

    def test_system_exit_preserves_its_integer_code_in_the_log_and_session_comment(self):
        for code, expected in ((7, 7), (0, 0), ("stopped", 1), (None, 1)):
            with self.subTest(code=code):
                self.gql, self.delegate = (
                    Gql(),
                    FakeRun(self.order, self.api, error=SystemExit(code)),
                )
                n = len(logged(self.logs))
                self.assertEqual(self.main(), 0)
                records = logged(self.logs)[n:]
                end = next(record for record in records if record["kind"] == "end")
                self.assertEqual(end["exit"], expected)
                errors = [record for record in records if record["kind"] == "run-error"]
                self.assertEqual(len(errors), 1)
                self.assertIn("SystemExit", errors[0]["traceback"])
                status = "done" if expected == 0 else "interrupted"
                self.assertEqual(self.gql.calls, [*created(ended(status, expected, seq=1)), REPAIR])

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
                dict(error=delegate.ConfigError("client 'x' is not configured")),
                [
                    "run run-error TASK-7 error=ConfigError: client 'x' is not configured",
                    "run end TASK-7 sid={} exit=1",
                    "run no-outcome TASK-7 error=no result",
                ],
            ),
            (
                dict(error=AttributeError("run defect")),
                [
                    "run run-error TASK-7 error=AttributeError: run defect",
                    "run end TASK-7 sid={} exit=1",
                    "run no-outcome TASK-7 error=no result",
                ],
            ),
        ]
        for kw, lines in cases:
            with self.subTest(kw=kw):
                self.gql, self.delegate = Gql(), FakeRun(self.order, self.api, **kw)
                n = len(self.said())
                self.assertEqual(self.main(), 0)
                self.assertEqual(self.said()[n + 1 :], [x.format(SID) for x in lines])
                if error := kw.get("error"):
                    record = next(e for e in logged(self.logs)[n:] if e["kind"] == "run-error")
                    self.assertIn("in inner", record["traceback"])
                    self.assertIn(f"{type(error).__name__}: {error}", record["traceback"])
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
                (
                    "linear-orchestrator/config.toml: required, unset: harness_key: set them in "
                    "~/.agent-pm/config.local.toml (linear-orchestrator/config.toml's `# local:` lines)"
                ),
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
            (
                "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n"
            ),
        )
        self.nothing_ran()

    def test_bad_uuid_or_target(self):
        for kw in (dict(uuid="nope"), dict(target="a/b/c")):
            with self.subTest(kw=kw):
                self.assertEqual(self.main(**kw), 2)
                self.assertTrue(self.err.startswith("run.py: bad issue uuid or target: "), self.err)
        self.nothing_ran()


class RunTui(Case):
    MINE = [f"engineer-{ID}-aaaaaaaa", f"engineer-{ID}-bbbbbbbb"]

    OTHER = [
        f"engineer-{ID}-aaaaaaaa-drive",
        f"pm-{ID}-cccccccc",
        "engineer-TASK-70-dddddddd",
        f"agent-pm-engineer-{ID}",
    ]

    def prepare_cleanup(self, *extra):
        cfg = config.load()
        event_log.use_logs(cfg.logs_dir)
        return router.begin(cfg, RunSpec.from_argv(self.argv(*extra)))

    def test_selected_recorded_run_closes_all_owned_sessions_before_new_run(self):
        for extra in ((), ("--tui",)):
            with self.subTest(extra=extra):
                self.record_cleanup([*self.MINE, "owned-harness-on-another-host"])
                self.api.live = [*self.MINE, *self.OTHER]
                self.gql = Gql()
                self.order.clear()
                before = len(self.said())
                self.prepare_cleanup(*extra)
                self.assertEqual(self.main(*extra), 0, (self.said(), self.err))
                self.assertEqual(
                    self.order[:4],
                    [*(("close", x) for x in self.cleanup_sessions), "run"],
                )
                self.assertEqual(
                    self.said()[before : before + 2],
                    ["router tui-closed TASK-7", f"run launch TASK-7 mode=new sid={SID}"],
                )

    def test_unrecorded_matching_sessions_remain_untouched(self):
        self.api.live = [*self.MINE, *self.OTHER]
        self.prepare_cleanup()
        self.assertEqual(self.main(), 0, (self.said(), self.err))
        self.assertEqual(self.order, ["run"])
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=new sid={SID}")

    def test_other_recorded_issue_remains_untouched(self):
        self.record_cleanup(["unrelated-agent"], issue="TASK-70")
        self.prepare_cleanup()
        self.assertEqual(self.main(), 0, (self.said(), self.err))
        self.assertEqual(self.order, ["run"])
        self.assertFalse(any("tui-" in line for line in self.said()))

    def test_unknown_recorded_view_logs_error_and_the_run_goes_on(self):
        self.record_cleanup(self.MINE)
        os.unlink(os.path.join(self.rd, "delegate.json"))
        self.prepare_cleanup()
        self.assertEqual(self.main(), 0, (self.said(), self.err))
        self.assertEqual(self.order, ["run"])
        self.assertEqual(
            self.said()[0],
            f"router tui-error TASK-7 msg=ConfigError: {self.rd!r}: no delegate.json",
        )

    def test_close_error_logs_and_other_owned_sessions_close_before_run(self):
        self.record_cleanup(self.MINE)
        self.api.errors = {self.MINE[0]: DriverError("kill failed")}
        self.done(Progress("start", "x"))
        self.prepare_cleanup()
        self.assertEqual(self.main(), 0, (self.said(), self.err))
        self.assertEqual(self.order[:3], [*(("close", x) for x in self.MINE), "run"])
        self.assertEqual(self.said()[0], "router tui-error TASK-7 msg=DriverError: kill failed")
        self.assertIn(f"writeback step TASK-7 sid={SID} step=move:in_review", self.said())
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("done", 0)), REPAIR])

    def test_tui_runs_in_the_delegates_tui_mode_at_its_display(self):
        self.done()
        extra = ("--tui", "--split=below", "--split-from=dev", "--opener=w0t0p0:ABC")
        self.assertEqual(self.main(*extra, "--events=/x/ev.log"), 0)
        _, spec = self.call()
        self.assertEqual(
            (spec.mode, spec.name, spec.events, spec.sid), ("tui", TUI, "/x/ev.log", SID)
        )
        self.assertEqual(self.delegate.built[0][2], "w0t0p0:ABC/below/dev")
        self.assertEqual(self.err, "")
        self.assertIn(f"writeback step TASK-7 sid={SID} step=move:in_review", self.said())


class RunIdentity(Case):
    def test_session_comments_go_as_the_harness_and_write_back_as_the_role(self):
        self.done(Progress("start", "x"))
        self.assertEqual(self.main(mode="resume"), 0)
        self.assertEqual(config.load().harness_key, HARNESS)
        by = {}
        for (_, service, v), timeout in zip(self.gql.calls, self.gql.timeouts, strict=True):
            by.setdefault(v.get("c") == SESSION, set()).add((service, timeout))
        self.assertEqual(by, {True: {(HARNESS, run_comments.LIMIT)}, False: {(KEY, 30)}})
        for step in ("start", "comment"):
            self.assertIn(("find", KEY, {"c": cid(step)}), self.gql.calls)


class RunUsageLimits(Case):
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
                        "client": CLIENT,
                        "resets_at": writeback.local_time(at),
                        "delegate_kind": "harness",
                    },
                )

    def test_a_resumes_limit_row_keeps_the_recorded_delegate_kind(self):
        runs.note(
            self.runs,
            "start",
            issue=ID,
            sid=SID,
            role="engineer",
            client=CLIENT,
            delegate_kind="saved",
        )
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        self.assertEqual(self.main(mode="resume"), 0)
        with open(self.runs) as f:
            last = json.loads(f.read().splitlines()[-1])
        self.assertEqual((last["kind"], last["delegate_kind"]), ("limit", "saved"))

    def test_a_failed_runs_note_is_a_run_error_and_the_session_comment_still_ends(self):
        self.done(lambda: os.makedirs(self.runs), result=Result(-9, None, "usage limit", Limit(T)))
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[1:],
            [
                f"run end TASK-7 sid={SID} exit=-9",
                f"writeback step TASK-7 sid={SID} step=limit",
                (
                    f"run run-error TASK-7 error=IsADirectoryError: [Errno 21] Is a directory: '{self.runs}'"
                ),
            ],
        )
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])

    def test_any_runs_note_exception_is_a_run_error_and_the_session_comment_still_ends(self):
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        with mock.patch.object(os, "fsync", side_effect=OSError("disk")):
            self.assertEqual(self.main(), 0)
        self.assertEqual(self.said()[-1], "run run-error TASK-7 error=OSError: disk")
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])


class RunArguments(Case):
    def test_a_bad_session_config_ends_the_run_as_a_logged_run_error(self):
        with open(self.local, "a") as f:
            f.write('\n[session.hosts]\ndefault = "absent"\n')
        with redirect_stderr(io.StringIO()):
            rc = run.main(self.argv(), gql=self.gql, now=lambda: NOW)
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.said(),
            [
                f"run launch TASK-7 mode=new sid={SID}",
                "run run-error TASK-7 error=ConfigError: [hosts] default: want one of tmux",
                f"run end TASK-7 sid={SID} exit=1",
                "run no-outcome TASK-7 error=no result",
            ],
        )
        body = f"Run {SID} · {ended('interrupted', 1, seq=1)}"
        self.assertEqual(
            (self.delegate.calls, self.gql.calls),
            (
                [],
                [
                    ("update", HARNESS, {"c": SESSION, "b": body}),
                    ("comment", HARNESS, {"c": SESSION, "i": ID, "b": body}),
                    REPAIR,
                ],
            ),
        )

    def test_launch_arguments_preserve_input_and_display_values(self):
        values = self.argv() + [
            "--task=light-build",
            "--target=ophis/agent-pm",
            "--tui",
            "--split=below",
            "--split-from=dev",
            "--opener=w0t0p0:ABC",
            "--events=/tmp/events.log",
        ]
        parsed = RunSpec.from_argv(values)
        self.assertEqual(
            (
                parsed.task,
                parsed.target,
                parsed.tui,
                parsed.split,
                parsed.split_from,
                parsed.opener,
                parsed.events,
            ),
            (
                "light-build",
                "ophis/agent-pm",
                True,
                "below",
                "dev",
                "w0t0p0:ABC",
                "/tmp/events.log",
            ),
        )
        self.assertEqual((parsed.issue, parsed.uuid, parsed.sid), (ID, UUID, SID))

    def test_tui_is_off_by_default_and_main_hands_delegate_the_config(self):
        self.done()
        self.assertEqual(self.main(), 0)
        role, spec = self.call()
        self.assertEqual((spec.mode, spec.client), ("headless", CLIENT))
        self.assertEqual(role, config.load().roles["engineer"].run)
        self.assertEqual(event_log.LOGS_DIR, self.logs)
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
        no_client = [x for x in argv if not x.startswith("--client")]
        for bad in (
            argv[:-1],
            argv[2:],
            no_client,
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
                run.main(bad, gql=self.gql, delegate_cls=self.delegate, now=lambda: NOW)
            self.assertEqual(cm.exception.code, 2)
        self.nothing_ran()

    def test_bad_issue_or_session_id(self):
        argv = self.argv()
        for flag, value in (("--issue", "task-7"), ("--sid", "not-a-sid"), ("--sid", "z" * 36)):
            with self.subTest(flag=flag, value=value):
                bad = [f"{flag}={value}" if x.startswith(flag + "=") else x for x in argv]
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(
                        run.main(bad, gql=self.gql, delegate_cls=self.delegate, now=lambda: NOW),
                        2,
                    )
                issue, sid = (value, SID) if flag == "--issue" else (ID, value)
                self.assertEqual(
                    err.getvalue(), f"run.py: bad issue or session id: {issue} {sid}\n"
                )
        self.nothing_ran()

    def test_bad_client(self):
        for client in ("", "cl\x07aude"):
            with self.subTest(client=client):
                self.assertEqual(self.main(client=client), 2)
                self.assertEqual(self.err, f"run.py: bad client: {client!r}\n")
        self.nothing_ran()
