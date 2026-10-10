"""workers.py and tui_claude.py run as processes against a private tmux server, fake_claude.py as `claude`: start,
state, events, blocked, dead, restart, early death; the manager directory the events file defaults to; the grid
(columns, re-tile, name order, sub-workers, a state needing two passes settling, concurrent tiles, the mute, the
breaker), its grid_retile modes (open-close: a drag, a window resize and a restart hold, an open and a stop re-tile;
off: placement only, then tile @N; a switch to open-close) and a split outside it; the roster (two managers' lease,
concurrent attaches, a killed worker and a killed role run gone, then back by their recovery commands, `attach --resume`
bringing back a killed worker but not a stopped one, and a worker, a role run and a pipeline run (STAND_IN) after the
tmux server is lost, a role run stopped by its tui session, a role run ended on needs_input waiting, which
`attach --resume` leaves alone, an unshown worker reopened, a take-over resuming from the event cursor, events rotation)."""
import contextlib
import functools
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS)
import workers  # noqa: E402
TESTS = os.path.join(workers.CORE, "src", "tests")
sys.path[:0] = [TESTS, os.path.join(TESTS, "integration")]
import hermetic  # noqa: E402
import fake_claude  # noqa: E402
import live_tmux  # noqa: E402
tui_claude = workers.tui_claude
manager = workers.manager

WORKERS = os.path.join(SCRIPTS, "workers.py")
TUI = os.path.join(workers.CORE, "src", "tui_claude.py")
DRIVE = os.path.join(workers.CORE, "src", "drive.py")
ROLE = "dummy-tester"
ROLE_SID = "26700000-0000-4000-8000-000000000005"
ROLE_TUI = f"{ROLE}-{ROLE_SID[:8]}"
ROLE_DRIVER = f"{ROLE_TUI}-drive"
MANAGER = "mgr"
SECOND = "mgr2"
TIMEOUT = 60   # seconds, per process
BORDER = " #{session_name} #{@state} "
IGNORED = "tui: show: grid: --split/--split-from ignored"
STEADY = 1   # seconds a settled layout must hold
BREAKER_TIMEOUT = 30   # seconds a forced re-tile loop may run before the breaker must have stopped it
RETILE_WAIT = 0.5   # seconds the loop waits for a re-tile of its hand layout
PACE = 0.02   # seconds between the loop's looks
WIDTH = 64   # bytes of a filler event line, its newline included
PIPE_ISSUE = "TASK-1"
PIPE_SID = "26800000-0000-4000-8000-000000000007"
PIPE_DRIVER = f"agent-pm-{ROLE}-{PIPE_ISSUE}"
PIPE_TUI = f"{ROLE}-{PIPE_ISSUE}-{PIPE_SID[:8]}"
STAND_IN = '''"""Stands in for orchestrator/src/router.py --issue ID --tui --events FILE --manager NAME, which needs Linear:
what it does to tmux and the roster. Its driver session live: router.py's refusal. Else drive.detach starts driver session
agent-pm-<role>-<ID> and writes its pipeline entry, as router.outer does; the driver runs drive.py --runner tui (router.py's
runs run.py, whose drive.start this is) as session SID, resumed once the cwd has a run.jsonl (router.py: the issue is In
Progress)."""
import os
import subprocess
import sys

sys.path.insert(0, {src!r})
import drive  # noqa: E402
import manager  # noqa: E402
import tui_claude  # noqa: E402

ROLE, SID, DRIVE = {role!r}, {sid!r}, {drive!r}
args = sys.argv[1:]
issue, events, name = (args[args.index(flag) + 1] for flag in ("--issue", "--events", "--manager"))
driver, prefix, cwd = f"agent-pm-{{ROLE}}-{{issue}}", f"{{ROLE}}-{{issue}}", os.getcwd()
if subprocess.run(["tmux", "has-session", "-t", f"={{driver}}"], capture_output=True).returncode == 0:
    sys.exit(f"router.py: {{issue}} has a live agent run: {{tui_claude.attach_command(driver)}}")
opener = drive.caller_layout(None, None).opener
argv = [sys.executable, DRIVE, f"--role={{ROLE}}", "--input=Hello.", "--out=out.md", "--workdir=.", f"--sid={{SID}}",
        "--runner=tui", f"--prefix={{prefix}}", f"--events={{events}}", f"--opener={{opener}}", f"--driver={{driver}}",
        *(["--resume"] if os.path.exists("run.jsonl") else [])]
drive.detach(driver, argv, cwd=cwd, env=os.environ, iterm="", roster=(manager.directory(name), {{
    "kind": "pipeline", "sid": SID, "cwd": cwd, "note": issue, "tui": f"{{prefix}}-{{SID[:8]}}", "opener": opener,
    "resume": [sys.executable, os.path.abspath(__file__), *args]}}))
'''


