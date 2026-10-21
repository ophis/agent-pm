"""Image plugin installation with fake client commands, never host credentials."""

import io
import json
import os
import runpy
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
        self.markets = {"ophis/claude-autopilot": "claude-autopilot"}
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
            name = self.markets[argv[4]]
            market = (
                home
                / ("plugins/marketplaces" if client == "claude" else ".tmp/marketplaces")
                / name
            )
            (market / ".claude-plugin").mkdir(parents=True)
            (market / ".claude-plugin/marketplace.json").write_text(json.dumps({"name": name}))
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
            self.assertEqual(kw["stdin"], self.code["subprocess"].DEVNULL)
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
        error = self.code["subprocess"].CalledProcessError
        with self.assertRaises(error):
            self.install(run=mock.Mock(side_effect=error(1, "fake")))

    def test_distinct_repositories_use_their_manifest_marketplace_names(self):
        self.markets["vendor/catalogue-repo"] = "team-market"
        self.install([self.plugins[0], {"name": "beta", "repo": "vendor/catalogue-repo"}])
        commands = [argv for argv, _ in self.calls]
        for client, verb in (("claude", "install"), ("codex", "add")):
            self.assertIn(
                [client, "plugin", "marketplace", "add", "vendor/catalogue-repo"], commands
            )
            self.assertIn([client, "plugin", verb, "autopilot@claude-autopilot"], commands)
            self.assertIn([client, "plugin", verb, "beta@team-market"], commands)
        self.assertEqual(
            (self.root / "plugins/beta/skills/build/SKILL.md").read_text(), "original skill"
        )

    def test_bad_installed_metadata_and_revision_fail_for_either_client(self):
        for client, verb, home, version in (
            ("claude", "install", "claude-plugins", "1.0.0"),
            ("codex", "add", "codex-plugins", "2.0.0"),
        ):
            for case in (
                "missing-version",
                "invalid-version",
                "ambiguous",
                "missing-manifest",
                "revision",
            ):
                with self.subTest(client=client, case=case):
                    self.root = Path(self.home, client, case)

                    def bad_metadata(
                        argv,
                        *,
                        client=client,
                        verb=verb,
                        home=home,
                        version=version,
                        case=case,
                        **kw,
                    ):
                        result = self.client_run(argv, **kw)
                        if argv[:3] == [client, "plugin", verb]:
                            installed = (
                                self.root
                                / home
                                / "plugins/cache/claude-autopilot/autopilot"
                                / version
                            )
                            manifest = installed / ".claude-plugin/plugin.json"
                            if case == "missing-version":
                                manifest.write_text('{"name":"autopilot"}')
                            elif case == "invalid-version":
                                manifest.write_text('{"name":"autopilot","version":42}')
                            elif case == "ambiguous":
                                (installed.parent / "3.0.0").mkdir()
                            elif case == "missing-manifest":
                                manifest.unlink()
                        if case == "revision" and argv[0] == "git" and home in Path(argv[2]).parts:
                            return SimpleNamespace(stdout="invalid revision", returncode=0)
                        return result

                    with self.assertRaisesRegex(ValueError, "plugin-install:"):
                        self.install([self.plugins[0]], run=bad_metadata)

    def test_main_reads_the_sources_file(self):
        source = Path(self.home, "sources.json")
        source.write_text(json.dumps([self.plugins[0]]))
        with (
            mock.patch("subprocess.run", side_effect=self.client_run),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.code["main"]([str(source), str(self.root)])
        self.assertTrue((self.root / "plugins/autopilot/skills/build/SKILL.md").is_file())

    def test_main_requires_input_file_and_absolute_root(self):
        for argv in ([], ["input"], ["input", "relative"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.code["main"](argv)
