"""HostContract, FR-47's Host contract: a subclass also inherits Case and defines make_host() ->
Host. Each case runs short Python programs written against the Host ABC only, polling files under
self.home for events and output.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from unittest import mock

from agent_pm.session import Displayable, DriverError, Host
from agent_pm.session.engine.types import Key, Paste
from agent_pm.utils import Record

from session.tests.case import PY, REPO, child_env

OTHER_DISPLAY = "elsewhere:1.2"
DEADLINE = 10
POLL = 0.05
PUT = (
    "import json, os, subprocess, sys, time\n"
    "def put(text):\n"
    "    with open(sys.argv[1] + '.tmp', 'w') as f:\n"
    "        f.write(text)\n"
    "    os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
)
EXIT_7 = "import sys; sys.exit(7)"
SLEEP = "import time; time.sleep(30)"
READ_LINE = PUT + "put(sys.stdin.readline())"
DUMP_ENV = PUT + "put(json.dumps(dict(os.environ)))"
SPAWN = (
    PUT + "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "put(f'{os.getpid()} {child.pid}')\n"
    "time.sleep(30)"
)
CHILD = """import json, sys
from agent_pm.session import Host
from agent_pm.utils import load_class
path, host, settings, name, argv, cwd, env = sys.argv[1:]
load_class(path, Host)(host, json.loads(settings)).terminal(
    name, json.loads(argv), cwd, json.loads(env)
)
"""


def program(code: str, *args: str) -> list[str]:
    return [sys.executable, "-I", "-c", code, *args]


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class HostContract:
    """TASK-365 (356-4) adds FR-47's cannot-display case (Session.displayable, NotDisplayable) with
    HostedSession.
    """

    def make_host(self) -> Host:
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.host = self.make_host()

    def fresh(self) -> str:
        name = f"hc-{uuid.uuid4().hex[:12]}"
        self.addCleanup(self.forget, name)
        return name

    def forget(self, name: str) -> None:
        with contextlib.suppress(DriverError):
            self.host.attach(name).close()

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home, **extra}

    def start(self, kind: str, name: str, code: str, *args: str, **kw):
        return getattr(self.host, kind)(name, program(code, *args), self.home, self.env(), **kw)

    def file(self, label: str) -> str:
        return os.path.join(self.home, f"{label}-{uuid.uuid4().hex[:8]}")

    def poll(self, check, what: str):
        deadline = time.monotonic() + DEADLINE
        while not (found := check()):
            if time.monotonic() > deadline:
                self.fail(f"no {what} within {DEADLINE} s")
            time.sleep(POLL)
        return found

    def event(self, events: str, name: str, tail: str) -> None:
        line = re.compile(rf"\d\d:\d\d:\d\d {re.escape(name)} {tail}")
        self.poll(
            lambda: any(line.fullmatch(each) for each in (read(events) or "").splitlines()),
            f"event {name} {tail} in {events}",
        )

    def a_record(self, name: str) -> Record:
        return Record(
            1, "fake", name, (name,), None, None, ("fake-ctl",), ("fake-ctl", "x"), None, None
        )

    def test_detach_writes_exited_and_the_returncode_when_its_program_exits(self):
        name, events = self.fresh(), self.file("events")
        process = self.start("detach", name, EXIT_7, events=events)
        self.event(events, name, "exited 7")
        self.assertTrue(process.wait_exit(DEADLINE))
        self.assertFalse(process.alive())

    def test_a_terminal_writes_dead_when_its_program_exits(self):
        name, events = self.fresh(), self.file("events")
        self.start("terminal", name, "pass", events=events)
        self.event(events, name, "dead")

    def test_a_paste_and_the_enter_key_reach_the_terminal_programs_stdin_as_one_line(self):
        name, out = self.fresh(), self.file("line")
        terminal = self.start("terminal", name, READ_LINE, out)
        terminal.send(Paste("hello host"))
        terminal.send(Key("Enter"))
        self.poll(lambda: (read(out) or "").rstrip("\r\n") == "hello host", f"line in {out}")

    def test_the_program_gets_the_env_given_verbatim_and_nothing_of_its_creators(self):
        name, out = self.fresh(), self.file("env")
        env = self.env(HOST_CONTRACT_VALUE="a b 'c' \"$HOME\" ;*")
        with mock.patch.dict(os.environ, {"HOST_CONTRACT_CREATOR_ONLY": "1"}):
            self.host.detach(name, program(DUMP_ENV, out), self.home, env)
        got = json.loads(self.poll(lambda: read(out), f"env in {out}"))
        self.assertEqual({k: got.get(k) for k in env}, env)
        self.assertNotIn("HOST_CONTRACT_CREATOR_ONLY", got)

    def test_a_foreign_display_is_kept_and_listed_only_under_itself(self):
        name = self.fresh()
        self.start("detach", name, SLEEP, display=OTHER_DISPLAY)
        self.assertIn(name, self.host.list(OTHER_DISPLAY))
        self.assertEqual(self.host.attach(name).display(), OTHER_DISPLAY)
        for display in ("other", ""):
            with self.subTest(display=display):
                self.assertNotIn(name, self.host.list(display))

    def test_list_without_a_display_names_every_session_sorted(self):
        first, second = self.fresh(), self.fresh()
        self.start("detach", first, SLEEP)
        self.start("terminal", second, SLEEP, display=OTHER_DISPLAY)
        names = self.host.list()
        self.assertLessEqual({first, second}, set(names))
        self.assertEqual(names, sorted(names))

    def test_attach_returns_the_record_given_verbatim(self):
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind):
                name = self.fresh()
                self.start(kind, name, SLEEP, record=self.a_record(name))
                self.assertEqual(self.host.attach(name).record(), self.a_record(name))
        name = self.fresh()
        self.start("detach", name, SLEEP)
        self.assertIsNone(self.host.attach(name).record())

    def test_kill_ends_the_program_and_its_child_process(self):
        name, pids = self.fresh(), self.file("pids")
        process = self.start("detach", name, SPAWN, pids)
        own, child = map(int, self.poll(lambda: read(pids), f"pids in {pids}").split())
        self.assertFalse(process.wait_exit(0.2))
        process.kill()
        self.poll(lambda: gone(own) and gone(child), f"end of {own} and {child}")
        self.assertFalse(process.alive())

    def test_close_twice_is_fine_and_forgets_the_session(self):
        name = self.fresh()
        process = self.start("terminal", name, SLEEP)
        process.close()
        process.close()
        self.assertFalse(process.alive())
        self.assertNotIn(name, self.host.list())
        with self.assertRaises(DriverError):
            self.host.attach(name)

    def test_attach_of_an_unknown_name_raises_driver_error(self):
        with self.assertRaises(DriverError):
            self.host.attach(f"nope-{uuid.uuid4().hex[:12]}")

    def test_a_live_name_is_refused(self):
        name = self.fresh()
        process = self.start("detach", name, SLEEP)
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind), self.assertRaises(DriverError):
                self.start(kind, name, SLEEP)
        self.assertTrue(process.alive())

    def test_a_terminal_started_by_another_process_is_alive_here_and_takes_a_paste(self):
        name, out = self.fresh(), self.file("line")
        cls = type(self.host)
        done = subprocess.run(
            [
                *PY,
                "-c",
                CHILD,
                f"{cls.__module__}:{cls.__qualname__}",
                self.host.name,
                json.dumps(self.host.settings),
                name,
                json.dumps(program(READ_LINE, out)),
                self.home,
                json.dumps(self.env()),
            ],
            env=child_env(None, REPO),
            capture_output=True,
            text=True,
            timeout=DEADLINE * 3,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        terminal = self.host.attach(name)
        self.assertTrue(terminal.alive())
        terminal.send(Paste("from afar"))
        terminal.send(Key("Enter"))
        self.poll(lambda: (read(out) or "").rstrip("\r\n") == "from afar", f"line in {out}")

    def test_a_host_that_cannot_display_returns_each_display_as_given(self):
        if isinstance(self.host, Displayable):
            self.skipTest("a Displayable Host's display cases: TASK-368 (356-8)")
        for value in (None, "", OTHER_DISPLAY):
            with self.subTest(value=value):
                self.assertEqual(self.host.display(value), value)
