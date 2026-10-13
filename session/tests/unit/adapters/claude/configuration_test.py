"""configuration test behavior."""

import os

from agent_pm.session.adapters.claude import ClaudeAdapter
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
)

from session.tests.unit.adapters.claude.fixtures import CONFIG, SID, UNSET, WHAT, Base, ClaudeCommon


class ConfigTest(Base):
    def test_every_key_is_optional_and_each_is_accepted(self):
        self.assertEqual(ClaudeAdapter({}).config, {})
        self.assertEqual(ClaudeAdapter(CONFIG).config, CONFIG)

    def test_an_unknown_key_is_refused(self):
        self.refused(
            lambda: ClaudeAdapter({"model": "m"}),
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
            ("tiers", {"1": " "}),
            ("efforts", {"low": "a\nb"}),
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
                    lambda *, key=key, value=value: ClaudeAdapter({key: value}),
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
                    ClaudeAdapter({"env": {key: value}})
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
                    lambda *, flags=flags: ClaudeAdapter({"flags": flags}),
                    "--setting-sources is the Driver's: remove it from [adapters.claude].flags",
                )

    def test_the_env_attributes(self):
        adapter = ClaudeAdapter({})
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
                    lambda *, common=common: common.model(2),
                    "no model for tier 2 in [adapters.claude].tiers",
                )
                self.refused(
                    lambda *, common=common: common.effort("medium"),
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
                self.refused(
                    lambda *, bad=bad: self.launch(sid=bad), f"session ids are UUIDs, got {bad!r}"
                )
        self.refused(
            lambda: ClaudeCommon.uuid(SID + "\n"),
            f"claude session ids are UUIDs, got {SID + chr(10)!r}",
        )
        self.refused(lambda: ClaudeCommon.uuid(None), "claude session ids are UUIDs, got None")
        self.assertEqual(ClaudeCommon.uuid(SID), SID)
