"""new/core/src/adapters/claude/: ClaudeCommon's config, flags, env, paths, usage and session status;
ClaudeHeadless's launch and parse; ClaudeTui's launch, hook payloads and usage-limit screen; ClaudeAdapter's hooks; the
committed [adapters.claude] table. HOME and CLAUDE_CONFIG_DIR are temp dirs; nothing starts a process."""

from new.tests import hermetic

import json
import os
import re
import shlex
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.adapters.base import (
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
    Spec,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.adapters.claude import ClaudeAdapter
from agent_pm.adapters.claude.common import ClaudeCommon
from agent_pm.adapters.claude.headless import ClaudeHeadless
from agent_pm.adapters.claude.tui import ClaudeTui
from agent_pm.driver import registry

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SETTINGS = '{"enabledPlugins": {"agent-pm@agent-pm": false}}'
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
    "env": "a table of strings to strings",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.home = os.path.dirname(hermetic.home(self))
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
        return ClaudeAdapter(config).headless().launch(prompt, spec, resume)


class ConfigTest(Base):
    def test_every_key_is_optional_and_each_is_accepted(self):
        self.assertEqual(ClaudeAdapter({}).config, {})
        self.assertEqual(ClaudeAdapter(CONFIG).config, CONFIG)

    def test_an_unknown_key_is_refused(self):
        self.refused(
            lambda: ClaudeAdapter({"model": "m"}), "unknown key 'model' in ClaudeAdapter's config"
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
            ("env", "SECRET"),
            ("env", {"K": 1}),
            ("env", {1: "SECRET"}),
            ("env", ["SECRET"]),
        )
        for key, value in rows:
            with self.subTest(key=key, value=value):
                self.refused(
                    lambda: ClaudeAdapter({key: value}),
                    f"[adapters.claude].{key} must be {WHAT[key]}",
                )

    def test_setting_sources_in_flags_is_refused(self):
        for flags in (
            ["--setting-sources"],
            ["--setting-sources=user"],
            ["--verbose", "--setting-sources", "user"],
        ):
            with self.subTest(flags=flags):
                self.refused(
                    lambda: ClaudeAdapter({"flags": flags}),
                    "--setting-sources is the Driver's: remove it from [adapters.claude].flags",
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
        access = Access(dirs=("/d1", "/d 2"), commands=("git status", "ls"))
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
                "--settings",
                SETTINGS,
                "--add-dir",
                "/d1",
                "--add-dir",
                "/d 2",
                "--allowedTools",
                "Bash(git status)",
                "Bash(ls)",
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
                "--settings",
                SETTINGS,
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
        argv = self.launch(config={}, access=Access(cwd=cwd, project=True)).argv
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
            self.launch(config={}, access=Access(project=True)).argv[6:8],
            ("--mcp-config", os.path.join(self.work, ".mcp.json")),
        )
        self.assertNotIn("--mcp-config", self.launch(config={}, access=Access(cwd=cwd)).argv)

    def test_an_mcp_path_that_is_no_file_is_skipped(self):
        os.mkdir(os.path.join(self.work, ".mcp.json"))
        self.assertNotIn("--mcp-config", self.launch(config={}, access=Access(project=True)).argv)

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
                self.refused(
                    lambda: self.launch(sid=bad), f"claude session ids are UUIDs, got {bad!r}"
                )
        self.refused(
            lambda: ClaudeCommon.uuid(SID + "\n"),
            f"claude session ids are UUIDs, got {SID + chr(10)!r}",
        )
        self.refused(lambda: ClaudeCommon.uuid(None), "claude session ids are UUIDs, got None")
        self.assertEqual(ClaudeCommon.uuid(SID), SID)


class EnvTest(Base):
    def test_defaults_overlaid_by_the_config_env(self):
        self.assertEqual(ClaudeCommon({}).env(), CEILING)
        self.assertEqual(
            ClaudeCommon({"env": {"K": "v", "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1"}}).env(),
            {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "1", "K": "v"},
        )

    def test_the_launch_env_unset_and_cwd(self):
        launch = self.launch()
        self.assertEqual(dict(launch.env), {**CEILING, "K": "v"})
        self.assertEqual(launch.unset, UNSET)
        self.assertEqual(ClaudeCommon.UNSET, UNSET)
        self.assertEqual(launch.cwd, "")


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

    def test_a_workdir_other_than_cwd_is_added(self):
        common = ClaudeCommon({})
        self.assertEqual(
            common.resume("/w", SID, "/o x"), f"cd /w && claude --resume {SID} --add-dir '/o x'"
        )
        link = os.path.join(self.tmp, "link")
        os.symlink(self.work, link)
        self.assertEqual(
            common.resume(self.work, SID, link),
            f"cd {shlex.quote(self.work)} && claude --resume {SID}",
        )

    def test_the_launch_resume(self):
        cwd = os.path.join(self.tmp, "cwd")
        q = shlex.quote
        self.assertEqual(self.launch().resume, f"cd {q(self.work)} && claude --resume {SID}")
        self.assertEqual(
            self.launch(access=Access(cwd=cwd)).resume,
            f"cd {q(cwd)} && claude --resume {SID} --add-dir {q(self.work)}",
        )


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
        self.run = ClaudeAdapter({}).headless()

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
                self.assertEqual(list(ClaudeAdapter({}).headless().parse(record)), events)

    def test_a_rejected_rate_limit_event_is_a_limit_hit(self):
        self.assertEqual(self.parse(rejected(1700000000)), [LimitHit(1700000000.0)])
        for record in (rejected(status="allowed"), rejected(status="allowed_warning")):
            with self.subTest(record=record):
                self.assertEqual(list(ClaudeAdapter({}).headless().parse(record)), [])
        for resets in (None, True, "1700000000", 1e999):
            with self.subTest(resets=resets):
                self.assertEqual(
                    list(ClaudeAdapter({}).headless().parse(rejected(resets))), [LimitHit(None)]
                )

    def test_an_assistant_rate_limit_error_is_one_limit_hit_with_the_remembered_reset(self):
        self.assertEqual(
            self.parse(rejected(5), assistant(text("limit"), error="rate_limit"), rejected(6)),
            [LimitHit(5.0), Reply("limit")],
        )
        run = ClaudeAdapter({}).headless()
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
        self.assertEqual(list(ClaudeAdapter({}).headless().parse(assistant(error="other"))), [])

    def test_each_run_sends_its_own(self):
        adapter = ClaudeAdapter({})
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


class ParseUsageTest(unittest.TestCase):
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


TUI_SETTINGS = json.dumps(
    {
        "enabledPlugins": {"agent-pm@agent-pm": False},
        "hooks": {
            "Stop": [entry("Stop")],
            "PermissionRequest": [entry("PermissionRequest")],
            "Notification": [{"matcher": MATCHER, **entry("Notification")}],
            "PostToolUse": [entry("PostToolUse")],
            "UserPromptSubmit": [entry("UserPromptSubmit")],
        },
    }
)


class TuiArgvTest(Base):
    def tui(self, prompt="hi", config=CONFIG, resume=False, **spec) -> Launch:
        spec = Spec(**{"workdir": self.work, "sid": SID, **spec})
        return ClaudeAdapter(config).tui().launch(prompt, spec, resume, HOOK)

    def test_a_new_session_with_every_part(self):
        access = Access(dirs=("/d1", "/d 2"), commands=("git status", "ls"))
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
        tui, headless = self.tui(access=access), self.launch(access=access)
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
        self.assertEqual(dict(tui.env), {**CEILING, "K": "v"})
        self.assertEqual(
            (tui.env, tui.unset, tui.cwd, tui.transcript, tui.resume),
            (headless.env, headless.unset, headless.cwd, headless.transcript, headless.resume),
        )

    def test_a_bad_sid_or_tier_is_refused(self):
        self.refused(lambda: self.tui(sid="abc"), "claude session ids are UUIDs, got 'abc'")
        self.refused(lambda: self.tui(tier=3), "no model for tier 3 in [adapters.claude].tiers")


def said(mid, *blocks) -> dict:
    """An assistant transcript record of message `mid` (None: no id)."""
    return {
        "type": "assistant",
        "message": {**({"id": mid} if mid else {}), "content": list(blocks)},
    }


class TuiPayloadTest(Base):
    def setUp(self):
        super().setUp()
        self.run = ClaudeAdapter({}).tui()
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


class LimitTest(unittest.TestCase):
    def limit(self, screen):
        with mock.patch("time.time", return_value=NOW):
            return ClaudeAdapter({}).tui().limit(screen)

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
        adapter = ClaudeAdapter(CONFIG)
        self.assertEqual(
            ClaudeAdapter.keys, frozenset({"flags", "allow", "tiers", "efforts", "env"})
        )
        self.assertEqual((ClaudeAdapter.runtime_note, ClaudeCommon.NOTE), (NOTE, NOTE))
        self.assertIsInstance(adapter.common, ClaudeCommon)
        for mode, kind in ((adapter.headless, ClaudeHeadless), (adapter.tui, ClaudeTui)):
            with self.subTest(kind=kind):
                self.assertIs(type(mode()), kind)
                self.assertIsNot(mode(), mode())
                self.assertIs(mode().adapter, adapter)

    def test_the_usage_probe_and_its_reading(self):
        self.assertEqual(ClaudeCommon.PROBE, PROBE)
        self.assertEqual(
            ClaudeAdapter(CONFIG).usage_probe(),
            Launch(PROBE, env={**CEILING, "K": "v"}, unset=UNSET),
        )
        self.assertEqual(ClaudeAdapter({}).usage(event(five=0.25)), Usage(Window(25.0, 1.0), None))
        self.assertIsNone(ClaudeAdapter({}).usage("garbage"))

    def test_session_status(self):
        self.assertEqual(
            ClaudeAdapter({}).session_status(SID, self.work),
            SessionStatus("no", None, f"cd {shlex.quote(self.work)} && claude --resume {SID}"),
        )
        self.assertEqual(
            ClaudeAdapter({}).session_status("x", self.work), SessionStatus("no", None, "")
        )


class CommittedConfigTest(Base):
    def test_the_claude_table_builds_a_claude_adapter(self):
        tables = registry.tables(registry.CONFIG, os.path.join(self.tmp, "absent.toml"))
        self.assertEqual(
            tables["claude"],
            {
                "implementation": "adapters.claude:ClaudeAdapter",
                "flags": ["--permission-mode", "auto", "--strict-mcp-config"],
                "tiers": {"1": "fable", "2": "opus", "3": "sonnet", "4": "haiku"},
                "efforts": {e: e for e in ("low", "medium", "high", "xhigh", "max")},
            },
        )
        registry.configure({"adapters": tables})
        self.addCleanup(registry.reset)
        adapter = registry.adapter("claude")
        self.assertIs(type(adapter), ClaudeAdapter)
        argv = (
            adapter.headless()
            .launch("hi", Spec(workdir=self.work, tier=4, effort="low", sid=SID), False)
            .argv
        )
        self.assertEqual(
            argv[4:12],
            (
                "--model",
                "haiku",
                "--effort",
                "low",
                "--permission-mode",
                "auto",
                "--strict-mcp-config",
                "--setting-sources",
            ),
        )


if __name__ == "__main__":
    unittest.main()
