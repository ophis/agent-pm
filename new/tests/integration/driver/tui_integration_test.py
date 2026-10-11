"""driver.api's tui mode end to end on a private tmux server (new/tests/live_tmux.py), os.environ that server's env:
the fake adapter's Tui (new/tests/fake_adapter/) started with no pane, its hook steps as events and events-file lines,
send, the idle timer and its Blocked pause, attach from another process, stop, a runtime's own exit, close, and
sessions while tmux's state is unknown."""
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from new.tests import SRC, fake_adapter, hermetic, live_tmux
from adapters.base import Activity, Blocked, Exited, SessionBound, TurnEnd
from driver import api, records, registry
from driver.modes import tui

MANAGER = "mgr"
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
IDLE = 0.5   # idle_timeout, in seconds
WAIT = 20    # upper bound on any wait, in seconds
# Another process: attaches by name, sends text, prints [sid, state, reply, its first event but IdleTimeout, reply].
ATTACH = """import importlib.util, json, sys
src, fake, config, name, text = sys.argv[1:]
sys.path.insert(0, src)
spec = importlib.util.spec_from_file_location("adapters.fake", fake)
sys.modules["adapters.fake"] = module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from driver import api, registry
registry.configure(json.loads(config))
handle = api.attach(name)
before = [handle.sid, handle.state(), handle.reply]
handle.send(text)
event = next(e for e in handle.events() if not isinstance(e, api.IdleTimeout))
print(json.dumps([*before, repr(event), handle.reply]))
"""


class Stream:
    """A Handle's events(), drained by a thread into a queue."""

    def __init__(self, handle: api.Handle):
        self.queue = queue.Queue()
        threading.Thread(target=self._drain, args=(handle,), daemon=True).start()

    def _drain(self, handle):
        try:
            for event in handle.events():
                self.queue.put(event)
        except BaseException as e:
            self.queue.put(e)

    def next(self, poke=None, idle=True):
        """The next event (idle False: but IdleTimeout), within WAIT s; poke() runs between checks."""
        deadline = time.monotonic() + WAIT
        while True:
            try:
                item = self.queue.get(timeout=live_tmux.POLL)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise AssertionError(f"no event within {WAIT} s") from None
                if poke:
                    poke()
                continue
            if isinstance(item, BaseException):
                raise item
            if idle or not isinstance(item, api.IdleTimeout):
                return item

    def until_exited(self) -> list:
        """The events up to Exited, IdleTimeouts dropped."""
        found = [self.next(idle=False)]
        while not isinstance(found[-1], Exited):
            found.append(self.next(idle=False))
        return found

    def quiet(self, seconds: float) -> None:
        time.sleep(seconds)
        if not self.queue.empty():
            raise AssertionError(f"an event within {seconds} s: {self.queue.get()!r}")


