"""crew.py Codex workers against a private tmux server, fake_codex.py as `codex` (its interactive and app-server modes):
start with a prompt, the hook helper's blocked and done, the bound worker record, reply, follow-up, restart and recovery
keeping the native session, flags and NOTIFY, recovery from a Claude session; a Codex manager's session marker and
injected defaults kept out of a Codex worker (colors shown) and a Claude worker; a Codex worker's own crew.py start
defaulting to Codex."""
import json
import os
import re
import shlex
import stat
import sys
import unittest

from tests.integration.core import fake_claude, fake_codex, live_tmux
from tests.integration.tmux.crew_integration_test import CREW, MANAGER, Live, has_pair, is_event
import crew   # after crew_integration_test, which imports tests/hermetic.py first

NOTIFY = 'tui.notification_method="bel"'
HOOKED = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop", "Interrupt")
CODEX_TOOL_ENV = {"NO_COLOR": "1", "CODEX_CI": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat"}
GONE = ("CODEX_THREAD_ID", "CLAUDECODE", *CODEX_TOOL_ENV)
CALLER = "26900000-0000-4000-8000-000000000009"   # the Codex manager's own thread


def codex_session(thread: str = CALLER) -> dict[str, str]:
    """What Codex gives each command it runs: its thread's session marker and unified exec's forced values."""
    return {"CODEX_THREAD_ID": thread, **CODEX_TOOL_ENV}


class CodexLive(Live):
    """Live with fake_codex.py as `codex` too. Codex worker <name>'s scenario logs to its own file."""

    def setUp(self):
        super().setUp()
        fake_codex.install(self.server.bin)
        home = os.path.dirname(self.agent_pm)
        self.codex_home = os.path.realpath(os.path.join(home, ".codex"))
        self.records = os.path.join(os.path.realpath(home), ".agent-pm", "core-sessions", "workers")

    def codex_log(self, name: str) -> str:
        return os.path.join(self.server.root, f"{name}.codex.jsonl")

    def codex_scenario(self, name: str, steps: list, turns: list | tuple = ()) -> dict[str, str]:
        """Writes `name`'s scenario; the env naming it."""
        with open(self.scenario(name), "w") as f:
            json.dump({"steps": steps, "turns": list(turns), "log": self.codex_log(name)}, f)
        return {fake_codex.ENV: self.scenario(name)}

    def start_codex(self, name: str, *args: str, steps: list | None = None, turns: list | tuple = (),
                    inside: str | None = MANAGER, **extra: str) -> str:
        """`crew.py start <name> --cwd … --prompt hi args…`'s session id, its scenario `steps` (default one text,
        `Hi from <name>.`) and `turns`, env plus `extra`; fails unless it exits 0 printing `<name> <sid>`."""
        scenario = self.codex_scenario(name, [f"Hi from {name}."] if steps is None else steps, turns)
        res = self.ok(self.workers("start", name, "--cwd", self.cwd, "--prompt", "hi", *args, inside=inside,
                                   **scenario, **extra))
        m = re.fullmatch(rf"{re.escape(name)} ({crew.SESSION_ID.pattern})\n", res.stdout)
        self.assertIsNotNone(m, res.stdout)
        return m[1]

    def logged(self, name: str) -> list[dict]:
        try:
            with open(self.codex_log(name)) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def calls(self, name: str) -> list[dict]:
        """The fake codex's interactive starts for worker `name`, oldest first (Live.diagnose reads their pids)."""
        return [e for e in self.logged(name) if "argv" in e and e["argv"][:1] != ["app-server"]]

    def hooks_run(self, name: str) -> list[dict]:
        return [e for e in self.logged(name) if "hook" in e]

    def native(self, name: str) -> str:
        """The session id of `name`'s hook payloads, one for all."""
        (sid,) = {h["payload"]["session_id"] for h in self.hooks_run(name)}
        return sid

    def record(self, sid: str) -> dict:
        with open(os.path.join(self.records, f"{sid}.json")) as f:
            return json.load(f)

    def done_in(self, events: str, name: str, count: int = 1) -> list[str]:
        """`events`' lines once `count` of them are `HH:MM:SS <name> done`."""
        def check():
            lines = live_tmux.events(events)
            return lines if sum(is_event(line, name, "done") for line in lines) >= count else None

        return live_tmux.wait(check, what=f"{count} `{name} done` in {events}")

    def roster_entry(self, name: str) -> dict:
        with open(os.path.join(os.path.dirname(self.events), "roster.json")) as f:
            return json.load(f)["entries"][name]

    def no_session_env(self, env: dict[str, str]) -> None:
        """Fails unless the logged env has none of GONE: a session marker or a Codex tool's forced value."""
        self.assertEqual({k: env[k] for k in GONE if k in env}, {})


class CodexLifecycle(CodexLive):
    def test_start_blocked_done_reply_follow_up(self):
        sid = self.start_codex("w1", "--client", "codex", "--", "-m", "gpt-test",
                               steps=["Allow this command?", {"kind": "permission"}, "Hi from w1."],
                               turns=[["Second."]])
        blocked, done = self.wait_event("w1", "done")
        self.assertTrue(is_event(blocked, "w1", "blocked"), blocked)
        (call,) = self.calls("w1")
        argv = call["argv"]
        self.assertEqual(argv[:5], ["--no-daemon", "-C", self.cwd, "-c", NOTIFY])
        self.assertEqual(argv[-4:], ["-m", "gpt-test", "--", "hi"])
        self.assertEqual([w.partition("=")[0] for w in argv[6:-4:2]],
                         [*(f"hooks.{e}" for e in HOOKED), "hooks.state"])
        self.assertEqual(call["env"]["CODEX_HOME"], self.codex_home)
        hooks = self.hooks_run("w1")
        self.assertEqual([h["hook"] for h in hooks], ["SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop"])
        self.assertEqual([(h["exit"], h["stdout"]) for h in hooks], [(0, "")] * 4)
        native = self.native("w1")
        self.assertEqual(self.record(sid), {"version": 1, "sid": sid, "cwd": self.cwd, "codex_home": self.codex_home,
                                            "native": native})
        for path in (f"{sid}.json", f"{sid}.reply"):
            self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.records, path)).st_mode), 0o600, path)
        options = ("sid", "cwd", "client", "exe", "flags", "env")
        self.assertEqual({k: self.server.option("w1", f"@{k}") for k in options},
                         {"sid": sid, "cwd": self.cwd, "client": "codex", "exe": os.path.join(self.server.bin, "codex"),
                          "flags": '["-m", "gpt-test"]', "env": "{}"})
        self.assertEqual(self.roster_entry("w1")["resume"],
                         [sys.executable, CREW, "start", "w1", "--manager", MANAGER, "--client", "codex", "--resume",
                          sid, "--cwd", self.cwd, "--", "-m", "gpt-test"])
        n, event, content = self.next_event(0)
        self.assertEqual((n, event), (1, blocked))
        self.assertIn("Allow this command?", content)
        self.assertEqual(self.next_event(1), (2, done, "Hi from w1."))
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Hi from w1.\n")
        self.ok(self.tui("send", "w1", "more"))
        n, event, content = self.next_event(2)
        self.assertEqual((n, content), (3, "Second."))
        self.assertTrue(is_event(event, "w1", "done"), event)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Second.\n")
        self.assertEqual(self.native("w1"), native)
        self.assertEqual(len(self.calls("w1")), 1)

    def test_restart_and_recovery_from_a_claude_session_keep_the_native_session(self):
        sid = self.start_codex("w1", "--client", "codex", "--", "-m", "gpt-test")
        self.wait_event("w1", "done")
        native = self.native("w1")
        (first,) = self.calls("w1")
        self.ok(self.workers("restart", "w1", **self.codex_scenario("w1", ["Resumed."])))
        _, again = self.calls("w1")
        self.assertEqual(again["argv"][:7], ["resume", native, "-C", self.cwd, "--no-daemon", "-c", NOTIFY])
        self.assertEqual(again["argv"][7:], first["argv"][5:-2])
        self.ok(self.tui("send", "w1", "again"))
        self.wait_event("w1", "done", 2)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Resumed.\n")
        resumed = [h for h in self.hooks_run("w1") if h["pid"] == again["pid"]]
        self.assertEqual(resumed[0]["payload"]["source"], "resume")
        self.assertEqual(self.native("w1"), native)
        self.ok(self.kill("w1"))
        out = self.attach("--resume", CLAUDECODE="1", **self.codex_scenario("w1", ["Recovered."])).stdout
        self.assertRegex(out, rf"\nresumed w1: exit 0: w1 {sid}\n\Z")
        *_, recovered = self.calls("w1")
        self.assertEqual(recovered["argv"][:2], ["resume", native])
        self.assertEqual(recovered["argv"][2:], again["argv"][2:])
        self.no_session_env(recovered["env"])
        self.assertEqual(self.server.option("w1", "@client"), "codex")
        self.ok(self.tui("send", "w1", "again"))
        self.wait_event("w1", "done", 3)
        self.assertEqual(self.ok(self.workers("reply", "w1")).stdout, "Recovered.\n")
        self.assertEqual(self.native("w1"), native)
        self.assertEqual(self.record(sid)["native"], native)


