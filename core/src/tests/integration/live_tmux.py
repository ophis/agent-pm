"""A private tmux server per test, for integration tests that run workers.py, tui_claude.py and drive.py as processes
against real tmux: Server (its socket and env, a control-mode client that shows a session, readers), events, wait and
assert_grid. Stdlib only, importing no core module."""
import os
import resource
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import NamedTuple

import fake_claude

# Two words, so tmux execs them; one would run as `<default-shell> -c`, which without SHELL in env() is the user's
# login shell, reading its rc files.
IDLE = ("cat", "-")
TIMEOUT = 10   # seconds
POLL = 0.05   # seconds
SOCKET_MAX = 100   # bytes; sun_path holds 104 (macOS), 108 (Linux)
HEADROOM = 256   # processes
PANES = "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height}"


class Client(NamedTuple):
    """A control-mode client: its process, its pty's master and the thread draining it until stop."""
    proc: subprocess.Popen
    master: int
    thread: threading.Thread
    stop: threading.Event


class Server:
    """A tmux server of the test's own at socket <root>/tmux-<uid>/default, so TMUX_TMPDIR=<root> and -S reach it;
    `home` is the test's HOME. The case's cleanups kill the server (by -S), reap the clients and remove the root."""

    def __init__(self, case, home: str):
        self.case, self.home, self.size, self.clients = case, home, (200, 50), []
        self.program = shutil.which("tmux")
        if self.program is None:
            case.fail("tmux not found on PATH")
        self.root = os.path.realpath(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.mkdir(os.path.join(self.root, f"tmux-{os.getuid()}"), 0o700)
        self.socket = os.path.join(self.root, f"tmux-{os.getuid()}", "default")
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.path = fake_claude.install(self.bin, tail=f"{os.path.dirname(self.program)}:/usr/bin:/bin")
        case.addCleanup(self._close_clients)
        case.addCleanup(self._kill)
        if len(os.fsencode(self.socket)) > SOCKET_MAX:
            case.fail(f"tmux socket path over {SOCKET_MAX} bytes: {self.socket}")

    def env(self, **extra: str) -> dict[str, str]:
        """The env of every process the test starts, built from scratch (nothing of os.environ), plus `extra`."""
        return {"PATH": self.path, "HOME": self.home, "PYTHONUTF8": "1", "TMUX_TMPDIR": self.root, **extra}

    def start(self, manager: str, width: int = 200, height: int = 50) -> None:
        """Starts the server, its global env env(), with session `manager` idle in a width x height window. The server
        and its jobs are capped (RLIMIT_NPROC) at the user's process count (thread count on Linux, where RLIMIT_NPROC
        counts threads) plus HEADROOM, so a fork loop cannot take the host."""
        self.size = width, height
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        uid = str(os.getuid())
        ps = (["ps", "-L", "-U", uid, "-o", "lwp="] if sys.platform.startswith("linux")
              else ["ps", "-U", uid, "-o", "pid="])
        cap = len(subprocess.run(ps, capture_output=True, text=True, check=True).stdout.split()) + HEADROOM
        cap = cap if hard == resource.RLIM_INFINITY else min(cap, hard)
        self._out("-f", "/dev/null", "new-session", "-d", "-s", manager, "-x", str(width), "-y", str(height), *IDLE,
                  preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NPROC, (cap, hard)))

    def attach(self, session: str) -> None:
        """Shows `session` in a control-mode client in a pty (a tty tui_claude counts, no iTerm2 path), sized as
        start's window; returns once list-clients lists it and the window has that size."""
        master, slave = os.openpty()
        try:
            proc = subprocess.Popen([self.program, "-S", self.socket, "-CC", "attach", "-t", f"={session}"],
                                    stdin=slave, stdout=slave, stderr=slave, env=self.env(TERM="xterm-256color"),
                                    start_new_session=True)
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        stop = threading.Event()
        thread = threading.Thread(target=_drain, args=(master, stop), daemon=True)
        thread.start()
        self.clients.append(Client(proc, master, thread, stop))

        def listed() -> bool:
            if proc.poll() is not None:
                self.case.fail(f"tmux -CC attach -t ={session} exited {proc.returncode}")
            rows = self._out("list-clients", "-t", f"={session}", "-F",
                             "#{client_pid} #{client_control_mode} #{client_tty}").splitlines()
            return any(r.startswith(f"{proc.pid} 1 /") for r in rows)

        wait(listed, what=f"a control-mode client of {session}")
        width, height = self.size
        os.write(master, f"refresh-client -C {width}x{height}\n".encode())
        wait(lambda: self._out("display-message", "-p", "-t", f"={session}:", "#{window_width}x#{window_height}")
             == f"{width}x{height}\n", what=f"{session}'s window at {width}x{height}")

    def tmux(self, *args: str, preexec_fn=None) -> subprocess.CompletedProcess:
        """`tmux -S <socket> args…` with env(), captured."""
        # -u: env() has no locale and no TMUX, so tmux would print each tab or non-ASCII character to us as "_"
        return subprocess.run([self.program, "-u", "-S", self.socket, *args], env=self.env(), capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT, preexec_fn=preexec_fn)

    def inside(self, session: str) -> dict[str, str]:
        """TMUX and TMUX_PANE for a process run in `session`'s pane."""
        pid = self._out("display-message", "-p", "#{pid}").strip()
        pane = self._out("display-message", "-p", "-t", f"={session}:", "#{pane_id}").strip()
        return {"TMUX": f"{self.socket},{pid},0", "TMUX_PANE": pane}

    def panes(self, window: str) -> dict[str, tuple[int, int, int, int]]:
        """Each pane of `window` (a target): its (left, top, width, height)."""
        rows = (line.split() for line in self._out("list-panes", "-t", window, "-F", PANES).splitlines())
        return {r[0]: (int(r[1]), int(r[2]), int(r[3]), int(r[4])) for r in rows}

    def pane_of(self, session: str) -> str:
        """`session`'s @pane; "" when unset."""
        return self.option(session, "@pane")

    def option(self, session: str, key: str) -> str:
        """`session`'s session option `key`; "" when unset."""
        return self._out("show-options", "-qv", "-t", f"={session}:", key).removesuffix("\n")

    def _out(self, *args: str, **kw) -> str:
        """tmux's stdout; fails the case when tmux fails."""
        res = self.tmux(*args, **kw)
        if res.returncode:
            self.case.fail(f"tmux {' '.join(args)}: exit {res.returncode}: {res.stderr.strip()}")
        return res.stdout

    def _kill(self) -> None:
        res = self.tmux("list-panes", "-a", "-F", "#{pid} #{pane_pid}")
        self.tmux("kill-server")
        pids = {int(p) for p in res.stdout.split() if p.isdigit()} if res.returncode == 0 else set()
        wait(lambda: not any(map(_alive, pids)), what=f"the tmux server and its panes' processes {sorted(pids)} to end")

    def _close_clients(self) -> None:
        for c in self.clients:
            try:
                c.proc.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                c.proc.kill()
                c.proc.wait()
            c.stop.set()
            c.thread.join()
            os.close(c.master)


