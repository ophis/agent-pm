"""HostContract (FR-47) against PtyHost; then what only PtyHost does: its session dir and output,
a bracketed paste once the program asks for one, its tui hook's marks and reclaim.
"""

import os
import stat
import unittest

from agent_pm.session import ConfigError
from agent_pm.session.engine.types import HookCall, Key, Launch, Paste
from agent_pm.session.hosts.pty import PtyHost
from agent_pm.session.hosts.pty.hook import on_hook
from agent_pm.session.hosts.pty.host import TERM
from agent_pm.utils import StatusState

from session.tests.case import Case
from session.tests.contracts.host import PUT, SLEEP, HostContract, gone, program, read

SETTINGS = {"outer_terminal": ["TMUX", "TMUX_PANE"]}
BRACKETED = (
    PUT + "import tty\n"
    "tty.setraw(0)\n"
    "os.write(1, b'\\x1b[?2004h')\n"
    "put('ready')\n"
    "seen = b''\n"
    "while not seen.endswith(b'\\x1b[201~'):\n"
    "    seen += os.read(0, 64)\n"
    "put(seen.hex())\n"
)
GATED = (
    "import os, sys, time\n"
    "print('hello pty', flush=True)\n"
    "while not os.path.exists(sys.argv[1]):\n"
    "    time.sleep(0.01)\n"
)
LEAVES = (
    PUT + "child = subprocess.Popen([sys.executable, '-c', "
    "'import signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(30)'])\n"
    "put(str(child.pid))\n"
)


class PtyHostTest(HostContract, Case):
    def make_host(self):
        return PtyHost("pty", SETTINGS)

    def assert_terminal_identity(self, env):
        self.assertEqual(env.get("TERM"), TERM)
        for key in SETTINGS["outer_terminal"]:
            self.assertNotIn(key, env)

    def folder(self, name: str) -> str:
        return os.path.join(self.home, ".agent-pm", "pty", name)

    def test_a_session_lives_in_its_dir_whose_control_is_its_input_socket(self):
        name = self.fresh()
        self.start("terminal", name, SLEEP)
        folder = self.folder(name)
        self.assertLessEqual({"meta.json", "pid", "output", "control"}, set(os.listdir(folder)))
        self.assertTrue(stat.S_ISSOCK(os.stat(os.path.join(folder, "control")).st_mode))
        self.assertFalse(gone(int(read(os.path.join(folder, "pid")) or "0")))
        self.host.attach(name).close()
        self.assertFalse(os.path.exists(folder))

    def test_a_detached_programs_output_is_kept_until_it_exits_and_its_dir_goes(self):
        name, gate = self.fresh(), self.file("gate")
        self.start("detach", name, GATED, gate)
        output = os.path.join(self.folder(name), "output")
        self.poll(lambda: "hello pty" in (read(output) or ""), f"its output in {output}")
        with open(gate, "w", encoding="utf-8"):
            pass
        self.poll(lambda: not os.path.exists(self.folder(name)), f"{name}'s dir removed")

    def test_a_paste_is_bracketed_once_the_program_asks_for_bracketed_paste(self):
        name, out = self.fresh(), self.file("pasted")
        terminal = self.start("terminal", name, BRACKETED, out)
        self.poll(lambda: read(out) == "ready", f"ready in {out}")
        output = os.path.join(self.folder(name), "output")
        self.poll(lambda: "\x1b[?2004h" in (read(output) or ""), "its bracketed paste request")
        terminal.send(Paste("two\nlines"))
        self.poll(lambda: read(out) == b"\x1b[200~two\nlines\x1b[201~".hex(), f"paste in {out}")

    def test_a_key_outside_the_notation_is_refused_and_the_terminal_takes_the_next(self):
        name, out = self.fresh(), self.file("line")
        terminal = self.start("terminal", name, PUT + "put(sys.stdin.readline())", out)
        with self.assertRaises(ConfigError):
            terminal.send(Key("Nope"))
        terminal.send(Paste("still"))
        terminal.send(Key("Enter"))
        self.poll(lambda: (read(out) or "").rstrip("\r\n") == "still", f"line in {out}")

    def test_the_tui_hook_marks_only_the_launch_it_was_installed_for(self):
        name, events = self.fresh(), self.file("events")
        metadata = {"sid": "s-1", "client": "fake", "generation": "g-1"}
        self.host.terminal(
            name, Launch(tuple(program(SLEEP)), cwd=self.home, env=self.env()), metadata=metadata
        )
        callback = self.host.hook_callback(name, metadata)
        self.assertEqual(callback.module, "agent_pm.session.hosts.pty.hook")

        def hooked(word, sid, generation):
            state = {**callback.state, "generation": generation}
            on_hook(word, HookCall("Stop", {}, sid, self.home, events, state))

        hooked(StatusState.DONE, "s-1", "g-2")
        hooked(StatusState.DONE, "s-2", "g-1")
        self.assertIsNone(self.host.attach(name).activity())
        hooked(StatusState.BLOCKED, "s-1", "g-1")
        self.assertEqual(self.host.attach(name).activity(), StatusState.BLOCKED)
        self.event(events, name, "blocked")
        hooked(StatusState.WORKING, "s-1", "g-1")
        self.assertEqual(self.host.attach(name).activity(), StatusState.WORKING)
        self.assertEqual(len((read(events) or "").splitlines()), 1)
        (row,) = self.host.rows()
        self.assertEqual(
            (row.name, row.sid, row.client, row.state), (name, "s-1", "fake", "working")
        )

    def test_reclaim_holds_a_live_group_by_its_token_after_its_session_went(self):
        name, pids = self.fresh(), self.file("pids")
        process = self.start("detach", name, LEAVES, pids)
        self.addCleanup(process.kill)
        token = process.token
        self.assertIsNotNone(token)
        child = int(self.poll(lambda: read(pids), f"pid in {pids}"))
        self.poll(lambda: name not in self.host.list(), f"{name} gone from the list")
        group = self.host.reclaim(token or "")
        self.assertIsNotNone(group)
        if group is not None:
            group.kill()
        self.poll(lambda: gone(child), f"end of {child}")
        self.assertIsNone(self.host.reclaim(token or ""))
        for bad in ("", "/etc", "/tmp/apm-group-nope"):
            with self.subTest(bad=bad):
                self.assertIsNone(self.host.reclaim(bad))


if __name__ == "__main__":
    unittest.main()
