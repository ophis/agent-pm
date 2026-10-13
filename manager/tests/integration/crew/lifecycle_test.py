"""Lifecycle behavior."""

import json
import os
import signal

from agent_pm.manager.engine import monitor
from agent_pm.utils import LOCAL

from manager.tests.integration.crew.case import Live
from manager.tests.integration.crew.data import (
    CLAUDE_HOOKS,
    CLAUDE_SESSION,
    CODEX_HOOKS,
    NOTIFY,
    codex_session,
    has_pair,
    is_event,
    logged,
)
from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude
from session.tests.integration.adapters.codex import fake_codex, fake_tui


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
            argv[:7] + argv[8:],
            [
                "--session-id",
                sid,
                "--name",
                "w1",
                "--setting-sources",
                "user,project,local",
                "--settings",
                "--",
                "hi",
            ],
        )
        self.assertEqual(set(json.loads(argv[7])), {"hooks"})
        self.assertEqual(set(json.loads(argv[7])["hooks"]), CLAUDE_HOOKS)
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
        # the dialog's text: a hook step prints nothing, so the pane, next-event's content, would be
        # blank
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
        self.assertEqual(content, self.pane_text("w1", monitor.PANE_LINES))

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
        self.assertEqual(content, self.pane_text("w1", monitor.PANE_LINES))
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
        self.assertEqual(argv[:7], ["--no-daemon", "-C", self.cwd, "-c", NOTIFY, "-m", "gpt-test"])
        self.assertEqual(
            [w.partition("=")[0] for w in argv[8:19:2]], [f"hooks.{e}" for e in CODEX_HOOKS]
        )
        self.assertEqual(argv[19:20] + argv[21:], ["-c", "--", "hi"])
        self.assertTrue(argv[20].startswith("hooks.state="), argv[20])
        self.assertNotIn("-s", argv)
        policy = ("approval_policy", "approvals_reviewer", "sandbox_workspace_write")
        self.assertFalse([w for w in argv if w.startswith(policy)], argv)
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
        with open(os.path.join(self.agent_pm, os.path.basename(LOCAL)), "w") as f:
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
            self.assertEqual({k: env[k] for k in (*identity, "PYTHONPATH") if k in env}, {})
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
                f"workers: Codex tui writable root {workers} could rewrite "
                f"hook dependency {workers}\n",
            ),
        )
        self.assertTrue(self.gone("w1"))
        self.assertEqual(logged(self.codex_log("w1")), [])
        self.assertNotIn("w1", self.roster()["entries"])
