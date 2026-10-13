"""fake hooks integration test behavior."""

import json
import os
import shlex
import time
from unittest import mock

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import Access, SessionRequest, Spec

from session.tests import live_tmux
from session.tests.integration.adapters.claude import fake_claude
from session.tests.integration.adapters.claude.fake_fixtures import LIMIT, SESSION, SID, TRUST, Case


class Hooks(Case):
    def test_payloads_and_order(self):
        self.scene(
            steps=[
                {"kind": "tool", "name": "Read"},
                {
                    "kind": "hook",
                    "event": "PermissionRequest",
                    "tool_name": "Bash",
                    "cwd": "/x",
                    "n": [1],
                },
                "Done.",
            ],
            turns=[["Next."], []],
            background=2,
        )
        events = ("UserPromptSubmit", "PostToolUse", "PermissionRequest", "Stop")
        res = self.run_fake(
            *SESSION, "--settings", self.settings(*events), "--", "hi", stdin="next\nlast\n"
        )
        self.assertEqual((res.returncode, res.stdout), (0, "Done.\nNext.\n"), res.stderr)
        (path,) = self.transcripts()

        def at(event: str, **fields) -> dict:
            return {
                "session_id": SID,
                "transcript_path": path,
                "cwd": self.proj,
                "hook_event_name": event,
                **fields,
            }

        def stop(last: str) -> dict:
            return at(
                "Stop",
                stop_hook_active=False,
                last_assistant_message=last,
                background_tasks=[{}, {}],
            )

        fired = self.fired()
        self.assertEqual(
            fired,
            [
                at("UserPromptSubmit", prompt="hi"),
                at("PostToolUse", tool_name="Read"),
                at("PermissionRequest", tool_name="Bash", cwd="/x", n=[1]),
                stop("Done."),
                at("UserPromptSubmit", prompt="next"),
                stop("Next."),
                at("UserPromptSubmit", prompt="last"),
                stop(""),
            ],
        )
        self.assertEqual(
            (type(fired[3]["stop_hook_active"]), fired[3]["stop_hook_active"]), (bool, False)
        )
        self.assertEqual(
            self.convo(),
            [
                ("user", "hi"),
                ("assistant", "tool_use Read"),
                ("assistant", "Done."),
                ("user", "next"),
                ("assistant", "Next."),
                ("user", "last"),
            ],
        )

    def test_matchers_and_malformed_entries(self):
        tag = self.tag
        entries = [
            {"matcher": "^x$", "hooks": [tag("skipped")]},
            {"matcher": None, "hooks": [tag("null")]},
            "text",
            {"matcher": "*"},
            {"hooks": "no list"},
            {
                "hooks": [
                    "text",
                    {"type": "command"},
                    {**tag("http"), "type": "http"},
                    {**tag("five"), "command": 5},
                ]
            },
            {"matcher": "*", "hooks": [tag("star")]},
            {"matcher": "", "hooks": [tag("empty")]},
            {"hooks": [tag("absent")]},
            {"hooks": [tag("a"), tag("b")]},
        ]
        self.scene()
        settings = json.dumps({"hooks": {"Stop": entries, "UserPromptSubmit": "not a list"}})
        res = self.run_fake("hi", *SESSION, "--settings", settings)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["star", "empty", "absent", "a", "b"])

    def test_a_regex_matcher_searches_the_tool_name_or_notification_type(self):
        def entry(matcher: str, name: str) -> dict:
            return {"matcher": matcher, "hooks": [self.tag(name)]}

        hooks = {
            "PostToolUse": [entry("^Rea", "rea"), entry("Edit|Write", "edit"), entry("(", "bad")],
            "Notification": [entry(ClaudeTui.MATCHER, "note")],
            "PermissionRequest": [entry("Bash", "ask")],
        }
        self.scene(
            steps=[
                *({"kind": "tool", "name": n} for n in ("Read", "Write", "Grep", "Bread")),
                *(
                    {"kind": "hook", "event": "Notification", "notification_type": t}
                    for t in ("idle_prompt", "permission_prompt", "idle_prompt_x")
                ),
                {"kind": "hook", "event": "Notification"},
                *(
                    {"kind": "hook", "event": "PermissionRequest", "tool_name": n}
                    for n in ("Read", "Bash")
                ),
            ]
        )
        res = self.run_fake("hi", *SESSION, "--settings", json.dumps({"hooks": hooks}))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["rea", "edit", "note", "ask"])

    def test_failing_hooks_do_not_stop_the_fake(self):
        marker = shlex.quote(self.hooklog)
        commands = [
            "exit 1",
            "cat >/dev/null; kill -9 $$",
            "no-such-command-anywhere",
            f"echo after >> {marker}",
        ]
        settings = json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}}
        )
        self.scene(steps=["Hi."])
        res = self.run_fake("hi", *SESSION, "--settings", settings, stdin="again\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), ["after", "after"])
        self.assertEqual(self.users(), ["hi", "again"])

    def test_cwd_env_stdout_and_stderr(self):
        command = (
            'echo "$PWD $MARK" >> '
            + shlex.quote(self.hooklog)
            + "; echo to-stdout; echo to-stderr >&2"
        )
        self.env["MARK"] = "m"
        self.scene()
        res = self.run_fake(
            "hi",
            *SESSION,
            "--settings",
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(live_tmux.events(self.hooklog), [f"{self.proj} m"])
        self.assertNotIn("to-stdout", res.stdout)
        self.assertIn("to-stderr", res.stderr)

    def test_settings_without_usable_hooks(self):
        self.scene(steps=[{"kind": "hook", "event": "Stop"}])
        for settings in (("--settings", '{"hooks": "x"}'), ("--settings", "{}"), ()):
            with self.subTest(settings=settings):
                res = self.run_fake("hi", *SESSION, *settings)
                self.assertEqual(res.returncode, 0, res.stderr)


class Steps(Case):
    def test_a_dialog_prints_and_waits_for_its_answer(self):
        self.scene(steps=[{"kind": "dialog", "text": TRUST}, "Hi."], turns=[["Again."]])
        proc = self.popen(
            *SESSION, "--settings", self.settings("UserPromptSubmit", "Stop"), "--", "hello"
        )
        self.assertEqual(proc.stdout.readline(), TRUST + "\n")
        time.sleep(0.5)
        self.assertEqual(self.names(), ["UserPromptSubmit"])
        out, err = proc.communicate("yes\nmore\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Hi.\nAgain.\n"), err)
        self.assertEqual(self.names(), ["UserPromptSubmit", "Stop", "UserPromptSubmit", "Stop"])
        self.assertEqual(
            self.convo(),
            [("user", "hello"), ("assistant", "Hi."), ("user", "more"), ("assistant", "Again.")],
        )

    def test_eof_at_a_dialog_exits_with_exit(self):
        self.scene(steps=[{"kind": "dialog", "text": TRUST}, "Hi."], exit=4)
        res = self.run_fake(*SESSION, "--settings", self.settings("Stop"), "--", "hello")
        self.assertEqual(
            (res.returncode, res.stdout, self.fired()), (4, TRUST + "\n", []), res.stderr
        )

    def test_a_limit_ends_the_turn_without_stop(self):
        self.scene(
            steps=["Working.", {"kind": "limit", "text": LIMIT}, "Skipped."], turns=[["After."]]
        )
        proc = self.popen(
            *SESSION, "--settings", self.settings("UserPromptSubmit", "Stop"), "--", "hello"
        )
        self.assertEqual([proc.stdout.readline() for _ in range(2)], ["Working.\n", LIMIT + "\n"])
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.names(), ["UserPromptSubmit"])
        out, err = proc.communicate("again\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "After.\n"), err)
        self.assertEqual(self.names(), ["UserPromptSubmit", "UserPromptSubmit", "Stop"])
        self.assertEqual(
            self.convo(),
            [
                ("user", "hello"),
                ("assistant", "Working."),
                ("user", "again"),
                ("assistant", "After."),
            ],
        )

    def test_sleep(self):
        self.scene(steps=[{"kind": "sleep", "seconds": 0.5}, "Hi."])
        start = time.monotonic()
        res = self.run_fake(*SESSION, "--", "hello")
        self.assertGreaterEqual(time.monotonic() - start, 0.5)
        self.assertEqual((res.returncode, res.stdout), (0, "Hi.\n"), res.stderr)


class Pane(Case):
    def test_lines_typed_into_a_tmux_pane_are_turns(self):
        server = live_tmux.Server(self, self.home)
        server.start("mgr")
        self.scene(steps=["Hi."], turns=[["Again."]])
        hook = {
            "type": "command",
            "command": f'printf "%s %s\\n" "$TMUX_PANE" "$(cat)" >> {shlex.quote(self.hooklog)}',
        }
        res = server.tmux(
            "new-session",
            "-d",
            "-s",
            "fake",
            "-c",
            self.proj,
            "-e",
            f"{fake_claude.ENV}={self.scenario}",
            "-e",
            f"CLAUDE_CONFIG_DIR={self.config}",
            "claude",
            *SESSION,
            "--settings",
            json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}}),
            "--",
            "hello",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        pane = server.inside("fake")["TMUX_PANE"]

        def stops(n: int) -> list[tuple[str, dict]]:
            rows = [line.split(" ", 1) for line in live_tmux.events(self.hooklog) if line]
            return len(rows) == n and [(p, json.loads(payload)) for p, payload in rows]

        live_tmux.wait(lambda: stops(1), what="turn 1's Stop")
        for keys in (("-l", "next"), ("Enter",)):
            self.assertEqual(server.tmux("send-keys", "-t", "=fake:", *keys).returncode, 0)
        (first, one), (second, two) = live_tmux.wait(lambda: stops(2), what="turn 2's Stop")
        self.assertEqual((first, second), (pane, pane))
        self.assertEqual(
            (one["last_assistant_message"], two["last_assistant_message"]), ("Hi.", "Again.")
        )
        screen = server.tmux("capture-pane", "-p", "-t", "=fake:").stdout
        self.assertLess(screen.index("Hi."), screen.index("next"))
        self.assertLess(screen.index("next"), screen.index("Again."))
        self.assertEqual(self.users(), ["hello", "next"])


