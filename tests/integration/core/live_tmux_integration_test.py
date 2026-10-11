"""live_tmux.py against a real tmux server: its env, control-mode client, readers, cleanup, grid assertion and wait."""
import os
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(TESTS), TESTS]
import hermetic  # noqa: E402
import live_tmux  # noqa: E402


class Live(unittest.TestCase):
    def setUp(self):
        self.home = os.path.dirname(hermetic.home(self))
        self.server = live_tmux.Server(self, self.home)

    def ok(self, *args) -> str:
        res = self.server.tmux(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.strip()


class Env(Live):
    def test_env_is_built_from_scratch(self):
        leaks = {"TMUX": "/nonexistent/default,1,0", "TMUX_PANE": "%0", "ITERM_SESSION_ID": "w0t0p0:X",
                 "TERM_PROGRAM": "iTerm.app"}
        with mock.patch.dict(os.environ, leaks):
            env = self.server.env(EXTRA="x")
        root = self.server.root
        tmux = os.path.dirname(shutil.which("tmux"))
        self.assertEqual(env, {"PATH": f"{root}/bin:{tmux}:/usr/bin:/bin", "HOME": self.home, "PYTHONUTF8": "1",
                               "TMUX_TMPDIR": root, "EXTRA": "x"})
        self.assertEqual(shutil.which("claude", path=env["PATH"]), os.path.join(root, "bin", "claude"))

    def test_tmpdir_and_socket_reach_one_server(self):
        sock = self.server.socket
        self.assertEqual(sock, os.path.join(self.server.root, f"tmux-{os.getuid()}", "default"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(sock)).st_mode), 0o700)
        self.server.start("mgr")
        res = subprocess.run(["tmux", "ls", "-F", "#{session_name}"], env=self.server.env(), capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual((res.returncode, res.stdout), (0, "mgr\n"), res.stderr)
        self.assertEqual(self.ok("display-message", "-p", "-t", "=mgr:",
                                 "#{socket_path} #{window_width}x#{window_height} #{pane_current_command}"),
                         f"{sock} 200x50 cat")

    def test_inside_runs_a_process_in_the_session_pane(self):
        self.server.start("mgr")
        inside = self.server.inside("mgr")
        pid, pane = self.ok("display-message", "-p", "-t", "=mgr:", "#{pid} #{pane_id}").split()
        self.assertEqual(inside, {"TMUX": f"{self.server.socket},{pid},0", "TMUX_PANE": pane})
        res = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
                             env=self.server.env(**inside), capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(res.stdout, "mgr\n", res.stderr)


class Client(Live):
    def test_attach_shows_the_session_in_a_control_mode_client(self):
        self.server.start("mgr")
        self.server.attach("mgr")
        (row,) = self.ok("list-clients", "-t", "=mgr", "-F", "#{client_control_mode} #{client_tty}").splitlines()
        control, tty = row.split(" ", 1)
        self.assertEqual(control, "1")
        self.assertTrue(tty.startswith("/dev/"), tty)
        self.assertEqual(self.ok("display-message", "-p", "-t", "=mgr:", "#{window_width}x#{window_height}"),
                         "200x50")


class Readers(Live):
    def test_option_pane_of_panes_events(self):
        self.server.start("mgr")
        pane = self.ok("display-message", "-p", "-t", "=mgr:", "#{pane_id}")
        self.assertEqual(self.server.panes("=mgr:"), {pane: (0, 0, 200, 50)})
        self.assertEqual((self.server.option("mgr", "@state"), self.server.pane_of("mgr")), ("", ""))
        self.ok("set-option", "-t", "=mgr:", "@state", "done")
        self.ok("set-option", "-t", "=mgr:", "@pane", pane)
        self.assertEqual((self.server.option("mgr", "@state"), self.server.pane_of("mgr")), ("done", pane))
        path = os.path.join(self.server.root, "events")
        self.assertEqual(live_tmux.events(path), [])
        with open(path, "w") as f:
            f.write("12:00:00 w1 done\n12:00:01 w1 dead\n")
        self.assertEqual(live_tmux.events(path), ["12:00:00 w1 done", "12:00:01 w1 dead"])


class Cleanup(unittest.TestCase):
    def test_cleanups_kill_the_server_and_client_and_remove_the_root(self):
        home = os.path.dirname(hermetic.home(self))
        seen = {}

        def body():
            server = live_tmux.Server(nested, home)
            server.start("mgr")
            server.attach("mgr")
            seen.update(server=server, pid=int(server.inside("mgr")["TMUX"].split(",")[1]))

        nested = unittest.FunctionTestCase(body)
        result = unittest.TestResult()
        nested.run(result)
        self.assertEqual(result.errors + result.failures, [])
        server = seen["server"]
        self.assertFalse(os.path.exists(server.root))
        with self.assertRaises(ProcessLookupError):
            os.kill(seen["pid"], 0)
        (client,) = server.clients
        self.assertIsNotNone(client.proc.returncode)
        self.assertFalse(client.thread.is_alive())
        with self.assertRaises(OSError):
            os.fstat(client.master)


class Grid(Live):
    def split(self, pane: str, flag: str, size: int) -> str:
        """The new pane, `size` wide (-h, right of `pane`) or high (-v, below)."""
        return self.ok("split-window", "-d", flag, "-l", str(size), "-t", pane, "-P", "-F", "#{pane_id}",
                       *live_tmux.IDLE)

    def test_assert_grid_on_a_hand_built_window(self):
        self.server.start("mgr")
        manager = self.ok("display-message", "-p", "-t", "=mgr:", "#{pane_id}")
        self.ok("set-option", "-w", "-t", "=mgr:", "@grid-manager", manager)
        # 200x50: manager 99 wide; column 1 (x 100, 50 wide): w1 over s1, s2; column 2 (x 151, 49 wide): p, z1 over z2
        right = self.split(manager, "-h", 100)
        col2 = self.split(right, "-h", 49)
        group = self.split(right, "-v", 25)
        panes = {"w1": right, "s1": group, "s2": self.split(group, "-v", 12), "p": col2}
        panes["z1"] = self.split(col2, "-h", 24)
        panes["z2"] = self.split(panes["z1"], "-v", 25)
        for name, pane in panes.items():
            self.ok("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.ok("set-option", "-t", f"={name}:", "@pane", pane)
        grid = [["w1", ["s1", "s2"]], [("p", ["z1", "z2"])]]
        self.assertTrue(live_tmux.assert_grid(self, self.server, "mgr", grid))
        self.assertTrue(live_tmux.assert_grid(self, self.server, "mgr", grid, manager_width=99))
        wrong = {"column order": ([grid[1], grid[0]], None),
                 "cell order": ([[["s1", "s2"], "w1"], grid[1]], None),
                 "orphan order": ([["w1", ["s2", "s1"]], grid[1]], None),
                 "sub order": ([grid[0], [("p", ["z2", "z1"])]], None),
                 "other panes": ([grid[0], ["p"]], None),
                 "manager width": (grid, 100)}
        for why, (columns, width) in wrong.items():
            with self.subTest(why), self.assertRaises(AssertionError):
                live_tmux.assert_grid(self, self.server, "mgr", columns, manager_width=width)


class Wait(unittest.TestCase):
    def test_returns_the_truthy_value(self):
        values = iter([0, "", None, "yes"])
        self.assertEqual(live_tmux.wait(lambda: next(values)), "yes")

    def test_a_failed_assertion_counts_as_falsy(self):
        values = iter([AssertionError("not yet"), 7])

        def check():
            value = next(values)
            if isinstance(value, AssertionError):
                raise value
            return value

        self.assertEqual(live_tmux.wait(check), 7)

    def test_times_out_with_what_and_the_last_value(self):
        with self.assertRaises(AssertionError) as cm:
            live_tmux.wait(lambda: [], timeout=0.2, what="the moon to rise")
        self.assertIn("the moon to rise", str(cm.exception))
        self.assertIn("[]", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
