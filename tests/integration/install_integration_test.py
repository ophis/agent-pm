import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

from tests.case import REPO, Case

SCRIPT = Path(REPO, "install.sh")
FAKE_TOOLS = Path(REPO, "tests/integration/install_fakes.py")
REAL = (
    "bash",
    "sh",
    "grep",
    "sed",
    "cat",
    "mkdir",
    "mv",
    "rm",
    "cp",
    "chmod",
    "sleep",
    "sort",
    "find",
    "cut",
    "dirname",
    "basename",
    "cksum",
    "true",
    "head",
    "tail",
)
FAKED = ("uname", "id", "sudo", "curl", "security", "open", "launchctl", "shasum", "base64")
APPS = ("brew", "gh", "tmux", "git", "go", "claude", "codex", "docker", "agent-pm-broker")
PASSWORD = "pw-SENTINEL-sudo"
TOKEN = "sk-ant-oat01-SENTINEL-token"
DOCKER_Q = r"Run workers in Docker \(Docker Desktop and the agent-pm broker\)\? \[y/N\]"
SUDO_Q = r"Password:"
WRITE_Q = r"Write it\? \[y/N\]"
TOKEN_Q = r"Paste the setup-token \(hidden\):"
PEM_Q = r"private key \(\.pem\), empty to skip:"
DOCKER_LOGINS = ((TOKEN_Q, TOKEN), (PEM_Q, ""))
DOCKER_STEPS = (
    "Docker Desktop running",
    "broker",
    "broker login: Claude",
    "broker login: GitHub",
    "broker Codex install",
    "broker login: Codex",
)
BROKER_INSTALL = """#!/bin/sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
echo "broker-install $*" >> "$LOG"
target="$AGENT_PM_INSTALL_ROOT/usr/local/libexec/agent-pm"
sudo mkdir -p "$target"
{ cat "$FAKES/apps/agent-pm-broker"; echo "# $(cksum < "$here/internal/codex/versions.go")"; } > "$target/agent-pm-broker"
chmod 755 "$target/agent-pm-broker"
"""
CTMUX = """#!/bin/sh
echo "ctmux $*" >> "$LOG"
mkdir -p "$HOME/.agent-pm/bin"
for n in ctmux agent-pm-crew agent-pm-session agent-pm-delegate agent-pm-orchestrator; do
  printf '#!/bin/sh\\n' > "$HOME/.agent-pm/bin/$n"
  chmod 755 "$HOME/.agent-pm/bin/$n"
done
"""
Result = namedtuple("Result", "code stdout stderr tty")


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class Tty:
    """A pty whose slave is the script's terminal; answers go in as each prompt appears."""

    def __init__(self):
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.text, self.done = "", False
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        while not self.done:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                time.sleep(0.05)
                continue
            with self.lock:
                self.text += chunk.decode(errors="replace")

    def answer(self, answers, proc):
        position = 0
        for pattern, reply in answers:
            deadline = time.monotonic() + 30
            while True:
                with self.lock:
                    found = re.compile(pattern).search(self.text, position)
                if found:
                    position = found.end()
                    os.write(self.master, (reply + "\n").encode())
                    break
                if proc.poll() is not None or time.monotonic() > deadline:
                    return f"no prompt {pattern!r} in terminal output:\n{self.text}"
                time.sleep(0.02)
        return None

    def close(self):
        self.done = True
        os.close(self.slave)
        os.close(self.master)
        self.reader.join(timeout=5)


