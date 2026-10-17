import json
import os
import runpy
import stat
from pathlib import Path
from unittest import mock

from tests.case import REPO, Case

ASSETS = Path(REPO, "broker/internal/launcher/assets")


def script(name):
    return runpy.run_path(str(ASSETS / name), run_name="docker_worker_script")


class Bootstrap(Case):
    def setUp(self):
        self.bootstrap = script("bootstrap.py")
        self.templates = Path(self.home, "templates")
        self.templates.mkdir()
        (self.templates / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
        (self.templates / "settings.json").write_text("{}")
        self.target = Path(self.home, "claude")
        self.target.mkdir()

    def test_bootstrap_claude_copies_templates_and_trusts_cwd(self):
        self.bootstrap["bootstrap_claude"](self.templates, self.target, "/work/project")
        config = json.loads((self.target / ".claude.json").read_text())
        self.assertEqual(config["hasCompletedOnboarding"], True)
        self.assertEqual(config["projects"]["/work/project"], {"hasTrustDialogAccepted": True})
        self.assertEqual((self.target / "settings.json").read_text(), "{}")

    def test_bootstrap_claude_keeps_existing_files_and_keys(self):
        (self.target / "settings.json").write_text('{"mine": 1}')
        existing = {"theme": "light", "projects": {"/other": {"x": 1}}}
        (self.target / ".claude.json").write_text(json.dumps(existing))
        self.bootstrap["bootstrap_claude"](self.templates, self.target, "/work/project")
        config = json.loads((self.target / ".claude.json").read_text())
        self.assertEqual(config["theme"], "light")
        self.assertNotIn("hasCompletedOnboarding", config)
        self.assertEqual(config["projects"]["/other"], {"x": 1})
        self.assertTrue(config["projects"]["/work/project"]["hasTrustDialogAccepted"])
        self.assertEqual((self.target / "settings.json").read_text(), '{"mine": 1}')

    def test_bootstrap_claude_refuses_symlinked_config(self):
        elsewhere = Path(self.home, "elsewhere.json")
        elsewhere.write_text("{}")
        (self.target / ".claude.json").symlink_to(elsewhere)
        with self.assertRaises(SystemExit):
            self.bootstrap["bootstrap_claude"](self.templates, self.target, "/work/project")
        self.assertEqual(elsewhere.read_text(), "{}")

    def test_template_has_onboarding_and_theme(self):
        template = json.loads((ASSETS / "claude.json").read_text())
        self.assertTrue(template["hasCompletedOnboarding"] is True)
        self.assertTrue(template["theme"])


class InstallHook(Case):
    def setUp(self):
        self.install = script("bootstrap.py")["install_hook"]
        self.stub = Path(self.home, "stub")
        self.stub.write_text("#!/bin/sh\nexit 0\n")
        self.uid = os.getuid()

    def test_install_hook_creates_parents_and_writes_stub(self):
        destination = Path(self.home, "usr/local/bin/agent-pm-session")
        self.install(self.stub, destination, owner=self.uid, top=Path(self.home))
        self.assertEqual(destination.read_text(), self.stub.read_text())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o755)

    def test_install_hook_rejects_unsafe_parent(self):
        parent = Path(self.home, "open")
        parent.mkdir()
        parent.chmod(0o777)
        with self.assertRaises(SystemExit):
            self.install(self.stub, parent / "hook", owner=self.uid, top=Path(self.home))
        self.assertFalse((parent / "hook").exists())

    def test_install_hook_rejects_parent_of_other_owner(self):
        with self.assertRaises(SystemExit):
            self.install(
                self.stub, Path(self.home, "x/hook"), owner=self.uid + 1, top=Path(self.home)
            )

    def test_install_hook_checks_every_parent_from_root_by_default(self):
        with self.assertRaises(SystemExit):
            self.install(self.stub, Path(self.home, "hook"), owner=self.uid)

    def test_install_hook_rejects_existing_path(self):
        destination = Path(self.home, "hook")
        destination.write_text("old")
        with self.assertRaises(SystemExit):
            self.install(self.stub, destination, owner=self.uid, top=Path(self.home))
        self.assertEqual(destination.read_text(), "old")

    def test_hook_stub_is_a_placeholder_that_exits_zero(self):
        text = (ASSETS / "hook-stub").read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn(">&2", text)
        self.assertTrue(text.rstrip().endswith("exit 0"))


class WithSecrets(Case):
    def setUp(self):
        self.main = script("with-secrets")["main"]
        self.secret = Path(self.home, "claude")

    def test_with_secrets_execs_with_token_in_child_env_only(self):
        self.secret.write_text("tok-123")
        with mock.patch("os.execvpe") as execvpe:
            self.main(["claude", "--help"], secret=self.secret)
        (path, argv, env), _ = execvpe.call_args
        self.assertEqual((path, argv), ("claude", ["claude", "--help"]))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-123")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", os.environ)

    def test_with_secrets_missing_or_empty_token(self):
        for content in (None, "", "\n"):
            if content is not None:
                self.secret.write_text(content)
            with mock.patch("os.execvpe") as execvpe, self.assertRaises(SystemExit) as raised:
                self.main(["claude"], secret=self.secret)
            execvpe.assert_not_called()
            self.assertIn("no Claude token", str(raised.exception.code))

    def test_with_secrets_requires_command(self):
        self.secret.write_text("tok-123")
        with mock.patch("os.execvpe") as execvpe, self.assertRaises(SystemExit) as raised:
            self.main([], secret=self.secret)
        execvpe.assert_not_called()
        self.assertNotIn("tok-123", str(raised.exception.code))


class DefaultDockerfile(Case):
    def test_default_dockerfile_pins(self):
        text = Path(REPO, "broker/worker/Dockerfile").read_text()
        self.assertIn(
            "FROM mcr.microsoft.com/devcontainers/base:bookworm@sha256:"
            "9a5942e64b35c5497c591b9bf177f4a3002d43440bf0a5e1a14fc34667b1a5c1",
            text,
        )
        for needle in (
            "NODE_VERSION=24.22.0",
            "fef00bb4d36e1c50104a9775a10359beee98f162b4104dd12472c75065c6a143",
            "cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008",
            "uv==0.13.1",
            "tmux git gh python3",
            "useradd --create-home --shell /bin/bash agent",
            "apt-get purge -y sudo",
            "rm -rf /etc/sudoers.d",
            "USER agent",
            "NPM_CONFIG_PREFIX=/home/agent/.npm-global",
            "npm install -g @anthropic-ai/claude-code @openai/codex",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("SHASUMS256", text)
