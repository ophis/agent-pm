"""tui launch test behavior."""

import json
import os
import re
import shlex

from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.engine.types import (
    Access,
    Launch,
)

from session.tests.unit.adapters.claude.fixtures import (
    CEILING,
    CONFIG,
    HOOK,
    HOOKS,
    MATCHER,
    NAME,
    NOT_WAITING,
    SID,
    TUI_HOOKS,
    TUI_SETTINGS,
    UNSET,
    WAITING,
    WANT_SETTINGS,
    Base,
    ClaudeCommon,
    tui_launch,
)


class TuiArgvTest(Base):
    def test_named_session_keeps_its_name_fresh_and_resumed(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                argv = self.tui(name="worker-name", resume=resume).argv
                start = argv.index("--name")
                self.assertEqual(argv[start : start + 2], ("--name", "worker-name"))

    def tui(self, prompt="hi", config=CONFIG, *, resume=False, **spec) -> Launch:
        return tui_launch(self.work, prompt, config, resume=resume, **spec)

    def test_a_new_session_with_every_part(self):
        access = Access(dirs=("/d1", "/d 2"), commands=("git status", "ls"), reads=("/r1", "/r 2/"))
        self.assertEqual(
            self.tui("do it", tier=4, effort="low", access=access).argv,
            (
                "claude",
                "--session-id",
                SID,
                "--model",
                "haiku",
                "--effort",
                "lo",
                "--permission-mode",
                "auto",
                "--setting-sources",
                "user",
                "--settings",
                TUI_SETTINGS,
                "--add-dir",
                "/d1",
                "--add-dir",
                "/d 2",
                "--allowedTools",
                "Bash(git status)",
                "Bash(ls)",
                "Read(//r1/**)",
                "Read(//r 2/**)",
                "Read",
                "Edit(/x)",
                "--",
                "do it",
            ),
        )

    def test_resume_with_none_of_the_optional_parts(self):
        self.assertEqual(
            self.tui(config={}, resume=True).argv,
            (
                "claude",
                "--resume",
                SID,
                "--setting-sources",
                "user",
                "--settings",
                TUI_SETTINGS,
                "--",
                "hi",
            ),
        )

    def test_an_empty_prompt_has_no_separator(self):
        self.assertEqual(self.tui("", config={}).argv[-2:], ("--settings", TUI_SETTINGS))
        self.assertEqual(
            self.tui("", config={"allow": ["Read"]}).argv[-2:], ("--allowedTools", "Read")
        )
        self.assertEqual(self.tui("-p --help", config={}).argv[-2:], ("--", "-p --help"))

    def test_each_hook_runs_the_hook_argv_and_its_label(self):
        self.assertEqual(ClaudeTui.hooks, HOOKS)
        hooks = json.loads(self.tui(config={}).argv[6])["hooks"]
        self.assertEqual(list(hooks), list(HOOKS))
        for label, entries in hooks.items():
            with self.subTest(label=label):
                self.assertEqual(shlex.split(entries[0]["hooks"][0]["command"]), [*HOOK, label])

    def test_the_notification_matcher_takes_only_the_types_waiting_on_the_user(self):
        for kind, hit in ((k, k in WAITING) for k in (*WAITING, *NOT_WAITING)):
            with self.subTest(kind=kind):
                self.assertEqual(re.search(ClaudeTui.MATCHER, kind) is not None, hit)
        self.assertEqual(ClaudeTui.MATCHER, MATCHER)

    def test_a_project_run_and_the_rest_as_headless(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        with open(os.path.join(cwd, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        access = Access(cwd=cwd, project=True)
        strict = ("--strict-mcp-config",)
        tui, headless = (
            self.tui(access=access, flags=strict),
            self.launch(access=access, flags=strict),
        )
        self.assertEqual(
            tui.argv[3:9],
            (
                "--permission-mode",
                "auto",
                "--setting-sources",
                "user,project,local",
                "--mcp-config",
                os.path.join(cwd, ".mcp.json"),
            ),
        )
        self.assertEqual((dict(tui.env), dict(headless.env)), ({"K": "v"}, {**CEILING, "K": "v"}))
        self.assertEqual(
            (tui.unset, tui.cwd, tui.transcript, tui.resume),
            (headless.unset, headless.cwd, headless.transcript, headless.resume),
        )

    def test_a_bad_sid_or_tier_is_refused(self):
        self.refused(lambda: self.tui(sid="abc"), "session ids are UUIDs, got 'abc'")
        self.refused(lambda: self.tui(tier=3), "no model for tier 3 in [adapters.claude].tiers")

    def test_the_flags_go_before_the_prompt_the_rest_unchanged(self):
        access = Access(dirs=("/d1",), commands=("ls",))
        flags = ("--verbose", "--add-dir", "/x")
        for prompt in ("do it", ""):
            with self.subTest(prompt=prompt):
                base = self.tui(prompt, access=access, name=NAME).argv
                cut = len(base) - 2 if prompt else len(base)
                self.assertEqual(
                    self.tui(prompt, access=access, flags=flags, name=NAME).argv,
                    (*base[:cut], *flags, *base[cut:]),
                )


class SessionTuiTest(Base):
    def session(self, prompt="hi", flags=(), config=None, *, resume=False, **spec) -> Launch:
        return tui_launch(
            self.work,
            prompt,
            {} if config is None else config,
            resume=resume,
            name=NAME,
            flags=flags,
            **spec,
        )

    def settings(self, flags) -> tuple[dict, tuple[str, ...]]:
        argv = self.session(flags=flags).argv
        self.assertEqual(
            argv[:8],
            (
                "claude",
                "--session-id",
                SID,
                "--name",
                NAME,
                "--setting-sources",
                "user",
                "--settings",
            ),
        )
        return json.loads(argv[8]), argv[9:]

    def test_new_and_resumed_share_config_access_flags_and_env(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        with open(os.path.join(cwd, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        access = Access(
            dirs=("/d1",),
            commands=("ls",),
            reads=("/r1",),
            cwd=cwd,
            project=True,
            env={"K": "access", "S": "s"},
        )
        flags = ("--permission-mode", "plan", "--strict-mcp-config", "--add-dir", "/x")
        for resume, word in ((False, "--session-id"), (True, "--resume")):
            with self.subTest(resume=resume):
                launch = self.session(
                    "do it",
                    flags,
                    config=CONFIG,
                    resume=resume,
                    tier=4,
                    effort="low",
                    access=access,
                    env={"K": "spec"},
                )
                self.assertEqual(
                    launch.argv,
                    (
                        "claude",
                        word,
                        SID,
                        "--model",
                        "haiku",
                        "--effort",
                        "lo",
                        "--name",
                        NAME,
                        "--permission-mode",
                        "auto",
                        "--setting-sources",
                        "user,project,local",
                        "--mcp-config",
                        os.path.join(cwd, ".mcp.json"),
                        "--settings",
                        TUI_SETTINGS,
                        "--add-dir",
                        "/d1",
                        "--allowedTools",
                        "Bash(ls)",
                        "Read(//r1/**)",
                        "Read",
                        "Edit(/x)",
                        *flags,
                        "--",
                        "do it",
                    ),
                )
                self.assertEqual(
                    (dict(launch.env), launch.unset, launch.cwd, launch.transcript, launch.resume),
                    (
                        {"K": "spec", "S": "s"},
                        UNSET,
                        "",
                        ClaudeCommon({}).transcript(cwd, SID),
                        f"cd {shlex.quote(cwd)} && K=spec S=s claude --resume {SID} --add-dir /d1",
                    ),
                )

    def test_none_of_the_optional_parts(self):
        launch = self.session("")
        self.assertEqual(
            launch.argv,
            (
                "claude",
                "--session-id",
                SID,
                "--name",
                NAME,
                "--setting-sources",
                "user",
                "--settings",
                TUI_SETTINGS,
            ),
        )
        self.assertEqual(
            (dict(launch.env), launch.resume),
            ({}, f"cd {shlex.quote(self.work)} && claude --resume {SID}"),
        )
        self.assertEqual(self.session("-p --help").argv[-2:], ("--", "-p --help"))

    def test_the_hooks_merge_into_the_users_settings_which_move_before_the_rest(self):
        mine = {"type": "command", "command": "mine"}
        own = {"model": "x", "hooks": {"PostToolUse": [mine], "SessionStart": [mine]}}
        merged = {
            "model": "x",
            "hooks": {
                **TUI_HOOKS,
                "PostToolUse": [mine, *TUI_HOOKS["PostToolUse"]],
                "SessionStart": [mine],
            },
        }
        for flags in (
            ("--verbose", "--settings", json.dumps(own), "--x"),
            ("--verbose", "--settings=" + json.dumps(own), "--x"),
        ):
            with self.subTest(flags=flags):
                self.assertEqual(
                    self.settings(flags),
                    (merged, ("--verbose", "--x", "--", "hi")),
                )
        self.assertEqual(
            self.settings(("--settings", '{"model": "x"}')),
            ({"model": "x", "hooks": TUI_HOOKS}, ("--", "hi")),
        )
        after = ("--", "--settings", "[]")
        self.assertEqual(
            self.settings(after),
            ({"hooks": TUI_HOOKS}, (*after, "--", "hi")),
        )

    def test_a_malformed_settings_flag_is_refused(self):
        long = "x" * 100
        for flags, value in (
            (("--settings",), ""),
            (("--settings", "--"), "--"),
            (("--settings", "/a/settings.json"), "/a/settings.json"),
            (("--settings=[]",), "[]"),
            (("--settings", '{"hooks": null}'), '{"hooks": null}'),
            (("--settings", '{"hooks": []}'), '{"hooks": []}'),
            (("--settings", '{"hooks": {"Stop": {}}}'), '{"hooks": {"Stop": {}}}'),
            (("--settings", long), long),
        ):
            with self.subTest(flags=flags):
                self.refused(
                    lambda *, flags=flags: self.session(flags=flags),
                    f"--settings {value!r:.80}: {WANT_SETTINGS}",
                )