class InstallTest(Case):
    def setUp(self):
        self.bin, self.fakes, self.root = (Path(self.home, n) for n in ("bin", "fakes", "root"))
        self.bin.mkdir()
        for tool in REAL:
            (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / "python3").symlink_to(sys.executable)
        wrapper = '#!/bin/sh\nexec {} -I {} {} "$@"\n'
        for tool in FAKED:
            executable(self.bin / tool, wrapper.format(sys.executable, FAKE_TOOLS, tool))
        for app in APPS:
            executable(self.fakes / "apps" / app, wrapper.format(sys.executable, FAKE_TOOLS, app))
        (self.fakes / "sudo_password").write_text(PASSWORD)
        (self.fakes / "claude_token").write_text(TOKEN)
        (self.root / "dev").mkdir(parents=True)
        self.pkg_src = self.fakes / "pkg-src"
        executable(self.pkg_src / "broker/install.sh", BROKER_INSTALL)
        self.versions = self.pkg_src / "broker/internal/codex/versions.go"
        self.versions.parent.mkdir(parents=True)
        self.versions.write_text('package codex\n\nconst MinVersion = "0.163.0"\n')
        executable(self.pkg_src / "manager/skills/manage/scripts/ctmux", CTMUX)
        (self.pkg_src / ".claude-plugin").mkdir()
        (self.pkg_src / ".claude-plugin/plugin.json").write_text('{"version": "9.8.7"}\n')
        shutil.copytree(Path(REPO, "orchestrator/launchd"), self.pkg_src / "orchestrator/launchd")
        self.log, self.envlog = Path(self.home, "calls.log"), Path(self.home, "env.log")
        self.env = {
            "HOME": self.home,
            "PATH": str(self.bin),
            "SHELL": "/bin/zsh",
            "LOG": str(self.log),
            "ENVLOG": str(self.envlog),
            "FAKES": str(self.fakes),
            "AGENT_PM_INSTALL_ROOT": str(self.root),
        }

    def run_install(self, *args, answers=(), stdin=None, tty=True, script=SCRIPT):
        self.log.unlink(missing_ok=True)
        terminal = Tty() if tty else None
        link = self.root / "dev/tty"
        link.unlink(missing_ok=True)
        if terminal:
            link.symlink_to(terminal.path)
        out, err = Path(self.home, "stdout"), Path(self.home, "stderr")
        try:
            with out.open("w") as stdout, err.open("w") as stderr:
                proc = subprocess.Popen(
                    ["bash", str(script), *args],
                    env=self.env,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                )
                if stdin is not None:
                    proc.stdin.write(stdin)
                    proc.stdin.close()
                missed = terminal.answer(answers, proc) if terminal else None
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    missed = missed or "install.sh hung (waiting on the terminal?)"
            text = terminal.text if terminal else ""
        finally:
            if terminal:
                terminal.close()
        result = Result(proc.returncode, out.read_text(), err.read_text(), text)
        if missed:
            self.fail(f"{missed}\nstderr:\n{result.stderr}\nterminal:\n{result.tty}")
        return result

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def state(self):
        path = Path(self.agent_pm, "install/state")
        lines = path.read_text().splitlines() if path.exists() else []
        return dict(line.split("=", 1) for line in lines)

    def assert_ok(self, result):
        self.assertEqual(result.code, 0, f"stderr:\n{result.stderr}\nterminal:\n{result.tty}")

    def assert_secret_free(self, *secrets, result):
        kept = {self.fakes / "keychain", self.fakes / "broker/claude_token"}
        kept |= {self.fakes / "sudo_password", self.fakes / "claude_token"}
        texts = {"stdout": result.stdout, "stderr": result.stderr}
        for path in Path(self.home).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(path == k or k in path.parents for k in kept)
            ):
                texts[str(path)] = path.read_text(errors="replace")
        for secret in secrets:
            for name, text in texts.items():
                self.assertNotIn(secret, text, name)

    def fresh(self, *args, docker="n", extra=None):
        if extra is None:
            extra = DOCKER_LOGINS if docker == "y" else ()
        return self.run_install(
            *args,
            answers=[
                (DOCKER_Q, docker),
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                *extra,
                (WRITE_Q, "y"),
            ],
        )

    def test_usage_and_platform(self):
        result = self.run_install("--help", tty=False)
        self.assertEqual(result.code, 0)
        self.assertIn("usage: install.sh [--ref <ref>]", result.stdout)
        cases = (["--bogus"], ["--ref"], ["--ref", "-x"], ["--docker", "--no-docker"])
        for args in cases:
            result = self.run_install(*args, tty=False)
            self.assertEqual(result.code, 2, args)
            self.assertIn("install.sh: usage: install.sh [--ref <ref>]", result.stderr)
        tty = Tty()
        try:
            with open(tty.path) as stdin:
                proc = subprocess.run(
                    ["bash", str(SCRIPT), "--password-stdin"],
                    env=self.env,
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        finally:
            tty.close()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--password-stdin reads a pipe, not a terminal", proc.stderr)
        (self.fakes / "platform").write_text("Linux x86_64\n")
        result = self.run_install(tty=False)
        self.assertEqual(result.code, 1)
        self.assertIn(
            "needs macOS on Apple silicon (Darwin arm64), not Linux x86_64", result.stderr
        )
        (self.fakes / "platform").unlink()
        result = self.run_install(tty=False)
        self.assertEqual(result.code, 1)
        self.assertIn("install.sh: needs a terminal for its questions and logins", result.stderr)
        self.assertEqual(self.calls(), ["uname -sm"])

    def test_password_stdin(self):
        result = self.run_install(
            "--password-stdin",
            "--no-docker",
            stdin=PASSWORD + "\n",
            answers=[(WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertNotRegex(result.tty, SUDO_Q)
        calls = self.calls()
        self.assertIn("sudo -S -p  -v", calls)
        self.assertNotIn("sudo -v", calls)
        self.assert_secret_free(PASSWORD, result=result)

    def test_wrong_password_on_stdin_fails(self):
        result = self.run_install("--password-stdin", "--no-docker", stdin="wrong\n")
        self.assertEqual(result.code, 1)
        self.assertIn("install.sh: sudo rejected the password from stdin", result.stderr)
        self.assertNotIn("brew", "\n".join(self.calls()))

    def test_sudo_prompt_once(self):
        result = self.fresh()
        self.assert_ok(result)
        self.assertEqual(len(re.findall(SUDO_Q, result.tty)), 1)
        calls = self.calls()
        self.assertEqual(calls.count("sudo -v"), 1)
        self.assertIn("sudo -n -v", calls)
        self.assertEqual(calls[-1], "sudo -k")
        keepalive = int((self.fakes / "keepalive.pid").read_text())
        deadline = time.monotonic() + 2
        with self.assertRaises(ProcessLookupError):
            while time.monotonic() < deadline:
                os.kill(keepalive, 0)
                time.sleep(0.05)
        self.assertFalse((self.fakes / "sudo_ticket").exists())

    def test_fresh_install_core(self):
        result = self.fresh()
        self.assert_ok(result)
        calls = self.calls()
        installer = calls.index(
            "curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
        )
        self.assertLess(calls.index("sudo -v"), installer)
        brew = [c for c in calls if c.startswith("brew install")]
        self.assertEqual(
            brew,
            [
                "brew install gh",
                "brew install tmux",
                "brew install git",
                "brew install --cask iterm2",
                "brew install --cask claude-code",
                "brew install --cask codex",
            ],
        )
        logins = [c for c in calls if " login" in c or c.endswith(" status")]
        self.assertEqual(
            logins,
            [
                "gh auth status",
                "gh auth login --hostname github.com --git-protocol https --web",
                "claude auth status",
                "claude auth login",
                "codex login status",
                "codex login",
            ],
        )
        self.assertIn("python3 is 3.11+", result.stderr)
        profile = Path(self.home, ".zprofile").read_text()
        self.assertIn(f'eval "$({self.root}/opt/homebrew/bin/brew shellenv)"\n', profile)
        self.assertIn("Log in to gh in your browser.", result.tty)
        self.assertEqual(self.state()["docker"], "no")

    def test_docker_choice_kept_and_flag_overrides(self):
        self.assert_ok(self.fresh(docker="y"))
        self.assertEqual(self.state()["docker"], "yes")
        self.assertIn("brew install --cask docker-desktop", self.calls())
        result = self.run_install(answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertNotRegex(result.tty, DOCKER_Q)
        self.assertIn("Docker: yes", result.stderr)
        result = self.run_install("--no-docker", answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertEqual(self.state()["docker"], "no")
        Path(self.agent_pm, "config.local.toml").write_text('[session.hosts]\ndefault = "docker"\n')
        result = self.run_install(answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertEqual(self.state()["docker"], "yes")

    def plugin_calls(self):
        return [c for c in self.calls() if " plugin " in c and " list " not in c]

    def test_ref_installs_branch(self):
        result = self.fresh("--ref", "orchestrator-revamped")
        self.assert_ok(result)
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin marketplace add ophis/agent-pm#orchestrator-revamped",
                "claude plugin marketplace add ophis/claude-autopilot",
                "claude plugin install agent-pm@agent-pm",
                "claude plugin install autopilot@claude-autopilot",
                "codex plugin marketplace add ophis/agent-pm --ref orchestrator-revamped",
                "codex plugin marketplace add ophis/claude-autopilot",
                "codex plugin add agent-pm@agent-pm",
                "codex plugin add autopilot@claude-autopilot",
            ],
        )
        pkg = Path(self.fakes, "claude/cache/0")
        self.assertEqual((pkg / "REF").read_text(), "orchestrator-revamped")
        self.assertIn(f"    {pkg}", result.stderr)
        self.assertIn("ctmux --install", self.calls())
        for name in ("ctmux", "agent-pm-orchestrator"):
            self.assertTrue(os.access(Path(self.agent_pm, "bin", name), os.X_OK))
        profile = Path(self.home, ".zprofile").read_text()
        self.assertTrue(profile.endswith('export PATH="$HOME/.agent-pm/bin:$PATH"\n'), profile)

    def test_ref_change_readds_marketplaces(self):
        self.assert_ok(self.fresh())
        self.assertIn("claude plugin marketplace add ophis/agent-pm", self.calls())
        result = self.run_install("--ref", "next", answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin uninstall agent-pm@agent-pm",
                "claude plugin marketplace remove agent-pm",
                "claude plugin marketplace add ophis/agent-pm#next",
                "claude plugin install agent-pm@agent-pm",
                "codex plugin remove agent-pm@agent-pm",
                "codex plugin marketplace remove agent-pm",
                "codex plugin marketplace add ophis/agent-pm --ref next",
                "codex plugin add agent-pm@agent-pm",
            ],
        )
        self.assertNotIn("ctmux --install", self.calls())
        result = self.run_install("--ref", "next", answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertEqual(self.plugin_calls(), [])

    def test_disabled_plugin_is_enabled(self):
        self.assert_ok(self.fresh())
        plugins = Path(self.fakes, "claude/plugins.json")
        plugins.write_text(plugins.read_text().replace("true", "false"))
        self.assert_ok(self.run_install(answers=[(SUDO_Q, PASSWORD)]))
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin enable agent-pm@agent-pm",
                "claude plugin enable autopilot@claude-autopilot",
            ],
        )

    def broker_calls(self):
        calls = self.calls()
        own = {"sudo -n true", "sudo -v", "sudo -n -v", "sudo -k", "sudo -S -p  -v"}
        wanted = ("broker-", "sudo", "agent-pm-broker")
        return [c for c in calls if c.startswith(wanted) and c not in own and "status" not in c]

    def broker_path(self):
        return Path(self.root, "usr/local/libexec/agent-pm/agent-pm-broker")

    def test_fresh_install(self):
        result = self.fresh(docker="y")
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("brew install --cask docker-desktop", calls)
        self.assertNotIn("brew install go", calls)
        broker = str(self.broker_path())
        self.assertEqual(
            self.broker_calls(),
            [
                "broker-install ",
                f"sudo mkdir -p {self.root}/usr/local/libexec/agent-pm",
                "agent-pm-broker login claude",
                f"sudo {broker} install-codex 0.163.0",
                "agent-pm-broker install-codex 0.163.0",
                "agent-pm-broker login codex",
            ],
        )
        self.assertLess(calls.index("open -a Docker"), calls.index("broker-install "))
        self.assertLess(
            calls.index("claude setup-token"), calls.index("agent-pm-broker login claude")
        )
        self.assertEqual((self.fakes / "broker/claude_token").read_text(), TOKEN)
        self.assertIn("skip: no agent-pm-bot App key", result.stderr)
        state = self.state()
        self.assertEqual(state["github_app_key"], "skipped")
        self.assertEqual(len(state["login_claude"]), 64)
        self.assertNotIn("login_github", state)
        self.assert_secret_free(PASSWORD, TOKEN, result=result)

    def test_no_docker(self):
        result = self.run_install(
            "--no-docker", answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (WRITE_Q, "y")]
        )
        self.assert_ok(result)
        for name in DOCKER_STEPS:
            self.assertIn(f"==> {name}\n    skip: Docker not chosen", result.stderr)
        calls = "\n".join(self.calls())
        for absent in (
            "docker-desktop",
            "broker",
            "open -a Docker",
            "setup-token",
            "brew install go",
        ):
            self.assertNotIn(absent, calls)

    def test_branch_broker_is_built(self):
        self.assert_ok(self.fresh("--ref", "next", docker="y"))
        self.assertIn("brew install go", self.calls())
        self.assertIn("broker-install --build", self.calls())

    def test_docker_not_running_skip_fails(self):
        (self.fakes / "docker_wont_start").touch()
        skip = (r"first-run setup.*\(or type skip\)", "skip")
        result = self.run_install(
            answers=[(DOCKER_Q, "y"), (SUDO_Q, PASSWORD), (WRITE_Q, "y"), skip]
        )
        self.assertEqual(result.code, 1)
        self.assertIn("install.sh: Docker Desktop not running", result.stderr)
        self.assertIn(
            "install.sh: Docker Desktop running failed; rerun the same command", result.stderr
        )
        self.assertNotIn("broker-install ", self.calls())
        (self.fakes / "docker_wont_start").unlink()
        result = self.run_install(answers=[(SUDO_Q, PASSWORD), *DOCKER_LOGINS, (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("broker-install ", self.calls())

    def test_github_app_key_optional(self):
        self.assert_ok(self.fresh(docker="y"))
        result = self.run_install(answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertNotRegex(result.tty, PEM_Q)
        self.assertIn("skip: no agent-pm-bot App key", result.stderr)
        (self.fakes / "keychain").mkdir(exist_ok=True)
        (self.fakes / "keychain/agent-pm-bot").write_text("stored by hand")
        result = self.run_install(answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertIn("agent-pm-broker login github agent-pm-bot", self.calls())
        self.assertEqual(len(self.state()["login_github"]), 64)

    def test_github_app_key_stored_by_pipe(self):
        pem = Path(self.home, "app.pem")
        secret = "-----BEGIN RSA PRIVATE KEY-----SENTINELKEY-----END RSA PRIVATE KEY-----"
        pem.write_text(secret)
        encoded = base64.b64encode(secret.encode()).decode()
        answers = [(TOKEN_Q, TOKEN), (PEM_Q, str(pem)), (r"App ID \(digits\):", "12 -w x")]
        answers.append((r"App ID \(digits\):", "5352992"))
        result = self.fresh(docker="y", extra=answers)
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/agent-pm-bot").read_text(), encoded)
        self.assertEqual((self.fakes / "keychain/agent-pm-bot.account").read_text(), "5352992")
        self.assertIn("security -i", self.calls())
        self.assertIn("agent-pm-broker login github agent-pm-bot", self.calls())
        pem.unlink()
        self.assert_secret_free(encoded, "SENTINELKEY", TOKEN, result=result)

    def test_broker_change_logs_in_again(self):
        self.assert_ok(self.fresh(docker="y"))
        result = self.run_install(answers=[(SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertEqual(self.broker_calls(), [])
        self.assertIn("==> broker\n    skip: installed from this plugin", result.stderr)
        self.assertIn("==> Docker Desktop running\n    skip: broker current", result.stderr)
        versions = Path(self.fakes, "claude/cache/0/broker/internal/codex/versions.go")
        versions.write_text('package codex\n\nconst MinVersion = "0.170.0"\n')
        result = self.run_install(answers=[(SUDO_Q, PASSWORD), (TOKEN_Q, TOKEN)])
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("broker-install ", calls)
        self.assertIn("agent-pm-broker login claude", calls)
        self.assertIn(f"sudo {self.broker_path()} update-codex 0.170.0", calls)
        self.assertNotIn("agent-pm-broker login codex", calls)

    def test_codex_install_or_update(self):
        self.assert_ok(self.fresh(docker="y"))
        codex = self.fakes / "broker/codex.json"
        broker = self.broker_path()
        cases = (
            (
                {"state": "installation_invalid", "repair": "sudo B update-codex <version>"},
                "update",
            ),
            ({"state": "ready", "version": "0.163.0", "repair": ""}, None),
            ({"state": "expired", "version": "0.163.1", "repair": "B login codex"}, None),
            ({"state": "busy", "version": "0.163.0"}, "busy"),
        )
        for status, action in cases:
            codex.write_text(json.dumps(status))
            result = self.run_install(answers=[(SUDO_Q, PASSWORD)])
            calls = self.calls()
            if action == "busy":
                self.assertEqual(result.code, 1)
                self.assertIn("install.sh: broker busy; rerun", result.stderr)
                continue
            self.assert_ok(result)
            update = f"sudo {broker} update-codex 0.163.0"
            self.assertEqual(update in calls, action == "update", status)
            self.assertNotIn(f"sudo {broker} install-codex 0.163.0", calls)
            login = "agent-pm-broker login codex" in calls
            self.assertEqual(login, status["state"] != "ready", status)
