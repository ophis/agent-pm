"""LinearRun owns exact plans and forwards views to the saved Delegate."""

import os
from dataclasses import replace

from agent_pm.delegate import ConfigError as DelegateConfigError
from agent_pm.delegate import NotDisplayable
from agent_pm.linear_orchestrator.engine import runs
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.types import ConfigError
from agent_pm.utils import Record
from orchestrator_tests.linear_run_case import SID, ReadyDelegate, RunCase

from delegate.tests.fakes.delegate import FakeDelegate


class CapturedDelegate(ReadyDelegate):
    def __init__(self, role, spec, **options):
        self.accepted = (role, spec, options)
        super().__init__(role, spec, **options)


class OtherDelegate(FakeDelegate):
    @classmethod
    def attach(cls, record_or_workdir, *, sid=None):
        raise AssertionError("current default must not attach this run")


class CheckedDelegate(FakeDelegate):
    checks: list = []

    @classmethod
    def check_tui(cls, *, display=None, events=None):
        cls.checks.append((display, events))
        if display == "refused":
            raise DelegateConfigError("no place for the TUI")


class Runs(RunCase):
    def test_constructed_delegate_receives_exact_spec_and_display(self):
        owner, record = self.start(delegate_cls=CapturedDelegate, display="opaque:\nvalue")
        role, spec, options = owner.delegate.accepted
        self.assertEqual(role, "researcher")
        self.assertEqual(spec.input, "Echo input.")
        self.assertEqual(spec.workdir, self.cfg.run_dir("TASK-1"))
        self.assertEqual(
            (spec.task, spec.client, spec.sid, spec.resume), ("light-research", "fake", SID, False)
        )
        self.assertEqual(spec.name, "researcher-TASK-1-1234abcd")
        self.assertEqual(spec.cwd, spec.workdir)
        self.assertEqual(spec.out, os.path.join(spec.workdir, "out.md"))
        self.assertEqual(
            options,
            {
                "display": "opaque:\nvalue",
                "callback": ["agent-pm-linear", "writeback", "--issue", "TASK-1", "--workdir"]
                + [spec.workdir],
                "overlays": (self.cfg.core,),
            },
        )
        self.assertEqual(record.display, "opaque:\nvalue")
        self.assertEqual((record.name, record.kind), ("TASK-1", "pipeline"))
        self.assertEqual(record.sessions, ("researcher-TASK-1-1234abcd",))
        self.assertEqual(record.ctl, ("agent-pm-linear",))
        self.assertEqual(record.resume, ("agent-pm-linear", "resume", "TASK-1"))

    def test_check_tui_asks_the_configured_delegate_kind_and_passes_on_its_refusal(self):
        cfg = replace(
            self.cfg,
            delegate_kind="checked",
            delegates={**self.cfg.delegates, "checked": CheckedDelegate},
        )
        CheckedDelegate.checks = []
        LinearRun.check_tui(cfg=cfg, display="opaque", events="/e")
        with self.assertRaisesRegex(DelegateConfigError, "no place for the TUI"):
            LinearRun.check_tui(cfg=cfg, display="refused", events="/e")
        self.assertEqual(CheckedDelegate.checks, [("opaque", "/e"), ("refused", "/e")])
        with self.assertRaisesRegex(ConfigError, "no longer configured"):
            LinearRun.check_tui(cfg=replace(cfg, delegate_kind="gone"), display=None, events="/e")

    def test_a_resumed_run_asks_its_delegate_to_resume_the_session(self):
        owner = LinearRun(
            "TASK-1",
            "Answer.",
            self.cfg.run_dir("TASK-1"),
            cfg=self.cfg,
            role="researcher",
            sid=SID,
            client="fake",
            resume=True,
            delegate_cls=CapturedDelegate,
        )
        _, spec, _ = owner.delegate.accepted
        self.assertEqual(
            (spec.sid, spec.resume, spec.task, spec.input), (SID, True, None, "Answer.")
        )

    def test_saved_provider_survives_a_changed_default_and_live_resume_is_idempotent(self):
        owner, record = self.start(display="foreign display")
        changed = replace(
            self.cfg,
            delegate_kind="other",
            delegates={**self.cfg.delegates, "other": OtherDelegate},
        )
        view = LinearRun.attach("TASK-1", cfg=changed)
        self.assertTrue(view.alive())
        self.assertEqual(view.delegate_kind, "fake")
        self.assertEqual(view.status(), replace(owner.delegate.status(), name="TASK-1"))
        self.assertEqual(view.session_info(), owner.delegate.session_info())
        self.assertEqual(Record.parse(view.resume().to_line()), record)
        self.assertEqual(view.outcome(), None)
        self.assertEqual(view.attach_command(), None)
        with self.assertRaises(NotDisplayable):
            view.show()
        view.close()
        self.assertFalse(owner.alive())

    def test_completed_resume_keeps_record_and_interrupted_resume_starts_once(self):
        owner, record = self.start()
        FakeDelegate.finish(self.cfg.run_dir("TASK-1"))
        view = LinearRun.attach("TASK-1", cfg=self.cfg)
        self.assertEqual(view.outcome(), FakeDelegate.outcome_result)
        self.assertEqual(view.resume(), record)
        self.assertFalse(view.alive())
        FakeDelegate.end(self.cfg.run_dir("TASK-1"))
        self.assertEqual(view.resume(), record)
        self.assertEqual(view.resume(), record)
        self.assertTrue(owner.alive())

    def test_forget_has_its_delegate_forget_the_run(self):
        self.start()
        view = LinearRun.attach("TASK-1", cfg=self.cfg)
        view.close()
        view.forget()
        with self.assertRaises(DelegateConfigError):
            LinearRun.attach("TASK-1", cfg=self.cfg)
        self.assertTrue(os.path.isdir(self.cfg.run_dir("TASK-1")))

    def test_missing_record_provider_or_invalid_identity_refuses_attach(self):
        with self.assertRaisesRegex(ConfigError, "no recorded run"):
            LinearRun.attach("TASK-1", cfg=self.cfg)
        self.start()
        with self.assertRaisesRegex(ConfigError, "no longer configured"):
            LinearRun.attach("TASK-1", cfg=replace(self.cfg, delegates={}))
        for issue, sid in (("../TASK-1", None), ("TASK-1", "bad sid")):
            with self.subTest(issue=issue, sid=sid), self.assertRaises(ConfigError):
                LinearRun.attach(issue, cfg=self.cfg, sid=sid)

    def test_nonlatest_sid_is_refused_before_attaching_the_wrong_view(self):
        self.start()
        newer = "abcdef12-0000-4000-8000-000000000000"
        self.start(sid=newer)
        with self.assertRaisesRegex(ConfigError, "no longer the latest"):
            LinearRun.attach("TASK-1", cfg=self.cfg, sid=SID)

    def test_a_newer_row_without_its_own_delegate_state_never_views_the_previous_session(self):
        self.start()
        newer = "abcdef12-0000-4000-8000-000000000000"
        runs.note(
            self.cfg.runs_log,
            "start",
            issue="TASK-1",
            sid=newer,
            role="researcher",
            client="fake",
            delegate_kind="fake",
        )
        with self.assertRaisesRegex(DelegateConfigError, f"sid {newer} is not the run's"):
            LinearRun.attach("TASK-1", cfg=self.cfg)

    def test_a_kindless_row_is_a_bad_row_whatever_the_current_default(self):
        owner = LinearRun(
            "TASK-1", "Input", self.cfg.run_dir("TASK-1"), cfg=self.cfg, role="dummy", sid=SID
        )
        owner.detach()
        runs.note(self.cfg.runs_log, "start", issue="TASK-1", sid=SID, role="dummy", client="fake")
        changed = replace(
            self.cfg,
            delegate_kind="other",
            delegates={**self.cfg.delegates, "other": OtherDelegate},
        )
        with self.assertRaisesRegex(
            ConfigError, f"session {SID}'s runs.jsonl row has no delegate kind"
        ):
            LinearRun.attach("TASK-1", cfg=changed)
