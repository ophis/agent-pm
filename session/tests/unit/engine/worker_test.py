"""Worker with a recorded Session class and Host handle: state, Record, the early watch and the
FR-36 replace ladder (no process, no tmux; files only under the test's HOME).
"""

import errno
import itertools
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from agent_pm.session import ConfigError, Host, HostProcess, Mode, Session, SessionError
from agent_pm.session.engine import worker
from agent_pm.session.engine.types import Blocked, Exited, IdleTimeout, Reply
from agent_pm.session.engine.worker import Worker, WorkerState
from agent_pm.utils import Record

from session.tests.case import Case

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "1f8fad5b-d9cb-469f-a165-70867728950e"


class Killed(BaseException):
    """The start's process dying: no handler runs."""


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def state(**fields) -> WorkerState:
    return WorkerState(
        **{
            "name": "w1",
            "sid": SID,
            "client": "claude",
            "cwd": "/work",
            "host": "tmux",
            "flags": ("-m", "x"),
            "env": {"K": ""},
            "events": "/events",
            "display": "mgr",
            **fields,
        }
    )


def state_path() -> str:
    return os.path.join(state().workdir(), worker.STATE_FILE)


def pointer() -> str:
    return os.path.join(os.path.dirname(state().workdir()), worker.BY_NAME, "w1")


class Base(Case):
    def setUp(self):
        super().setUp()
        self.order = mock.Mock()
        self.handle = mock.create_autospec(HostProcess, instance=True)
        self.handle.token = "/tmp/apm-group-1"
        self.handle.session_identity.return_value = (SID, "g1")
        self.handle.alive.return_value = False
        self.handle.record.return_value = None
        self.host = mock.create_autospec(Host, instance=True)
        self.host.list.return_value = ["w1"]
        self.host.attach.return_value = self.handle
        self.host.reclaim.return_value = None
        self.session_cls = mock.Mock()
        self.session_cls.host.return_value = self.host
        self.session = self.session_cls.start.return_value
        self.session.displayable = False
        self.session.opening_pending = False
        self.session.events.return_value = iter([IdleTimeout(worker.EARLY)])
        self.order.attach_mock(self.session_cls.retire, "retire")
        self.order.attach_mock(self.session_cls.start, "start")
        self.order.attach_mock(self.session_cls.preflight, "preflight")
        for call in ("close", "wait_exit", "kill", "publish"):
            self.order.attach_mock(getattr(self.handle, call), call)
        self.said = []

    def worker(self, **fields) -> Worker:
        return Worker(state(**fields), session_cls=self.session_cls, say=self.said.append)

    def calls(self) -> list[str]:
        return [call[0] for call in self.order.mock_calls if "." not in call[0]]


