"""HostedSession's tui mode end to end on a private tmux server (session/tests/live_tmux.py),
os.environ that server's env: the fake adapter's Tui (session/tests/fake_adapter/) started with no
pane, its hook steps as events and events-file lines, send, the idle timer and its Blocked pause,
attach from another process, stop, a client's own exit, retire, list while tmux's state is
unknown, an old session once its name is another session's, two starts of one name at once, and a
start while another process retires the name.
"""

import contextlib
import itertools
import json
import os
import signal
import subprocess
import threading
import unittest
from collections.abc import Iterator
from unittest import mock

from agent_pm.session import Session
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    Exited,
    IdleTimeout,
    Mode,
    SessionError,
    SessionStatus,
    Spec,
    TurnEnd,
)
from agent_pm.session.hosts.tmux.host import Tmux
from agent_pm.session.modes import tui

from session.tests import fake_adapter, live_tmux
from session.tests.case import PY, SPAWNS
from session.tests.tui_support import DIALOG, IDLE, MANAGER, SID, WAIT, Live, Stream

# Another process: attaches by name, sends text, prints [sid, state, reply, its first event but
# IdleTimeout, reply].
ATTACH = """import importlib.util, json, sys
fake, adapters, name, text = sys.argv[1:]
spec = importlib.util.spec_from_file_location("agent_pm.session.adapters.fake", fake)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from agent_pm.session import Host, IdleTimeout
from agent_pm.session.engine.hosted_session import HostedSession
from agent_pm.utils import load_class
def adapter_class(path, base):
    return module.FakeAdapter if path == "agent_pm.session.adapters.fake:FakeAdapter" else load_class(path, base)
hosts = Host.parse_hosts({"default": "tmux", "tmux": {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"}})
session_cls = HostedSession.configured(adapters=json.loads(adapters), hosts=hosts, class_loader=adapter_class)
handle = session_cls.attach(name)
before = [handle.sid, handle.state(), handle.reply]
handle.send(text)
event = next(e for e in handle.events() if not isinstance(e, IdleTimeout))
print(json.dumps([*before, repr(event), handle.reply]))
"""
CLOSE = "import sys; from agent_pm.session import Host, Session; Session.configure(adapters={}, hosts=Host.parse_hosts({'default': 'tmux', 'tmux': {'implementation': 'agent_pm.session.hosts.tmux:TmuxHost'}})); Session.retire(sys.argv[1])"
SLOW_CLOSE = """import sys, time, shutil
from unittest import mock
from agent_pm.session import Host, Session
remove = shutil.rmtree
def slow(*args, **kwargs):
    open(sys.argv[2], "w").close()
    time.sleep(1)
    remove(*args, **kwargs)
with mock.patch("shutil.rmtree", slow):
    Session.configure(
        adapters={},
        hosts=Host.parse_hosts(
            {"default": "tmux", "tmux": {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"}}
        ),
    )
    Session.retire(sys.argv[1])
"""


