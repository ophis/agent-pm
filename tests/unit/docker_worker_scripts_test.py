import io
import json
import os
import runpy
import stat
from pathlib import Path
from types import SimpleNamespace
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

    def test_hook_stub_hands_off_to_the_sessions_stub_and_exits_zero(self):
        text = (ASSETS / "hook-stub").read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        stub = "session/src/agent_pm/session/hosts/docker/stub.py"
        self.assertIn(f'/opt/agent-pm/venv/bin/python -I /opt/agent-pm/{stub} "$@"', text)
        self.assertTrue(os.path.isfile(os.path.join(REPO, stub)))
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
            "SUDO_FORCE_REMOVE=yes apt-get purge -y sudo",
            "rm -rf /etc/sudoers.d",
            "USER agent",
            "NPM_CONFIG_PREFIX=/home/agent/.npm-global",
            "npm install -g @anthropic-ai/claude-code @openai/codex",
        ):
            self.assertIn(needle, text)
        self.assertNotIn("SHASUMS256", text)


class WorkspaceCheck(Case):
    """The walk, argv and output over a fake git; real git: workspace_check_integration_test.py."""

    def setUp(self):
        self.check = script("workspace-check")
        self.root = Path(self.home, "work")
        self.calls = []

    def repo(self, rel):
        (self.root / rel / ".git").mkdir(parents=True)

    def run(self, dirty=(), failing=()):
        def run(argv, **kw):
            rel = os.path.relpath(kw["cwd"], self.root)
            self.calls.append((argv, kw))
            if rel in failing:
                raise OSError("no git")
            out = "?? x\n" if rel in dirty and argv[8] == "status" else ""
            return SimpleNamespace(returncode=0, stdout=out)

        return run

    def test_repos_up_to_three_levels_outside_git_and_cache_dirs(self):
        for rel in (
            ".",
            "publish",
            "src/o/a",
            "a/b/c/d",
            ".agent-pm-cache/x",
            "src/o/a/.git/modules/m",
        ):
            self.repo(rel)
        (self.root / "src" / "link").symlink_to(self.root / "a")
        self.assertEqual(
            [os.path.relpath(p, self.root) for p in self.check["repos"](str(self.root))[0]],
            [".", "publish", "src/o/a"],
        )

    def test_git_runs_guarded_without_optional_locks_and_a_failure_counts_as_dirty(self):
        for rel in ("publish", "src/o/a", "src/o/b"):
            self.repo(rel)
        found = self.check["dirty"](
            str(self.root), run=self.run(dirty={"src/o/a"}, failing={"src/o/b"})
        )
        self.assertEqual(found, ["src/o/a", "src/o/b"])
        guard = [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "safe.directory=*",
        ]
        publish = [argv for argv, kw in self.calls if kw["cwd"] == str(self.root / "publish")]
        self.assertEqual(
            publish,
            [
                [
                    *guard,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    ".",
                    ":(exclude).agent-pm-cache",
                ],
                [*guard, "rev-list", "--all", "--not", "--remotes"],
            ],
        )

    def test_a_dir_it_cannot_list_counts_as_dirty(self):
        self.repo("src/o/a")
        locked = str(self.root / "src" / "o")
        scandir = os.scandir

        def denied(path):
            if path == locked:
                raise PermissionError(13, "Permission denied", path)
            return scandir(path)

        with mock.patch("os.scandir", side_effect=denied):
            found = self.check["dirty"](str(self.root), run=self.run())
        self.assertEqual(found, ["src/o"])

    def test_paths_have_control_characters_escaped(self):
        self.repo("src/o/bad\x1b[31m")
        found = self.check["dirty"](str(self.root), run=self.run(dirty={"src/o/bad\x1b[31m"}))
        self.assertEqual(found, ["src/o/bad\\x1b[31m"])

    def test_main_prints_the_dirty_list_and_wants_one_absolute_path(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.check["main"]([str(self.root)])
        self.assertEqual(json.loads(out.getvalue()), {"dirty": []})
        for argv in ([], ["relative"], ["/a", "/b"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.check["main"](argv)
