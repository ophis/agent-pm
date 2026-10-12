"""crew.py end to end: its CLI run as a manager runs it, on a private tmux server (new/tests/live_tmux.py) with a
control-mode client on the manager. Workers are plain tui sessions of the real Adapters running new/tests/fake_claude.py
and the Codex fake_tui.py (on PATH as `claude` and `codex`); follow-ups go through the Driver's tmux_host.py send. HOME
and the server's root lie under AGENT_PM_TEST_TMP (else new/), outside the temp dirs a plain Codex session can write."""

from new.tests import CORE, NEW, SRC, fake_claude, live_tmux
from new.tests.integration.adapters.codex import fake_codex, fake_tui

import contextlib
import importlib.util
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import roster


def load(path):
    spec = importlib.util.spec_from_file_location("crew", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CREW = os.path.join(CORE, "skills", "tmux", "scripts", "crew.py")
crew = load(CREW)
TMUX_HOST = os.path.join(SRC, "driver", "tmux_host.py")
MANAGER = "mgr"
SECOND = "mgr2"
TIMEOUT = 60
WIDTH = 64
NOTIFY = 'tui.notification_method="bel"'
CODEX_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "Interrupt",
)
CLAUDE_HOOKS = {"Stop", "PermissionRequest", "Notification", "PostToolUse", "UserPromptSubmit"}
CALLER = "26900000-0000-4000-8000-000000000009"
ROLE = "dummy-tester"
ROLE_SID = "26700000-0000-4000-8000-000000000005"
ROLE_TUI = f"{ROLE}-{ROLE_SID[:8]}"
ROLE_DRIVER = f"{ROLE_TUI}-drive"
CLAUDE_SESSION = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": CALLER,
}
ENV_DUMP = (
    "import json, os, sys; open(sys.argv[1], 'a')"
    ".write(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ)}) + '\\n')"
)


def codex_session(thread: str = CALLER) -> dict[str, str]:
    return {"CODEX_THREAD_ID": thread, "CODEX_SANDBOX": "seatbelt", **fake_codex.DEFAULTS}


def is_event(line: str, name: str, kind: str) -> bool:
    return bool(crew.EVENT.fullmatch(line)) and line.split(" ")[1:] == [name, kind]


def has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i : i + 2] == [flag, value] for i in range(len(argv)))


def tail(directory: str, n: int = 0, gen: int = 0, *backlog: str) -> str:
    me, events, name = (
        shlex.quote(CREW),
        shlex.quote(os.path.join(directory, "events")),
        os.path.basename(directory),
    )
    lines = [
        *backlog,
        f"N={n} GEN={gen}",
        f"monitor: tail -n +{n + 1} -F {events}",
        f"monitor expired: python3 {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: python3 {me} next-event --manager {name} --after {n} --gen {gen}",
    ]
    return "".join(f"{line}\n" for line in lines)