class TuiTest(Live):
    def test_a_session_over_hooks_send_idle_attach_and_stop(self):
        handle = self.start(
            "w1",
            {"hook": "tool", "payload": {"session_id": "n1"}},
            {"hook": "stop", "payload": {"last_message": "r1"}},
            {"read": True},
            {"hook": "ask", "payload": {}},
            {"read": True},
            {"hook": "stop", "payload": {"last_message": "r2", "tasks": [1]}},
            {"read": True},
            sid=SID,
            idle_timeout=IDLE,
        )
        self.assertEqual(len(self.server.panes(f"={MANAGER}:")), 1)
        self.assertEqual(self.server.option("w1", "@sid"), SID)
        self.assertEqual(
            [(r["kind"], r["sid"]) for r in RunLog(self.workdir).read()],
            [("session", SID)],
        )
        self.assertEqual(tui.TuiRuntime.snapshot("w1")["sid"], SID)
        stream = Stream(self, handle)
        self.assertEqual(
            [stream.next() for _ in range(3)], [Activity(""), TurnEnd("r1", 0), IdleTimeout(IDLE)]
        )
        self.assertEqual(
            [(r["kind"], r.get("client_id")) for r in RunLog(self.workdir).read()],
            [("session", None), ("client_session", "n1")],
        )
        self.lines("w1 done")
        self.assertEqual(self.server.option("w1", "@state"), "done")

        handle.send("go")
        self.assertEqual(stream.next(idle=False), Blocked(""))
        self.lines("w1 done", "w1 blocked")
        stream.quiet(3 * IDLE)

        res = subprocess.run(
            [*PY, "-c", ATTACH, fake_adapter.__file__, json.dumps(self.adapters), "w1", "more"],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [SID, "live", "r1", repr(TurnEnd("r2", 1)), "r2"])
        self.assertEqual(stream.next(), TurnEnd("r2", 1))
        self.lines("w1 done", "w1 blocked", "w1 done")
        self.assertEqual(handle.reply, "r2")

        handle.stop()
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])
        self.assertEqual(self.session_cls.list("w1"), [])
        self.assertFalse(os.path.lexists(tui._Store("w1", host=self.session_cls.host()).dir))

    def test_inspect_an_ended_tui_session_after_its_pane_and_store_are_removed(self):
        self.adapters["fake"]["implementation"] = "agent_pm.session.adapters.fake:InspectAdapter"
        self.session_cls = self.fake_session()
        session = self.start("historical", {"exit": 3}, sid=SID)
        stream = Stream(self, session)
        self.assertEqual(stream.next(poke=self.poke), Exited(3))
        info = session.info()
        session.close()
        self.assertEqual(self.session_cls.list("historical"), [])
        self.assertFalse(
            os.path.lexists(tui._Store("historical", host=self.session_cls.host()).dir)
        )
        with contextlib.ExitStack() as stack:
            starts = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            status = self.session_cls.inspect(
                Spec(workdir=self.workdir, client="fake", sid=SID, access=Access(cwd=self.dir))
            )
        self.assertEqual(
            status, SessionStatus("yes", 1.5, f"{SID} {info.cwd} {info.transcript} {info.resume}")
        )
        for start in starts:
            start.assert_not_called()

    def test_a_client_exit_ends_with_its_code_the_session_live_until_close(self):
        handle = self.start("w2", {"hook": "stop", "payload": {"last_message": "bye"}}, {"exit": 3})
        stream = Stream(self, handle)
        self.assertEqual(stream.next(), TurnEnd("bye", 0))
        self.assertEqual(stream.next(poke=self.poke), Exited(3))
        self.lines("w2 done", "w2 dead")
        self.assertEqual(
            (handle.state(), [r.state for r in self.session_cls.list("w2")]), ("live", ["dead"])
        )
        self.session_cls.retire("w2")
        self.assertEqual((handle.state(), self.session_cls.list("w2")), ("absent", []))
        self.assertFalse(os.path.lexists(tui._Store("w2", host=self.session_cls.host()).dir))

    def test_an_unreachable_server_is_unknown_and_no_end(self):
        if os.getuid() == 0:
            self.skipTest("root connects to a mode 000 socket")
        handle = self.start("w3", {"read": True})
        stream = Stream(self, handle)
        mode = os.stat(self.server.socket).st_mode
        os.chmod(self.server.socket, 0)
        self.addCleanup(os.chmod, self.server.socket, mode)
        with self.assertRaisesRegex(SessionError, "^tmux sessions unknown: "):
            self.session_cls.list("w.*")
        self.assertEqual(handle.state(), "unknown")
        stream.quiet(1.0)
        os.chmod(self.server.socket, mode)
        self.assertEqual([r.name for r in self.session_cls.list("w3")], ["w3"])
        handle.stop()
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])