def _drain(master: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if select.select([master], [], [], POLL)[0] and not os.read(master, 65536):
                return
        except OSError:
            return


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def events(path: str) -> list[str]:
    """The events file's lines; none while it is missing."""
    try:
        with open(path) as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def wait(check, timeout: float = TIMEOUT, what: str = "check() to hold"):
    """check()'s first truthy value, polled; a check raising AssertionError counts as falsy, its error the value. On
    timeout: AssertionError naming `what` and the last value."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = check()
        except AssertionError as e:
            value = e
        else:
            if value:
                return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}; last value: {value!r}")
        time.sleep(POLL)


class Box(NamedTuple):
    """An area: right and bottom are exclusive, so a neighbour beyond the 1-cell border starts at right + 1."""
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def _union(boxes: list[Box]) -> Box:
    return Box(min(b.left for b in boxes), min(b.top for b in boxes), max(b.right for b in boxes),
               max(b.bottom for b in boxes))


def _sessions(cell) -> list[str]:
    return [cell] if isinstance(cell, str) else [cell[0], *cell[1]] if isinstance(cell, tuple) else list(cell)


def assert_grid(case, server: Server, manager: str, columns: list, manager_width: int | None = None) -> bool:
    """Fails `case` unless manager's window is the grid `columns` gives: left to right, each column's top-level cells
    top to bottom, a cell a session name, (parent, [subs…]) or an orphan group [subs…]; a cell's box bounds its
    sessions' panes (@pane). The manager pane is the window's @grid-manager and first pane (lowest id), at left 0,
    manager_width wide when given. Placement rules: core/skills/tmux/SKILL.md › Start 3; stored options:
    core/CLAUDE.md › Rules. True when it holds (for wait)."""
    window, width, stored = server._out("display-message", "-p", "-t", f"={manager}:",
                                        "#{window_id}\t#{window_width}\t#{@grid-manager}").rstrip("\n").split("\t")
    panes = server.panes(window)
    at = dict(line.split("\t", 1) for line in server._out("list-sessions", "-F", "#{session_name}\t#{@pane}")
              .splitlines())
    names = {pane: name for name, pane in at.items() if pane in panes}
    names[stored] = f"{manager} (manager)"
    layout = "\n".join(f"  {p} {names.get(p, '?')}: left {x} top {y} {w}x{h}"
                       for p, (x, y, w, h) in sorted(panes.items(), key=lambda kv: kv[1][:2]))

    def fail(problem: str):
        case.fail(f"grid: {problem}\n want {columns!r}\n window {window} {width} wide:\n{layout}")

    def box(name: str) -> Box:
        if at.get(name) not in panes:
            fail(f"{name}: @pane {at.get(name)!r} is no pane of the window")
        x, y, w, h = panes[at[name]]
        return Box(x, y, x + w, y + h)

    def stacked(boxes: list[Box], what: str) -> None:
        if any(a.bottom >= b.top for a, b in zip(boxes, boxes[1:])):
            fail(f"{what}: not stacked top to bottom in the order given")

    def cell_box(cell) -> Box:
        if isinstance(cell, str):
            return box(cell)
        if isinstance(cell, tuple) and len(cell) == 2 and isinstance(cell[1], list) and cell[1]:
            parent, subs = box(cell[0]), [box(s) for s in cell[1]]
            whole = _union([parent, *subs])
            if (parent.left, parent.top, parent.bottom) != (whole.left, whole.top, whole.bottom):
                fail(f"{cell!r}: the parent is not at the cell's left, full height")
            if len({s.left for s in subs}) != 1 or subs[0].left <= parent.right:
                fail(f"{cell!r}: the subs share no left right of the parent")
            stacked(subs, repr(cell))
            if (subs[0].top, subs[-1].bottom) != (whole.top, whole.bottom):
                fail(f"{cell!r}: the subs are not full cell height")
            return whole
        if isinstance(cell, list) and cell:
            subs = [box(s) for s in cell]
            whole = _union(subs)
            if any((s.left, s.right) != (whole.left, whole.right) for s in subs):
                fail(f"{cell!r}: the orphan group's subs are not full cell width")
            stacked(subs, repr(cell))
            return whole
        fail(f"bad cell {cell!r}: want a name, (parent, [subs…]) or [subs…]")

    first = min(panes, key=lambda p: int(p[1:]))
    if stored != first:
        fail(f"@grid-manager {stored!r} is not the window's first pane {first}")
    x, y, w, h = panes[stored]
    m = Box(x, y, x + w, y + h)
    if m.left != 0 or manager_width not in (None, m.width):
        fail(f"manager at left {m.left}, {m.width} wide; want left 0, width {manager_width or 'any'}")
    if not all(columns):
        fail("an empty column")
    cells = [[cell_box(cell) for cell in column] for column in columns]
    owned = [at[name] for column in columns for cell in column for name in _sessions(cell)]
    if len(set(owned)) != len(owned):
        fail("two sessions given share a pane")
    if extra := sorted(set(panes) - {stored, *owned}):
        fail(f"panes of no session given: {extra}")
    boxes = [_union(column) for column in cells]
    if boxes and (boxes[0].left != m.right + 1 or boxes[-1].right != int(width)):
        fail(f"the columns span {boxes[0].left} to {boxes[-1].right}; want {m.right + 1} to {width}")
    if any(a.right >= b.left for a, b in zip(boxes, boxes[1:])):
        fail("the columns overlap or are out of order")
    if boxes and max(b.width for b in boxes) - min(b.width for b in boxes) > 1:
        fail(f"column widths {[b.width for b in boxes]} differ by more than 1")
    for i, column in enumerate(cells, 1):
        if any((c.left, c.right) != (column[0].left, column[0].right) for c in column):
            fail(f"column {i}: its cells differ in left or width")
        stacked(column, f"column {i}")
        if (column[0].top, column[-1].bottom) != (m.top, m.bottom):
            fail(f"column {i}: spans {column[0].top} to {column[-1].bottom}; want the manager's {m.top} to {m.bottom}")
        if max(c.height for c in column) - min(c.height for c in column) > 1:
            fail(f"column {i}: cell heights {[c.height for c in column]} differ by more than 1")
    return True
