import io
import json
import os
import runpy
import shutil
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


class CodexBootstrap(Case):
    def setUp(self):
        self.code = script("bootstrap.py")
        self.target = Path(self.home, "codex")
        self.target.mkdir()
        self.secrets = Path(self.home, "secrets")
        self.secrets.mkdir()

    def test_codex_home_has_file_auth_trusted_project_and_only_a_secret_link(self):
        import tomllib

        self.code["bootstrap_codex"](self.target, '/work/"project', self.secrets)
        config = tomllib.loads((self.target / "config.toml").read_text())
        self.assertEqual(config["cli_auth_credentials_store"], "file")
        self.assertEqual(config["forced_login_method"], "chatgpt")
        self.assertEqual(config["projects"]['/work/"project']["trust_level"], "trusted")
        self.assertTrue((self.target / "auth.json").is_symlink())
        self.assertEqual((self.target / "auth.json").readlink(), self.secrets / "codex/auth.json")

    def test_access_document_is_owner_read_only_and_atomically_replaced(self):
        inject = self.code["inject_codex"]
        inject(io.BytesIO(b'{"synthetic":"one"}'), self.secrets)
        path = self.secrets / "codex/auth.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.assertEqual(path.stat().st_uid, os.getuid())
        inject(io.BytesIO(b'{"synthetic":"two"}'), self.secrets)
        self.assertEqual(path.read_bytes(), b'{"synthetic":"two"}')
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["auth.json"])

    def test_projection_writes_one_record_and_rejects_escape_or_links(self):
        stage = self.code["stage_runtime"]
        root = Path(self.home, "run")
        root.mkdir()
        destination = root / "codex-store" / ("a" * 64 + ".json")
        stage(io.StringIO(json.dumps({str(destination): "{}"})), root)
        self.assertEqual(destination.read_text(), "{}")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        for files in (
            {},
            {str(root / "secrets/x"): "{}"},
            {str(destination): 1},
            {str(destination): "a" * ((1 << 20) + 1)},
        ):
            with self.subTest(paths=list(files)), self.assertRaises(SystemExit):
                stage(io.StringIO(json.dumps(files)), root)
        linked = root / "linked-store"
        linked.symlink_to(destination.parent, target_is_directory=True)
        with self.assertRaises(SystemExit):
            stage(io.StringIO(json.dumps({str(linked / ("b" * 64 + ".json")): "{}"})), root)