def raw(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def filler(name: str, count: int) -> list[str]:
    return [f"12:00:00 {name}{i} done ".ljust(WIDTH - 1, ".") for i in range(1, count + 1)]


def logged(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


class Live(unittest.TestCase):
    ATTACH = True

    def setUp(self):
        parent = os.environ.get("AGENT_PM_TEST_TMP", NEW)
        tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home = os.path.join(self.root, "home")
        self.agent_pm = os.path.join(self.home, ".agent-pm")
        self.cwd = os.path.join(self.root, "work")
        for path in (self.agent_pm, self.cwd):
            os.makedirs(path)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": self.home}))
        mkdtemp = tempfile.mkdtemp
        with mock.patch.object(
            live_tmux.tempfile, "mkdtemp", side_effect=lambda: mkdtemp(dir=parent)
        ):
            self.server = live_tmux.Server(self, self.home)
        fake_tui.install(self.server.bin)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.events = self.events_of(MANAGER)
        self.directory = os.path.dirname(self.events)
        self.codex_home = os.path.join(self.home, ".codex")
        if self.ATTACH:
            self.attach()

    def events_of(self, manager: str) -> str:
        return os.path.join(self.agent_pm, "managers", manager, "events")

    def ok(self, res: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            res.returncode, 0, f"{res.args}\nstdout: {res.stdout}\nstderr: {res.stderr}"
        )
        return res

    def poke(self) -> None:
        # tmux on Linux (CI: 3.4) can leave a dead pane's process unreaped, so no pane-died, until another child of
        # the server exits: a no-op job is one.
        self.server.tmux("run-shell", "-b", "true")

    @contextlib.contextmanager
    def poking(self):
        stop = threading.Event()

        def run():
            while not stop.wait(live_tmux.POLL * 4):
                self.poke()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def call(
        self, *argv: str, inside: str | None = MANAGER, cwd: str | None = None, **extra: str
    ) -> subprocess.CompletedProcess:
        env = self.server.env(**(self.server.inside(inside) if inside else {}), **extra)
        with self.poking():
            return subprocess.run(
                list(argv),
                env=env,
                cwd=cwd or self.cwd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=TIMEOUT,
            )

    def workers(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.call(sys.executable, CREW, *args, **kw)

    def attach(self, *args: str, **kw) -> subprocess.CompletedProcess:
        return self.ok(self.workers("attach", *args, **kw))

    def send(self, name: str, text: str, **kw) -> subprocess.CompletedProcess:
        return self.ok(self.call(sys.executable, TMUX_HOST, "send", name, text, **kw))

    def kill(self, name: str) -> subprocess.CompletedProcess:
        return self.ok(self.server.tmux("kill-session", "-t", f"={name}"))

    def scene(self, name: str, steps: list | None = None, turns=(), **codex) -> dict[str, str]:
        """Both fakes' scenario files for worker `name`, as the env a crew call passes on to its worker."""
        steps = [f"Hi from {name}."] if steps is None else steps
        claude, scenario = (
            os.path.join(self.root, f"{name}.{k}.json") for k in ("claude", "codex")
        )
        with open(claude, "w") as f:
            json.dump({"steps": steps, "turns": list(turns), "log": self.claude_log(name)}, f)
        with open(scenario, "w") as f:
            json.dump({"tui_turns": [steps, *turns], **codex}, f)
        return {
            fake_claude.ENV: claude,
            "FAKE_SCENARIO": scenario,
            "FAKE_LOG": self.codex_log(name),
        }

    def claude_log(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.claude.jsonl")

    def codex_log(self, name: str) -> str:
        return os.path.join(self.root, f"{name}.codex.jsonl")

    def start(
        self,
        name: str,
        *args: str,
        steps: list | None = None,
        turns=(),
        inside: str | None = MANAGER,
        codex: dict | None = None,
        **extra: str,
    ) -> subprocess.CompletedProcess:
        return self.workers(
            "start",
            name,
            "--cwd",
            self.cwd,
            "--prompt",
            "hi",
            *args,
            inside=inside,
            **self.scene(name, steps, turns, **(codex or {})),
            **extra,
        )

    def started(self, name: str, *args: str, **kw) -> str:
        res = self.ok(self.start(name, *args, **kw))
        m = re.fullmatch(rf"{re.escape(name)} ({roster.SID.pattern})\n", res.stdout)
        self.assertIsNotNone(m, res.stdout)
        return m[1]

    def restart(self, name: str, steps: list, **extra: str) -> subprocess.CompletedProcess:
        return self.ok(self.workers("restart", name, **self.scene(name, steps), **extra))

    def calls(self, name: str) -> list[dict]:
        return logged(self.claude_log(name))

    def codex_calls(self, name: str) -> list[dict]:
        return [e for e in logged(self.codex_log(name)) if e.get("mode") == "tui"]

    def native(self, name: str) -> str:
        (native,) = {
            e["payload"]["session_id"] for e in logged(self.codex_log(name)) if "hook" in e
        }
        return native

    def next_event(self, after: int, *args: str, gen: int = 0, **kw) -> tuple[int, str, str]:
        out = self.ok(
            self.workers("next-event", *args, "--after", str(after), "--gen", str(gen), **kw)
        ).stdout
        head, _, content = out.partition("\n")
        n, _, event = head.partition(" ")
        return int(n), event, content.removesuffix("\n")

    def wait_event(
        self, name: str, kind: str, count: int = 1, events: str | None = None
    ) -> list[str]:
        path = events or self.events

        def check():
            self.poke()
            lines = live_tmux.events(path)
            return lines if sum(is_event(line, name, kind) for line in lines) >= count else None

        return live_tmux.wait(check, what=f"{count} `{name} {kind}` events in {path}")

    def record(self, sid: str) -> dict:
        path = os.path.join(self.agent_pm, "workers", sid, crew.RECORD)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        with open(path) as f:
            return json.load(f)

    def roster(self, directory: str | None = None) -> dict:
        with open(os.path.join(directory or self.directory, "roster.json")) as f:
            return json.load(f)

    def display(self, name: str, fmt: str) -> str:
        return self.ok(
            self.server.tmux("display-message", "-p", "-t", f"={name}:", fmt)
        ).stdout.removesuffix("\n")

    def pane_text(self, name: str, lines: int) -> str:
        text = self.ok(
            self.server.tmux("capture-pane", "-p", "-J", "-t", f"={name}:", "-S", f"-{lines}")
        ).stdout
        rows = text.splitlines()
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(rows[-lines:])

    def shown(self, name: str) -> str:
        """The pane showing `name` in the manager's window."""
        pane = self.server.pane_of(name)
        self.assertIn(pane, self.server.panes(f"={MANAGER}:"))
        return pane

    def grid(self, columns: list) -> None:
        def check():
            return live_tmux.assert_grid(self, self.server, MANAGER, columns)

        with contextlib.suppress(AssertionError):
            live_tmux.wait(check)
        check()

    def live(self, *names: str) -> bool:
        return all(self.server.tmux("has-session", "-t", f"={n}").returncode == 0 for n in names)

    def gone(self, *names: str) -> bool:
        return not any(self.live(n) for n in names)

    def dumping(self, runtime: str, fake: str) -> str:
        """Puts a `runtime` on PATH that logs its argv and env, then runs `fake`; returns that log."""
        path = os.path.join(self.root, f"{runtime}.env.jsonl")
        q = shlex.quote
        with open(os.path.join(self.server.bin, runtime), "w") as f:
            f.write(
                f'#!/bin/sh\n{q(sys.executable)} -I -c {q(ENV_DUMP)} {q(path)} "$@"\n'
                f'exec {q(sys.executable)} -I {q(fake)} "$@"\n'
            )
        return path

    def second(self) -> None:
        self.ok(self.server.tmux("new-session", "-d", "-s", SECOND, *live_tmux.IDLE))
        self.server.attach(SECOND)

    def role(self, work: str, *sessions: str) -> dict:
        """A role run's entry in the manager's roster, as drive.py --detach writes one, and its sessions."""
        for session in sessions:
            self.ok(self.server.tmux("new-session", "-d", "-s", session, *live_tmux.IDLE))
        resume = [sys.executable, os.path.join(self.root, "drive.py"), f"--role={ROLE}"]
        e = roster.entry("role", sid=ROLE_SID, cwd=work, resume=resume, tui=ROLE_TUI)
        roster.put(self.directory, ROLE_DRIVER, e)
        return e

    def recover(self, name: str, **extra: str) -> subprocess.CompletedProcess:
        e = self.roster()["entries"][name]
        return self.call(*roster.recovery(e), cwd=e["cwd"], **extra)

    def worker_entry(self, name: str, sid: str, *rest: str, client: str = "claude") -> list[str]:
        return [
            sys.executable,
            CREW,
            "start",
            name,
            "--manager",
            MANAGER,
            "--client",
            client,
            "--resume",
            sid,
            "--cwd",
            self.cwd,
            *rest,
        ]


class ClaudeLifecycleTest(Live):
    def test_start_done_next_event_reply_send_done_reply(self):
        res = self.ok(self.start("w1", turns=[["Second."]]))
        sid = res.stdout.split()[1]
        self.assertEqual(res.stdout, f"w1 {sid}\n")
        self.assertEqual(res.stderr, "tui: session w1: tmux attach -t '=w1'\n")
        self.shown("w1")
        (call,) = self.calls("w1")
        argv = call["argv"]
        self.assertEqual(
            argv[:5] + argv[6:], ["--session-id", sid, "--name", "w1", "--settings", "--", "hi"]
        )
        self.assertEqual(set(json.loads(argv[5])), {"hooks"})
        self.assertEqual(set(json.loads(argv[5])["hooks"]), CLAUDE_HOOKS)
        self.assertEqual(call["cwd"], self.cwd)
        self.assertEqual(
            self.record(sid),
            {
                "version": 1,
                "name": "w1",
                "sid": sid,
                "runtime": "claude",
                "cwd": self.cwd,
                "flags": [],
                "env": {},
                "events": self.events,
                "manager": self.directory,
                "split": None,
                "split_from": None,
            },
        )
        self.assertEqual(self.roster()["entries"]["w1"]["resume"], self.worker_entry("w1", sid))
        (done,) = self.wait_event("w1", "done")
        self.assertEqual(self.server.option("w1", "@state"), "done")
        self.assertEqual(self.next_event(0), (1, done, "Hi from w1."))
        self.send("w1", "more")
        n, event, content = self.next_event(1)
        self.assertEqual((n, content), (2, "Second."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(live_tmux.events(self.events), [done, event])
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Second.\n")
        self.assertEqual(len(self.calls("w1")), 1)

    def test_blocked_on_a_hook(self):
        # the dialog's text: a hook step prints nothing, so the pane, next-event's content, would be blank
        hook = {"kind": "hook", "event": "PermissionRequest", "tool_name": "Edit"}
        res = self.ok(self.start("w1", steps=["Allow this edit?", hook]))
        self.assertIn("workers: w1: waits on a dialog: Edit", res.stderr.splitlines())
        lines = self.wait_event("w1", "done")
        self.assertEqual(
            [line.split(" ")[1:] for line in lines], [["w1", "blocked"], ["w1", "done"]]
        )
        n, event, content = self.next_event(0)
        self.assertEqual((n, event), (1, lines[0]))
        self.assertIn("Allow this edit?", content)
        self.assertEqual(content, self.pane_text("w1", crew.PANE_LINES))

    def test_dead_then_restart_resumes_the_sid_in_a_new_pane(self):
        sid = self.started("w1")
        self.wait_event("w1", "done")
        pane = self.shown("w1")
        os.kill(self.calls("w1")[-1]["pid"], signal.SIGTERM)
        _, dead = self.wait_event("w1", "dead")
        self.assertEqual(self.server.option("w1", "@state"), "dead")
        n, event, content = self.next_event(1)
        self.assertEqual((n, event), (2, dead))
        self.assertTrue(content)
        self.assertEqual(content, self.pane_text("w1", crew.PANE_LINES))
        res = self.restart("w1", ["Resumed."])
        self.assertEqual((res.stdout, res.stderr), ("", "tui: session w1: tmux attach -t '=w1'\n"))
        _, again = self.calls("w1")
        self.assertTrue(has_pair(again["argv"], "--resume", sid), again["argv"])
        self.assertTrue(has_pair(again["argv"], "--name", "w1"), again["argv"])
        self.assertNotIn("--session-id", again["argv"])
        self.assertEqual(self.display("w1", "#{pane_dead}"), "0")
        self.assertEqual(self.server.option("w1", "@state"), "")
        self.assertNotEqual(self.shown("w1"), pane)
        self.send("w1", "again")
        self.wait_event("w1", "done", 2)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Resumed.\n")
        self.assertEqual(self.roster()["entries"]["w1"]["resume"], self.worker_entry("w1", sid))

    def test_an_early_death_exits_1_with_its_last_lines_its_session_kept(self):
        res = self.start("w2", "--", "--bogus")
        self.assertEqual(res.returncode, 1, res.stderr)
        lines = res.stderr.splitlines()
        head = "workers: w2: claude exited 2 at once; its pane's last lines:"
        self.assertIn(head, lines, res.stderr)
        at = lines.index(head)
        self.assertEqual(lines[:at], ["tui: session w2: tmux attach -t '=w2'"])
        self.assertIn("claude: error: unrecognized arguments: --bogus", lines[at + 1 :])
        self.assertTrue(self.live("w2"))
        self.assertEqual(self.display("w2", "#{pane_dead}"), "1")


class CodexLifecycleTest(Live):
    def test_start_blocked_done_reply_follow_up(self):
        steps = ["Allow this command?", {"kind": "permission"}, "Hi from w1."]
        res = self.ok(
            self.start(
                "w1", "--client", "codex", "--", "-m", "gpt-test", steps=steps, turns=[["Second."]]
            )
        )
        sid = res.stdout.split()[1]
        self.assertEqual(res.stdout, f"w1 {sid}\n")
        self.assertIn("workers: w1: waits on a dialog: Bash", res.stderr.splitlines())
        blocked, done = self.wait_event("w1", "done")
        self.assertTrue(is_event(blocked, "w1", "blocked"), blocked)
        self.shown("w1")
        (call,) = self.codex_calls("w1")
        argv = call["argv"]
        self.assertEqual(argv[:5], ["--no-daemon", "-C", self.cwd, "-c", NOTIFY])
        self.assertEqual(
            [w.partition("=")[0] for w in argv[6:17:2]], [f"hooks.{e}" for e in CODEX_HOOKS]
        )
        self.assertEqual(argv[17:20] + argv[21:], ["-m", "gpt-test", "-c", "--", "hi"])
        self.assertTrue(argv[20].startswith("hooks.state="), argv[20])
        self.assertNotIn("-s", argv)
        self.assertEqual(call["env"]["CODEX_HOME"], self.codex_home)
        self.assertEqual(
            {k: self.record(sid)[k] for k in ("runtime", "flags", "env")},
            {"runtime": "codex", "flags": ["-m", "gpt-test"], "env": {}},
        )
        self.assertEqual(
            self.roster()["entries"]["w1"]["resume"],
            self.worker_entry("w1", sid, "--", "-m", "gpt-test", client="codex"),
        )
        n, event, content = self.next_event(0)
        self.assertEqual((n, event), (1, blocked))
        self.assertIn("Allow this command?", content)
        self.assertEqual(self.next_event(1), (2, done, "Hi from w1."))
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Hi from w1.\n")
        self.send("w1", "more")
        n, event, content = self.next_event(2)
        self.assertEqual((n, content), (3, "Second."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Second.\n")
        self.assertEqual(len(self.codex_calls("w1")), 1)

    def test_the_trust_dialog_waits_on_a_dialog(self):
        release = os.path.join(self.root, "release")
        steps = [
            {"kind": "screen", "text": "Trust this folder?"},
            {"kind": "pause", "file": release},
        ]
        res = self.ok(self.start("w1", "--client", "codex", steps=steps))
        self.assertIn("workers: w1: waits on a dialog: Trust this folder?", res.stderr.splitlines())
        (blocked,) = self.wait_event("w1", "blocked")
        self.assertEqual(self.server.option("w1", "@state"), "blocked")
        n, event, content = self.next_event(0)
        self.assertEqual((n, event), (1, blocked))
        self.assertIn("Trust this folder?", content)

    def test_dead_then_restart_and_recovery_from_a_claude_session_resume_the_native_session(self):
        sid = self.started("w1", "--client", "codex", "--", "-m", "gpt-test")
        self.wait_event("w1", "done")
        native = self.native("w1")
        (first,) = self.codex_calls("w1")
        os.kill(first["pid"], signal.SIGTERM)
        self.wait_event("w1", "dead")
        self.restart("w1", ["Resumed."])
        _, again = self.codex_calls("w1")
        self.assertEqual(again["argv"][:2], ["resume", native])
        self.assertEqual(again["argv"][2:], first["argv"][:-2])
        self.send("w1", "again")
        self.wait_event("w1", "done", 2)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Resumed.\n")
        self.assertEqual(self.native("w1"), native)
        self.assertEqual(
            self.roster()["entries"]["w1"]["resume"],
            self.worker_entry("w1", sid, "--", "-m", "gpt-test", client="codex"),
        )
        self.kill("w1")
        out = self.attach("--resume", **CLAUDE_SESSION, **self.scene("w1", ["Recovered."])).stdout
        self.assertRegex(out, rf"\nresumed w1: exit 0: w1 {sid}\n\Z")
        *_, recovered = self.codex_calls("w1")
        self.assertEqual(recovered["argv"], again["argv"])
        self.assertIsNone(recovered["env"]["CLAUDECODE"])
        self.send("w1", "again")
        self.wait_event("w1", "done", 3)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Recovered.\n")
        self.assertEqual(self.native("w1"), native)


class RuntimeTest(Live):
    def test_a_worker_started_in_a_codex_worker_defaults_to_codex(self):
        self.started("w1", "--client", "codex")
        self.wait_event("w1", "done")
        self.attach(inside="w1")
        sid = self.started("z1", inside="w1", **codex_session(self.native("w1")))
        inner = self.events_of("w1")
        self.assertEqual(
            {k: self.record(sid)[k] for k in ("runtime", "events")},
            {"runtime": "codex", "events": inner},
        )
        self.assertEqual(self.calls("z1"), [])
        (call,) = self.codex_calls("z1")
        self.assertEqual(
            {k: call["env"][k] for k in (*fake_codex.MARKERS, *fake_codex.DEFAULTS)},
            dict.fromkeys((*fake_codex.MARKERS, *fake_codex.DEFAULTS)),
        )
        self.wait_event("z1", "done", events=inner)
        self.assertNotEqual(self.native("z1"), self.native("w1"))
        resume = self.roster(os.path.dirname(inner))["entries"]["z1"]["resume"]
        self.assertTrue(has_pair(resume, "--client", "codex"), resume)

    def test_a_top_level_manager_defaults_to_the_configs_runtime(self):
        sid = self.started("w1", **codex_session())
        self.assertEqual(self.record(sid)["runtime"], "claude")
        self.assertEqual((len(self.calls("w1")), self.codex_calls("w1")), (1, []))
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
            f.write('runtime = "codex"\n')
        sid = self.started("w2")
        self.assertEqual(self.record(sid)["runtime"], "codex")
        self.assertEqual((self.calls("w2"), len(self.codex_calls("w2"))), ([], 1))

    def test_env_scrubbed_both_ways(self):
        claude = self.dumping("claude", fake_claude.__file__)
        codex = self.dumping("codex", fake_tui.__file__)
        self.started("w1", "--client", "codex", **CLAUDE_SESSION, NO_COLOR="1")
        self.started("c1", "--client", "claude", **codex_session())
        self.started("w2", "--client", "codex", **codex_session())
        (w1, w2) = (e["env"] for e in logged(codex) if e["argv"][:1] != ["app-server"])
        (c1,) = (e["env"] for e in logged(claude))
        identity = (*CLAUDE_SESSION, "CODEX_THREAD_ID", "CODEX_SANDBOX")
        for env in (w1, c1, w2):
            self.assertEqual({k: env[k] for k in identity if k in env}, {})
        self.assertEqual(w1["NO_COLOR"], "1")
        for env in (c1, w2):
            self.assertEqual({k: env[k] for k in fake_codex.DEFAULTS if k in env}, {})
        self.wait_event("w2", "done")
        screen = self.ok(self.server.tmux("capture-pane", "-p", "-e", "-t", "=w2:")).stdout
        self.assertRegex(screen, r"\x1b\[[0-9;]*36m\s*Hi from w2\.")

    def test_a_codex_worker_whose_cwd_covers_the_workers_dir_is_refused(self):
        workers = os.path.join(self.agent_pm, "workers")
        os.mkdir(workers, 0o700)
        res = self.workers(
            "start",
            "w1",
            "--client",
            "codex",
            "--cwd",
            workers,
            "--prompt",
            "hi",
            **self.scene("w1"),
        )
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (
                1,
                "",
                f"workers: Codex tui writable root {workers} could rewrite hook dependency {workers}\n",
            ),
        )
        self.assertTrue(self.gone("w1"))
        self.assertEqual(logged(self.codex_log("w1")), [])
        self.assertNotIn("w1", self.roster()["entries"])


class ManagersTest(Live):
    ATTACH = False

    def test_own_session_and_worker_in_worker(self):
        self.attach()
        sid = self.started("w1")
        self.assertEqual(self.record(sid)["events"], self.events)
        self.wait_event("w1", "done")
        self.attach(inside="w1")
        sid = self.started("w2", inside="w1")
        inner = self.events_of("w1")
        self.assertEqual(self.record(sid)["events"], inner)
        self.assertEqual(
            [x.split(" ")[1:] for x in self.wait_event("w2", "done", events=inner)],
            [["w2", "done"]],
        )
        self.assertEqual(
            [x.split(" ")[1:] for x in live_tmux.events(self.events)], [["w1", "done"]]
        )

    def test_manager_flag(self):
        self.attach("--manager", "other")
        sid = self.started("w1", "--manager", "other")
        other = self.events_of("other")
        self.assertEqual(self.record(sid)["events"], other)
        (done,) = self.wait_event("w1", "done", events=other)
        self.assertEqual(self.next_event(0, "--manager", "other"), (1, done, "Hi from w1."))
        self.assertFalse(os.path.exists(self.directory))

    def test_no_pane_needs_a_manager(self):
        res = self.start("w1", inside=None)
        self.assertEqual(
            (res.returncode, res.stderr),
            (1, "workers: no manager directory: run inside tmux or give --manager <name>\n"),
        )
        self.assertFalse(os.path.exists(os.path.dirname(self.directory)))
        self.assertTrue(self.gone("w1"))


class RosterTest(Live):
    ATTACH = False

    def held(self, holder: str) -> str:
        h = self.roster()["holder"]
        self.assertEqual(h["session"], holder)
        return (
            f"workers: manager directory {self.directory}: held by tmux session {holder} since {h['since']}; ask "
            "that manager to run crew.py release, or end that session\n"
        )

    def refused(self) -> None:
        before = self.roster()
        res = self.workers("attach", "--manager", MANAGER, inside=SECOND)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", self.held(MANAGER)))
        self.assertEqual(self.roster(), before)

    def taken_over(self) -> None:
        res = self.attach("--manager", MANAGER, inside=SECOND)
        self.assertEqual(res.stdout, f"{crew.HEADER}\n{tail(self.directory)}")
        self.assertEqual(self.roster()["holder"]["session"], SECOND)

    def after_w1(self, out: str, rest: str) -> None:
        self.assertRegex(
            out, rf"\A{re.escape(crew.HEADER)}\nw1\tworker\t[^\n]*\n{re.escape(rest)}\Z"
        )

    def test_second_manager_after_release(self):
        self.attach()
        self.second()
        self.refused()
        self.assertEqual(
            self.ok(self.workers("release")).stdout, f"workers: released {self.directory}\n"
        )
        self.assertIsNone(self.roster()["holder"])
        self.taken_over()

    def test_second_manager_after_the_holder_ends(self):
        self.attach()
        self.second()
        self.refused()
        self.kill(MANAGER)
        self.taken_over()

    def test_concurrent_attaches_one_holds(self):
        self.second()
        procs = {
            s: subprocess.Popen(
                [sys.executable, CREW, "attach", "--manager", MANAGER],
                env=self.server.env(**self.server.inside(s)),
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for s in (MANAGER, SECOND)
        }
        for p in procs.values():
            self.addCleanup(p.communicate)  # cleanups run last first: after the kill, reaps it
            self.addCleanup(p.kill)
        out = {s: p.communicate(timeout=TIMEOUT) for s, p in procs.items()}
        winners = [s for s, p in procs.items() if p.returncode == 0]
        self.assertEqual(len(winners), 1, out)
        (winner,) = winners
        (loser,) = set(procs) - {winner}
        self.assertEqual(out[winner], (f"{crew.HEADER}\n{tail(self.directory)}", ""))
        self.assertEqual((procs[loser].returncode, out[loser]), (1, ("", self.held(winner))))

    def test_killed_worker_gone_then_recovered(self):
        self.attach()
        sid = self.started("w1")
        pane = self.server.pane_of("w1")
        resume = self.worker_entry("w1", sid)
        e = self.roster()["entries"]["w1"]
        self.assertEqual(
            {k: e[k] for k in ("kind", "sid", "cwd", "resume", "pane", "opener", "state")},
            {
                "kind": "worker",
                "sid": sid,
                "cwd": self.cwd,
                "resume": resume,
                "pane": pane,
                "opener": MANAGER,
                "state": "working",
            },
        )
        (done,) = self.wait_event("w1", "done")  # its transcript, which --resume needs
        self.kill("w1")
        self.assertEqual(
            self.attach().stdout,
            f"{crew.HEADER}\nw1\tworker\t{sid}\tgone\t-\t{self.cwd}\t{pane}\n"
            f"resume w1: {shlex.join(resume)}\n"
            f"{tail(self.directory, 0, 0, f'1 {done}')}",
        )
        self.assertEqual(self.roster()["entries"]["w1"]["state"], "gone")
        res = self.ok(self.recover("w1", **self.scene("w1")))
        self.assertEqual(res.stdout, f"w1 {sid}\n")
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sid))
        self.grid([["w1"]])
        pane = self.server.pane_of("w1")
        table = f"{crew.HEADER}\nw1\tworker\t{sid}\tworking\t-\t{self.cwd}\t{pane}\tshown in {MANAGER}\n"
        out = self.attach().stdout
        self.assertEqual(out[: len(table)], table, out)
        self.assertEqual(list(self.roster()["entries"]), ["w1"])

    def test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one(self):
        self.attach()
        sids = {name: self.started(name) for name in ("w1", "w2")}
        for name in sids:
            self.wait_event(name, "done")  # its transcript, which --resume needs
        self.kill("w1")
        self.assertEqual(self.ok(self.workers("stop", "w2")).stdout, "")
        self.assertTrue(self.gone("w1", "w2"))
        self.assertEqual(list(self.roster()["entries"]), ["w1"])
        out = self.attach().stdout
        self.assertIn("\nresume w1: ", out)
        self.assertNotIn("resumed", out)
        self.assertTrue(self.gone("w1"))
        out = self.attach("--resume", **self.scene("w1")).stdout
        self.assertRegex(out, rf"\nnext-event: [^\n]+\nresumed w1: exit 0: w1 {sids['w1']}\n\Z")
        self.assertTrue(self.live("w1") and self.gone("w2"))
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sids["w1"]))
        self.grid([["w1"]])
        self.attach()
        self.assertEqual(
            {n: e["state"] for n, e in self.roster()["entries"].items()}, {"w1": "working"}
        )

    def test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker(self):
        self.attach()
        sids = {name: self.started(name) for name in ("w1", "w2")}
        for name in sids:
            self.wait_event(name, "done")
        self.ok(self.workers("stop", "w2"))
        self.server.kill()
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        out = self.attach("--resume", **self.scene("w1")).stdout
        self.assertRegex(out, rf"\nresumed w1: exit 0: w1 {sids['w1']}\n\Z")
        self.assertTrue(self.live("w1") and self.gone("w2"))
        self.assertTrue(has_pair(self.calls("w1")[-1]["argv"], "--resume", sids["w1"]))
        self.attach()
        self.assertEqual(
            {n: e["state"] for n, e in self.roster()["entries"].items()}, {"w1": "working"}
        )

    def test_stop_all_stops_what_mgr_opened_and_only_that(self):
        self.attach()
        self.started("w1")
        self.started("w2")
        self.attach(inside="w1")
        self.started("w3", inside="w1")
        self.role(self.cwd, ROLE_DRIVER, ROLE_TUI)
        self.second()
        self.ok(self.server.tmux("new-session", "-d", "-s", "o1", *live_tmux.IDLE))
        self.ok(self.server.tmux("set-option", "-t", "=o1:", "@opener", SECOND))
        mine = ["w3", ROLE_TUI, ROLE_DRIVER, "w1", "w2"]
        res = self.ok(self.workers("stop-all", "--dry-run"))
        self.assertEqual(res.stdout, "".join(f"would stop {n}\n" for n in mine))
        self.assertTrue(self.live(*mine, "o1"))
        res = self.ok(self.workers("stop-all"))
        self.assertEqual(res.stdout, "".join(f"stopped {n}\n" for n in mine))
        self.assertTrue(self.gone(*mine))
        self.assertTrue(self.live(MANAGER, SECOND, "o1"))
        self.assertEqual(self.roster()["entries"], {})
        self.assertEqual(self.roster(os.path.dirname(self.events_of("w1")))["entries"], {})
        self.assertEqual(
            self.ok(self.workers("stop-all")).stdout, "workers: stop-all: nothing to stop\n"
        )

    def test_stop_a_role_run_by_its_tui_session(self):
        self.attach()
        self.role(self.cwd, ROLE_DRIVER, ROLE_TUI)
        self.assertEqual(list(self.roster()["entries"]), [ROLE_DRIVER])
        self.assertEqual(self.ok(self.workers("stop", ROLE_TUI)).stdout, "")
        self.assertTrue(self.gone(ROLE_DRIVER, ROLE_TUI))
        self.assertEqual(self.roster()["entries"], {})

    def test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume(self):
        self.attach()
        work = os.path.join(self.root, "role-work")
        os.mkdir(work)
        with open(os.path.join(work, "run.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "result", "outcome": {"status": "needs_input"}}) + "\n")
        self.role(work)
        table = f"{crew.HEADER}\n{ROLE_DRIVER}\trole\t{ROLE_SID}\twaiting\t-\t{work}\t-\n"
        self.assertEqual(self.attach().stdout, table + tail(self.directory))
        self.assertEqual(self.attach("--resume").stdout, table + tail(self.directory))
        self.assertEqual(self.roster()["entries"][ROLE_DRIVER]["state"], "waiting")

    def test_unshown_worker_reopened(self):
        self.attach()
        sid = self.started("w1")
        (done,) = self.wait_event("w1", "done")
        self.grid([["w1"]])
        closed = self.server.pane_of("w1")
        self.ok(self.server.tmux("kill-pane", "-t", closed))
        live_tmux.wait(
            lambda: not self.ok(self.server.tmux("list-clients", "-t", "=w1")).stdout,
            what="w1 shown by no client",
        )
        res = self.attach()
        pane = self.server.pane_of("w1")
        self.assertNotEqual(pane, closed)
        self.grid([["w1"]])
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)
        self.assertEqual(
            res.stdout,
            f"{crew.HEADER}\nw1\tworker\t{sid}\tdone\t-\t{self.cwd}\t{pane}\n"
            f"{tail(self.directory, 0, 0, f'1 {done}')}",
        )
        self.assertEqual(res.stderr, "tui: session w1: tmux attach -t '=w1'\n")
        e = self.roster()["entries"]["w1"]
        self.assertEqual((e["pane"], e["opener"]), (pane, MANAGER))

    def test_take_over_resumes_from_the_cursor(self):
        self.attach()
        self.started("w1", steps=["One."], turns=[["Two."], ["Three."], ["Four."]])
        (one,) = self.wait_event("w1", "done")
        self.assertEqual(self.next_event(0), (1, one, "One."))
        self.send("w1", "more")
        n, two, content = self.next_event(1)
        self.assertEqual((n, content), (2, "Two."))
        self.send("w1", "more")
        *_, three = self.wait_event("w1", "done", 3)
        resumed = tail(self.directory, 2, 0, f"3 {three}")
        self.after_w1(self.attach("--after", "2", "--gen", "0").stdout, resumed)
        self.kill(MANAGER)
        self.second()
        self.after_w1(self.attach("--manager", MANAGER, inside=SECOND).stdout, resumed)
        self.assertEqual(self.roster()["holder"]["session"], SECOND)
        self.assertEqual(
            self.next_event(2, "--manager", MANAGER, inside=SECOND), (3, three, "Three.")
        )
        waiting = subprocess.Popen(
            [
                sys.executable,
                CREW,
                "next-event",
                "--manager",
                MANAGER,
                "--after",
                "3",
                "--gen",
                "0",
            ],
            env=self.server.env(**self.server.inside(SECOND)),
            cwd=self.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(waiting.communicate)  # cleanups run last first: after the kill, reaps it
        self.addCleanup(waiting.kill)
        live_tmux.wait(lambda: self.roster()["cursor"] == 3, what="next-event's cursor 3")
        self.send("w1", "more", inside=SECOND)
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
            f.write(
                "".join(f"{line}\n" for line in filler("f", roster.EVENTS_MAX // WIDTH + 1) + kept)
            )
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
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr),
            (1, "workers: stale line numbers (gen 0, now 1): run crew.py attach\n", ""),
        )
        self.assertEqual(self.roster(), before)
        self.send("w1", "more")
        n, event, content = self.next_event(3, gen=1)
        self.assertEqual((n, content), (4, "Again."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(live_tmux.events(self.events), [*kept, event])
        self.assertEqual(raw(old), original)


if __name__ == "__main__":
    unittest.main()