class SplitTest(Live):
    def test_a_split_and_refocused_session_keeps_its_client_pane(self):
        self.start(
            "w5",
            {"read": True},
            {"hook": "stop", "payload": {"last_message": "r1"}},
            {"read": True},
            sid=SID,
        )
        client = self.active("w5")
        added = self.server.tmux(
            "split-window", "-P", "-F", "#{pane_id}", "-t", "=w5:", *live_tmux.IDLE
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        other = added.stdout.strip()
        self.assertEqual(self.active("w5"), other)

        attached = self.session_cls.attach("w5")
        attached.send("ping")
        self.lines("w5 done")
        self.assertNotIn("ping", self.server.tmux("capture-pane", "-p", "-t", other).stdout)
        host = self.session_cls.host()
        status = host.attach("w5")
        self.assertEqual(
            (status.identity.split(" ")[2], status.alive(), status.activity()),
            (client, True, "done"),
        )

        attached.stop()
        panes = self.server.tmux("list-panes", "-s", "-t", "=w5:", "-F", "#{pane_id}")
        self.assertEqual(panes.returncode, 0, panes.stderr)
        self.assertEqual(panes.stdout.split(), [other])
        self.assertFalse(os.path.lexists(tui._Store("w5", host=host).dir))

    def active(self, session: str) -> str:
        found = self.server.tmux("display-message", "-p", "-t", f"={session}:", "#{pane_id}")
        self.assertEqual(found.returncode, 0, found.stderr)
        return found.stdout.strip()


class SameNameTest(Live):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(fake_adapter.FakeTui, "dialogs", (DIALOG,))
        patch.start()
        self.addCleanup(patch.stop)

    def old(self, name: str) -> tuple[Session, Iterator]:
        """Session `name` and its events() past its turn's end."""
        handle = self.start(
            name, {"hook": "stop", "payload": {"last_message": "r1"}}, {"read": True}
        )
        events = handle.events()
        self.assertEqual(next(events), TurnEnd("r1", 0))
        self.lines(f"{name} done")
        return handle, events

    def renewed(self, name: str, old: Session, events: Iterator) -> None:
        """`name` started again showing DIALOG, with an events file of its own: old's events end,
        never scanning it; old.stop() leaves it; the new session gets Blocked and the blocked line.
        """
        mine = os.path.join(self.dir, "events-new")
        new = self.start(name, {"raw": DIALOG}, {"read": True}, events=mine)
        live_tmux.wait(
            lambda: DIALOG in new.screen(), what="the dialog on the new session's screen"
        )
        self.assertEqual(next(events), Exited(-signal.SIGHUP))
        self.assertEqual((list(events), old.state()), ([], "absent"))
        old.stop()
        stream = Stream(self, new)
        self.addCleanup(new.stop)
        self.assertEqual(stream.next(), Blocked(DIALOG))
        self.lines(f"{name} blocked", events=mine)
        self.lines(f"{name} done")
        self.assertEqual((new.state(), self.server.option(name, "@state")), ("live", "blocked"))

    def test_a_restart_after_stop(self):
        old, events = self.old("w4")
        old.stop()
        self.renewed("w4", old, events)

    def test_two_starts_of_one_name_at_once_make_one_session_its_dir_kept(self):
        with open(self.adapters["fake"]["scenario"], "w") as f:
            json.dump([{"read": True}], f)
        tmux = Tmux()
        real, order, second, first_done = (
            tmux.start,
            itertools.count(),
            threading.Event(),
            threading.Event(),
        )
        results, threads = [], []

        def run():
            try:
                results.append(
                    self.session_cls.start(
                        Spec(
                            workdir=self.workdir,
                            name="w6",
                            client="fake",
                            mode=Mode.TUI,
                            prompt="hello",
                        )
                    )
                )
            except SessionError as e:
                results.append(e)

        def start(*args, **kwargs):
            if next(order):
                second.set()
                first_done.wait(WAIT)
                return real(*args, **kwargs)
            threads.append(threading.Thread(target=run))
            threads[0].start()
            second.wait(1)  # unlocked, the second start prepares meanwhile
            try:
                return real(*args, **kwargs)
            finally:
                first_done.set()

        self.session_cls = self.fake_session(
            host=type(
                "HostProxy",
                (),
                {"__getattr__": lambda _, key: start if key == "start" else getattr(tmux, key)},
            )()
        )
        run()
        threads[0].join(WAIT)
        handle, error = sorted(results, key=lambda result: isinstance(result, Exception))
        self.addCleanup(handle.stop)
        self.assertEqual(tui.TuiRuntime.snapshot("w6")["sid"], handle.sid)
        self.assertEqual(str(error), "tui session w6 is running; tmux attach -t '=w6'")
        self.assertEqual(
            (handle.state(), os.listdir(tui._Store("w6", host=self.session_cls.host()).dir)),
            ("live", ["session.json"]),
        )

    def test_a_restart_after_a_close_from_another_process(self):
        old, events = self.old("w5")
        res = subprocess.run(
            [*PY, "-c", CLOSE, "w5"],
            env=self.server.child_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=WAIT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.renewed("w5", old, events)

    def test_a_close_from_another_process_then_a_start_of_its_name_keeps_the_new_sessions_dir(self):
        self.start("w7", {"read": True})
        killed = os.path.join(self.dir, "killed")
        closer = subprocess.Popen(
            [*PY, "-c", SLOW_CLOSE, "w7", killed],
            env=self.server.child_env(),
            text=True,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        live_tmux.wait(lambda: os.path.exists(killed), what="the close's kill")
        new = self.start("w7", {"read": True})
        self.assertEqual((closer.communicate(timeout=WAIT)[1], closer.returncode), ("", 0))
        self.assertEqual((new.state(), tui.TuiRuntime.snapshot("w7")["sid"]), ("live", new.sid))


if __name__ == "__main__":
    unittest.main()