class ClaudeTuiLaunch(Case):
    def test_its_argv_runs_and_its_hooks_fit_its_readers(self):
        bin_dir = os.path.join(self.root, "bin")
        os.mkdir(bin_dir)
        self.env["PATH"] = fake_claude.install(bin_dir)
        with open(os.path.join(self.proj, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.config})
        patch.start()
        self.addCleanup(patch.stop)
        tui = ClaudeAdapter(
            {
                "allow": ["Read"],
                "tiers": {"1": "m"},
                "efforts": {"high": "h"},
            },
        ).tui()
        spec = Spec(
            workdir=self.proj,
            flags=("--permission-mode", "auto", "--strict-mcp-config"),
            sid=SID,
            tier=1,
            effort="high",
            access=Access(dirs=(self.root,), commands=("git status",), project=True),
        )
        q = shlex.quote(self.hooklog)
        hook = ("/bin/sh", "-c", f'printf "%s " "$0" >> {q}; cat >> {q}; echo >> {q}')
        self.scene(
            steps=[
                {"kind": "tool", "name": "Read"},
                {"kind": "hook", "event": "PermissionRequest", "tool_name": "Bash"},
                *(
                    {
                        "kind": "hook",
                        "event": "Notification",
                        "notification_type": t,
                        "message": "Needs you",
                    }
                    for t in ("idle_prompt", "permission_prompt")
                ),
                "Done.",
            ],
            background=1,
        )
        new, resume = (
            tui.launch("do it", SessionRequest(spec, resumed=False), hook),
            tui.launch("", SessionRequest(spec, resumed=True), hook),
        )
        self.assertEqual(new.argv[-2:], ("--", "do it"))
        res = self.run_argv(new.argv)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.scene()
        res = self.run_argv(resume.argv, "more\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.users(), ["do it", "more"])
        labels = [line.split(" ", 1) for line in live_tmux.events(self.hooklog) if line]
        self.assertEqual(
            [label for label, _ in labels],
            [
                "UserPromptSubmit",
                "PostToolUse",
                "PermissionRequest",
                "Notification",
                "Stop",
                "UserPromptSubmit",
                "Stop",
            ],
        )
        payloads = {label: json.loads(payload) for label, payload in labels[:5]}
        self.assertEqual(payloads["Notification"]["notification_type"], "permission_prompt")
        self.assertEqual(
            (
                tui.activity(payloads["PostToolUse"]),
                tui.blocked_reason(payloads["PermissionRequest"]),
                tui.blocked_reason(payloads["Notification"]),
            ),
            ("Read", "Bash", "Needs you"),
        )
        stop = payloads["Stop"]
        self.assertEqual(
            (tui.reply(stop), tui.pending(stop), stop["transcript_path"]),
            ("Done.", 1, new.transcript),
        )
