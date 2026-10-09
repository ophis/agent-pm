"""workers.py and tui_claude.py run as processes against a private tmux server, fake_claude.py as `claude`: start,
state, events, blocked, dead, restart, early death."""
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

    def wait_event(self, name: str, kind: str, count: int = 1) -> list[str]:
        """The events file's lines once `count` of them are `HH:MM:SS <name> <kind>`."""
        def check():
            lines = live_tmux.events(self.events)
            return lines if sum(is_event(line, name, kind) for line in lines) >= count else None

        return live_tmux.wait(check, what=f"{count} `{name} {kind}` events in {self.events}")

    def calls(self, name: str) -> list[dict]:
        """The fake's log entries for worker `name` (argv has `--name <name>`), oldest first."""
        try:
            with open(self.log) as f:
                entries = [json.loads(line) for line in f]
        except FileNotFoundError:
            return []
        return [e for e in entries if any(e["argv"][i:i + 2] == ["--name", name] for i in range(len(e["argv"])))]

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

    def border(self, name: str) -> str:
        """The pane-border-format window option of `name`'s window."""
        return self.ok(self.server.tmux("show-options", "-wqv", "-t", f"={name}:", "pane-border-format")
                       ).stdout.removesuffix("\n")


def is_event(line: str, name: str, kind: str) -> bool:
    return bool(workers.EVENT.fullmatch(line)) and line.split(" ")[1:] == [name, kind]


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
        self.assertEqual(self.border("w1"), BORDER)

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
        _, dead = self.wait_event("w1", "dead")
        self.assertTrue(is_event(dead, "w1", "dead"), dead)
        self.assertEqual(self.server.option("w1", "@state"), "dead")
        state, sig = self.display("w1", "#{pane_dead} #{pane_dead_signal}").split(" ")
        self.assertEqual(state, "1")
        self.assertTrue(sig == str(int(signal.SIGTERM)) or sig.upper() == "TERM", sig)
        n, event, content = self.next_event(1)
        self.assertEqual((n, event), (2, dead))
        self.assertEqual(content, self.pane_text("w1", workers.PANE_LINES))
        self.ok(self.restart("w1"))
        _, again = self.calls("w1")
        self.assertIn(["--resume", sid], [again["argv"][i:i + 2] for i in range(len(again["argv"]))])
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
        self.assertTrue(all(line.startswith("tui: ") for line in lines[:at]), res.stderr)
        self.assertIn("claude: error: unrecognized arguments: --bogus", lines[at + 1:])
        self.ok(self.server.tmux("has-session", "-t", "=w2"))
        self.assertEqual(self.display("w2", "#{pane_dead}"), "1")


if __name__ == "__main__":
    unittest.main()
