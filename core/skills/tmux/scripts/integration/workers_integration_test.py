"""workers.py and tui_claude.py run as processes against a private tmux server, fake_claude.py as `claude`: start,
state, events, blocked, dead, restart, early death; the grid (columns, re-tile, name order, sub-workers) and a split
outside it."""
import contextlib
import functools
import json
import os
import re
import signal
import subprocess
import sys
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import workers  # noqa: E402
TESTS = os.path.join(workers.CORE, "src", "tests")
sys.path[:0] = [TESTS, os.path.join(TESTS, "integration")]
import hermetic  # noqa: E402
import fake_claude  # noqa: E402
import live_tmux  # noqa: E402

WORKERS = os.path.join(SCRIPTS, "workers.py")
TUI = os.path.join(workers.CORE, "src", "tui_claude.py")
MANAGER = "mgr"
TIMEOUT = 60   # seconds, per process
BORDER = " #{session_name} #{@state} "
IGNORED = "tui: show: grid: --split/--split-from ignored"


class Live(unittest.TestCase):
    """HOME of the test's own (its ~/.agent-pm: self.agent_pm), a private tmux server with manager session mgr
    started and attached, one events file and one fake log for all workers, the workers' cwd. Commands run as
    processes in mgr's pane unless `inside` names another session (None: no pane, Server.env() alone)."""

    def setUp(self):
        self.agent_pm = hermetic.home(self)
        self.server = live_tmux.Server(self, os.path.dirname(self.agent_pm))
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        root = self.server.root
        self.events, self.log, self.cwd = (os.path.join(root, n) for n in ("events", "log.jsonl", "work"))
        os.mkdir(self.cwd)

    def ok(self, res: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(res.returncode, 0, f"{res.args}\nstdout: {res.stdout}\nstderr: {res.stderr}")
        return res

    def call(self, *argv: str, inside: str | None = MANAGER, **extra: str) -> subprocess.CompletedProcess:
        """argv (argv[0] found on the env's PATH) as a process in `inside`'s pane, env Server.env() plus `extra`."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}), **extra)
        return subprocess.run(list(argv), env=env, cwd=self.cwd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT)

    def workers(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call(sys.executable, WORKERS, *args, **kw)

    def tui(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call(sys.executable, TUI, *args, **kw)

    def kill(self, name: str, **kw) -> subprocess.CompletedProcess:
        """SKILL.md › Direct › Stop's command."""
        return self.call("tmux", "kill-session", "-t", f"={name}", **kw)

    def scenario(self, name: str) -> str:
        return os.path.join(self.server.root, f"{name}.scenario.json")

    def start(self, name: str, *args: str, steps: list | None = None, turns: list | tuple = (),
              inside: str | None = MANAGER) -> subprocess.CompletedProcess:
        """`workers.py start <name> --events … --cwd … --prompt hi args…`, its fake's scenario `steps` (default one
        text, `Hi from <name>.`) and `turns`, logging to self.log."""
        with open(self.scenario(name), "w") as f:
            json.dump({"steps": [f"Hi from {name}."] if steps is None else steps, "turns": list(turns),
                       "log": self.log}, f)
        return self.workers("start", name, "--events", self.events, "--cwd", self.cwd, "--prompt", "hi", *args,
                            inside=inside, **{fake_claude.ENV: self.scenario(name)})

    def started(self, name: str, *args: str, **kw) -> str:
        """start's session id; fails unless it exits 0 printing `<name> <sid>`."""
        res = self.ok(self.start(name, *args, **kw))
        m = re.fullmatch(rf"{re.escape(name)} ({workers.SESSION_ID.pattern})\n", res.stdout)
        self.assertIsNotNone(m, res.stdout)
        return m[1]

    def restart(self, name: str, **kw) -> subprocess.CompletedProcess:
        return self.workers("restart", name, **kw, **{fake_claude.ENV: self.scenario(name)})

    def next_event(self, after: int) -> tuple[int, str, str]:
        """`next-event --after <after>`'s line number, event and content."""
        out = self.ok(self.workers("next-event", "--events", self.events, "--after", str(after))).stdout
        head, _, content = out.partition("\n")
        n, _, event = head.partition(" ")
        return int(n), event, content.removesuffix("\n")

    def wait_event(self, name: str, kind: str, count: int = 1, timeout: float = live_tmux.TIMEOUT,
                   poke=None) -> list[str]:
        """The events file's lines once `count` of them are `HH:MM:SS <name> <kind>`; `poke()` before each look."""
        def check():
            if poke:
                poke()
            lines = live_tmux.events(self.events)
            return lines if sum(is_event(line, name, kind) for line in lines) >= count else None

        try:
            return live_tmux.wait(check, timeout, what=f"{count} `{name} {kind}` events in {self.events}")
        except AssertionError as e:
            raise AssertionError(f"{e}\n{self.diagnose(name)}") from None

    def diagnose(self, name: str) -> str:
        """`name`'s pane state and hooks, the processes of the server and of `name`'s fakes, and the events file: what
        a failed event wait needs."""
        pane = self.server.tmux("display-message", "-p", "-t", f"={name}:",
                                "pid #{pane_pid} dead #{pane_dead} status #{pane_dead_status} "
                                "signal #{pane_dead_signal} @state #{@state}")
        hooks = self.server.tmux("show-hooks", "-p", "-t", f"={name}:")
        server = self.server.tmux("display-message", "-p", "#{pid}").stdout.strip()
        pids = [server, *(str(e["pid"]) for e in self.calls(name))]
        ps = subprocess.run(["ps", "-o", "pid,ppid,stat,comm", "-p", ",".join(pids)], capture_output=True, text=True)
        return "\n".join([f"pane: {pane.stdout.strip()}{pane.stderr.strip()}",
                          f"hooks: {hooks.stdout.strip()}{hooks.stderr.strip()}",
                          f"server {server}, fakes {pids[1:]}:", ps.stdout.strip(),
                          "events:", *live_tmux.events(self.events)])

    def calls(self, name: str) -> list[dict]:
        """The fake's log entries for worker `name` (argv has `--name <name>`), oldest first."""
        try:
            with open(self.log) as f:
                entries = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [e for e in entries if has_pair(e["argv"], "--name", name)]

    def display(self, name: str, fmt: str) -> str:
        """tmux's display-message `fmt` for `name`'s pane."""
        return self.ok(self.server.tmux("display-message", "-p", "-t", f"={name}:", fmt)).stdout.removesuffix("\n")

    def pane_text(self, name: str, lines: int) -> str:
        """`name`'s pane: its last `lines` lines, history included, trailing blank lines dropped."""
        text = self.ok(self.server.tmux("capture-pane", "-p", "-J", "-t", f"={name}:", "-S", f"-{lines}")).stdout
        rows = text.splitlines()
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(rows[-lines:])

    def window_option(self, name: str, key: str) -> str:
        """Window option `key` of `name`'s window; "" when unset."""
        return self.ok(self.server.tmux("show-options", "-wqv", "-t", f"={name}:", key)).stdout.removesuffix("\n")

    def grid(self, columns: list, manager_width: int | None = None) -> None:
        """Fails unless mgr's window is the grid `columns` (live_tmux.assert_grid) within wait's timeout."""
        check = functools.partial(live_tmux.assert_grid, self, self.server, MANAGER, columns, manager_width)
        with contextlib.suppress(AssertionError):   # on timeout, check's own message below rather than wait's repr
            live_tmux.wait(check)
        check()

    def per_column(self, n: int) -> None:
        """Core config's workers_per_column, in this test's ~/.agent-pm/core.local.toml."""
        with open(os.path.join(self.agent_pm, "core.local.toml"), "w") as f:
            f.write(f"workers_per_column = {n}\n")


def is_event(line: str, name: str, kind: str) -> bool:
    return bool(workers.EVENT.fullmatch(line)) and line.split(" ")[1:] == [name, kind]


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    """argv has `flag value`, adjacent."""
    return any(argv[i:i + 2] == [flag, value] for i in range(len(argv)))


class Lifecycle(Live):
    def test_start_state_events(self):
        sid = self.started("w1", steps=["Hi from w1."], turns=[["Second."]])
        self.ok(self.server.tmux("has-session", "-t", "=w1"))
        claude = os.path.join(self.server.root, "bin", "claude")
        self.assertEqual({k: self.server.option("w1", f"@{k}") for k in ("sid", "cwd", "claude", "flags", "events")},
                         {"sid": sid, "cwd": self.cwd, "claude": claude, "flags": "[]", "events": self.events})
        (call,) = self.calls("w1")
        argv = call["argv"]
        self.assertEqual(argv[:1] + argv[2:], ["--settings", "--session-id", sid, "--name", "w1", "--", "hi"])
        self.assertEqual(json.loads(argv[1]), json.loads(workers.tui_claude.hooks(self.events)))
        (done,) = self.wait_event("w1", "done")
        self.assertEqual(self.server.option("w1", "@state"), "done")
        self.assertEqual(self.next_event(0), (1, done, "Hi from w1."))
        self.ok(self.tui("send", "w1", "more"))
        n, event, content = self.next_event(1)
        self.assertEqual((n, content), (2, "Second."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(live_tmux.events(self.events), [done, event])
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Second.\n")
        self.assertEqual(self.window_option("w1", "pane-border-format"), BORDER)

    def test_blocked(self):
        # the dialog's text: a hook step prints nothing, so the pane, next-event's content, would be blank
        self.started("w1", steps=["Allow this edit?", {"kind": "hook", "event": "PermissionRequest"}])
        lines = self.wait_event("w1", "done")
        self.assertEqual([line.split(" ")[1:] for line in lines], [["w1", "blocked"], ["w1", "done"]])
        n, event, content = self.next_event(0)
        self.assertEqual((n, event), (1, lines[0]))
        self.assertTrue(content)
        self.assertEqual(content, self.pane_text("w1", workers.PANE_LINES))

    def test_dead_restart(self):
        sid = self.started("w1")
        self.wait_event("w1", "done")
        pane = self.server.pane_of("w1")
        cell = self.server.panes(f"={MANAGER}:")[pane]
        os.kill(self.calls("w1")[-1]["pid"], signal.SIGTERM)
        live_tmux.wait(lambda: self.display("w1", "#{pane_dead}") == "1", what="w1's pane dead")
        # tmux on Linux (CI: 3.4) can leave the dead pane's process unreaped, so no pane-died, until another child of
        # the server exits: a no-op job is one.
        _, dead = self.wait_event("w1", "dead", poke=lambda: self.server.tmux("run-shell", "-b", "true"))
        self.assertTrue(is_event(dead, "w1", "dead"), dead)
        self.assertEqual(self.server.option("w1", "@state"), "dead")
        state, sig = self.display("w1", "#{pane_dead} #{pane_dead_signal}").split(" ")
        self.assertEqual(state, "1")
        self.assertTrue(sig == str(int(signal.SIGTERM)) or sig.upper() == "TERM", sig)
        n, event, content = self.next_event(1)
        self.assertEqual((n, event), (2, dead))
        self.assertTrue(content)
        self.assertEqual(content, self.pane_text("w1", workers.PANE_LINES))
        self.ok(self.restart("w1"))
        _, again = self.calls("w1")
        self.assertTrue(has_pair(again["argv"], "--resume", sid), again["argv"])
        self.assertNotIn("--session-id", again["argv"])
        self.assertEqual(self.display("w1", "#{pane_dead}"), "0")
        self.assertEqual(self.server.option("w1", "@state"), "")
        self.assertEqual(self.server.pane_of("w1"), pane)
        self.assertEqual(self.server.panes(f"={MANAGER}:")[pane], cell)

    def test_early_death(self):
        res = self.start("w2", "--", "--bogus")
        self.assertEqual(res.returncode, 1, res.stderr)
        lines = res.stderr.splitlines()
        head = "workers: w2: claude exited 2 at once; its pane's last lines:"
        self.assertIn(head, lines, res.stderr)
        at = lines.index(head)
        # before workers.py's error, only the show's `tui: ` lines (its attach command)
        self.assertTrue(at > 0 and all(line.startswith("tui: ") for line in lines[:at]), res.stderr)
        self.assertIn("claude: error: unrecognized arguments: --bogus", lines[at + 1:])
        self.ok(self.server.tmux("has-session", "-t", "=w2"))
        self.assertEqual(self.display("w2", "#{pane_dead}"), "1")


class Grid(Live):
    """Placement rules: core/skills/tmux/SKILL.md › Start 3."""

    def test_columns_retile(self):
        self.per_column(2)
        # tmux's split leaves the split pane the wider half (100 of 200); tile keeps the manager's width
        width = self.server.size[0] // 2
        self.started("w1")
        self.grid([["w1"]], width)
        self.assertEqual(self.window_option(MANAGER, "@grid-per-column"), "2")
        res = self.ok(self.start("w2", "--split", "below"))
        self.assertIn(IGNORED, res.stderr.splitlines())
        self.grid([["w1", "w2"]], width)
        self.started("w3")
        self.grid([["w1", "w2"], ["w3"]], width)
        self.ok(self.kill("w2"))
        self.grid([["w1", "w3"]], width)

    def test_name_order(self):
        self.per_column(2)
        for name, columns in (
                ("pm-TASK-10", [["pm-TASK-10"]]),
                ("engineer-TASK-2", [["engineer-TASK-2", "pm-TASK-10"]]),
                ("pm-TASK-9", [["engineer-TASK-2", "pm-TASK-9"], ["pm-TASK-10"]]),
                ("engineer-TASK-11", [["engineer-TASK-2", "engineer-TASK-11"], ["pm-TASK-9", "pm-TASK-10"]])):
            self.started(name)
            self.grid(columns)

    def test_sub_workers(self):
        for i, name in enumerate(("m1", "m2", "m3"), 1):
            self.started(name)
            self.grid([[f"m{k}" for k in range(1, i + 1)]])
        self.started("z1", inside="m2")
        self.grid([["m1", ("m2", ["z1"]), "m3"]])
        self.started("z2", inside="m2")
        self.grid([["m1", ("m2", ["z1", "z2"]), "m3"]])
        self.ok(self.kill("z1"))
        self.ok(self.kill("z2"))
        self.grid([["m1", "m2", "m3"]])
        self.started("z3", inside="m2")
        self.grid([["m1", ("m2", ["z3"]), "m3"]])
        self.ok(self.kill("m2"))
        self.grid([["m1", ["z3"], "m3"]])


class Splits(Live):
    def test_split_outside_grid(self):
        manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.started("w1", "--split-from", MANAGER, "--split", "below", inside=None)
        pane = self.server.pane_of("w1")
        panes = self.server.panes(f"={MANAGER}:")
        self.assertIn(pane, panes)
        (mx, my, _, _), (x, y, _, _) = panes[manager], panes[pane]
        self.assertEqual(x, mx)
        self.assertGreater(y, my)
        self.assertEqual([self.window_option(MANAGER, k) for k in ("@grid-manager", "@grid-per-column")], ["", ""])


if __name__ == "__main__":
    unittest.main()
