"""Image plugin installation with fake client commands, never host credentials."""

import io
import json
import os
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.case import REPO, Case


class PluginInstall(Case):
    def setUp(self):
        self.code = runpy.run_path(
            str(Path(REPO, "broker/internal/launcher/assets/plugin-install.py"))
        )
        self.root = Path(self.home, "image")
        self.calls = []
        self.plugins = [
            {"name": "autopilot", "repo": "ophis/claude-autopilot"},
            {"name": "beta", "repo": "ophis/claude-autopilot"},
        ]

    def client_run(self, argv, **kw):
        self.calls.append((argv, kw))
        env = kw["env"]
        client = argv[0]
        home = Path(env["CLAUDE_CONFIG_DIR"] if client == "claude" else env["CODEX_HOME"])
        if argv[1:4] == ["plugin", "marketplace", "add"]:
            market = home / (
                "plugins/marketplaces/claude-autopilot"
                if client == "claude"
                else ".tmp/marketplaces/claude-autopilot"
            )
            (market / ".claude-plugin").mkdir(parents=True)
            (market / ".claude-plugin/marketplace.json").write_text(
                json.dumps({"name": "claude-autopilot"})
            )
        if argv[1:3] in (["plugin", "install"], ["plugin", "add"]):
            name, market = argv[3].split("@")
            version = "2.0.0" if client == "codex" else "1.0.0"
            installed = home / "plugins/cache" / market / name / version
            (installed / ".claude-plugin").mkdir(parents=True)
            (installed / ".claude-plugin/plugin.json").write_text(
                json.dumps({"name": name, "version": version})
            )
            (installed / "skills/build").mkdir(parents=True)
            (installed / "skills/build/SKILL.md").write_text("original skill")
            if client == "codex":
                with (home / "config.toml").open("a") as stream:
                    stream.write(f'[plugins."{name}@{market}"]\nenabled = true\n')
        return SimpleNamespace(stdout="a" * 40 + "\n", returncode=0)

    def install(self, plugins=None, run=None):
        with (
            mock.patch("subprocess.run", side_effect=run or self.client_run),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.code["install"](self.plugins if plugins is None else plugins, self.root)
        return output.getvalue()

    def test_each_client_installs_every_plugin_from_the_repo_without_refs(self):
        output = self.install()
        commands = [argv for argv, _ in self.calls if argv[0] != "git"]
        self.assertEqual(
            commands,
            [
                ["claude", "plugin", "marketplace", "add", "ophis/claude-autopilot"],
                ["codex", "plugin", "marketplace", "add", "ophis/claude-autopilot"],
                ["claude", "plugin", "install", "autopilot@claude-autopilot"],
                ["codex", "plugin", "add", "autopilot@claude-autopilot"],
                ["claude", "plugin", "install", "beta@claude-autopilot"],
                ["codex", "plugin", "add", "beta@claude-autopilot"],
            ],
        )
        for name in ("autopilot", "beta"):
            self.assertIn(
                f"claude {name}@claude-autopilot version=1.0.0 commit=" + "a" * 40, output
            )
            self.assertIn(f"codex {name}@claude-autopilot version=2.0.0 commit=" + "a" * 40, output)

    def test_opencode_reads_original_claude_skill_without_host_install(self):
        self.install()
        original = self.root / "claude-plugins/plugins/cache/claude-autopilot/autopilot/1.0.0"
        shared = self.root / "plugins/autopilot"
        self.assertEqual(shared.resolve(), original)
        self.assertEqual((shared / "skills/build/SKILL.md").read_text(), "original skill")
        self.assertFalse(Path(self.home, ".claude").exists())
        self.assertTrue(
            (self.root / "codex-plugins/plugins/cache/claude-autopilot/autopilot/2.0.0").is_dir()
        )

    def test_client_commands_have_only_image_environment_and_closed_stdin(self):
        with mock.patch.dict(os.environ, {"PERSONAL_SECRET": "do-not-copy"}):
            self.install()
        for _, kw in self.calls:
            self.assertNotIn("PERSONAL_SECRET", kw["env"])
            self.assertEqual(kw["stdin"], subprocess.DEVNULL)
            self.assertTrue(kw["check"])

    def test_empty_plugins_still_prepare_native_empty_homes(self):
        self.assertEqual(self.install([]), "")
        self.assertEqual(self.calls, [])
        self.assertTrue((self.root / "plugins").is_dir())
        self.assertTrue((self.root / "codex-plugins/plugins/cache").is_dir())
        self.assertEqual((self.root / "codex-plugins/config.toml").read_text(), "")

    def test_missing_metadata_and_command_failures_fail_the_build(self):
        def no_metadata(_argv, **_kw):
            return SimpleNamespace(stdout="", returncode=0)

        with self.assertRaises((ValueError, FileNotFoundError)):
            self.install(run=no_metadata)
        with self.assertRaises(subprocess.CalledProcessError):
            self.install(run=mock.Mock(side_effect=subprocess.CalledProcessError(1, "fake")))

    def test_main_requires_input_file_and_absolute_root(self):
        for argv in ([], ["input"], ["input", "relative"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.code["main"](argv)