class CodexCIGuard(Case):
    def test_ci_probe_refuses_local_execution_and_untrusted_image_without_docker(self):
        main = runpy.run_path(str(Path(REPO, ".github/worker_codex.py")))["main"]
        for ci, image in (("false", "agent-pm-worker:" + "a" * 64), ("true", "untrusted")):
            with (
                self.subTest(ci=ci),
                mock.patch.dict(os.environ, {"CI": ci, "GITHUB_ACTIONS": ci}),
                mock.patch("subprocess.run") as run,
                self.assertRaises(SystemExit),
            ):
                main(image)
            run.assert_not_called()


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

    def test_codex_uses_its_file_auth_without_reading_claude(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("os.execvpe") as execvpe,
        ):
            self.main(["--client", "codex", "codex", "--version"], secret=self.secret)
        self.assertEqual(execvpe.call_args.args[:2], ("codex", ["codex", "--version"]))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", execvpe.call_args.args[2])

    def test_claude_inherited_codex_home_still_requires_its_own_token(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/unrelated"}):
            with mock.patch("os.execvpe") as execute, self.assertRaises(SystemExit):
                self.main(["--client", "claude", "claude"], secret=self.secret)
            execute.assert_not_called()
            self.secret.write_text("claude-synthetic")
            with mock.patch("os.execvpe") as execute:
                self.main(["--client", "claude", "claude"], secret=self.secret)
            self.assertEqual(execute.call_args.args[:2], ("claude", ["claude"]))
            self.assertEqual(
                execute.call_args.args[2]["CLAUDE_CODE_OAUTH_TOKEN"], "claude-synthetic"
            )

    def test_invalid_client_or_missing_command_is_rejected(self):
        for argv in (["--client"], ["--client", "codex"], ["--client", "other", "cmd"]):
            with self.subTest(argv=argv), mock.patch("os.execvpe") as execute:
                with self.assertRaises(SystemExit):
                    self.main(argv, secret=self.secret)
                execute.assert_not_called()


class GitHubTokens(Case):
    def setUp(self):
        self.tokens = Path(self.home, "github")
        self.tokens.mkdir()
        for name, token in (
            ("Ophis", "ghs_ophis\n"),
            ("other", "ghs_other"),
            (".ophis.new", "ghs_dot"),
        ):
            (self.tokens / name).write_text(token)


class GitCredential(GitHubTokens):
    def get(self, text, argv=("get",)):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            script("git-credential-agent-pm")["main"](
                list(argv), stdin=io.StringIO(text), tokens=self.tokens
            )
        return out.getvalue()

    def test_get_answers_the_owners_token_for_github_https(self):
        for path in ("ophis/agent-pm.git", "OPHIS/agent-pm", "ophis"):
            with self.subTest(path=path):
                found = self.get(f"protocol=https\nhost=github.com\npath={path}\n\n")
                self.assertEqual(found, "username=x-access-token\npassword=ghs_ophis\n")

    def test_anything_else_prints_nothing(self):
        for text in (
            "protocol=http\nhost=github.com\npath=ophis/x\n",
            "protocol=https\nhost=gitlab.com\npath=ophis/x\n",
            "protocol=https\nhost=github.com\n",
            "protocol=https\nhost=github.com\npath=nobody/x\n",
            "protocol=https\nhost=github.com\npath=../github/Ophis\n",
            "protocol=https\nhost=github.com\npath=.ophis.new/x\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.get(text), "")
        good = "protocol=https\nhost=github.com\npath=ophis/x\n"
        for argv in (("store",), ("erase",), ()):
            self.assertEqual(self.get(good, argv), "")


class GhWrapper(GitHubTokens):
    def setUp(self):
        super().setUp()
        lib, wrapper, self.real = (Path(self.home, d) for d in ("lib", "wrapper", "real"))
        for folder in (lib, wrapper, self.real):
            folder.mkdir()
        for name in ("gh", "git-credential-agent-pm"):  # as in the image: lib/, linked from bin/
            shutil.copy(ASSETS / name, lib)
        (wrapper / "gh").symlink_to(lib / "gh")
        self.wrapper = str(lib / "gh")
        (self.real / "gh").write_text("#!/bin/sh\n")
        (self.real / "gh").chmod(0o755)
        self.path = f"{wrapper}:{self.real}"
        self.origin = ""

    def gh(self, *argv):
        """The GH_TOKEN the real gh gets, None for none."""
        origin = SimpleNamespace(returncode=0, stdout=self.origin + "\n")
        with (
            mock.patch.dict(os.environ, {"PATH": self.path}),
            mock.patch("os.execve") as execve,
            mock.patch("subprocess.run", return_value=origin),
        ):
            runpy.run_path(self.wrapper)["main"](list(argv), tokens=self.tokens)
        (path, args, env), _ = execve.call_args
        self.assertEqual((path, args), (str(self.real / "gh"), ["gh", *argv]))
        self.assertNotIn("GH_TOKEN", os.environ)
        return env.get("GH_TOKEN")

    def test_the_repo_it_acts_on_picks_the_owners_token(self):
        self.origin = "https://github.com/ophis/agent-pm.git"
        for argv in (
            ("repo", "clone", "other/x"),
            ("repo", "clone", "https://github.com/Other/x"),
            ("pr", "list", "-R", "other/x"),
            ("pr", "list", "--repo", "github.com/other/x"),
            ("pr", "list", "--repo=other/x"),
            ("api", "repos/other/x/pulls"),
            ("api", "-X", "GET", "/repos/other/x"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.gh(*argv), "ghs_other")

    def test_otherwise_the_origin_then_the_only_token(self):
        for origin in ("https://github.com/other/x.git", "git@github.com:other/x.git"):
            self.origin = origin
            self.assertEqual(self.gh("pr", "view"), "ghs_other")
        self.origin = "https://gitlab.com/other/x.git"
        self.assertIsNone(self.gh("pr", "view"))
        (self.tokens / "other").unlink()
        self.assertEqual(self.gh("pr", "view"), "ghs_ophis")
        self.origin = ""
        self.assertEqual(self.gh("pr", "view"), "ghs_ophis")

    def test_another_host_or_no_token_adds_none(self):
        self.origin = "https://github.com/ophis/x"
        for argv in (
            ("pr", "list", "-R", "gitlab.com/ophis/x"),
            ("repo", "clone", "https://gitlab.com/ophis/x"),
            ("pr", "list", "-R", "nobody/x"),
            ("pr", "list", "-R", "-bad/x"),
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(self.gh(*argv))

    def test_no_real_gh_fails(self):
        self.path = str(Path(self.home, "wrapper"))
        with (
            mock.patch.dict(os.environ, {"PATH": self.path}),
            mock.patch("os.execve") as execve,
            self.assertRaises(SystemExit),
        ):
            runpy.run_path(self.wrapper)["main"](["pr", "view"], tokens=self.tokens)
        execve.assert_not_called()


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


CACHE = ".agent-pm-cache"


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
            [os.path.relpath(p, self.root) for p in self.check["repos"](str(self.root), CACHE)[0]],
            [".", "publish", "src/o/a"],
        )

    def test_git_runs_guarded_without_optional_locks_and_a_failure_counts_as_dirty(self):
        for rel in ("publish", "src/o/a", "src/o/b"):
            self.repo(rel)
        found = self.check["dirty"](
            str(self.root), CACHE, run=self.run(dirty={"src/o/a"}, failing={"src/o/b"})
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
            found = self.check["dirty"](str(self.root), CACHE, run=self.run())
        self.assertEqual(found, ["src/o"])

    def test_paths_have_control_characters_escaped(self):
        self.repo("src/o/bad\x1b[31m")
        found = self.check["dirty"](
            str(self.root), CACHE, run=self.run(dirty={"src/o/bad\x1b[31m"})
        )
        self.assertEqual(found, ["src/o/bad\\x1b[31m"])

    def test_main_prints_the_dirty_list_and_wants_one_absolute_path(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.check["main"]([str(self.root), CACHE])
        self.assertEqual(json.loads(out.getvalue()), {"dirty": []})
        for argv in (
            [],
            [str(self.root)],
            ["relative", CACHE],
            ["/a", "x/y"],
            ["/a", ".."],
            ["/a", CACHE, "x"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.check["main"](argv)