class Live(unittest.TestCase):
    def setUp(self):
        home = os.path.dirname(hermetic.home(self))
        self.server = live_tmux.Server(self, home)
        self.server.start(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.events = os.path.join(self.dir, "events")
        self.config = {"adapters": {"fake": {"implementation": "adapters.fake:FakeAdapter", "tui": True,
                                             "scenario": os.path.join(self.dir, "scenario.json"),
                                             "log": os.path.join(self.dir, "runtime.log")}}}
        patch = mock.patch.dict(sys.modules, {"adapters.fake": fake_adapter})
        patch.start()
        self.addCleanup(patch.stop)
        registry.configure(self.config)
        self.addCleanup(registry.reset)

    def start(self, name: str, *steps, **kwargs) -> api.Handle:
        """Tui session `name` of the fake runtime running `steps`, its events file self.events."""
        with open(self.config["adapters"]["fake"]["scenario"], "w") as f:
            json.dump(list(steps), f)
        handle = api.start("fake", "tui", "hello", api.Spec(workdir=self.workdir), name=name, events=self.events,
                           **kwargs)
        self.addCleanup(handle.stop)
        return handle

    def lines(self, *want: str) -> None:
        """Waits until the events file's lines, times dropped, are `want`; pokes the server meanwhile."""
        def check():
            self.poke()
            return [line.split(" ", 1)[1] for line in live_tmux.events(self.events)] == list(want)
        live_tmux.wait(check, what=f"events lines {want}")

    def poke(self):
        # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so its status unset and no pane-died,
        # until another child of the server exits: a no-op job is one.
        self.server.tmux("run-shell", "-b", "true")


class TuiTest(Live):
    def test_a_session_over_hooks_send_idle_attach_and_stop(self):
        handle = self.start("w1", {"hook": "tool", "payload": {"session_id": "n1"}},
                            {"hook": "stop", "payload": {"last_message": "r1"}}, {"read": True},
                            {"hook": "ask", "payload": {}}, {"read": True},
                            {"hook": "stop", "payload": {"last_message": "r2", "tasks": [1]}}, {"read": True},
                            sid=SID, idle_timeout=IDLE)
        self.assertEqual(len(self.server.panes(f"={MANAGER}:")), 1)
        self.assertEqual(self.server.option("w1", "@sid"), SID)
        self.assertEqual([(r["kind"], r["sid"]) for r in records.read(records.path(self.workdir))], [("session", SID)])
        self.assertEqual(tui.read_state("w1")["sid"], SID)
        stream = Stream(handle)
        self.assertEqual([stream.next() for _ in range(4)],
                         [SessionBound("n1"), Activity(""), TurnEnd("r1", 0), api.IdleTimeout(IDLE)])
        self.lines("w1 done")
        self.assertEqual(self.server.option("w1", "@state"), "done")

        handle.send("go")
        self.assertEqual(stream.next(idle=False), Blocked(""))
        self.lines("w1 done", "w1 blocked")
        stream.quiet(3 * IDLE)

        res = subprocess.run([sys.executable, "-I", "-c", ATTACH, SRC, fake_adapter.__file__, json.dumps(self.config),
                              "w1", "more"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=WAIT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), [SID, "live", "r1", repr(TurnEnd("r2", 1)), "r2"])
        self.assertEqual(stream.next(), TurnEnd("r2", 1))
        self.lines("w1 done", "w1 blocked", "w1 done")
        self.assertEqual(handle.reply, "r2")

        handle.stop()
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])
        self.assertEqual(api.sessions("w").state("w1"), "absent")
        self.assertFalse(os.path.lexists(tui.session_dir("w1")))

    def test_a_runtime_exit_ends_with_its_code_the_session_live_until_close(self):
        handle = self.start("w2", {"hook": "stop", "payload": {"last_message": "bye"}}, {"exit": 3})
        stream = Stream(handle)
        self.assertEqual(stream.next(), TurnEnd("bye", 0))
        self.assertEqual(stream.next(poke=self.poke), Exited(3))
        self.lines("w2 done", "w2 dead")
        self.assertEqual((handle.state(), api.sessions().state("w2")), ("live", "live"))
        api.close("w2")
        self.assertEqual((handle.state(), api.sessions().state("w2")), ("absent", "absent"))
        self.assertFalse(os.path.lexists(tui.session_dir("w2")))

    def test_an_unreachable_server_is_unknown_and_no_end(self):
        if os.getuid() == 0:
            self.skipTest("root connects to a mode 000 socket")
        handle = self.start("w3", {"read": True})
        stream = Stream(handle)
        mode = os.stat(self.server.socket).st_mode
        os.chmod(self.server.socket, 0)
        self.addCleanup(os.chmod, self.server.socket, mode)
        found = api.sessions("w")
        self.assertEqual((found.names, found.state("w3"), handle.state()), (None, "unknown", "unknown"))
        stream.quiet(1.0)
        os.chmod(self.server.socket, mode)
        self.assertEqual(api.sessions("w").state("w3"), "live")
        handle.stop()
        self.assertEqual(stream.until_exited(), [Exited(-signal.SIGHUP)])


if __name__ == "__main__":
    unittest.main()