class Live(unittest.TestCase):
    """HOME of the test's own (its ~/.agent-pm: self.agent_pm), a private tmux server with manager session mgr
    started and shown in a client, one fake log for all workers, the workers' cwd. self.events is mgr's events file, in
    mgr's manager directory (core/src/manager.py), which mgr attaches to (`workers.py attach`) unless ATTACH is False.
    Commands run as processes in mgr's pane unless `inside` names another session (None: no pane, Server.env()
    alone)."""
    ATTACH = True

    def setUp(self):
        self.agent_pm = hermetic.home(self)
        self.server = live_tmux.Server(self, os.path.dirname(self.agent_pm))
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        root = self.server.root
        self.events = self.events_of(MANAGER)
        self.log, self.cwd = (os.path.join(root, n) for n in ("log.jsonl", "work"))
        os.mkdir(self.cwd)
        self.local = {}
        if self.ATTACH:
            self.attach()

    def events_of(self, manager: str) -> str:
        return os.path.join(self.agent_pm, "managers", manager, "events")

    def ok(self, res: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(res.returncode, 0, f"{res.args}\nstdout: {res.stdout}\nstderr: {res.stderr}")
        return res

    def call(self, *argv: str, inside: str | None = MANAGER, cwd: str | None = None,
             **extra: str) -> subprocess.CompletedProcess:
        """argv (argv[0] found on the env's PATH) as a process in `inside`'s pane, in `cwd` (default self.cwd), env
        Server.env() plus `extra`."""
        env = self.server.env(**(self.server.inside(inside) if inside else {}), **extra)
        return subprocess.run(list(argv), env=env, cwd=cwd or self.cwd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT)

    def workers(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call(sys.executable, WORKERS, *args, **kw)

    def attach(self, *args: str, **kw) -> subprocess.CompletedProcess:
        """`workers.py attach args…`, which start and next-event need first; fails unless it exits 0."""
        return self.ok(self.workers("attach", *args, **kw))

    def tui(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call(sys.executable, TUI, *args, **kw)

    def kill(self, name: str, **kw) -> subprocess.CompletedProcess:
        """`tmux kill-session` of `name`, as a user's by hand."""
        return self.call("tmux", "kill-session", "-t", f"={name}", **kw)

    def scenario(self, name: str) -> str:
        return os.path.join(self.server.root, f"{name}.scenario.json")

    def start(self, name: str, *args: str, steps: list | None = None, turns: list | tuple = (),
              inside: str | None = MANAGER) -> subprocess.CompletedProcess:
        """`workers.py start <name> --cwd … --prompt hi args…`, its fake's scenario `steps` (default one
        text, `Hi from <name>.`) and `turns`, logging to self.log."""
        with open(self.scenario(name), "w") as f:
            json.dump({"steps": [f"Hi from {name}."] if steps is None else steps, "turns": list(turns),
                       "log": self.log}, f)
        return self.workers("start", name, "--cwd", self.cwd, "--prompt", "hi", *args, inside=inside,
                            **{fake_claude.ENV: self.scenario(name)})

    def started(self, name: str, *args: str, **kw) -> str:
        """start's session id; fails unless it exits 0 printing `<name> <sid>`."""
        res = self.ok(self.start(name, *args, **kw))
        m = re.fullmatch(rf"{re.escape(name)} ({workers.SESSION_ID.pattern})\n", res.stdout)
        self.assertIsNotNone(m, res.stdout)
        return m[1]

    def restart(self, name: str, **kw) -> subprocess.CompletedProcess:
        return self.workers("restart", name, **kw, **{fake_claude.ENV: self.scenario(name)})

    def next_event(self, after: int, *args: str, gen: int = 0, **kw) -> tuple[int, str, str]:
        """`next-event args… --after <after> --gen <gen>`'s line number, event and content."""
        out = self.ok(self.workers("next-event", *args, "--after", str(after), "--gen", str(gen), **kw)).stdout
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

    def core_local(self, key: str, value: int | str) -> None:
        """Core config's `key`, in this test's ~/.agent-pm/core.local.toml, the keys set before kept."""
        self.local[key] = value
        with open(os.path.join(self.agent_pm, "core.local.toml"), "w") as f:
            f.write("".join(f"{k} = {json.dumps(v)}\n" for k, v in self.local.items()))

    def per_column(self, n: int) -> None:
        """Core config's workers_per_column."""
        self.core_local("workers_per_column", n)

    def retile(self, mode: str) -> None:
        """Core config's grid_retile."""
        self.core_local("grid_retile", mode)

    def window_of(self, name: str) -> str:
        """`name`'s window id."""
        return self.display(name, "#{window_id}")

    def hooks(self, name: str) -> list[str]:
        """The tui_claude.GRID_HOOKS set on `name`'s window (show-hooks -w)."""
        out = self.ok(self.server.tmux("show-hooks", "-w", "-t", self.window_of(name))).stdout
        return [h for h in tui_claude.GRID_HOOKS if re.search(rf"^{h}\b", out, re.M)]

    def holds(self, read, value, what: str):
        """Fails unless read() is `value` throughout STEADY s, polled; `value`."""
        end = time.monotonic() + STEADY
        while time.monotonic() < end:
            self.assertEqual(read(), value, what)
            time.sleep(live_tmux.POLL)
        return value


def is_event(line: str, name: str, kind: str) -> bool:
    return bool(workers.EVENT.fullmatch(line)) and line.split(" ")[1:] == [name, kind]


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    """argv has `flag value`, adjacent."""
    return any(argv[i:i + 2] == [flag, value] for i in range(len(argv)))


def tail(directory: str, n: int = 0, gen: int = 0, *backlog: str) -> str:
    """What attach prints after the table for manager directory `directory`: the backlog, N and GEN, the arm
    commands."""
    me, events, name = shlex.quote(WORKERS), shlex.quote(os.path.join(directory, "events")), os.path.basename(directory)
    lines = [*backlog, f"N={n} GEN={gen}", f"monitor: tail -n +{n + 1} -F {events}",
             f"monitor expired: python3 {me} attach --manager {name} --after LINE --gen {gen} --resume",
             f"next-event: python3 {me} next-event --manager {name} --after {n} --gen {gen}"]
    return "".join(f"{line}\n" for line in lines)


def raw(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def filler(name: str, count: int) -> list[str]:
    """count event lines `12:00:00 <name><i> done`, i from 1, dot-padded to WIDTH bytes with their newline."""
    return [f"12:00:00 {name}{i} done ".ljust(WIDTH - 1, ".") for i in range(1, count + 1)]


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
    """Placement rules: core/skills/tmux/SKILL.md › Start 3. Core config's grid_retile all: every grid hook set."""

    def setUp(self):
        super().setUp()
        self.retile("all")

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
        self.attach(inside="m2")
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


class Settle(Live):
    """mgr's window as an incident left it, built by hand before any grid exists: mgr's, w1's and w2's panes (ids in
    that order; w1, w2 sessions of gone openers old1, old2) in pane order w2 w1 mgr under an old top-bottom layout. At
    per-column 1 it needs two tile passes, the second swapping. A layout by hand is even-vertical: even-horizontal of
    these three panes already is their grid. Core config's grid_retile all, as grid_up's."""

    def setUp(self):
        super().setUp()
        self.retile("all")
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.window = self.display(MANAGER, "#{window_id}")
        w1 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", self.manager, *live_tmux.IDLE)
        w2 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", w1, *live_tmux.IDLE)
        for name, opener, pane in (("w1", "old1", w1), ("w2", "old2", w2)):
            self.tmux("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.tmux("set-option", "-t", f"={name}:", "@opener", opener, ";", "set-option", "-t", f"={name}:",
                      "@pane", pane)
        self.tmux("swap-pane", "-d", "-s", self.manager, "-t", w2)
        self.assertEqual(self.tmux("list-panes", "-t", self.window, "-F", "#{pane_id}").split(),
                         [w2, w1, self.manager])
        old = tui_claude._render_layout(tui_claude._parse_layout(
            f"200x50,0,0[200x25,0,0{{100x25,0,0,{w2[1:]},99x25,101,0,{w1[1:]}}},200x24,0,26,{self.manager[1:]}]"))
        self.tmux("select-layout", "-t", self.window, old)
        self.assertEqual(self.layout(), old)

    def tmux(self, *args: str) -> str:
        return self.ok(self.server.tmux(*args)).stdout.removesuffix("\n")

    def layout(self) -> str:
        return self.tmux("display-message", "-p", "-t", self.window, "#{window_layout}")

    def grid_up(self) -> None:
        """_grid_up on the window at per-column 1, retile all, in a process in mgr's pane: how the incident installed
        the grid."""
        code = (f"import sys; sys.path.insert(0, {os.path.dirname(TUI)!r}); import subprocess, tui_claude; "
                f"sys.exit(tui_claude._grid_up({self.window!r}, {self.manager!r}, 1, 'all', subprocess.run))")
        self.ok(self.call(sys.executable, "-I", "-c", code))

    def settled(self) -> str:
        """The grid of the two orphan groups, one per column (the manager 99 wide: half its old full width), then
        steady; its layout."""
        self.grid([[["w1"]], [["w2"]]], 99)
        return self.steady(self.layout())

    def steady(self, layout: str) -> str:
        """Fails unless the window's layout is `layout` throughout STEADY s."""
        return self.holds(self.layout, layout, "the window's layout")

    def hand_layout(self, tiles: str) -> tuple[str, str]:
        """A hand even-vertical, then the window's @grid-manager and @grid-tiles once it is re-tiled (@grid-tiles no
        longer `tiles`, @grid-busy clear), the grid is down or RETILE_WAIT s pass: one hand layout per re-tile."""
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        end = time.monotonic() + RETILE_WAIT
        state = f"#{{{tui_claude.GRID_MANAGER}}}\t#{{{tui_claude.GRID_BUSY}}}\t#{{{tui_claude.GRID_TILES}}}"
        while True:
            manager, busy, now = self.tmux("display-message", "-p", "-t", self.window, state).split("\t")
            if not manager or now != tiles and not busy or time.monotonic() > end:
                return manager, now
            time.sleep(PACE)

    def test_grid_up_settles(self):
        self.grid_up()
        self.settled()

    def test_concurrent_tiles_settle(self):
        self.tmux("set-option", "-w", "-t", self.window, tui_claude.GRID_MANAGER, self.manager, ";",
                  "set-option", "-w", "-t", self.window, tui_claude.GRID_PER_COLUMN, "1")
        env = self.server.env(**self.server.inside(MANAGER))
        tiles = [subprocess.Popen([sys.executable, TUI, "tile", *flag, self.window], env=env, cwd=self.cwd,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for flag in (["--no-wait"], []) for _ in range(8)]
        for p in tiles:
            self.addCleanup(p.communicate)   # cleanups run last first: after the kill, reaps it, closes its pipes
            self.addCleanup(p.kill)
        for p in tiles:
            _, err = p.communicate(timeout=TIMEOUT)
            self.assertEqual(p.returncode, 0, f"{p.args}: {err}")
        self.settled()

    def test_busy_mutes_the_hooks(self):
        self.grid_up()
        grid = self.settled()
        self.tmux("set-option", "-w", "-t", self.window, tui_claude.GRID_BUSY, self.window)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.assertNotEqual(self.steady(self.layout()), grid)
        self.tmux("set-option", "-u", "-w", "-t", self.window, tui_claude.GRID_BUSY)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.settled()

    def test_breaker_stops_a_loop(self):
        self.grid_up()
        self.settled()
        start, seen, most = time.monotonic(), 0, 0
        tiles = self.window_option(MANAGER, tui_claude.GRID_TILES)
        while True:
            manager, now = self.hand_layout(tiles)
            if not manager:
                break
            seen, most, tiles = seen + (now != tiles), max(most, len(now.split())), now
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, BREAKER_TIMEOUT,
                            f"still a grid after {elapsed:.0f} s: {seen} re-tiles seen, {seen / elapsed:.1f}/s; it "
                            f"trips past {tui_claude.TILE_LIMIT} in {tui_claude.TILE_WINDOW} s")
        # the trip follows TILE_LIMIT re-tiles; the loop's last look may miss the last one
        self.assertGreaterEqual(most, tui_claude.TILE_LIMIT - 1)
        options = (tui_claude.GRID_MANAGER, tui_claude.GRID_PER_COLUMN, tui_claude.GRID_TILES, tui_claude.GRID_BUSY)
        self.assertEqual([self.window_option(MANAGER, k) for k in options], [""] * len(options))
        hooks = self.tmux("show-hooks", "-w", "-t", self.window)
        self.assertEqual([h for h in tui_claude.GRID_HOOKS if h in hooks], [], hooks)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.steady(self.layout())
        self.started("w3")
        self.grid([[["w1"], ["w2"], "w3"]])


class Retile(Live):
    """Core config's grid_retile, unset (open-close) unless a test sets it: core/skills/tmux/SKILL.md › Start 3. A
    check that nothing re-tiles holds STEADY s, longer than a hook's tile takes in Settle."""

    def setUp(self):
        super().setUp()
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]

    def panes(self) -> dict[str, tuple[int, int, int, int]]:
        return self.server.panes(f"={MANAGER}:")

    def layout(self) -> str:
        return self.display(MANAGER, "#{window_layout}")

    def tiles(self) -> str:
        return self.window_option(MANAGER, tui_claude.GRID_TILES)

    def drag(self, name: str, rows: int) -> str:
        """The border below `name`'s pane moved `rows` down (up when negative), as a mouse drag: resize-pane. The
        layout after it."""
        before = self.layout()
        self.ok(self.server.tmux("resize-pane", "-t", self.server.pane_of(name), "-D" if rows > 0 else "-U",
                                 str(abs(rows))))
        after = self.layout()
        self.assertNotEqual(after, before)
        return after

    def split_of(self, before: dict, name: str, target: str, side: str) -> None:
        """Fails unless `name`'s pane is new, split from pane `target` on `side` (right or below): the two fill
        target's old box, a border between; every other pane of `before` unchanged."""
        after, new = self.panes(), self.server.pane_of(name)
        self.assertEqual(set(after), {*before, new})
        self.assertEqual({p: after[p] for p in before if p != target}, {p: before[p] for p in before if p != target})
        (x, y, w, h), kept, added = before[target], after[target], after[new]
        if side == "right":
            want = (x, y, kept[2], h), (x + kept[2] + 1, y, w - kept[2] - 1, h)
        else:
            want = (x, y, w, kept[3]), (x, y + kept[3] + 1, w, h - kept[3] - 1)
        self.assertEqual((kept, added), want, f"{name}: {side} of {target} ({before[target]})")
        self.assertTrue(min(*kept[2:], *added[2:]) > 0, (kept, added))

    def test_open_close_retiles_on_an_open_only(self):
        width = self.server.size[0] // 2
        self.started("w1")
        self.started("w2")
        self.grid([["w1", "w2"]], width)
        self.assertEqual(self.hooks(MANAGER), ["pane-exited"])
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")
        tiles = self.tiles()
        self.ok(self.server.tmux("resize-window", "-t", self.window_of(MANAGER), "-x", "160", "-y", "40"))
        self.assertEqual(self.display(MANAGER, "#{window_width}x#{window_height}"), "160x40")
        heights = [self.panes()[self.server.pane_of(n)][3] for n in ("w1", "w2")]
        self.assertGreater(heights[0] - heights[1], 1)   # still dragged: a tile would change it
        self.holds(lambda: (self.layout(), self.tiles()), (self.layout(), tiles),
                   "the layout and @grid-tiles after a window resize")
        width = self.panes()[self.manager][2]
        self.started("w3")
        self.grid([["w1", "w2", "w3"]], width)
        self.assertEqual(self.hooks(MANAGER), ["pane-exited"])

    def test_open_close_retiles_on_a_stop(self):
        width = self.server.size[0] // 2
        for name in ("w1", "w2", "w3"):
            self.started(name)
        self.grid([["w1", "w2", "w3"]], width)
        # stop kills w2's session: the attach client in its pane exits, so the pane closes and pane-exited runs (A2)
        self.assertEqual(self.ok(self.workers("stop", "w2")).stdout, "")
        self.grid([["w1", "w3"]], width)
        self.assertEqual(self.hooks(MANAGER), ["pane-exited"])

    def test_off_places_only_till_tile(self):
        self.retile("off")
        before = self.panes()
        self.started("w1")
        w1 = self.server.pane_of("w1")
        self.split_of(before, "w1", self.manager, "right")
        before = self.panes()
        self.started("w2")
        self.split_of(before, "w2", w1, "below")
        self.drag("w1", -10)   # w2's pane now the largest worker pane: a sub-worker of w1 goes below it
        before = self.panes()
        largest = max((p for p in before if p != self.manager), key=lambda p: (before[p][2] * before[p][3], int(p[1:])))
        self.assertEqual(largest, self.server.pane_of("w2"))
        self.attach(inside="w1")
        self.started("z1", inside="w1")
        self.split_of(before, "z1", largest, "below")
        self.holds(self.panes, self.panes(), "the panes as placed")
        self.assertEqual(self.hooks(MANAGER), [])
        options = (tui_claude.GRID_MANAGER, tui_claude.GRID_PER_COLUMN)
        self.assertEqual([self.window_option(MANAGER, k) for k in options], [self.manager, str(tui_claude.PER_COLUMN)])
        self.ok(self.tui("tile", self.window_of(MANAGER)))
        self.grid([[("w1", ["z1"]), "w2"]], self.server.size[0] // 2)

    def test_a_switch_to_open_close_leaves_only_pane_exited(self):
        self.retile("all")
        self.started("w1")
        self.grid([["w1"]])
        self.assertEqual(self.hooks(MANAGER), list(tui_claude.GRID_HOOKS))
        self.retile("open-close")
        self.started("w2")
        self.grid([["w1", "w2"]])
        self.assertEqual(self.hooks(MANAGER), ["pane-exited"])
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")

    def test_restart_keeps_a_dragged_layout(self):
        self.started("w1")
        self.started("w2")
        self.grid([["w1", "w2"]])
        self.wait_event("w1", "done")   # its transcript, which --resume needs
        dragged = self.drag("w1", 5)
        self.ok(self.restart("w1"))
        self.assertEqual(len(self.calls("w1")), 2)
        self.holds(self.layout, dragged, "the layout after a restart")


class Managers(Live):
    """Where a worker's events go: core/skills/tmux/SKILL.md › Start 1. Each test attaches only the directories it
    uses."""
    ATTACH = False

    def done_lines(self, path: str, name: str) -> list[str]:
        """`path`'s lines once one is `HH:MM:SS <name> done`."""
        def check():
            lines = live_tmux.events(path)
            return lines if any(is_event(line, name, "done") for line in lines) else None

        return live_tmux.wait(check, what=f"`{name} done` in {path}")

    def test_own_session_and_worker_in_worker(self):
        self.attach()
        self.started("w1")
        self.assertEqual(self.server.option("w1", "@events"), self.events)
        self.done_lines(self.events, "w1")
        self.attach(inside="w1")
        self.started("w2", inside="w1")
        inner = self.events_of("w1")
        self.assertEqual(self.server.option("w2", "@events"), inner)
        self.assertEqual([x.split(" ")[1:] for x in self.done_lines(inner, "w2")], [["w2", "done"]])
        self.assertEqual([x.split(" ")[1:] for x in live_tmux.events(self.events)], [["w1", "done"]])

    def test_manager_flag(self):
        self.attach("--manager", "other")
        self.started("w1", "--manager", "other")
        other = self.events_of("other")
        self.assertEqual(self.server.option("w1", "@events"), other)
        (done,) = self.done_lines(other, "w1")
        self.assertEqual(self.next_event(0, "--manager", "other"), (1, done, "Hi from w1."))
        self.assertFalse(os.path.exists(os.path.dirname(self.events)))

    def test_no_pane_needs_a_manager(self):
        res = self.start("w1", inside=None)
        self.assertEqual((res.returncode, res.stderr),
                         (1, "workers: no manager directory: run inside tmux or give --manager <name>\n"))
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(self.events))))
        self.assertEqual(self.server.tmux("has-session", "-t", "=w1").returncode, 1)


class Splits(Live):
    ATTACH = False

    def test_split_outside_grid(self):
        manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.attach("--manager", MANAGER, inside=None)   # outside tmux: no lease, so start from there may run
        self.started("w1", "--manager", MANAGER, "--split-from", MANAGER, "--split", "below", inside=None)
        pane = self.server.pane_of("w1")
        panes = self.server.panes(f"={MANAGER}:")
        self.assertIn(pane, panes)
        (mx, my, _, _), (x, y, _, _) = panes[manager], panes[pane]
        self.assertEqual(x, mx)
        self.assertGreater(y, my)
        self.assertEqual([self.window_option(MANAGER, k) for k in ("@grid-manager", "@grid-per-column")], ["", ""])


class Roster(Live):
    """mgr's roster.json, its lease and its event cursor against live sessions: core/skills/tmux/SKILL.md › Start 1. A
    second manager is session SECOND, shown in a client."""
    ATTACH = False

    def setUp(self):
        super().setUp()
        self.directory = os.path.dirname(self.events)

    def second(self) -> None:
        self.ok(self.server.tmux("new-session", "-d", "-s", SECOND, *live_tmux.IDLE))
        self.server.attach(SECOND)

    def roster(self) -> dict:
        with open(os.path.join(self.directory, "roster.json")) as f:
            return json.load(f)

    def held(self, holder: str) -> str:
        """FR-13's refusal naming `holder`, roster.json's holder."""
        h = self.roster()["holder"]
        self.assertEqual(h["session"], holder)
        return (f"workers: manager directory {self.directory}: held by tmux session {holder} since {h['since']}; ask "
                "that manager to run workers.py release, or end that session\n")

    def refused(self) -> None:
        """SECOND's attach to mgr's directory fails naming mgr, roster.json unchanged."""
        before = self.roster()
        res = self.workers("attach", "--manager", MANAGER, inside=SECOND)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", self.held(MANAGER)))
        self.assertEqual(self.roster(), before)

    def taken_over(self) -> None:
        """SECOND's attach to mgr's directory succeeds, SECOND the holder."""
        res = self.attach("--manager", MANAGER, inside=SECOND)
        self.assertEqual(res.stdout, f"{workers.HEADER}\n{tail(self.directory)}")
        self.assertEqual(self.roster()["holder"]["session"], SECOND)

    def recover(self, name: str, **extra: str) -> subprocess.CompletedProcess:
        """Entry `name`'s recovery argv (manager.recovery), no shell, in its cwd, as call() runs it in mgr's pane."""
        e = self.roster()["entries"][name]
        return self.call(*manager.recovery(e), cwd=e["cwd"], **extra)

    def after_w1(self, out: str, rest: str) -> None:
        """out is attach's table, w1's row (start's entry) only, then rest."""
        self.assertRegex(out, rf"\A{re.escape(workers.HEADER)}\nw1\tworker\t[^\n]*\n{re.escape(rest)}\Z")

    def live(self, *names: str) -> bool:
        return all(self.server.tmux("has-session", "-t", f"={n}").returncode == 0 for n in names)

    def gone(self, *names: str) -> bool:
        return not any(self.live(n) for n in names)

    def test_second_manager_after_release(self):
        self.attach()
        self.second()
        self.refused()
        self.assertEqual(self.ok(self.workers("release")).stdout, f"workers: released {self.directory}\n")
        self.assertIsNone(self.roster()["holder"])
        self.taken_over()

    def test_second_manager_after_the_holder_ends(self):
        self.attach()
        self.second()
        self.refused()
        self.ok(self.server.tmux("kill-session", "-t", f"={MANAGER}"))
        self.taken_over()

    def test_concurrent_attaches_one_holds(self):
        self.second()
        procs = {s: subprocess.Popen([sys.executable, WORKERS, "attach", "--manager", MANAGER],
                                     env=self.server.env(**self.server.inside(s)), cwd=self.cwd,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True)
                 for s in (MANAGER, SECOND)}
        for p in procs.values():
            self.addCleanup(p.communicate)   # cleanups run last first: after the kill, reaps it, closes its pipes
            self.addCleanup(p.kill)
        out = {s: p.communicate(timeout=TIMEOUT) for s, p in procs.items()}
        winners = [s for s, p in procs.items() if p.returncode == 0]
        self.assertEqual(len(winners), 1, out)
        (winner,) = winners
        (loser,) = set(procs) - {winner}
        self.assertEqual(out[winner], (f"{workers.HEADER}\n{tail(self.directory)}", ""))
        self.assertEqual((procs[loser].returncode, out[loser]), (1, ("", self.held(winner))))

    def test_killed_worker_gone_then_recovered(self):
        self.attach()
        sid = self.started("w1")
        pane = self.server.pane_of("w1")
        resume = [sys.executable, WORKERS, "start", "w1", "--manager", MANAGER, "--resume", sid, "--cwd", self.cwd]
        e = self.roster()["entries"]["w1"]
        self.assertEqual({k: e[k] for k in ("kind", "sid", "cwd", "resume", "pane", "opener", "state")},
                         {"kind": "worker", "sid": sid, "cwd": self.cwd, "resume": resume, "pane": pane,
                          "opener": MANAGER, "state": "working"})
        (done,) = self.wait_event("w1", "done")   # its transcript, which --resume needs
        self.ok(self.kill("w1"))
        self.assertEqual(self.attach().stdout, f"{workers.HEADER}\nw1\tworker\t{sid}\tgone\t-\t{self.cwd}\t{pane}\n"
                                               f"resume w1: {shlex.join(resume)}\n"
                                               f"{tail(self.directory, 0, 0, f'1 {done}')}")
        self.assertEqual(self.roster()["entries"]["w1"]["state"], "gone")
        res = self.ok(self.recover("w1", **{fake_claude.ENV: self.scenario("w1")}))
        self.assertEqual(res.stdout, f"w1 {sid}\n")
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sid), self.calls("w1")[-1]["argv"])
        self.grid([["w1"]])
        pane = self.server.pane_of("w1")
        table = f"{workers.HEADER}\nw1\tworker\t{sid}\tworking\t-\t{self.cwd}\t{pane}\tshown in {MANAGER}\n"
        out = self.attach().stdout
        self.assertEqual(out[:len(table)], table, out)
        self.assertEqual(list(self.roster()["entries"]), ["w1"])

    def echoing(self, name: str) -> dict[str, str]:
        """Scenario `name` for a tui run: turn 1 reports its start; the nudge's turn ends without an outcome, so the
        driver then waits, both sessions up. Returns the env naming it."""
        with open(self.scenario(name), "w") as f:
            json.dump({"steps": [{"kind": "progress", "name": "start", "text": "Echoing."}], "log": self.log}, f)
        return {fake_claude.ENV: self.scenario(name)}

    def role_run(self) -> str:
        """A dummy-tester tui run of session ROLE_SID (echoing), as SKILL.md › Role runs 3 starts it from mgr's pane, in
        a workdir of its own; returns the workdir once turn 1 has ended (its transcript, which --resume needs)."""
        work = os.path.join(self.server.root, "role-work")
        os.mkdir(work)
        self.ok(self.call(sys.executable, DRIVE, "--role", ROLE, "--input", "Hello.", "--out", "out.md", "--workdir",
                          ".", "--runner", "tui", "--detach", "--sid", ROLE_SID, cwd=work, **self.echoing(ROLE)))
        self.wait_event(ROLE_TUI, "done")
        return work

    def resumed(self, session: str, sid: str) -> list[dict]:
        """The fake's log entries for `session` that resume sid; none while a line is half appended."""
        try:
            return [c for c in self.calls(session) if has_pair(c["argv"], "--resume", sid)]
        except ValueError:
            return []

    def test_killed_role_run_gone_then_recovered(self):
        self.attach()
        work = os.path.join(self.server.root, "role-work")
        os.mkdir(work)
        out = os.path.join(work, "out.md")
        scenario = self.echoing(ROLE)
        self.ok(self.call(sys.executable, DRIVE, "--role", ROLE, "--runner", "tui", "--detach", "--input", "Hello.",
                          "--out", out, "--workdir", work, "--sid", ROLE_SID, **scenario))
        resume = [sys.executable, DRIVE, f"--role={ROLE}", f"--out={out}", f"--workdir={work}", "--runner=tui",
                  "--detach", f"--manager={MANAGER}"]
        [(name, e)] = self.roster()["entries"].items()
        self.assertEqual((name, {k: e[k] for k in ("kind", "sid", "cwd", "tui", "opener", "resume", "state")}),
                         (ROLE_DRIVER, {"kind": "role", "sid": ROLE_SID, "cwd": self.cwd, "tui": ROLE_TUI,
                                        "opener": MANAGER, "resume": resume, "state": "working"}))
        self.wait_event(ROLE_TUI, "done")   # turn 1's end: its transcript, which --resume needs
        self.ok(self.kill(ROLE_DRIVER))
        self.kill(ROLE_TUI)   # the driver, stopping, may kill it first
        # the old driver's last act is its outcome line, after it kills the TUI session: none of it may hit the new run
        live_tmux.wait(lambda: self.gone(ROLE_DRIVER, ROLE_TUI) and any(
            line.split(" ")[1:3] == [ROLE_DRIVER, "outcome"] for line in live_tmux.events(self.events)),
                       what=f"{ROLE_DRIVER} and {ROLE_TUI} gone, the driver's outcome line")
        res = self.attach()
        e = self.roster()["entries"][ROLE_DRIVER]
        backlog = [f"{i} {line}" for i, line in enumerate(live_tmux.events(self.events), 1)]
        self.assertEqual(res.stdout, f"{workers.HEADER}\n{ROLE_DRIVER}\trole\t{ROLE_SID}\tgone\t-\t{self.cwd}\t-\n"
                                     f"resume {ROLE_DRIVER}: cd {shlex.quote(self.cwd)} && "
                                     f"{shlex.join(manager.recovery(e))}\n{tail(self.directory, 0, 0, *backlog)}")
        self.assertIn("--input 'Continue the unfinished task.'", res.stdout)
        self.ok(self.recover(ROLE_DRIVER, **scenario))
        live_tmux.wait(lambda: self.live(ROLE_DRIVER, ROLE_TUI), what=f"{ROLE_DRIVER} and {ROLE_TUI} back")
        self.grid([[ROLE_TUI]])
        live_tmux.wait(lambda: self.resumed(ROLE_TUI, ROLE_SID), what=f"{ROLE_TUI}'s claude --resume {ROLE_SID}")
        again = self.roster()["entries"]
        self.assertEqual(list(again), [ROLE_DRIVER])
        self.assertEqual({**again[ROLE_DRIVER], "started": None}, {**e, "state": "working", "started": None})

    def test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one(self):
        self.attach()
        sids = {name: self.started(name) for name in ("w1", "w2")}
        for name in sids:
            self.wait_event(name, "done")   # its transcript, which --resume needs
        self.ok(self.kill("w1"))
        self.assertEqual(self.ok(self.workers("stop", "w2")).stdout, "")
        self.assertTrue(self.gone("w1", "w2"))
        self.assertEqual(list(self.roster()["entries"]), ["w1"])
        out = self.attach().stdout
        self.assertIn("\nresume w1: ", out)
        self.assertNotIn("resumed", out)
        self.assertTrue(self.gone("w1"))
        out = self.attach("--resume", **{fake_claude.ENV: self.scenario("w1")}).stdout
        self.assertRegex(out, rf"\nnext-event: [^\n]+\nresumed w1: exit 0: w1 {sids['w1']}\n\Z")
        self.assertTrue(self.live("w1") and self.gone("w2"))
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sids["w1"]), self.calls("w1")[-1]["argv"])
        self.grid([["w1"]])
        self.attach()
        self.assertEqual({n: e["state"] for n, e in self.roster()["entries"].items()}, {"w1": "working"})

    def test_stop_a_role_run_by_its_tui_session(self):
        self.attach()
        self.role_run()
        self.assertEqual(list(self.roster()["entries"]), [ROLE_DRIVER])
        self.assertEqual(self.ok(self.workers("stop", ROLE_TUI)).stdout, "")
        self.assertTrue(self.gone(ROLE_DRIVER, ROLE_TUI))
        self.assertEqual(self.roster()["entries"], {})

    def test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume(self):
        self.attach()
        work = os.path.join(self.server.root, "role-work")
        os.mkdir(work)
        asking = {"status": "needs_input", "title": "echo", "summary": "Asking.", "questions": ["Which greeting?"]}
        with open(self.scenario(ROLE), "w") as f:
            json.dump({"steps": [{"kind": "progress", "name": "start", "text": "Echoing."},
                                 {"kind": "outcome", "outcome": asking}], "log": self.log}, f)
        self.ok(self.call(sys.executable, DRIVE, "--role", ROLE, "--input", "Hello.", "--out", "out.md", "--workdir",
                          ".", "--runner", "headless", "--detach", "--sid", ROLE_SID, cwd=work,
                          **{fake_claude.ENV: self.scenario(ROLE)}))
        live_tmux.wait(lambda: self.gone(ROLE_DRIVER) and any(
            line.split(" ")[1:] == [ROLE_DRIVER, "outcome", "needs_input"] for line in live_tmux.events(self.events)),
                       what=f"{ROLE_DRIVER} gone after its outcome needs_input")
        backlog = [f"{i} {line}" for i, line in enumerate(live_tmux.events(self.events), 1)]
        table = f"{workers.HEADER}\n{ROLE_DRIVER}\trole\t{ROLE_SID}\twaiting\t-\t{work}\t-\n"
        self.assertEqual(self.attach().stdout, table + tail(self.directory, 0, 0, *backlog))
        with open(self.log) as f:
            calls = f.readlines()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.attach("--resume").stdout, table + tail(self.directory, 0, 0, *backlog))
        self.assertTrue(self.gone(ROLE_DRIVER))
        with open(self.log) as f:
            self.assertEqual(f.readlines(), calls)
        self.assertEqual(self.roster()["entries"][ROLE_DRIVER]["state"], "waiting")

    def test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker(self):
        """A worker, a role run and a pipeline run in the roster, a second worker stopped; the server killed and started
        again: attach --resume brings the three back, resumed, and not the stopped one. The pipeline run is STAND_IN's
        (router.py needs Linear: orchestrator/src/tests/integration B8 recovers a real one by the same argv)."""
        self.attach()
        sids = {name: self.started(name) for name in ("w1", "w2")}
        for name in sids:
            self.wait_event(name, "done")
        self.role_run()
        router = os.path.join(self.server.root, "orchestrator", "router.py")
        run = os.path.join(self.server.root, "pipeline-run")
        for d in (os.path.dirname(router), run):
            os.mkdir(d)
        with open(router, "w") as f:
            f.write(STAND_IN.format(src=os.path.join(workers.CORE, "src"), role=ROLE, sid=PIPE_SID, drive=DRIVE))
        scenario = self.echoing("pipeline")
        self.ok(self.call(sys.executable, router, "--issue", PIPE_ISSUE, "--tui", "--events", self.events, "--manager",
                          MANAGER, cwd=run, **scenario))
        self.wait_event(PIPE_TUI, "done")
        self.ok(self.workers("stop", "w2"))
        self.assertEqual(sorted(self.roster()["entries"]), [PIPE_DRIVER, ROLE_DRIVER, "w1"])
        self.server.kill()
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        out = self.attach("--resume", **scenario).stdout
        self.assertRegex(out, rf"\nresumed {PIPE_DRIVER}: exit 0\nresumed {ROLE_DRIVER}: exit 0: [^\n]+\n"
                              rf"resumed w1: exit 0: w1 {sids['w1']}\n\Z")
        live_tmux.wait(lambda: self.live("w1", ROLE_DRIVER, ROLE_TUI, PIPE_DRIVER, PIPE_TUI), what="all three back")
        for session, sid in (("w1", sids["w1"]), (ROLE_TUI, ROLE_SID), (PIPE_TUI, PIPE_SID)):
            live_tmux.wait(lambda s=session, i=sid: self.resumed(s, i), what=f"{session}'s claude --resume {sid}")
        self.assertTrue(self.gone("w2"))
        self.attach()
        entries = self.roster()["entries"]
        self.assertEqual(sorted(entries), [PIPE_DRIVER, ROLE_DRIVER, "w1"])
        self.assertNotIn("gone", [e["state"] for e in entries.values()])

    def test_unshown_worker_reopened(self):
        self.attach()
        sid = self.started("w1")
        (done,) = self.wait_event("w1", "done")
        self.grid([["w1"]])
        closed = self.server.pane_of("w1")
        self.ok(self.server.tmux("kill-pane", "-t", closed))
        live_tmux.wait(lambda: not self.ok(self.server.tmux("list-clients", "-t", "=w1")).stdout,
                       what="w1 shown by no client")
        res = self.attach()
        pane = self.server.pane_of("w1")
        self.assertNotEqual(pane, closed)
        self.grid([["w1"]])
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)
        self.assertEqual(res.stdout, f"{workers.HEADER}\nw1\tworker\t{sid}\tdone\t-\t{self.cwd}\t{pane}\n"
                                     f"{tail(self.directory, 0, 0, f'1 {done}')}")
        self.assertEqual(res.stderr, "tui: session w1: tmux attach -t '=w1'\n")
        e = self.roster()["entries"]["w1"]
        self.assertEqual((e["pane"], e["opener"]), (pane, MANAGER))

    def test_take_over_resumes_from_the_cursor(self):
        self.attach()
        self.started("w1", steps=["One."], turns=[["Two."], ["Three."], ["Four."]])
        (one,) = self.wait_event("w1", "done")
        self.assertEqual(self.next_event(0), (1, one, "One."))
        self.ok(self.tui("send", "w1", "more"))
        n, two, content = self.next_event(1)
        self.assertEqual((n, content), (2, "Two."))
        self.ok(self.tui("send", "w1", "more"))
        *_, three = self.wait_event("w1", "done", 3)
        resumed = tail(self.directory, 2, 0, f'3 {three}')
        self.after_w1(self.attach("--after", "2", "--gen", "0").stdout, resumed)
        self.ok(self.server.tmux("kill-session", "-t", f"={MANAGER}"))
        self.second()
        self.after_w1(self.attach("--manager", MANAGER, inside=SECOND).stdout, resumed)
        self.assertEqual(self.roster()["holder"]["session"], SECOND)
        self.assertEqual(self.next_event(2, "--manager", MANAGER, inside=SECOND), (3, three, "Three."))
        waiting = subprocess.Popen([sys.executable, WORKERS, "next-event", "--manager", MANAGER, "--after", "3",
                                    "--gen", "0"], env=self.server.env(**self.server.inside(SECOND)), cwd=self.cwd,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(waiting.communicate)   # cleanups run last first: after the kill, reaps it, closes its pipes
        self.addCleanup(waiting.kill)
        live_tmux.wait(lambda: self.roster()["cursor"] == 3, what="next-event's cursor 3")
        self.ok(self.tui("send", "w1", "more", inside=SECOND))
        out, err = waiting.communicate(timeout=TIMEOUT)
        *head, four = live_tmux.events(self.events)
        self.assertEqual(head, [one, two, three])
        self.assertTrue(is_event(four, "w1", "done"), four)
        self.assertEqual((waiting.returncode, out, err), (0, f"4 {four}\nFour.\n", ""))

    def test_rotation_keeps_the_unhandled_lines(self):
        self.attach()
        self.started("w1", turns=[["Again."]])
        self.wait_event("w1", "done")
        kept = filler("k", 3)
        with open(self.events, "a") as f:
            f.write("".join(f"{line}\n" for line in filler("f", manager.EVENTS_MAX // WIDTH + 1) + kept))
        total = len(live_tmux.events(self.events))
        old, original, ino = f"{self.events}.1", raw(self.events), os.lstat(self.events).st_ino
        backlog = [f"{i} {line}" for i, line in enumerate(kept, 1)]
        res = self.attach("--after", str(total - 3), "--gen", "0")
        self.after_w1(res.stdout, tail(self.directory, 0, 1, *backlog))
        self.assertEqual((os.lstat(old).st_ino, raw(old)), (ino, original))
        self.assertEqual(live_tmux.events(self.events), kept)
        self.assertEqual(stat.S_IMODE(os.lstat(self.events).st_mode), 0o600)
        before = self.roster()
        self.assertEqual((before["cursor"], before["gen"]), (0, 1))
        res = self.workers("next-event", "--after", str(total - 3), "--gen", "0")
        self.assertEqual((res.returncode, res.stdout, res.stderr),
                         (1, "workers: stale line numbers (gen 0, now 1): run workers.py attach\n", ""))
        self.assertEqual(self.roster(), before)
        self.ok(self.tui("send", "w1", "more"))
        n, event, content = self.next_event(3, gen=1)
        self.assertEqual((n, content), (4, "Again."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(live_tmux.events(self.events), [*kept, event])
        self.assertEqual(raw(old), original)


if __name__ == "__main__":
    unittest.main()
