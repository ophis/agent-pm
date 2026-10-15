"""DelegateContract, FR-47's Delegate contract (FR-13 to FR-16): a subclass also inherits Case and
defines delegate_cls, ROLE (a role it runs), DISPLAYABLE (its sessions can display), make(workdir,
**kw) (a Delegate of kw's role, else ROLE, and a Spec for workdir with kw's other fields),
end(record) (its sessions gone, no outcome) and finish(record) (its sessions gone, a valid outcome).
Each case uses only the Delegate ABC and these hooks.
"""

import os
import uuid

from agent_pm.delegate import Begun, ConfigError, Delegate, Finished
from agent_pm.session import NotDisplayable, Usage
from agent_pm.utils import Record, Status

NO_ROLE = "no-such-role"
OUTER = {
    "kind": "pipeline",
    "name": "TASK-1",
    "ctl": ("agent-pm-linear",),
    "resume": ("agent-pm-linear", "resume", "TASK-1"),
}


class DelegateContract:
    delegate_cls: type[Delegate]
    ROLE: str
    DISPLAYABLE: bool

    def make(self, workdir: str, **kw) -> Delegate:
        raise NotImplementedError

    def end(self, record: Record) -> None:
        raise NotImplementedError

    def finish(self, record: Record) -> None:
        raise NotImplementedError

    def workdir(self) -> str:
        path = os.path.join(self.home, f"run-{uuid.uuid4().hex[:8]}")
        os.mkdir(path)
        return path

    def detached(self, outer=None) -> tuple[Record, str]:
        workdir = self.workdir()
        record = self.make(workdir).detach(outer=outer)
        self.addCleanup(self.delegate_cls.attach(workdir).close)
        return record, workdir

    def test_an_unknown_role_is_refused_with_config_error_writing_nothing(self):
        workdir = self.workdir()
        with self.assertRaises(ConfigError):
            self.make(workdir, role=NO_ROLE)
        self.assertEqual(os.listdir(workdir), [])

    def test_detach_returns_a_record_line_with_the_default_outer_fields(self):
        record, workdir = self.detached()
        self.assertIsInstance(record, Record)
        self.assertEqual(Record.parse(record.to_line()), record)
        self.assertEqual(
            (record.kind, record.ctl, record.detail), ("role", ("agent-pm-delegate",), self.ROLE)
        )
        self.assertEqual(record.resume[:2], ("agent-pm-delegate", "resume"))
        self.assertEqual(len(record.resume), 3)
        for path in (record.workdir, record.resume[2]):
            self.assertEqual(os.path.realpath(path), os.path.realpath(workdir))
        self.assertTrue(record.sessions)

    def test_detach_takes_the_outer_starters_kind_name_ctl_and_resume(self):
        record, _ = self.detached(OUTER)
        self.assertEqual(
            (record.kind, record.name, record.ctl, record.resume), tuple(OUTER.values())
        )

    def test_attach_by_record_or_workdir_views_the_live_run(self):
        record, workdir = self.detached()
        for key in (record, workdir):
            with self.subTest(key=type(key).__name__):
                view = self.delegate_cls.attach(key)
                self.assertTrue(view.alive())
                status = view.status()
                self.assertIsInstance(status, Status)
                self.assertEqual((status.name, status.alive), (record.name, True))

    def test_attach_with_another_sessions_sid_raises_config_error(self):
        record, workdir = self.detached()
        self.assertTrue(self.delegate_cls.attach(workdir, sid=record.sid).alive())
        with self.assertRaises(ConfigError):
            self.delegate_cls.attach(workdir, sid="00000000-0000-4000-8000-0000000000ff")

    def test_attach_of_a_dir_without_a_run_raises_config_error(self):
        with self.assertRaises(ConfigError):
            self.delegate_cls.attach(self.workdir())

    def test_close_twice_is_fine_and_leaves_it_not_alive(self):
        record, _ = self.detached()
        view = self.delegate_cls.attach(record)
        view.close()
        view.close()
        self.assertFalse(view.alive())
        self.assertFalse(self.delegate_cls.attach(record).alive())

    def test_forget_after_close_deletes_the_run_keeps_its_workdir_and_is_idempotent(self):
        workdir = self.workdir()
        record = self.make(workdir).detach()
        view = self.delegate_cls.attach(record)
        view.close()
        view.forget()
        view.forget()
        with self.assertRaises(ConfigError):
            self.delegate_cls.attach(workdir)
        self.assertTrue(os.path.isdir(workdir))

    def test_resume_after_its_sessions_ended_starts_them_once_with_the_same_sid(self):
        record, _ = self.detached()
        self.end(record)
        view = self.delegate_cls.attach(record)
        self.assertFalse(view.alive())
        first = view.resume()
        self.assertEqual(view.resume(), first)
        self.assertEqual((first.sid, first.sessions), (record.sid, record.sessions))
        self.assertTrue(view.alive())

    def test_resume_in_a_given_mode_starts_them_once_with_the_same_sid(self):
        record, _ = self.detached()
        self.end(record)
        view = self.delegate_cls.attach(record)
        resumed = view.resume(mode="headless")
        self.assertEqual(resumed.sid, record.sid)
        self.assertEqual(view.resume(mode="headless"), resumed)
        self.assertTrue(view.alive())

    def test_resume_after_a_valid_outcome_starts_nothing_and_returns_the_record(self):
        record, _ = self.detached()
        self.finish(record)
        view = self.delegate_cls.attach(record)
        self.assertIsNotNone(view.outcome())
        self.assertEqual(view.resume(), record)
        self.assertFalse(view.alive())

    def test_show_and_attach_command_reach_only_displayable_sessions(self):
        record, _ = self.detached()
        view = self.delegate_cls.attach(record)
        if self.DISPLAYABLE:
            self.assertIsInstance(view.attach_command(), str)
            return
        self.assertIsNone(view.attach_command())
        self.assertIsNone(view.status().attach)
        with self.assertRaises(NotDisplayable):
            view.show()

    def test_run_reports_begun_first_and_finished_last_and_returns_its_outcome(self):
        seen = []
        result = self.make(self.workdir()).run(seen.append)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.outcome)
        self.assertIsInstance(seen[0], Begun)
        self.assertIsInstance(seen[-1], Finished)
        self.assertEqual(seen[-1].result, result)

    def test_class_methods_answer_for_its_roles_and_refuse_an_unknown_one(self):
        cls = self.delegate_cls
        self.assertIn(self.ROLE, cls.roles())
        self.assertEqual(cls.resolve(self.ROLE).role, self.ROLE)
        self.assertIsInstance(cls.trusted_dirs(), frozenset)
        usage = cls.usage(self.ROLE)
        self.assertTrue(usage is None or isinstance(usage, Usage), usage)
        for method in (cls.resolve, cls.usage):
            with self.subTest(method=method.__name__), self.assertRaises(ConfigError):
                method(NO_ROLE)
