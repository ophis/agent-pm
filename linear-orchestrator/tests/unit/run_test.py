import os
import signal
from unittest import mock

from agent_pm import delegate
from agent_pm.delegate import Progress, Result, Spec
from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import Stopped
from agent_pm.session import SIGNALS
from orchestrator_test_helpers.unit.board_ids import STATES
from orchestrator_test_helpers.unit.config_support import GATE
from orchestrator_test_helpers.unit.run.data import (
    DONE,
    PR,
    REPAIR,
    RUNNING,
    RUNTIME,
    TITLE,
    cid,
    created,
    ended,
    updated,
)
from orchestrator_test_helpers.unit.run.fake_run_fixtures import FakeRun
from orchestrator_test_helpers.unit.run.inner_fixtures import Inner as InnerBase
from orchestrator_test_helpers.unit.run_fixtures import HARNESS, KEY, RESEARCHER, SID, UUID, Gql


class Inner1(InnerBase):
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
        self.assertEqual(
            self.call(),
            (
                config.load().roles["engineer"].run,
                Spec(
                    "Do it.\n",
                    self.rd,
                    runtime=RUNTIME,
                    sid=SID,
                    out=os.path.join(self.rd, "out.md"),
                    cwd=self.rd,
                ),
            ),
        )
        self.assertEqual(self.delegate.dirs, [True])
        self.assertEqual(self.api.info, [(RUNTIME, SID, self.rd)] * 2)
        self.assertEqual(os.environ["PATH"], config.path())
        self.assertEqual([o for o in self.order if o[0] == "show"], [])

    def test_main_passes_the_caller_owned_driver_to_delegate(self):
        self.done()
        self.assertEqual(self.main(), 0)
        self.assertIs(self.delegate.driver, self.api)

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
                "linear-orchestrator/config.toml: required, unset: harness_key: set them in "
                "~/.agent-pm/config.local.toml (linear-orchestrator/config.toml's `# local:` lines)",
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
            "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n",
        )
        self.nothing_ran()

    def test_bad_uuid_or_target(self):
        for kw in (dict(uuid="nope"), dict(target="a/b/c")):
            with self.subTest(kw=kw):
                self.assertEqual(self.main(**kw), 2)
                self.assertTrue(self.err.startswith("run.py: bad issue uuid or target: "), self.err)
        self.nothing_ran()
