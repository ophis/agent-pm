"""adapters/claude/: ClaudeCommon's config, flags, env, paths, usage and session status;
ClaudeHeadless's launch and parse; ClaudeTui's launch with session flags, hook payloads and
usage-limit screen; ClaudeAdapter's hooks and writable dirs; the committed [adapters.claude] table. HOME and
CLAUDE_CONFIG_DIR are temp dirs; nothing starts a process."""

import json
import os
import re
import shlex
import tempfile
import time
import unittest
from datetime import datetime
from functools import partial
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.session.adapters import registry
from agent_pm.session.adapters.base import (
    ACTIVITY,
    BLOCKED,
    TURN_END,
    Access,
    Activity,
    Blocked,
    ConfigError,
    Launch,
    LimitHit,
    Reply,
    SessionBound,
    SessionStatus,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.adapters.base import (
    Spec as BaseSpec,
)
from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.adapters.claude.common import ClaudeCommon as UnconfiguredCommon
from agent_pm.session.adapters.claude.headless import ClaudeHeadless
from agent_pm.session.adapters.claude.tui import ClaudeTui
from agent_pm.session.adapters.selection import select

from tests.case import Case
from tests.selection import configured


class ClaudeCommon(UnconfiguredCommon):
    def __init__(self, config):
        super().__init__(
            config,
            selector=lambda native, resume: select(
                native, resume, ClaudeAdapter.selection, uuid=True
            ),
        )


Spec = BaseSpec


SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
CEILING = {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"}
UNSET = (
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_JOB_DIR",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
)
PROBE = (
    "claude",
    "-p",
    "Reply with OK.",
    "--model",
    "haiku",
    "--output-format",
    "stream-json",
    "--verbose",
    "--setting-sources",
    "user",
    "--strict-mcp-config",
)
NOTE = (
    "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else waits on a "
    "permission prompt."
)
CONFIG = {
    "flags": ["--permission-mode", "auto"],
    "allow": ["Read", "Edit(/x)"],
    "tiers": {"1": "fable", "4": "haiku"},
    "efforts": {"low": "lo", "max": "mx"},
    "env": {"K": "v"},
}
WHAT = {
    "flags": "a list of strings",
    "allow": "a list of strings",
    "tiers": 'a table from tier "1"–"4" to a model name',
    "efforts": "a table from effort (low, medium, high, xhigh, max) to a string",
    "env": "a table of environment names to strings without NUL",
}


class Base(Case):
    def setUp(self):
        self.tmp = self.dir()
        self.config_dir = os.path.join(self.tmp, "config")
        self.env({"CLAUDE_CONFIG_DIR": self.config_dir})
        self.work = os.path.join(self.tmp, "work")
        os.mkdir(self.work)

    def dir(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return os.path.realpath(tmp.name)

    def env(self, values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def refused(self, call, message):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)

    def launch(self, prompt="hi", config=CONFIG, resume=False, **spec) -> Launch:
        spec = Spec(**{"workdir": self.work, "sid": SID, **spec})
        return configured(ClaudeAdapter, config).headless().launch(prompt, spec, resume)


class ConfigTest(Base):
    def test_every_key_is_optional_and_each_is_accepted(self):
        self.assertEqual(configured(ClaudeAdapter, {}).config, {})
        self.assertEqual(configured(ClaudeAdapter, CONFIG).config, CONFIG)

    def test_an_adapter_the_driver_never_configured_starts_and_resumes_nothing(self):
        adapter, spec = ClaudeAdapter({}), Spec(workdir=self.work, sid=SID)
        for launch in (
            lambda: adapter.headless().launch("hi", spec, False),
            lambda: adapter.tui().launch("hi", spec, True, HOOK),
            lambda: adapter.common.resume(self.work, SID),
        ):
            self.refused(launch, "session selector is not configured by Driver")

    def test_an_unknown_key_is_refused(self):
        self.refused(
            lambda: configured(ClaudeAdapter, {"model": "m"}),
            "unknown key 'model' in ClaudeAdapter's config",
        )

    def test_a_wrong_type_names_the_key_never_the_value(self):
        rows = (
            ("flags", "SECRET"),
            ("flags", ["--x", 1]),
            ("flags", ("SECRET",)),
            ("flags", {"SECRET": "x"}),
            ("allow", "SECRET"),
            ("allow", [None]),
            ("tiers", "SECRET"),
            ("tiers", ["SECRET"]),
            ("tiers", {"1": 4}),
            ("tiers", {"5": "SECRET"}),
            ("tiers", {"0": "SECRET"}),
            ("tiers", {1: "SECRET"}),
            ("efforts", "SECRET"),
            ("efforts", {"low": 1}),
            ("efforts", {"turbo": "SECRET"}),
            ("efforts", {1: "SECRET"}),
            ("env", "SECRET"),
            ("env", {"K": 1}),
            ("env", {1: "SECRET"}),
            ("env", ["SECRET"]),
        )
        for key, value in rows:
            with self.subTest(key=key, value=value):
                self.refused(
                    lambda: configured(ClaudeAdapter, {key: value}),
                    f"[adapters.claude].{key} must be {WHAT[key]}",
                )

    def test_invalid_environment_names_and_values_fail_before_launch(self):
        for key, value in (
            ("", "SECRET"),
            ("A=B", "SECRET"),
            ("A\0B", "SECRET"),
            (1, "SECRET"),
            ("A", "SECRET\0"),
            ("A", 1),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError) as caught:
                    configured(ClaudeAdapter, {"env": {key: value}})
                self.assertIn("[adapters.claude].env", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    def test_setting_sources_in_flags_is_refused(self):
        for flags in (
            ["--setting-sources"],
            ["--setting-sources=user"],
            ["--verbose", "--setting-sources", "user"],
        ):
            with self.subTest(flags=flags):
                self.refused(
                    lambda: configured(ClaudeAdapter, {"flags": flags}),
                    "--setting-sources is the Driver's: remove it from [adapters.claude].flags",
                )

    def test_the_env_attributes(self):
        adapter = configured(ClaudeAdapter, {})
        self.assertEqual(
            (adapter.session_env, adapter.identity_env, adapter.child_defaults),
            ("CLAUDECODE", UNSET, {}),
        )


class ModelEffortTest(Base):
    def test_none_omits_the_flag_and_a_value_is_the_tables(self):
        common = ClaudeCommon(CONFIG)
        self.assertEqual((common.model(None), common.effort(None)), (None, None))
        self.assertEqual(
            (common.model(1), common.model(4), common.effort("max")), ("fable", "haiku", "mx")
        )

    def test_a_missing_entry_names_the_table(self):
        for config in (CONFIG, {}):
            with self.subTest(config=config):
                common = ClaudeCommon(config)
                self.refused(
                    lambda: common.model(2), "no model for tier 2 in [adapters.claude].tiers"
                )
                self.refused(
                    lambda: common.effort("medium"),
                    "no effort for 'medium' in [adapters.claude].efforts",
                )
        self.refused(lambda: self.launch(tier=3), "no model for tier 3 in [adapters.claude].tiers")
        self.refused(
            lambda: self.launch(effort="high"), "no effort for 'high' in [adapters.claude].efforts"
        )


class ArgvTest(Base):
    def test_a_new_session_with_every_part(self):
        access = Access(dirs=("/d1", "/d 2"), commands=("git status", "ls"), reads=("/r1", "/r 2/"))
        self.assertEqual(
            self.launch("do it", tier=4, effort="low", access=access).argv,
            (
                "claude",
                "-p",
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
                "--output-format",
                "stream-json",
                "--verbose",
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

    def test_resume_continues_the_sid(self):
        self.assertEqual(
            self.launch(config={}, resume=True).argv[:4], ("claude", "-p", "--resume", SID)
        )

    def test_none_of_the_optional_parts(self):
        self.assertEqual(
            self.launch(config={}).argv,
            (
                "claude",
                "-p",
                "--session-id",
                SID,
                "--setting-sources",
                "user",
                "--output-format",
                "stream-json",
                "--verbose",
                "--",
                "hi",
            ),
        )

    def test_allowed_tools_from_commands_or_allow_alone(self):
        self.assertEqual(
            self.launch(config={}, access=Access(commands=("ls",))).argv[-4:],
            ("--allowedTools", "Bash(ls)", "--", "hi"),
        )
        self.assertEqual(
            self.launch(config={"allow": ["Read"]}).argv[-4:],
            ("--allowedTools", "Read", "--", "hi"),
        )

    def test_a_prompt_starting_with_a_dash_stays_last_after_the_separator(self):
        self.assertEqual(self.launch("-p --help", config={}).argv[-2:], ("--", "-p --help"))

    def test_a_project_run_loads_project_settings_and_cwds_mcp_file(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.mkdir(cwd)
        argv = self.launch(config={}, access=Access(cwd=cwd, project=True)).argv
        self.assertEqual(argv[4:7], ("--setting-sources", "user,project,local", "--output-format"))
        for parent in (cwd, self.work):
            with open(os.path.join(parent, ".mcp.json"), "w", encoding="utf-8") as f:
                f.write("{}")
        strict = ("--strict-mcp-config",)
        self.assertNotIn("--mcp-config", self.launch(access=Access(cwd=cwd, project=True)).argv)
        argv = self.launch(config={}, access=Access(cwd=cwd, project=True), flags=strict).argv
        self.assertEqual(
            argv[4:9],
            (
                "--setting-sources",
                "user,project,local",
                "--mcp-config",
                os.path.join(cwd, ".mcp.json"),
                "--output-format",
            ),
        )
        self.assertEqual(
            self.launch(config={}, access=Access(project=True), flags=strict).argv[6:8],
            ("--mcp-config", os.path.join(self.work, ".mcp.json")),
        )
        self.assertNotIn(
            "--mcp-config", self.launch(config={}, access=Access(cwd=cwd), flags=strict).argv
        )

    def test_an_mcp_path_that_is_no_file_is_skipped(self):
        os.mkdir(os.path.join(self.work, ".mcp.json"))
        self.assertNotIn(
            "--mcp-config",
            self.launch(
                config={}, access=Access(project=True), flags=("--strict-mcp-config",)
            ).argv,
        )

    def test_a_sid_that_is_no_canonical_uuid_is_refused(self):
        for bad in (
            "abc",
            SID.upper(),
            "{" + SID + "}",
            SID.replace("-", ""),
            SID + "0",
            " " + SID,
        ):
            with self.subTest(bad=bad):
                self.refused(lambda: self.launch(sid=bad), f"session ids are UUIDs, got {bad!r}")
        self.refused(
            lambda: ClaudeCommon.uuid(SID + "\n"),
            f"claude session ids are UUIDs, got {SID + chr(10)!r}",
        )
        self.refused(lambda: ClaudeCommon.uuid(None), "claude session ids are UUIDs, got None")
        self.assertEqual(ClaudeCommon.uuid(SID), SID)


class ReadRulesTest(Base):
    def modes(self, resume=False, config=None, **access):
        spec = Spec(workdir=self.work, sid=SID, access=Access(**access))
        adapter = configured(ClaudeAdapter, {} if config is None else config)
        return (
            lambda: adapter.headless().launch("hi", spec, resume),
            lambda: adapter.tui().launch("hi", spec, resume, HOOK),
        )

    def both(self, resume=False, config=None, **access) -> list[tuple[str, ...]]:
        return [launch().argv for launch in self.modes(resume, config, **access)]

    def test_reads_alone_are_allowed_tools_never_added_dirs(self):
        for argv in self.both(reads=("/r",)):
            self.assertEqual(argv[-4:], ("--allowedTools", "Read(//r/**)", "--", "hi"))
            self.assertNotIn("--add-dir", argv)

    def test_the_root_is_one_rule(self):
        for argv in self.both(reads=("/", "//")):
            self.assertEqual(argv[-4:], ("--allowedTools", "Read(//**)", "--", "hi"))

    def test_a_symlinked_read_dir_gets_a_rule_for_both_spellings_each_once(self):
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for reads, dirs in (
            ((link,), (link, target)),
            ((link + "/", target), (link, target)),
            ((target, link), (target, link)),
        ):
            with self.subTest(reads=reads):
                for argv in self.both(reads=reads):
                    self.assertEqual(
                        argv[-5:],
                        ("--allowedTools", *(f"Read(/{d}/**)" for d in dirs), "--", "hi"),
                    )

    def test_a_resume_keeps_the_rules(self):
        for argv in self.both(True, CONFIG, commands=("ls",), reads=("/r",)):
            self.assertEqual(argv[1 + argv.index("--resume")], SID)
            self.assertEqual(
                argv[-7:],
                ("--allowedTools", "Bash(ls)", "Read(//r/**)", "Read", "Edit(/x)", "--", "hi"),
            )

    def test_a_read_dir_a_rule_cannot_hold_literally_is_refused_before_any_launch(self):
        bad = ["", "r", "./r", "/r\n", "/r\0", *(f"/r{c}s" for c in "*?[]{}()\\,"), "/" + "*" * 99]
        for read in bad:
            for launch in self.modes(reads=("/ok", read)):
                with (
                    self.subTest(read=read),
                    mock.patch("agent_pm.session.adapters.claude.common.Launch") as made,
                ):
                    self.refused(
                        launch,
                        f"claude: read dir {read!r:.80} must be a printable absolute path "
                        "without *?[]{}()\\,",
                    )
                    made.assert_not_called()

    def test_a_read_dir_whose_real_path_a_rule_cannot_hold_is_refused(self):
        target = os.path.join(self.tmp, "a*b")
        os.mkdir(target)
        link = os.path.join(self.tmp, "link")
        os.symlink(target, link)
        for launch in self.modes(reads=(link,)):
            self.refused(
                launch,
                f"claude: read dir {target!r:.80} must be a printable absolute path without "
                "*?[]{}()\\,",
            )


class EnvTest(Base):
    def test_headless_defaults_overlaid_by_the_config_env_tui_only_the_config_env(self):
        self.assertEqual(ClaudeCommon({}).env(), {})
        config = {"env": {"K": "v", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1"}}
        self.assertEqual(dict(self.launch(config=config).env), config["env"])
        self.assertEqual(dict(self.launch(config={}).env), CEILING)
        self.assertEqual(dict(tui_launch(self.work, config={}).env), {})

    def test_the_launch_env_unset_and_cwd(self):
        launch = self.launch()
        self.assertEqual(dict(launch.env), {**CEILING, "K": "v"})
        self.assertEqual(launch.unset, UNSET)
        self.assertEqual(ClaudeCommon.UNSET, UNSET)
        self.assertEqual(launch.cwd, "")

    def test_headless_uses_session_flags_and_env(self):
        flags = ("--permission-mode", "auto")
        launch = self.launch(flags=flags, env={"K": "session", "CUSTOM": "value"})
        self.assertEqual(launch.argv[-4:], (*flags, "--", "hi"))
        self.assertEqual(dict(launch.env), {**CEILING, "K": "session", "CUSTOM": "value"})
        self.assertIn("K=session CUSTOM=value claude", launch.resume)

    def test_the_session_env_wins_over_the_config_env(self):
        config = {**CONFIG, "env": {"K": "v", "PYTHONPATH": "/config"}}
        access = Access(env={"PYTHONPATH": "/root", "S": "s"})
        self.env({"PYTHONPATH": "/parent"})
        for launch, defaults in (
            (self.launch(config=config, access=access), CEILING),
            (
                configured(ClaudeAdapter, config)
                .tui()
                .launch("hi", Spec(workdir=self.work, sid=SID, access=access), False, HOOK),
                {},
            ),
        ):
            self.assertEqual(
                dict(launch.env), {**defaults, "K": "v", "PYTHONPATH": "/root", "S": "s"}
            )


class TranscriptTest(Base):
    def test_projects_dir_named_by_the_mangled_cwd(self):
        self.assertEqual(
            ClaudeCommon({}).transcript("/agent-pm-absent/a.b_c d-é", SID),
            os.path.join(self.config_dir, "projects", "-agent-pm-absent-a-b-c-d--", f"{SID}.jsonl"),
        )

    def test_the_cwds_realpath(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.work, link)
        common = ClaudeCommon({})
        self.assertEqual(common.transcript(link, SID), common.transcript(self.work, SID))
        self.assertNotIn("link", common.transcript(link, SID))

    def test_the_config_dir_from_the_config_env_then_the_process_env_then_home(self):
        given = {"env": {"CLAUDE_CONFIG_DIR": "/given"}}
        self.assertEqual(
            ClaudeCommon(given).transcript("/w", SID), f"/given/projects/-w/{SID}.jsonl"
        )
        self.assertEqual(ClaudeCommon(given).config_dir(), "/given")
        self.assertEqual(ClaudeCommon({}).config_dir(), self.config_dir)
        del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertEqual(
            ClaudeCommon({}).transcript("/w", SID),
            os.path.join(self.home, ".claude", "projects", "-w", f"{SID}.jsonl"),
        )

    def test_the_launch_transcript_is_in_its_cwd(self):
        cwd = os.path.join(self.tmp, "cwd")
        common = ClaudeCommon({})
        self.assertEqual(self.launch().transcript, common.transcript(self.work, SID))
        self.assertEqual(
            self.launch(access=Access(cwd=cwd)).transcript, common.transcript(cwd, SID)
        )


class ResumeTest(Base):
    def test_the_command_quoted(self):
        self.assertEqual(
            ClaudeCommon({}).resume("/w d", SID), f"cd '/w d' && claude --resume {SID}"
        )

    def test_each_granted_dir_is_added(self):
        self.assertEqual(
            ClaudeCommon({}).resume("/w", SID, ("/o x", "/p")),
            f"cd /w && claude --resume {SID} --add-dir '/o x' --add-dir /p",
        )

    def test_the_launch_resume_adds_the_granted_dirs_never_the_workdir(self):
        cwd = os.path.join(self.tmp, "cwd")
        q = shlex.quote
        self.assertEqual(self.launch().resume, f"cd {q(self.work)} && claude --resume {SID}")
        for launch in (self.launch, partial(tui_launch, self.work)):
            with self.subTest(launch=launch):
                self.assertEqual(
                    launch(access=Access(cwd=cwd)).resume, f"cd {q(cwd)} && claude --resume {SID}"
                )
                self.assertEqual(
                    launch(access=Access(cwd=cwd, dirs=(self.work, "/d"))).resume,
                    f"cd {q(cwd)} && claude --resume {SID} --add-dir {q(self.work)} --add-dir /d",
                )

    def test_the_launch_resume_assigns_the_session_env_quoted(self):
        env = {"PYTHONPATH": "/a b;c", "S": "s"}
        resume = self.launch(access=Access(env=env)).resume
        self.assertEqual(
            resume,
            f"cd {shlex.quote(self.work)} && PYTHONPATH='/a b;c' S=s claude --resume {SID}",
        )
        self.assertEqual(shlex.split(resume)[3:6], ["PYTHONPATH=/a b;c", "S=s", "claude"])


def line(record) -> str:
    return json.dumps(record)


def assistant(*blocks, **fields) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}, **fields}


def text(value) -> dict:
    return {"type": "text", "text": value}


def tool(name) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": {}}


def rejected(resets=1700000000, status="rejected") -> dict:
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "resetsAt": resets}}


INIT = {"type": "system", "subtype": "init", "session_id": SID}


class ParseTest(Base):
    def setUp(self):
        super().setUp()
        self.run = configured(ClaudeAdapter, {}).headless()

    def parse(self, *records):
        return [event for record in records for event in self.run.parse(record)]

    def test_a_session_in_order(self):
        lines = [
            line(INIT),
            "not json",
            line(assistant(text("one"))),
            line(assistant(text("two"))),
            line({"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
            "[1]",
            line(assistant(text("three"))),
            line({"type": "result", "subtype": "success", "result": "three"}),
        ]
        self.assertEqual(
            list(self.run.events(lines)),
            [SessionBound(SID), Reply("one"), Reply("two"), Reply("three"), TurnEnd("three", 0)],
        )

    def test_init_binds_its_session_id(self):
        self.assertEqual(self.parse(INIT), [SessionBound(SID)])
        for record in (
            {**INIT, "subtype": "other"},
            {**INIT, "session_id": ""},
            {**INIT, "session_id": 1},
            {"type": "system", "subtype": "init"},
        ):
            with self.subTest(record=record):
                self.assertEqual(self.parse(record), [])

    def test_text_blocks_joined_blank_none(self):
        self.assertEqual(self.parse(assistant(text("a"), text("b"))), [Reply("a\nb")])
        self.assertEqual(self.parse(assistant(text(" \n"), text(""))), [])
        self.assertEqual(self.parse(assistant()), [])

    def test_tool_use_is_activity_after_the_reply(self):
        self.assertEqual(
            self.parse(assistant(tool("Bash"), text("x"), tool("Read"))),
            [Reply("x"), Activity("Bash"), Activity("Read")],
        )

    def test_result_is_the_turn_end(self):
        self.assertEqual(self.parse({"type": "result", "result": "done"}), [TurnEnd("done", 0)])
        self.assertEqual(
            self.parse({"type": "result", "result": None}, {"type": "result"}),
            [TurnEnd("", 0), TurnEnd("", 0)],
        )

    def test_other_records_nothing(self):
        self.assertEqual(
            self.parse({"type": "user"}, {"type": "stream_event"}, {}, {"type": 1}), []
        )

    def test_wrong_typed_fields_are_absent(self):
        rows = (
            ({"type": "assistant", "message": "x"}, []),
            ({"type": "assistant", "message": {"content": "x"}}, []),
            ({"type": "assistant"}, []),
            (assistant("x", 1, None, text("y")), [Reply("y")]),
            (assistant(text(1), {"type": "text"}), []),
            (assistant({"type": "tool_use", "name": 1}), [Activity("")]),
            (assistant({"type": "tool_use"}), [Activity("")]),
            ({"type": "rate_limit_event", "rate_limit_info": "rejected"}, []),
            ({"type": "rate_limit_event"}, []),
        )
        for record, events in rows:
            with self.subTest(record=record):
                self.assertEqual(
                    list(configured(ClaudeAdapter, {}).headless().parse(record)), events
                )

    def test_a_rejected_rate_limit_event_is_a_limit_hit(self):
        self.assertEqual(self.parse(rejected(1700000000)), [LimitHit(1700000000.0)])
        for record in (rejected(status="allowed"), rejected(status="allowed_warning")):
            with self.subTest(record=record):
                self.assertEqual(list(configured(ClaudeAdapter, {}).headless().parse(record)), [])
        for resets in (None, True, "1700000000", 1e999):
            with self.subTest(resets=resets):
                self.assertEqual(
                    list(configured(ClaudeAdapter, {}).headless().parse(rejected(resets))),
                    [LimitHit(None)],
                )

    def test_an_assistant_rate_limit_error_is_one_limit_hit_with_the_remembered_reset(self):
        self.assertEqual(
            self.parse(rejected(5), assistant(text("limit"), error="rate_limit"), rejected(6)),
            [LimitHit(5.0), Reply("limit")],
        )
        run = configured(ClaudeAdapter, {}).headless()
        self.assertEqual(
            [
                e
                for r in (
                    assistant(text("limit"), error="rate_limit"),
                    assistant(error="rate_limit"),
                    rejected(7),
                )
                for e in run.parse(r)
            ],
            [LimitHit(None), Reply("limit")],
        )
        self.assertEqual(
            list(configured(ClaudeAdapter, {}).headless().parse(assistant(error="other"))), []
        )

    def test_each_run_sends_its_own(self):
        adapter = configured(ClaudeAdapter, {})
        first, second = adapter.headless(), adapter.headless()
        self.assertIsNot(first, second)
        self.assertEqual(list(first.parse(rejected(1))), [LimitHit(1.0)])
        self.assertEqual(list(second.parse(assistant(error="rate_limit"))), [LimitHit(None)])


def event(status="allowed", five=0.1, kind="five_hour", resets=1, **week) -> str:
    """router_test's rate_limit_event line, its rateLimitType and resetsAt given."""
    windows = {"five_hour": {"utilization": five, "resetsAt": 1}}
    windows.update({k: {"utilization": v, "resetsAt": 2} for k, v in week.items()})
    return info_line(
        {
            "status": status,
            "utilization": five,
            "rateLimitType": kind,
            "resetsAt": resets,
            "unifiedWindows": windows,
        }
    )


def info_line(info) -> str:
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def unified(**found) -> str:
    """An allowed rate_limit_event line whose unifiedWindows are `found`."""
    return info_line({"status": "allowed", "unifiedWindows": found})


def usage(*lines) -> Usage | None:
    return ClaudeCommon.parse_usage("".join(f"{x}\n" for x in lines))


class ParseUsageTest(Case):
    def test_router_gate_cases(self):
        rows = (
            (['{"type":"system"}', "garbage"], False),
            ([event(five=0.85)], True),
            ([event(five=0.89)], True),
            ([event(five=0.9)], False),
            ([event(status="rejected", five=0.1)], False),
            ([event(five=0.1, seven_day=1.0)], False),
            ([event(five=0.89, seven_day=0.99, seven_day_opus=0.5)], True),
            ([event(five=0.1, seven_day=0.2, seven_day_opus=1.0)], False),
            ([event(status="rejected", five=0.1, kind="seven_day_opus", seven_day=0.2)], False),
            ([event(status="allowed_warning", five=0.5)], True),
            ([event(five=0.1), event(five=0.9)], False),
            ([event(five=0.9), "{}", event(five=0.1)], True),
        )
        for row, (lines, ok) in enumerate(rows):
            with self.subTest(row=row):
                found = usage(*lines)
                self.assertEqual(
                    found is not None
                    and found.five_hour is not None
                    and found.five_hour.used_percent < 90
                    and (found.weekly is None or found.weekly.used_percent < 100),
                    ok,
                )

    def test_each_window(self):
        rows = (
            (event(five=0.25), Usage(Window(25.0, 1.0), None)),
            (event(five=0.25, seven_day=0.5), Usage(Window(25.0, 1.0), Window(50.0, 2.0))),
            (event(five=1, seven_day=1), Usage(Window(100.0, 1.0), Window(100.0, 2.0))),
            (event(five=1.5, seven_day_opus=2), Usage(Window(150.0, 1.0), Window(200.0, 2.0))),
            (
                event(five=0.25, seven_day="1", seven_day_sonnet=True),
                Usage(Window(25.0, 1.0), None),
            ),
            (
                event(five=0.25, seven_day=None, seven_day_opus=1e999),
                Usage(Window(25.0, 1.0), None),
            ),
            (event(five=None, seven_day=0.5), Usage(None, Window(50.0, 2.0))),
            (event(five="0.5"), Usage(None, None)),
            (event(five=True), Usage(None, None)),
            (event(five=1e999), Usage(None, None)),
            (event(five=10**400), Usage(None, None)),
            (unified(five_hour=0.5, seven_day=[1]), Usage(None, None)),
            (
                unified(
                    five_hour={"utilization": 0.5},
                    weekly={"utilization": 0.9},
                    one_day={"utilization": 0.9},
                ),
                Usage(Window(50.0, None), None),
            ),
        )
        for row, (line, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(usage(line), want)

    def test_weekly_is_the_most_used_seven_day_window(self):
        rows = (
            (
                event(five=0.1, seven_day=0.3, seven_day_opus=0.8, seven_day_sonnet=0.5),
                Window(80.0, 2.0),
            ),
            (
                unified(
                    seven_day={"utilization": 0.5, "resetsAt": 3},
                    seven_day_opus={"utilization": 0.5, "resetsAt": 4},
                ),
                Window(50.0, 3.0),
            ),
            (
                unified(
                    seven_day_opus={"utilization": 0.5, "resetsAt": 4},
                    seven_day={"utilization": 0.5, "resetsAt": 3},
                ),
                Window(50.0, 4.0),
            ),
            (
                unified(seven_day={"utilization": "0.9"}, seven_day_opus={"utilization": 0.4}),
                Window(40.0, None),
            ),
            (
                unified(
                    seven_day_x={"utilization": 0.7, "resetsAt": 5}, seven_day={"utilization": 0.2}
                ),
                Window(70.0, 5.0),
            ),
        )
        for row, (line, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(usage(line).weekly, want)

    def test_each_window_resets_at_its_own_time(self):
        rows = (
            ({"resetsAt": 1700000000.5}, Window(25.0, 1700000000.5)),
            ({}, Window(25.0, None)),
            ({"resetsAt": None}, Window(25.0, None)),
            ({"resetsAt": True}, Window(25.0, None)),
            ({"resetsAt": "1"}, Window(25.0, None)),
            ({"resetsAt": 10**400}, Window(25.0, None)),
        )
        for row, (resets, want) in enumerate(rows):
            with self.subTest(row=row):
                line = info_line(
                    {
                        "status": "allowed",
                        "resetsAt": 9,
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.25, **resets},
                            "seven_day": {"utilization": 0.5, **resets},
                        },
                    }
                )
                self.assertEqual(usage(line), Usage(want, Window(50.0, want.resets_at)))

    def test_a_rejected_status_fills_its_window(self):
        rows = (
            (event(status="rejected", five=0.25, resets=9), Usage(Window(100.0, 1.0), None)),
            (event(status="rejected", five=1.5, resets=9), Usage(Window(150.0, 1.0), None)),
            (
                event(status="rejected", five=0.25, kind=None, resets=9),
                Usage(Window(100.0, 1.0), None),
            ),
            (
                event(status="rejected", five=0.25, kind=5, resets=9),
                Usage(Window(100.0, 1.0), None),
            ),
            (
                event(status="rejected", five=0.25, kind="five_hour_x", seven_day=0.5, resets=9),
                Usage(Window(100.0, 1.0), Window(50.0, 2.0)),
            ),
            (
                event(status="rejected", five=0.25, kind="seven_day", resets=9),
                Usage(Window(25.0, 1.0), Window(100.0, 9.0)),
            ),
            (
                event(status="rejected", five=0.25, kind="seven_day_opus", seven_day=0.5, resets=9),
                Usage(Window(25.0, 1.0), Window(100.0, 2.0)),
            ),
            (
                event(status="rejected", five=0.25, kind="seven_day", seven_day=1.5, resets=9),
                Usage(Window(25.0, 1.0), Window(150.0, 2.0)),
            ),
            (event(status="rejected", five="x", resets=9), Usage(Window(100.0, 9.0), None)),
            (event(status="rejected", five=0.25, resets=None), Usage(Window(100.0, 1.0), None)),
            (event(status="rejected", five=None, resets=None), Usage(Window(100.0, None), None)),
            (event(status="rejected", five=None, resets=True), Usage(Window(100.0, None), None)),
            (
                info_line({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 9}),
                Usage(Window(100.0, 9.0), None),
            ),
            (
                info_line(
                    {
                        "status": "rejected",
                        "rateLimitType": "seven_day",
                        "resetsAt": 9,
                        "unifiedWindows": {
                            "five_hour": {"utilization": 0.25},
                            "seven_day": {"utilization": 0.5},
                        },
                    }
                ),
                Usage(Window(25.0, None), Window(100.0, 9.0)),
            ),
            (
                info_line(
                    {
                        "status": "rejected",
                        "resetsAt": 9,
                        "unifiedWindows": {"five_hour": {"utilization": 0.25}},
                    }
                ),
                Usage(Window(100.0, 9.0), None),
            ),
            (event(status="allowed_warning", five=0.25, resets=9), Usage(Window(25.0, 1.0), None)),
            (event(status="Rejected", five=0.25, resets=9), Usage(Window(25.0, 1.0), None)),
        )
        for row, (line, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(usage(line), want)

    def test_an_event_without_windows_is_a_reading(self):
        for line in (
            info_line({}),
            info_line({"status": "allowed", "resetsAt": 9}),
            info_line({"unifiedWindows": {}}),
            info_line({"unifiedWindows": [1]}),
            info_line({"unifiedWindows": None}),
            info_line({"unifiedWindows": {"five_hour": 0.5}}),
        ):
            with self.subTest(line=line):
                self.assertEqual(usage(line), Usage(None, None))

    def test_no_event_or_a_bad_info_is_none(self):
        rows = (
            (),
            ("garbage",),
            ('{"type":"system"}',),
            ("[" * 100000,),
            ('[{"type": "rate_limit_event"}]',),
            (json.dumps({"type": "rate_limit_event"}),),
            (info_line(None),),
            (info_line([1]),),
            (info_line("x"),),
            (event(five=0.1), info_line(None)),
        )
        for lines in rows:
            with self.subTest(lines=[x[:40] for x in lines]):
                self.assertIsNone(usage(*lines))


class SessionStatusTest(Base):
    def setUp(self):
        super().setUp()
        self.common = ClaudeCommon({})
        self.path = self.common.transcript(self.work, SID)
        self.resume = f"cd {shlex.quote(self.work)} && claude --resume {SID}"

    def write(self, path, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.utime(path, (mtime, mtime))

    def status(self, sid=SID) -> SessionStatus:
        return self.common.session_status(sid, self.work)

    def test_a_transcript_is_resumable_with_its_mtime(self):
        self.write(self.path, 1000)
        self.assertEqual(self.status(), SessionStatus("yes", 1000.0, self.resume))

    def test_the_newest_file_under_the_session_dir_wins(self):
        self.write(self.path, 1000)
        sub = self.path[: -len(".jsonl")]
        self.write(os.path.join(sub, "subagents", "old.jsonl"), 500)
        self.assertEqual(self.status().last_active, 1000.0)
        self.write(os.path.join(sub, "subagents", "workflows", "r", "journal.jsonl"), 2000)
        self.write(os.path.join(sub, "subagents", "mid.jsonl"), 1500)
        self.assertEqual(self.status(), SessionStatus("yes", 2000.0, self.resume))

    def test_a_missing_transcript_is_not_resumable(self):
        self.assertEqual(self.status(), SessionStatus("no", None, self.resume))
        os.makedirs(os.path.dirname(os.path.dirname(self.path)))
        with open(os.path.dirname(self.path), "w", encoding="utf-8"):
            pass
        self.assertEqual(self.status().resumable, "no")

    @unittest.skipIf(os.geteuid() == 0, "root reads a 000 dir")
    def test_an_unreadable_project_dir_is_unknown(self):
        self.write(self.path, 1000)
        project = os.path.dirname(self.path)
        os.chmod(project, 0)
        self.addCleanup(os.chmod, project, 0o700)
        self.assertEqual(self.status(), SessionStatus("unknown", None, self.resume))

    def test_a_transcript_that_is_no_regular_file_is_unknown(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        self.assertEqual(self.status().resumable, "unknown")
        os.remove(self.path)
        target = os.path.join(self.tmp, "target.jsonl")
        self.write(target, 1000)
        os.symlink(target, self.path)
        self.assertEqual(self.status().resumable, "unknown")

    def test_a_sid_that_is_no_uuid_is_not_resumable(self):
        for sid in ("abc", SID.upper(), "../" + SID):
            with self.subTest(sid=sid):
                self.assertEqual(self.status(sid), SessionStatus("no", None, ""))


HOOK = ("/py thon", "-I", "/core/tui_hook.py", "/tui/w 1")
HOOKS = {
    "Stop": TURN_END,
    "PermissionRequest": BLOCKED,
    "Notification": BLOCKED,
    "PostToolUse": ACTIVITY,
    "UserPromptSubmit": ACTIVITY,
}
MATCHER = "^(permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input|quota_auto_resume_stale)$"
WAITING = (
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
    "quota_auto_resume_stale",
)
NOT_WAITING = (
    "idle_prompt",
    "auth_success",
    "elicitation_complete",
    "elicitation_response",
    "agent_completed",
    "quota_auto_resume_fired",
    "quota_auto_resume_disabled",
    "",
    "permission_prompt_x",
    "xidle_prompt",
)


def entry(label) -> dict:
    return {"hooks": [{"type": "command", "command": shlex.join((*HOOK, label))}]}


TUI_HOOKS = {
    "Stop": [entry("Stop")],
    "PermissionRequest": [entry("PermissionRequest")],
    "Notification": [{"matcher": MATCHER, **entry("Notification")}],
    "PostToolUse": [entry("PostToolUse")],
    "UserPromptSubmit": [entry("UserPromptSubmit")],
}
TUI_SETTINGS = json.dumps({"hooks": TUI_HOOKS})
NAME = "w-1"
WANT_SETTINGS = "want a JSON object with a list of hooks per event"


def tui_launch(work, prompt="hi", config=CONFIG, resume=False, **spec) -> Launch:
    spec = Spec(**{"workdir": work, "sid": SID, **spec})
    return configured(ClaudeAdapter, config).tui().launch(prompt, spec, resume, HOOK)


class TuiArgvTest(Base):
    def test_named_session_keeps_its_name_fresh_and_resumed(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                argv = self.tui(name="worker-name", resume=resume).argv
                start = argv.index("--name")
                self.assertEqual(argv[start : start + 2], ("--name", "worker-name"))

    def tui(self, prompt="hi", config=CONFIG, resume=False, **spec) -> Launch:
        return tui_launch(self.work, prompt, config, resume, **spec)

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
    def session(self, prompt="hi", flags=(), config=None, resume=False, **spec) -> Launch:
        return tui_launch(
            self.work,
            prompt,
            {} if config is None else config,
            resume,
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
                    lambda: self.session(flags=flags), f"--settings {value!r:.80}: {WANT_SETTINGS}"
                )


def said(mid, *blocks) -> dict:
    """An assistant transcript record of message `mid` (None: no id)."""
    return {
        "type": "assistant",
        "message": {**({"id": mid} if mid else {}), "content": list(blocks)},
    }


class TuiPayloadTest(Base):
    def setUp(self):
        super().setUp()
        self.run = configured(ClaudeAdapter, {}).tui()
        self.path = os.path.join(self.tmp, "t.jsonl")

    def transcript(self, *records):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("".join(f"{r if isinstance(r, str) else json.dumps(r)}\n" for r in records))

    def test_the_last_assistant_message_first(self):
        self.transcript(said("m1", text("old")))
        self.assertEqual(
            self.run.reply({"last_assistant_message": "new", "transcript_path": self.path}), "new"
        )
        self.assertEqual(
            self.run.reply({"last_assistant_message": "", "transcript_path": self.path}), ""
        )

    def test_else_the_transcripts_last_assistant_message(self):
        self.transcript(
            {"type": "user", "message": {"content": "q"}},
            said("m1", text("old")),
            said("m2", text("first")),
            said("m2", tool("Bash")),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            said("m2", text("second")),
            said("m3", tool("Read")),
            "not json",
            "[1]",
            "",
        )
        for extra in ({}, {"last_assistant_message": None}, {"last_assistant_message": 1}):
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.run.reply({"transcript_path": self.path, **extra}), "first\nsecond"
                )

    def test_a_record_without_an_id_is_its_own_message(self):
        self.transcript(said(None, text("x")), said(None, text("y")))
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "y")
        self.transcript(
            said("m1", text("x")),
            {"type": "assistant", "message": {"id": 1, "content": [text("y")]}},
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "y")

    def test_wrong_typed_records_are_skipped(self):
        self.transcript(
            said("m1", text("ok")),
            {"type": "assistant", "message": "x"},
            {"type": "assistant", "message": {"content": "x"}},
            {"type": "assistant"},
            said("m2", text(1), {"type": "text"}, "x", 1),
            {"type": "user", "message": said("m3", text("u"))},
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "ok")

    def test_only_the_last_4_mib_is_read(self):
        self.transcript(
            said("m1", text("early")), {"type": "user", "message": {"content": "x" * (5 << 20)}}
        )
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "")
        self.transcript(said("m1", text("x" * (5 << 20))), said("m2", text("late")))
        self.assertEqual(self.run.reply({"transcript_path": self.path}), "late")

    def test_no_readable_transcript_is_empty(self):
        self.transcript(said("m1", text("ok")))
        link = os.path.join(self.tmp, "link.jsonl")
        os.symlink(self.path, link)
        fifo = os.path.join(self.tmp, "fifo.jsonl")
        os.mkfifo(fifo)
        for path in (
            link,
            fifo,
            self.tmp,
            os.path.join(self.tmp, "absent.jsonl"),
            "",
            1,
            None,
            "a\0b",
            "\ud800",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.run.reply({"transcript_path": path}), "")
        self.assertEqual(self.run.reply({}), "")

    def test_pending_activity_and_blocked_reason(self):
        rows = (
            (self.run.pending, {"background_tasks": [1, {}]}, 2),
            (self.run.pending, {"background_tasks": []}, 0),
            (self.run.pending, {}, 0),
            (self.run.pending, {"background_tasks": 3}, 0),
            (self.run.pending, {"background_tasks": "ab"}, 0),
            (self.run.pending, {"background_tasks": {"a": 1}}, 0),
            (self.run.activity, {"tool_name": "Bash"}, "Bash"),
            (self.run.activity, {}, ""),
            (self.run.activity, {"tool_name": 1}, ""),
            (self.run.blocked_reason, {"tool_name": "Bash", "message": "m"}, "Bash"),
            (
                self.run.blocked_reason,
                {"message": "Claude needs your permission"},
                "Claude needs your permission",
            ),
            (self.run.blocked_reason, {"tool_name": 1, "message": "m"}, "m"),
            (self.run.blocked_reason, {"tool_name": "", "message": "m"}, "m"),
            (self.run.blocked_reason, {"tool_name": None, "message": 2}, ""),
            (self.run.blocked_reason, {}, ""),
        )
        for row, (call, payload, want) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(call(payload), want)

    def test_hook_events_by_label(self):
        rows = (
            (
                "Stop",
                {"session_id": "n1", "last_assistant_message": "hi", "background_tasks": [1, 2]},
                [SessionBound("n1"), TurnEnd("hi", 2)],
            ),
            ("PermissionRequest", {"session_id": "n0", "tool_name": "Bash"}, [Blocked("Bash")]),
            ("Notification", {"message": "Claude is waiting"}, [Blocked("Claude is waiting")]),
            ("PostToolUse", {"tool_name": "Edit"}, [Activity("Edit")]),
            ("UserPromptSubmit", {"prompt": "x"}, [Activity("")]),
            ("SessionStart", {"session_id": "n0"}, []),
        )
        for label, payload, events in rows:
            with self.subTest(label=label):
                self.assertEqual(self.run.hook_events(label, payload, "n0"), events)

    def test_the_trust_dialog(self):
        self.assertEqual(ClaudeTui.dialogs, ("Quick safety check",))


LA = "America/Los_Angeles"


def epoch(stamp, zone=LA) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(zone)).timestamp()


NOW = epoch("2026-10-11 10:00")  # a Sunday


class LimitTest(Case):
    def limit(self, screen, now=NOW):
        with mock.patch("time.time", return_value=now):
            return configured(ClaudeAdapter, {}).tui().limit(screen)

    def check(self, rows):
        for screen, resets in rows:
            with self.subTest(screen=screen):
                self.assertEqual(self.limit(screen), LimitHit(resets))

    def test_the_limit_messages_and_their_reset(self):
        self.check(
            (
                (
                    "You've hit your session limit · resets 3pm (America/Los_Angeles)",
                    epoch("2026-10-11 15:00"),
                ),
                (
                    "You've hit your session limit · resets 9:30am (America/Los_Angeles)",
                    epoch("2026-10-12 09:30"),
                ),
                (
                    "You've hit your weekly limit · resets Oct 15, 9am (America/Los_Angeles) · progress saved",
                    epoch("2026-10-15 09:00"),
                ),
                (
                    "You've hit your Opus limit · resets Jan 2, 2027, 12am (Europe/London)",
                    epoch("2027-01-02 00:00", "Europe/London"),
                ),
                (
                    "You've hit your usage limit · resets 12pm (UTC)",
                    epoch("2026-10-12 12:00", "UTC"),
                ),
                (
                    "You've hit your org's monthly spend limit · your session limit resets 4pm (Asia/Tokyo)",
                    epoch("2026-10-12 16:00", "Asia/Tokyo"),
                ),
                (
                    "You've hit your limit · resets 11pm (Etc/GMT+5)",
                    epoch("2026-10-11 23:00", "Etc/GMT+5"),
                ),
                (
                    "Usage limit reached · resets 3pm (America/New_York)",
                    epoch("2026-10-11 15:00", "America/New_York"),
                ),
                (
                    "> go on\n\n  ⎿  You've hit your\n   session  limit ·\n resets 3pm\n  (America/Los_Angeles)\n",
                    epoch("2026-10-11 15:00"),
                ),
            )
        )

    def test_a_weekday_is_the_next_such_day(self):
        week = "You've hit your weekly limit · resets {} (America/Los_Angeles)"
        self.check(
            (
                (week.format("Monday 9am"), epoch("2026-10-12 09:00")),
                (week.format("Sun 11am"), epoch("2026-10-11 11:00")),
                (week.format("Sunday, 9am"), epoch("2026-10-18 09:00")),
                (week.format("sat 9am"), epoch("2026-10-17 09:00")),
            )
        )

    def test_a_year_less_date_over_a_day_past_is_next_years(self):
        week = "You've hit your weekly limit · resets {} (America/Los_Angeles)"
        for now, when, resets in (
            ("2026-12-30 10:00", "Jan 2, 9am", "2027-01-02 09:00"),
            ("2026-01-01 10:00", "Jan 2, 9am", "2026-01-02 09:00"),
            ("2026-12-30 10:00", "Dec 30, 9am", "2026-12-30 09:00"),
            ("2026-12-30 10:00", "Dec 29, 9am", "2027-12-29 09:00"),
            ("2026-12-30 10:00", "Jan 2, 2026, 9am", "2026-01-02 09:00"),
        ):
            with self.subTest(now=now, when=when):
                self.assertEqual(self.limit(week.format(when), epoch(now)), LimitHit(epoch(resets)))

    def test_no_known_zone_is_local_time(self):
        self.addCleanup(time.tzset)
        patch = mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"})
        patch.start()
        self.addCleanup(patch.stop)
        time.tzset()
        today = epoch("2026-10-12 15:00", "Asia/Tokyo")
        self.check(
            (
                ("You've hit your session limit · resets 3pm", today),
                ("You've hit your session limit · resets 3pm (Mars/Olympus)", today),
                ("You've hit your session limit · resets 3pm (America)", today),
                ("You've hit your session limit · resets 3pm (../../etc/passwd)", today),
                ("You've hit your session limit · resets 3pm ()", today),
                ("You've hit your session limit · resets 3pm (A/B/C/D)", today),
                (
                    "You've hit your weekly limit · resets Oct 15, 9am",
                    epoch("2026-10-15 09:00", "Asia/Tokyo"),
                ),
            )
        )

    def test_the_last_message_counts(self):
        session = "You've hit your session limit · resets 3pm (America/Los_Angeles)"
        self.check(
            (
                (
                    f"{session} … You've hit your weekly limit · resets Oct 15, 9am (America/Los_Angeles)",
                    epoch("2026-10-15 09:00"),
                ),
                (f"{session} You've hit your fast limit · resets in 5m", epoch("2026-10-11 15:00")),
            )
        )

    def test_an_unparseable_reset_is_none(self):
        self.check(
            (
                ("You've hit your session limit", None),
                ("Usage limit reached · continuing automatically in 2h · esc to cancel", None),
                ("You've hit your session limit · resets soon", None),
                ("You've hit your session limit · resets 13pm (UTC)", None),
                ("You've hit your session limit · resets 0am (UTC)", None),
                ("You've hit your session limit · resets 3:75pm (UTC)", None),
                ("You've hit your weekly limit · resets Feb 30, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Monday 15, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Smarch 3, 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Funday 3pm (UTC)", None),
                ("You've hit your weekly limit · resets Oct 3pm (UTC)", None),
            )
        )

    def test_no_limit_message_is_none(self):
        for screen in (
            "",
            "Welcome back",
            "You've hit your fast limit · resets in 5m",
            "You've used 90% of your session limit · resets 3pm (America/Los_Angeles)",
            "Approaching weekly limit",
            "Your usage limit has reset",
            "usage limit reached",
        ):
            with self.subTest(screen=screen):
                self.assertIsNone(self.limit(screen))


class AdapterTest(Base):
    def test_its_parts(self):
        adapter = configured(ClaudeAdapter, CONFIG)
        self.assertEqual(
            ClaudeAdapter.keys, frozenset({"flags", "allow", "tiers", "efforts", "env"})
        )
        self.assertEqual((ClaudeAdapter.runtime_note, ClaudeCommon.NOTE), (NOTE, NOTE))
        self.assertIsInstance(adapter.common, UnconfiguredCommon)
        for mode, kind in ((adapter.headless, ClaudeHeadless), (adapter.tui, ClaudeTui)):
            with self.subTest(kind=kind):
                self.assertIs(type(mode()), kind)
                self.assertIsNot(mode(), mode())
                self.assertIs(mode().adapter, adapter)

    def test_the_usage_probe_and_its_reading(self):
        self.assertEqual(ClaudeCommon.PROBE, PROBE)
        self.assertEqual(
            configured(ClaudeAdapter, CONFIG).usage_probe(),
            Launch(PROBE, env={**CEILING, "K": "v"}, unset=UNSET),
        )
        self.assertEqual(
            configured(ClaudeAdapter, {}).usage(event(five=0.25)), Usage(Window(25.0, 1.0), None)
        )
        self.assertIsNone(configured(ClaudeAdapter, {}).usage("garbage"))

    def test_session_status(self):
        self.assertEqual(
            configured(ClaudeAdapter, {}).session_status(SID, self.work),
            SessionStatus("no", None, f"cd {shlex.quote(self.work)} && claude --resume {SID}"),
        )
        self.assertEqual(
            configured(ClaudeAdapter, {}).session_status("x", self.work),
            SessionStatus("no", None, ""),
        )


class WritableDirsTest(Base):
    def setUp(self):
        super().setUp()
        self.cwd, self.added = (os.path.join(self.tmp, name) for name in ("cwd", "added"))
        for path in (self.cwd, self.added):
            os.mkdir(path)
        self.link = os.path.join(self.tmp, "link")
        os.symlink(self.added, self.link)

    def dirs(self, resume=False, workdir=None, **access) -> tuple[str, ...]:
        spec = Spec(workdir=workdir or self.work, sid=SID, access=Access(**access))
        return configured(ClaudeAdapter, CONFIG).writable_dirs(spec, resume)

    def test_the_workdir_without_a_cwd_then_the_added_dirs(self):
        self.assertEqual(self.dirs(), (self.work,))
        self.assertEqual(self.dirs(dirs=(self.added, self.cwd)), (self.work, self.added, self.cwd))

    def test_the_cwd_first_when_set(self):
        self.assertEqual(self.dirs(cwd=self.cwd), (self.cwd,))
        self.assertEqual(self.dirs(cwd=self.cwd, dirs=(self.added,)), (self.cwd, self.added))

    def test_real_paths_each_once_in_order(self):
        work_link = os.path.join(self.tmp, "work-link")
        os.symlink(self.work, work_link)
        self.assertEqual(self.dirs(workdir=work_link), (self.work,))
        self.assertEqual(
            self.dirs(cwd=self.link, dirs=(self.added, self.work, work_link, self.cwd + "/")),
            (self.added, self.work, self.cwd),
        )

    def test_a_resume_the_same(self):
        for access, dirs in (
            ({}, (self.work,)),
            ({"cwd": self.cwd}, (self.cwd,)),
            ({"dirs": (self.link, self.work)}, (self.work, self.added)),
        ):
            with self.subTest(access=access):
                self.assertEqual(self.dirs(True, **access), dirs)

    def test_read_dirs_never(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertEqual(
                    self.dirs(resume, dirs=(self.added,), reads=(self.cwd, self.link)),
                    (self.work, self.added),
                )

    def test_access_and_flag_dirs_are_combined_for_every_session(self):
        os.mkdir(os.path.join(self.cwd, "sub"))
        flags = (
            "--verbose",
            "--add-dir",
            "sub",
            "--add-dir=../added",
            "--add-dir",
            self.link,
            "--",
            "--add-dir",
            "/x",
        )
        for resume in (False, True):
            for cwd, given, dirs in (
                ("", (), (self.work, self.tmp)),
                (self.cwd, (), (self.cwd, self.tmp)),
                (self.cwd, flags, (self.cwd, self.tmp, os.path.join(self.cwd, "sub"), self.added)),
            ):
                with self.subTest(resume=resume, cwd=cwd, flags=given):
                    spec = Spec(
                        workdir=self.work,
                        sid=SID,
                        access=Access(cwd=cwd, dirs=(self.tmp,), reads=(self.tmp,)),
                        flags=given,
                    )
                    self.assertEqual(
                        configured(ClaudeAdapter, CONFIG).writable_dirs(spec, resume), dirs
                    )


class CommittedConfigTest(Base):
    def test_the_claude_table_builds_a_claude_adapter(self):
        tables = registry.tables(registry.CONFIG, os.path.join(self.tmp, "absent.toml"))
        self.assertEqual(
            tables["claude"],
            {
                "implementation": "adapters.claude:ClaudeAdapter",
                "tiers": {"1": "fable", "2": "opus", "3": "sonnet", "4": "haiku"},
                "efforts": {e: e for e in ("low", "medium", "high", "xhigh", "max")},
            },
        )
        registry.configure({"adapters": tables})
        adapter = registry.adapter("claude")
        self.assertIs(type(adapter), ClaudeAdapter)
        argv = (
            adapter.headless()
            .launch("hi", Spec(workdir=self.work, tier=4, effort="low", sid=SID), False)
            .argv
        )
        self.assertEqual(
            argv[4:10],
            ("--model", "haiku", "--effort", "low", "--setting-sources", "user"),
        )


if __name__ == "__main__":
    unittest.main()
