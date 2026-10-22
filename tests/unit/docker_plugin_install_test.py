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
            {"name": "autopilot", "repo": "ophis/claude-autopilot", "commit": "a" * 40},
            {"name": "beta", "repo": "ophis/claude-autopilot", "commit": "a" * 40},
        ]

    def client_run(self, argv, **kw):
        self.calls.append((argv, kw))
        env = kw["env"]
        client = argv[0]
        home = Path(env["CLAUDE_CONFIG_DIR"] if client == "claude" else env["CODEX_HOME"])
        if client == "git" and "fetch" in argv:
            repo = argv[-2].removeprefix("https://github.com/").removesuffix(".git")
            market = Path(argv[2])
            (market / ".claude-plugin").mkdir(parents=True)
            (market / ".claude-plugin/marketplace.json").write_text(
                json.dumps(
                    {
                        "name": self.markets[repo],
                        "plugins": [
                            {"name": name, "source": "./"} for name in ("autopilot", "beta")
                        ],
                    }
                )
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

    def test_each_client_installs_from_its_own_verified_pinned_checkout(self):
        output = self.install()
        commands = [argv for argv, _ in self.calls]
        fetches = [argv for argv in commands if "fetch" in argv]
        self.assertEqual(len(fetches), 2)
        for client, verb in (("claude", "install"), ("codex", "add")):
            source = self.root / f"{client}-plugins/sources/ophis/claude-autopilot"
            fetch = [
                "git",
                "-C",
                str(source),
                "fetch",
                "--depth=1",
                "https://github.com/ophis/claude-autopilot.git",
                "a" * 40,
            ]
            self.assertIn(fetch, commands)
            register = [client, "plugin", "marketplace", "add", str(source)]
            self.assertIn(register, commands)
            verify = ["git", "-C", str(source), "rev-parse", "HEAD"]
            self.assertLess(commands.index(verify), commands.index(register))
            for name in ("autopilot", "beta"):
                self.assertIn([client, "plugin", verb, f"{name}@claude-autopilot"], commands)
                self.assertIn(f"{client} {name}@claude-autopilot version=", output)
        self.assertEqual(output.count("commit=" + "a" * 40), 4)

    def test_wrong_checkout_fails_before_registration_and_after_install_for_each_client(self):
        for client in ("claude", "codex"):
            for drift in (False, True):
                with self.subTest(client=client, drift=drift):
                    self.root = Path(self.home, client, str(drift))
                    self.calls = []
                    reads = 0

                    def wrong_revision(argv, **kw):
                        nonlocal reads
                        result = self.client_run(argv, **kw)
                        if (
                            argv[0] == "git"
                            and "rev-parse" in argv
                            and f"{client}-plugins" in Path(argv[2]).parts
                        ):
                            reads += 1
                            if not drift or reads > 1:
                                return SimpleNamespace(stdout="b" * 40, returncode=0)
                        return result

                    with self.assertRaisesRegex(
                        ValueError,
                        client
                        + ".*ophis/claude-autopilot.*expected "
                        + "a" * 40
                        + ".*actual .*"
                        + "b" * 40,
                    ):
                        self.install([self.plugins[0]], run=wrong_revision)
                    installed = [
                        argv
                        for argv, _ in self.calls
                        if argv[:3] in ([client, "plugin", "install"], [client, "plugin", "add"])
                    ]
                    self.assertEqual(bool(installed), drift)

    def test_invalid_pins_duplicates_and_conflicts_fail_before_commands(self):
        for plugins in (
            [{"name": "autopilot", "repo": "ophis/claude-autopilot"}],
            [self.plugins[0] | {"commit": "main"}],
            [self.plugins[0] | {"commit": "a" * 39}],
            [self.plugins[0] | {"commit": "A" * 40}],
            [self.plugins[0] | {"repo": "../outside"}],
            [self.plugins[0], self.plugins[0]],
            [self.plugins[0], self.plugins[1] | {"commit": "b" * 40}],
        ):
            with (
                self.subTest(plugins=plugins),
                self.assertRaisesRegex(ValueError, "plugin-install:"),
            ):
                self.install(plugins)
            self.assertEqual(self.calls, [])

    def test_remote_missing_or_escaping_plugin_source_fails_before_install(self):
        for index, source in enumerate(
            (
                {"source": "github", "repo": "vendor/repo"},
                "../outside",
                "/tmp/outside",
                "./missing",
                None,
            )
        ):
            with self.subTest(source=source):
                self.root = Path(self.home, f"source-{index}")
                self.calls = []

                def bad_source(argv, *, source=source, **kw):
                    result = self.client_run(argv, **kw)
                    if argv[0] == "git" and "fetch" in argv:
                        manifest = Path(argv[2], ".claude-plugin/marketplace.json")
                        data = json.loads(manifest.read_text())
                        data["plugins"][0]["source"] = source
                        manifest.write_text(json.dumps(data))
                    return result

                with self.assertRaisesRegex(ValueError, "plugin-install:.*source"):
                    self.install([self.plugins[0]], run=bad_source)
                self.assertFalse(any(argv[0] in ("claude", "codex") for argv, _ in self.calls))

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
        with self.assertRaisesRegex(ValueError, "claude.*ophis/claude-autopilot.*command failed"):
            self.install(run=mock.Mock(side_effect=error(1, "fake")))

    def test_distinct_repositories_use_their_manifest_marketplace_names(self):
        self.markets["vendor/catalogue-repo"] = "team-market"
        self.install(
            [self.plugins[0], {"name": "beta", "repo": "vendor/catalogue-repo", "commit": "a" * 40}]
        )
        commands = [argv for argv, _ in self.calls]
        for client, verb in (("claude", "install"), ("codex", "add")):
            self.assertIn(
                [
                    client,
                    "plugin",
                    "marketplace",
                    "add",
                    str(self.root / f"{client}-plugins/sources/vendor/catalogue-repo"),
                ],
                commands,
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

    def test_fetch_failure_names_client_repo_and_expected_commit(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                self.root = Path(self.home, f"failed-{client}")

                def fetch_failure(argv, *, client=client, **kw):
                    if "fetch" in argv and f"{client}-plugins" in Path(argv[2]).parts:
                        raise self.code["subprocess"].CalledProcessError(1, argv)
                    return self.client_run(argv, **kw)

                with self.assertRaisesRegex(
                    ValueError, client + ".*ophis/claude-autopilot.*" + "a" * 40 + ".*fetch"
                ):
                    self.install([self.plugins[0]], run=fetch_failure)

    def test_main_reports_invalid_input_without_running_a_client(self):
        source = Path(self.home, "bad.json")
        source.write_text('[{"name":"autopilot"}]')
        with (
            mock.patch("subprocess.run") as run,
            self.assertRaisesRegex(SystemExit, "plugin-install:.*full commit"),
        ):
            self.code["main"]([str(source), str(self.root)])
        run.assert_not_called()

    def test_marketplace_name_collision_between_repositories_fails(self):
        self.markets["vendor/catalogue-repo"] = "claude-autopilot"
        with self.assertRaisesRegex(ValueError, "duplicate marketplace name"):
            self.install(
                [
                    self.plugins[0],
                    {"name": "beta", "repo": "vendor/catalogue-repo", "commit": "a" * 40},
                ]
            )

    def test_invalid_marketplace_names_stop_before_other_client_or_plugin_install(self):
        for index, name in enumerate((None, 42, [], {}, "", "../escape", "a" * 65)):
            with self.subTest(name=name):
                self.root = Path(self.home, f"market-{index}")
                self.calls = []

                def bad_marketplace(argv, *, name=name, **kw):
                    result = self.client_run(argv, **kw)
                    if argv[0] == "git" and "fetch" in argv:
                        manifest = Path(argv[2], ".claude-plugin/marketplace.json")
                        manifest.write_text(json.dumps({"name": name}))
                    return result

                with self.assertRaisesRegex(ValueError, "invalid marketplace name"):
                    self.install([self.plugins[0]], run=bad_marketplace)
                self.assertFalse(any(argv[0] in ("claude", "codex") for argv, _ in self.calls))

    def test_main_requires_input_file_and_absolute_root(self):
        for argv in ([], ["input"], ["input", "relative"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.code["main"](argv)
