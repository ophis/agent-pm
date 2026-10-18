"""OpenCode's config checks and session bindings."""

import json
import os

from agent_pm.session.clients.opencode import OpenCodeClient
from agent_pm.session.clients.opencode.common import NATIVE, OpenCodeCommon
from agent_pm.session.engine.types import ConfigError, Effort

from session.tests.case import Case
from session.tests.unit.clients.opencode.support import SES, packaged_table


def common(**config) -> OpenCodeCommon:
    return OpenCodeClient({**packaged_table(), **config}).common


class ConfigTest(Case):
    def test_the_packaged_table_builds_the_client(self):
        found = OpenCodeClient(packaged_table()).common
        self.assertEqual(found.port, 11434)
        for tier in range(1, 5):
            self.assertEqual(found.model(tier), "ollama/qwen3.6:35b-mlx")
        self.assertEqual(found.variant(Effort.HIGH), "high")
        self.assertEqual(found.modes, {"auto": (), "yolo": ("--auto",)})

    def test_an_unknown_key_is_a_config_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown key 'model'"):
            OpenCodeClient({**packaged_table(), "model": "x"})

    def test_ollama_port_must_be_an_integer_port(self):
        for port in (True, 0, 65536, "11434", 1.5):
            with (
                self.subTest(port=port),
                self.assertRaisesRegex(ConfigError, r"ollama_port must be an integer port"),
            ):
                common(ollama_port=port)
        self.assertEqual(common(ollama_port=11500).port, 11500)

    def test_tiers_efforts_and_env_are_checked(self):
        for key, value in (("tiers", {"1": 3}), ("efforts", {"low": ""}), ("env", {"A": 1})):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, rf"\.{key} must be"):
                common(**{key: value})

    def test_a_missing_tier_or_effort_is_a_config_error(self):
        found = common(tiers={"1": "ollama/a"}, efforts={"low": "low"})
        self.assertIsNone(found.model(None))
        self.assertIsNone(found.variant(None))
        with self.assertRaisesRegex(ConfigError, r"no model for tier 3"):
            found.model(3)
        with self.assertRaisesRegex(ConfigError, r"no variant for effort 'high'"):
            found.variant(Effort.HIGH)

    def test_refused_flags(self):
        for flags in (
            ["-s", SES],
            ["--session=x"],
            ["-c"],
            ["--continue"],
            ["--fork"],
            ["--format", "default"],
            ["--attach", "http://127.0.0.1:4096"],
            ["--dir", "/tmp"],
            ["--auto"],
            ["--yolo"],
            ["--dangerously-skip-permissions"],
        ):
            with self.subTest(flags=flags), self.assertRaises(ConfigError):
                common(flags=flags)
        for mode in (["--session", SES], ["--format", "json"], ["--dir", "/tmp"]):
            with self.subTest(mode=mode), self.assertRaises(ConfigError):
                common(modes={"auto": mode})
        self.assertEqual(common(flags=["--agent", "build"]).flags, ("--agent", "build"))

    def test_env_overriding_the_inline_config_is_refused(self):
        for key in (
            "OPENCODE_PERMISSION",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_DIR",
            "OPENCODE_CONFIG_CONTENT",
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                common(env={key: "{}"})


class StoreTest(Case):
    def setUp(self):
        self.common = common()
        self.cwd = os.path.realpath(self.home)

    def test_a_binding_round_trips_by_cwd_and_sid(self):
        self.common.bind(self.cwd, "run-1", SES)
        self.assertEqual(self.common.native(self.cwd, "run-1"), SES)
        self.assertIsNone(self.common.native(self.cwd, "run-2"))
        self.assertIsNone(self.common.native(os.path.join(self.cwd, "x"), "run-1"))
        (name,) = os.listdir(self.common.store)
        self.assertEqual(os.stat(self.common.store).st_mode & 0o777, 0o700)
        self.assertTrue(name.endswith(".json"))

    def test_bind_refuses_an_invalid_session_id(self):
        for native in ("--auto", "ses_", "ses_a b", "x" * 10):
            with self.subTest(native=native), self.assertRaises(ConfigError):
                self.common.bind(self.cwd, "run-1", native)

    def test_a_tampered_binding_is_a_config_error(self):
        self.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.common.store)
        path = os.path.join(self.common.store, name)
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        for change in (
            {"native": "--auto"},
            {"sid": "run-2"},
            {"cwd": "/elsewhere"},
            {"version": 2},
            {"extra": 1},
        ):
            with self.subTest(change=change):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({**record, **change}, f)
                with self.assertRaises(ConfigError):
                    self.common.native(self.cwd, "run-1")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json")
        with self.assertRaises(ConfigError):
            self.common.native(self.cwd, "run-1")

    def test_a_symlinked_binding_is_not_followed(self):
        self.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.common.store)
        path = os.path.join(self.common.store, name)
        target = os.path.join(self.home, "target.json")
        os.replace(path, target)
        os.symlink(target, path)
        self.assertIsNone(self.common.native(self.cwd, "run-1"))

    def test_native_pattern(self):
        self.assertTrue(NATIVE.fullmatch(SES))
        self.assertFalse(NATIVE.fullmatch("ses_-x"))