class StateTest(Case):
    def test_its_json_round_trips_and_names_every_field_once(self):
        found = state(token="t")
        data = json.loads(found.to_json())
        self.assertEqual(data["version"], 1)
        self.assertEqual((data["flags"], data["env"]), (["-m", "x"], {"K": ""}))
        self.assertEqual(WorkerState.from_json(data), found)

    def test_a_bad_field_or_shape_is_a_config_error(self):
        for field, value in (
            ("name", "a b"),
            ("sid", ".."),
            ("sid", SID.upper()),
            ("client", "Bad"),
            ("cwd", "rel"),
            ("host", "X"),
            ("flags", ["-m"]),
            ("flags", ("a\0",)),
            ("env", {"A=B": "1"}),
            ("env", {"K": "a\nb"}),
            ("events", "rel"),
            ("display", "a\nb"),
            ("token", ""),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ConfigError):
                state(**{field: value})
        data = json.loads(state().to_json())
        for change in ({"version": 2}, {"extra": 1}, {"flags": "x"}, {"env": []}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                WorkerState.from_json({**data, **change})
        del data["token"]
        with self.assertRaises(ConfigError):
            WorkerState.from_json(data)

    def test_its_record_is_a_worker_answered_and_resumed_by_the_session_command(self):
        record = state().record()
        workdir = os.path.join(self.home, ".agent-pm", "workers", SID)
        self.assertEqual(
            record,
            Record(
                1,
                "worker",
                "w1",
                ("w1",),
                SID,
                workdir,
                ("agent-pm-session",),
                ("agent-pm-session", "resume", "w1"),
                "mgr",
                "claude",
            ),
        )
        self.assertEqual(Record.parse(record.to_line()), record)

    def test_its_spec_is_an_interactive_tui_in_its_cwd(self):
        spec = state().spec(prompt="hi", resume=True)
        self.assertEqual(
            (spec.mode, spec.sid, spec.name, spec.client, spec.prompt, spec.resume, spec.events),
            (Mode.TUI, SID, "w1", "claude", "hi", True, "/events"),
        )
        self.assertEqual((spec.access.cwd, spec.access.project), ("/work", True))
        self.assertEqual((spec.flags, dict(spec.env)), (("-m", "x"), {"K": ""}))
        self.assertEqual(spec.workdir, state().workdir())


class StartTest(Base):
    def test_a_quiet_start_saves_its_state_then_publishes_and_returns_its_record(self):
        found = self.worker().start(prompt="hi", resume=False)
        self.assertEqual(found, state(token="/tmp/apm-group-1").record())
        self.assertEqual(self.calls(), ["start", "publish"])
        saved = Worker.load("w1")
        self.assertEqual(saved.state, state(token="/tmp/apm-group-1"))
        _, kwargs = self.session_cls.start.call_args
        self.assertEqual(kwargs, {"display": "mgr", "host": "tmux", "idle_timeout": worker.TICK})

    def test_a_dialog_ends_the_watch_at_once_and_still_publishes(self):
        self.session.events.return_value = iter([Reply("x"), Blocked("Edit"), Exited(1)])
        self.worker().start(prompt="", resume=False)
        self.assertEqual(self.said, ["session w1: waits on a dialog: Edit"])
        self.handle.publish.assert_called_once()

    def test_an_early_exit_reports_its_last_lines_and_publishes_nothing_keeping_it(self):
        self.session.events.return_value = iter([Exited(7)])
        with self.assertRaises(SessionError) as caught:
            self.worker().start(prompt="", resume=False)
        self.assertEqual(str(caught.exception), "w1: claude exited 7 at once")
        self.handle.publish.assert_not_called()
        self.session.close.assert_not_called()
        self.assertEqual(Worker.load("w1").state.sid, SID)

    def test_the_watch_ends_at_its_deadline_without_an_idle_timeout(self):
        ticks = iter([0, 1, worker.EARLY + 1])
        self.session.events.return_value = iter([Reply("a"), Reply("b"), Exited(1)])
        Worker(state(), session_cls=self.session_cls, clock=lambda: next(ticks)).start(
            prompt="", resume=False
        )
        self.handle.publish.assert_called_once()

    def test_its_state_is_saved_before_its_client_starts(self):
        seen = []

        def launch(*_, **__):
            seen.append(Worker.load("w1").state)
            return self.session

        self.session_cls.start.side_effect = launch
        self.worker(token="/tmp/apm-group-0").start(prompt="", resume=False)
        self.assertEqual(seen, [state()])

    def test_a_start_killed_after_its_client_starts_is_resumed(self):
        self.host.attach.side_effect = Killed
        with self.assertRaises(Killed):
            self.worker().start(prompt="", resume=False)
        self.host.attach.side_effect = None
        self.handle.alive.return_value = True
        found = Worker.load("w1", session_cls=self.session_cls).resume()
        self.assertEqual(found, state().record())
        self.handle.publish.assert_called_once_with(found)

    def test_a_failed_start_puts_back_the_state_and_latest_start_it_found(self):
        Worker(state(sid=OTHER), session_cls=self.session_cls).start(prompt="", resume=False)
        self.session_cls.start.side_effect = SessionError("tui session w1 is running")
        with self.assertRaisesRegex(SessionError, "^tui session w1 is running$"):
            self.worker().start(prompt="", resume=False)
        self.assertFalse(os.path.exists(state_path()))
        self.assertEqual(Worker.load("w1").state.sid, OTHER)
        self.session_cls.start.side_effect = None
        self.worker().start(prompt="", resume=False)
        kept = read(state_path())
        self.session_cls.start.side_effect = ConfigError("unknown client claude")
        with self.assertRaises(ConfigError):
            self.worker(flags=()).start(prompt="", resume=True)
        self.assertEqual(read(state_path()), kept)
        self.assertEqual(Worker.load("w1").state.sid, SID)

    def test_a_state_it_cannot_save_starts_nothing(self):
        os.makedirs(pointer(), 0o700)
        with self.assertRaisesRegex(
            SessionError, f"^w1: {os.strerror(errno.EISDIR)}; nothing started$"
        ):
            self.worker().start(prompt="", resume=False)
        self.session_cls.start.assert_not_called()
        self.assertFalse(os.path.exists(state_path()))

    def test_a_token_it_cannot_save_undoes_the_start(self):
        def launch(*_, **__):
            os.remove(pointer())
            os.mkdir(pointer(), 0o700)
            return self.session

        self.session_cls.start.side_effect = launch
        with self.assertRaisesRegex(
            SessionError, f"^w1: {os.strerror(errno.EISDIR)}; start undone$"
        ):
            self.worker().start(prompt="", resume=False)
        self.session.close.assert_called_once_with()
        self.handle.publish.assert_not_called()
        self.assertFalse(os.path.exists(state_path()))


class ColorWatchTest(Base):
    UNSET = "session w1: color not set: {}; agent-pm-session send w1 '/color'"

    def watch(self, sent: float, events=None) -> int:
        """Starts w1 on a clock ticking 1 s per call, its opening pending until tick `sent`; the
        ticks used. opening_pending's call count varies by Python (3.11's isinstance calls it).
        """
        ticks = itertools.count()
        now = [0]

        def tick():
            now[0] = next(ticks)
            return now[0]

        type(self.session).opening_pending = mock.PropertyMock(side_effect=lambda: now[0] < sent)
        self.session.events.return_value = events or itertools.repeat(IdleTimeout(worker.TICK))
        Worker(state(), session_cls=self.session_cls, say=self.said.append, clock=tick).start(
            prompt="", resume=False
        )
        return next(ticks)

    def test_sent_by_the_early_deadline_returns_at_it(self):
        self.assertEqual(self.watch(0), worker.EARLY + 1)
        self.assertEqual(self.said, [])
        self.handle.publish.assert_called_once()

    def test_pending_keeps_watching_until_sent(self):
        self.assertEqual(self.watch(8), 9)
        self.assertEqual(self.said, [])
        self.handle.publish.assert_called_once()

    def test_pending_at_the_cap_says_color_not_set(self):
        self.assertEqual(self.watch(float("inf")), worker.COLOR_CAP + 1)
        self.assertEqual(self.said, [self.UNSET.format("not ready within 15 s")])
        self.handle.publish.assert_called_once()

    def test_a_dialog_while_pending_says_both(self):
        self.watch(float("inf"), events=iter([Blocked("Trust")]))
        self.assertEqual(
            self.said,
            ["session w1: waits on a dialog: Trust", self.UNSET.format("waits on a dialog")],
        )
        self.handle.publish.assert_called_once()

    def test_an_exit_while_pending_fails_without_the_color_line(self):
        with self.assertRaises(SessionError):
            self.watch(float("inf"), events=iter([Exited(1)]))
        self.assertEqual(self.said, [])
        self.handle.publish.assert_not_called()

    def test_a_session_without_an_opening_returns_at_the_early_deadline(self):
        plain = mock.create_autospec(Session, instance=True)
        plain.displayable = False
        plain.events.return_value = itertools.repeat(IdleTimeout(worker.TICK))
        self.session_cls.start.return_value = plain
        ticks = itertools.count()
        Worker(
            state(), session_cls=self.session_cls, say=self.said.append, clock=lambda: next(ticks)
        ).start(prompt="", resume=False)
        self.assertEqual(next(ticks), worker.EARLY + 1)
        self.assertEqual(self.said, [])


class LoadTest(Base):
    def test_by_its_latest_start_or_a_given_sid_of_its_own(self):
        self.worker().start(prompt="", resume=False)
        self.assertEqual(Worker.load("w1").state.sid, SID)
        self.assertEqual(Worker.load("w1", sid=SID).state.name, "w1")
        with self.assertRaisesRegex(ConfigError, f"^sid {SID} is worker w1's, not w2's$"):
            Worker.load("w2", sid=SID)
        with self.assertRaisesRegex(ConfigError, f"^sid {OTHER} is not worker w1's"):
            Worker.load("w1", sid=OTHER)
        Worker(state(sid=OTHER), session_cls=self.session_cls).start(prompt="", resume=False)
        self.assertEqual(Worker.load("w1", sid=OTHER).state.sid, OTHER)
        with self.assertRaisesRegex(
            ConfigError, f"^sid {SID} is not worker w1's .its latest: {OTHER}"
        ):
            Worker.load("w1", sid=SID)
        for sid in ("..", SID.upper()):
            with self.subTest(sid=sid), self.assertRaises(ConfigError):
                Worker.load("w1", sid=sid)

    def test_a_start_with_another_workers_sid_is_refused_before_anything(self):
        self.worker().start(prompt="", resume=False)
        with self.assertRaisesRegex(ConfigError, f"^sid {SID} is worker w1's$"):
            self.worker(name="w2").start(prompt="", resume=True)
        self.session_cls.start.assert_called_once()
        self.assertEqual(Worker.load("w1").state, state(token="/tmp/apm-group-1"))
        with self.assertRaisesRegex(SessionError, "^w2: not a worker"):
            Worker.load("w2")

    def test_of_two_starts_claiming_one_sid_at_once_only_the_first_starts(self):
        launching, go = threading.Event(), threading.Event()

        def launch(*_, **__):
            launching.set()
            go.wait(10)
            return self.session

        def run(each: Worker) -> Record:
            with each.locked():
                return each.start(prompt="", resume=False)

        self.session_cls.start.side_effect = launch
        first, second = self.worker(), self.worker(name="w2")
        first.check(prompt="", resume=False)
        with ThreadPoolExecutor(2) as pool:
            started = pool.submit(run, first)
            self.assertTrue(launching.wait(10))
            second.check(prompt="", resume=False)
            refused = pool.submit(run, second)
            go.set()
            self.assertEqual(started.result(10).name, "w1")
            with self.assertRaisesRegex(ConfigError, f"^sid {SID} is worker w1's$"):
                refused.result(10)
        self.session_cls.start.assert_called_once()

    def test_no_or_unreadable_state_is_not_a_worker(self):
        with self.assertRaisesRegex(SessionError, "^w1: not a worker"):
            Worker.load("w1")
        with self.assertRaisesRegex(SessionError, f"^w1: no worker {OTHER}"):
            Worker.load("w1", sid=OTHER)
        self.worker().start(prompt="", resume=False)
        path = os.path.join(state().workdir(), worker.STATE_FILE)
        for text in ("{", "[]", json.dumps({"version": 1})):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            with self.subTest(text=text), self.assertRaisesRegex(SessionError, "bad worker.json"):
                Worker.load("w1")
        with self.assertRaises(ConfigError):
            Worker.load("a b")


class ResumeTest(Base):
    def test_a_live_worker_returns_its_stored_record_and_starts_or_closes_nothing(self):
        stored = state().record()
        self.handle.alive.return_value = True
        self.handle.record.return_value = stored
        self.assertIs(self.worker().resume(), stored)
        self.assertEqual(self.calls(), [])

    def test_a_live_worker_without_its_record_gets_it_published(self):
        self.handle.alive.return_value = True
        found = self.worker().resume()
        self.assertEqual(found, state().record())
        self.assertEqual(self.calls(), ["publish"])

    def test_a_dead_one_is_retired_its_group_awaited_then_resumed_with_no_prompt(self):
        self.handle.wait_exit.return_value = True
        self.worker().resume()
        self.assertEqual(
            self.calls(), ["preflight", "retire", "close", "wait_exit", "start", "publish"]
        )
        self.handle.wait_exit.assert_called_once_with(worker.WAIT)
        spec = self.session_cls.start.call_args.args[0]
        self.assertEqual((spec.prompt, spec.resume, spec.sid), ("", True, SID))

    def test_a_group_still_there_after_wait_is_killed_then_awaited_again(self):
        self.handle.wait_exit.side_effect = [False, True]
        self.worker().resume()
        self.assertEqual(
            self.calls(),
            ["preflight", "retire", "close", "wait_exit", "kill", "wait_exit", "start", "publish"],
        )

    def test_a_group_that_outlives_its_kill_starts_nothing(self):
        self.handle.wait_exit.return_value = False
        with self.assertRaisesRegex(SessionError, "old client did not exit; nothing started"):
            self.worker().resume()
        self.session_cls.start.assert_not_called()

    def test_a_gone_session_reclaims_its_tokens_live_group_and_replaces_it(self):
        self.host.list.return_value = []
        self.host.reclaim.return_value = self.handle
        self.handle.alive.return_value = True
        self.handle.wait_exit.return_value = True
        self.worker(token="/tmp/apm-group-1").resume()
        self.host.reclaim.assert_called_once_with("/tmp/apm-group-1")
        self.assertEqual(self.calls()[:4], ["preflight", "retire", "close", "wait_exit"])

    def test_a_gone_session_without_a_group_starts_after_retiring(self):
        self.host.list.return_value = []
        self.worker(token=None).resume()
        self.assertEqual(self.calls()[:3], ["preflight", "retire", "start"])
        self.host.reclaim.assert_not_called()

    def test_a_start_it_cannot_make_is_refused_before_the_old_one_is_touched(self):
        self.session_cls.preflight.side_effect = ConfigError("unknown client claude")
        with self.assertRaisesRegex(ConfigError, "unknown client"):
            self.worker().resume()
        self.assertEqual(self.calls(), ["preflight"])

    def test_another_sessions_name_is_never_closed(self):
        for identity in ((OTHER, "g"), (None, None)):
            with self.subTest(identity=identity):
                self.handle.session_identity.return_value = identity
                with self.assertRaisesRegex(SessionError, f"^session w1 is not worker {SID}"):
                    self.worker().resume()
                with self.assertRaisesRegex(SessionError, f"^session w1 is not worker {SID}"):
                    self.worker().restart()
                self.assertEqual(self.calls(), [])


class RestartTest(Base):
    def test_a_session_is_replaced_by_a_resumed_start(self):
        self.handle.alive.return_value = True
        self.handle.wait_exit.return_value = True
        self.worker().restart()
        self.assertEqual(self.calls(), ["retire", "close", "wait_exit", "start", "publish"])

    def test_a_start_refused_once_the_old_session_is_gone_is_no_longer_a_config_error(self):
        self.handle.alive.return_value = True
        self.handle.wait_exit.return_value = True
        self.session_cls.start.side_effect = ConfigError("unknown client claude")
        for call in ("restart", "resume"):
            with self.subTest(call=call):
                if call == "resume":
                    self.handle.alive.return_value = False
                with self.assertRaisesRegex(
                    SessionError, "^w1: old session closed, none started: unknown client claude$"
                ):
                    getattr(self.worker(), call)()

    def test_no_session_is_refused_untouched(self):
        self.host.list.return_value = []
        with self.assertRaisesRegex(SessionError, "^w1: no session to restart; resume starts it$"):
            self.worker().restart()
        self.assertEqual(self.calls(), [])