class CodexSession(CodexLive):
    """crew.py run as a Codex session's command: codex_session()'s env."""

    def claude_env_log(self) -> str:
        """Puts a `claude` on PATH that appends its env to a file, a JSON line, then runs fake_claude.py; the file."""
        path = os.path.join(self.server.root, "claude-env.jsonl")
        dump = "import json, os, sys; open(sys.argv[1], 'a').write(json.dumps(dict(os.environ)) + '\\n')"
        python, q = sys.executable, shlex.quote
        with open(os.path.join(self.server.bin, "claude"), "w") as f:
            f.write(f'#!/bin/sh\n{q(python)} -I -c {q(dump)} {q(path)}\n'
                    f'exec {q(python)} {q(os.path.abspath(fake_claude.__file__))} "$@"\n')
        return path

    def test_a_codex_managers_marker_and_injected_defaults_stay_out_of_its_workers(self):
        session = codex_session()
        sid = self.start_codex("w1", **session)
        self.wait_event("w1", "done")
        self.assertEqual(self.server.option("w1", "@client"), "codex")
        (call,) = self.calls("w1")
        self.no_session_env(call["env"])
        screen = self.ok(self.server.tmux("capture-pane", "-p", "-e", "-t", "=w1:")).stdout
        self.assertRegex(screen, r"\x1b\[[0-9;]*36m\s*Hi from w1\.")

        env_log = self.claude_env_log()
        with open(self.scenario("c1"), "w") as f:
            json.dump({"steps": ["Hi from c1."], "log": self.log}, f)
        self.ok(self.workers("start", "c1", "--client", "claude", "--cwd", self.cwd, "--prompt", "hi",
                             **{fake_claude.ENV: self.scenario("c1")}, **session))
        self.assertEqual(self.server.option("c1", "@client"), "claude")
        with open(env_log) as f:
            (env,) = [json.loads(line) for line in f]
        self.no_session_env(env)

        self.ok(self.workers("restart", "w1", **self.codex_scenario("w1", []), **session))
        _, again = self.calls("w1")
        self.no_session_env(again["env"])
        self.ok(self.kill("w1"))
        out = self.attach("--resume", **self.codex_scenario("w1", []), **session).stdout
        self.assertRegex(out, rf"\nresumed w1: exit 0: w1 {sid}\n\Z")
        *_, recovered = self.calls("w1")
        self.assertEqual(recovered["argv"][:2], ["resume", self.native("w1")])
        self.no_session_env(recovered["env"])

    def test_a_codex_workers_own_crew_start_defaults_to_codex(self):
        self.start_codex("w1", "--client", "codex")
        self.wait_event("w1", "done")
        self.attach(inside="w1")
        sid = self.start_codex("z1", inside="w1", **codex_session(self.native("w1")))
        inner = self.events_of("w1")
        self.assertEqual({k: self.server.option("z1", k) for k in ("@client", "@events")},
                         {"@client": "codex", "@events": inner})
        (call,) = self.calls("z1")
        self.no_session_env(call["env"])
        self.done_in(inner, "z1")
        self.assertEqual(self.record(sid)["native"], self.native("z1"))
        self.assertNotEqual(self.native("z1"), self.native("w1"))
        with open(os.path.join(os.path.dirname(inner), "roster.json")) as f:
            self.assertTrue(has_pair(json.load(f)["entries"]["z1"]["resume"], "--client", "codex"))


if __name__ == "__main__":
    unittest.main()
