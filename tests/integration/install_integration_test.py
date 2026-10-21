import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from pathlib import Path

from tests.case import REPO, Case

SCRIPT = Path(REPO, "install.sh")
CLEANUP = Path(REPO, "scripts/install-cleanup.sh")
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
    "cmp",
    "awk",
    "tr",
    "mktemp",
    "mkfifo",
)
FAKED = ("uname", "id", "sudo", "curl", "security", "open", "launchctl", "shasum", "base64")
FAKED += ("xcode-select", "ps")
APPS = ("brew", "gh", "tmux", "git", "go", "claude", "codex", "docker", "agent-pm-broker")
APPS += ("npm", "node", "opencode")
PASSWORD = "pw-SENTINEL-sudo"
TOKEN = "sk-ant-oat01-SENTINEL-token"
KEY = "sk-SENTINEL-opencode-key"
CLIENTS_Q = r"Which clients, comma-separated \(claude, codex, opencode\)\? \[[a-z,]+\]"
PROVIDER_Q = r"OpenCode provider name:"
URL_Q = r"Its base URL \(ending /v1\):"
MODEL_Q = r"Its model:"
CONTEXT_Q = r"Its context window in tokens \[65536\]:"
OPENCODE_LOGIN_Q = r"Provider:"
PROVIDER_ANSWERS = (
    (PROVIDER_Q, "lan"),
    (URL_Q, "http://10.0.0.5:8080/v1"),
    (MODEL_Q, "m1"),
    (CONTEXT_Q, "204800"),
)
DOCKER_Q = r"Run workers in Docker \(Docker Desktop and the agent-pm broker\)\? \[y/N\]"
SUDO_Q = r"Password:"
WRITE_Q = r"Write it\? \[y/N\]"
TOKEN_Q = r"Paste the setup-token \(hidden\):"
PEM_Q = r"private key \(\.pem\), empty to skip:"
DOCKER_LOGINS = ((TOKEN_Q, TOKEN), (PEM_Q, ""))
REMOVE_Q = r"Remove all of this\? \[y/N\]"
SHARED = (
    "say",
    "step",
    "skip",
    "die",
    "ask",
    "ask_yn",
    "under_home",
    "ledger_valid",
    "sudo_once",
    "sudo_end",
)
RELOAD = (
    "install.sh: in this terminal, run source ~/.zprofile first (or open a new one): its PATH"
    " lacks what install.sh added to ~/.zprofile"
)
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
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
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
        (self.fakes / "tty_name").write_text("ttys001\n")
        (self.root / "dev").mkdir(parents=True)
        self.pkg_src = self.fakes / "pkg-src"
        executable(self.pkg_src / "broker/install.sh", BROKER_INSTALL)
        self.versions = self.pkg_src / "broker/internal/codex/versions.go"
        self.versions.parent.mkdir(parents=True)
        self.versions.write_text('package codex\n\nconst MinVersion = "0.163.0"\n')
        launcher = self.pkg_src / "broker/internal/launcher/versions.go"
        launcher.parent.mkdir(parents=True)
        launcher.write_text('package launcher\n\nconst opencodeMinVersion = "1.19.3"\n')
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
            "TMPDIR": str(self.tmp),
            "AGENT_PM_INSTALL_ROOT": str(self.root),
        }

    def run_install(self, *args, answers=(), stdin=None, tty=True, script=SCRIPT, ctty=None):
        """`ctty`: what /dev/tty opens, else the terminal, which is also /dev/ttys001."""
        self.log.unlink(missing_ok=True)
        terminal = Tty() if tty else None
        links = (self.root / "dev/tty", self.root / "dev/ttys001")
        for link in links:
            link.unlink(missing_ok=True)
        if terminal:
            links[0].symlink_to(ctty or terminal.path)
            links[1].symlink_to(terminal.path)
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

    def run_calls(self):
        """The calls, sorted, but the keep-alive's and the terminal lookup's (its pid varies):
        pipelines and the keep-alive interleave.
        """
        return sorted(
            c for c in self.calls() if c != "sudo -n -v" and not c.startswith("ps -o tty= -p ")
        )

    def state(self):
        path = Path(self.agent_pm, "install/state")
        lines = path.read_text().splitlines() if path.exists() else []
        return dict(line.split("=", 1) for line in lines)

    def assert_ok(self, result):
        self.assertEqual(result.code, 0, f"stderr:\n{result.stderr}\nterminal:\n{result.tty}")

    def assert_secret_free(self, *secrets, result):
        kept = {self.fakes / "keychain", self.fakes / "broker/claude_token"}
        kept |= {self.fakes / "sudo_password", self.fakes / "claude_token", self.auth_json()}
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

    def fresh(self, *args, clients="", docker="n", opencode=(), logins=None, ctty=None):
        """A first run answering every question: brew profile line, clients, Docker, the
        OpenCode provider, the config, the Docker logins and the PATH line.
        """
        chosen = args[args.index("--clients") + 1] if "--clients" in args else clients
        if logins is None and (docker == "y" or "--docker" in args):
            logins = [(TOKEN_Q, TOKEN)] if "claude" in (chosen or "claude") else []
            logins.append((PEM_Q, ""))
        answers = [(SUDO_Q, PASSWORD), (WRITE_Q, "y")]
        answers += [(CLIENTS_Q, clients)] if "--clients" not in args else []
        answers += [(DOCKER_Q, docker)] if not {"--docker", "--no-docker"} & set(args) else []
        answers += [*opencode, (WRITE_Q, "y"), *(logins or ()), (WRITE_Q, "y")]
        return self.run_install(*args, answers=answers, ctty=ctty)

    def again(self, *args, answers=()):
        return self.run_install(*args, answers=[(SUDO_Q, PASSWORD), *answers])

    def config(self):
        return Path(self.agent_pm, "config.local.toml")

    def plugin_calls(self):
        return [c for c in self.calls() if " plugin " in c and " list " not in c]

    def broker_calls(self):
        own = {"sudo -n true", "sudo -n -v", "sudo -k", "sudo -S -p  -v", "sudo -A -E -- true"}
        wanted = ("broker-", "sudo", "agent-pm-broker")
        return [
            c for c in self.calls() if c.startswith(wanted) and c not in own and "status" not in c
        ]

    def broker_path(self):
        return Path(self.root, "usr/local/libexec/agent-pm/agent-pm-broker")

    def auth_json(self):
        return Path(self.home, ".local/share/opencode/auth.json")

    def test_usage_and_platform(self):
        result = self.run_install("--help", tty=False)
        self.assertEqual(result.code, 0)
        self.assertIn("usage: install.sh [--ref <ref>]", result.stdout)
        cases = (
            ["--bogus"],
            ["--ref"],
            ["--ref", "-x"],
            ["--docker", "--no-docker"],
            ["--clients", "opencode"],
            ["--clients", "claude,,codex"],
            ["--clients", "claude,vim"],
            ["--opencode-provider", "ollama"],
            ["--opencode-provider", "lan.1"],
            ["--opencode-url", 'http://h"x/v1'],
            ["--opencode-url", "http://user:key@host/v1"],
            ["--opencode-url", "http://host/api"],
            ["--opencode-model", "a b"],
            ["--opencode-context", "0"],
            ["--opencode-context", "64k"],
        )
        for args in cases:
            result = self.run_install(*args, tty=False)
            self.assertEqual(result.code, 2, args)
            self.assertIn("install.sh: usage: install.sh [--ref <ref>]", result.stderr)
        tty = Tty()
        try:
            for flag in ("--password-stdin", "--opencode-key-stdin"):
                with open(tty.path) as stdin:
                    proc = subprocess.run(
                        ["bash", str(SCRIPT), flag],
                        env=self.env,
                        stdin=stdin,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                self.assertEqual(proc.returncode, 2, flag)
                self.assertIn("read a pipe, not a terminal", proc.stderr)
        finally:
            tty.close()
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
            "--clients",
            "claude,codex",
            stdin=PASSWORD + "\n",
            answers=[(WRITE_Q, "y"), (WRITE_Q, "y"), (WRITE_Q, "y")],
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

    def test_sudo_wrong_password_asked_again(self):
        result = self.run_install(
            "--no-docker",
            "--clients",
            "claude",
            answers=[(SUDO_Q, "wrong"), (SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 3],
        )
        self.assert_ok(result)
        self.assertEqual(result.tty.count("Sorry, try again."), 1)
        self.assertNotIn(PASSWORD, result.tty)

    def test_cask_sudo_gets_the_password_from_memory(self):
        """Homebrew's sudo for docker-desktop misses the ticket; the askpass FIFO answers it."""
        result = self.run_install(
            "--password-stdin",
            "--docker",
            "--clients",
            "claude",
            stdin=PASSWORD + "\n",
            answers=[(WRITE_Q, "y"), (WRITE_Q, "y"), *DOCKER_LOGINS, (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertIn("brew install --cask docker-desktop", self.calls())
        self.assertTrue((self.fakes / "askpass_used").exists())
        self.assertNotRegex(result.tty, SUDO_Q)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assert_secret_free(PASSWORD, TOKEN, result=result)
        self.assertNotIn(PASSWORD, self.envlog.read_text())

    def test_sudo_prompt_once(self):
        result = self.fresh()
        self.assert_ok(result)
        self.assertEqual(len(re.findall(SUDO_Q, result.tty)), 1)
        calls = self.calls()
        self.assertEqual(calls.count("sudo -S -p  -v"), 1)
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
        self.assertLess(calls.index("sudo -S -p  -v"), installer)
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
        tty = result.tty
        self.assertLess(tty.index("Write it?"), tty.index("Which clients"))
        self.assertLess(tty.index("Which clients"), tty.index("config.local.toml:"))
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
        self.assertTrue(result.stderr.endswith(RELOAD + "\n"), result.stderr)
        self.assertIn("| (_| | (_| |  __/ | | | ||_____| |_) | | | | | |\n", result.stderr)
        self.assertIn(
            "agent-pm is installed (ref main, clients claude,codex, Docker no).\n"
            "Welcome aboard, and congrats: your crew is ready.",
            result.stderr,
        )
        self.assertIn("Log in to gh in your browser.", result.tty)
        self.assertEqual(self.state()["docker"], "no")
        self.assertEqual(self.state()["clients"], "claude,codex")

    def test_no_reload_when_the_path_has_it(self):
        brew = f"{self.root}/opt/homebrew/bin"
        self.env["PATH"] = f"{brew}:{self.agent_pm}/bin:{self.bin}"
        result = self.fresh()
        self.assert_ok(result)
        self.assertNotIn("run source", result.stderr)
        self.env["PATH"] = f"{brew}:{self.bin}"
        result = self.again()
        self.assert_ok(result)
        self.assertTrue(result.stderr.endswith(RELOAD + "\n"), result.stderr)

    def test_answers_kept_and_flags_override(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertEqual((self.state()["clients"], self.state()["docker"]), ("claude", "yes"))
        self.assertNotIn("brew install --cask codex", self.calls())
        result = self.again()
        self.assert_ok(result)
        self.assertNotRegex(result.tty, DOCKER_Q)
        self.assertNotRegex(result.tty, CLIENTS_Q)
        result = self.again("--clients", "claude,codex")
        self.assert_ok(result)
        self.assertEqual(self.state()["clients"], "claude,codex")
        self.assertIn("brew install --cask codex", self.calls())
        self.assertIn("codex plugin add agent-pm@agent-pm", self.calls())
        result = self.again("--no-docker", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertEqual(self.state()["docker"], "no")
        self.assertIn('default = "tmux"', self.config().read_text())
        Path(self.agent_pm, "install/state").unlink()
        self.config().write_text(
            '[session.hosts]\ndefault = "docker"\n[delegate]\nclient = "codex"\n'
        )
        result = self.again(answers=[(WRITE_Q, "y"), (PEM_Q, "")])
        self.assert_ok(result)
        self.assertNotRegex(result.tty, CLIENTS_Q)
        self.assertEqual((self.state()["clients"], self.state()["docker"]), ("codex", "yes"))

    def test_config_template_and_edit(self):
        self.assert_ok(self.fresh("--docker", "--clients", "codex"))
        self.assertEqual(
            self.config().read_text(),
            "# Written by install.sh. Keys and defaults: each layer's packaged config.toml "
            "(its `# local:` lines);\n# the Linear board: docs/board.md#setup.\n"
            '[session.hosts]\ndefault = "docker"\n\n[delegate]\nclient = "codex"\n\n'
            '[delegate.delegates]\nharness_host = "tmux"\n',
        )
        cases = (
            (
                '# mine\n[session.hosts]\ndefault = "tmux" # x\n\n[other]\nx = 1\n',
                '# mine\n[session.hosts]\ndefault = "docker"\n\n[other]\nx = 1\n\n'
                '[delegate]\nclient = "codex"\n\n[delegate.delegates]\nharness_host = "tmux"\n',
            ),
            (
                '[delegate.delegates]\n\n[session.hosts]\ndefault = "docker"\n[delegate]\nclient = "codex"\n',
                '[delegate.delegates]\nharness_host = "tmux"\n\n[session.hosts]\ndefault = "docker"\n'
                '[delegate]\nclient = "codex"\n',
            ),
        )
        for before, after in cases:
            self.config().write_text(before)
            result = self.again(answers=[(WRITE_Q, "y")])
            self.assert_ok(result)
            self.assertEqual(self.config().read_text(), after)
        dotted = 'session.hosts.default = "tmux"\ndelegate.client = "codex"\n'
        self.config().write_text(dotted)
        result = self.again()
        self.assert_ok(result)
        self.assertIn("skip: set it by hand", result.stderr)
        self.assertIn('[session.hosts] default = "docker"', result.stderr)
        self.assertEqual(self.config().read_text(), dotted)
        self.config().write_text("[broken\n")
        result = self.again()
        self.assertEqual(result.code, 1)
        self.assertIn("config.local.toml failed", result.stderr)
        self.assertEqual(self.config().read_text(), "[broken\n")

    def test_declined_writes_nothing(self):
        result = self.run_install(
            "--no-docker",
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "n"),
                (CLIENTS_Q, ""),
                (WRITE_Q, "n"),
                (WRITE_Q, "n"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(result.stderr.count("skip: declined"), 3)
        self.assertFalse(Path(self.home, ".zprofile").exists())
        self.assertNotIn("run source", result.stderr)
        self.assertFalse(self.config().exists())

    def test_codex_only(self):
        result = self.fresh("--no-docker", clients="codex")
        self.assert_ok(result)
        calls = "\n".join(self.calls())
        for absent in ("claude-code", "claude auth", "claude plugin"):
            self.assertNotIn(absent, calls)
        pkg = Path(self.home, ".codex/plugins/cache/agent-pm/agent-pm/9.8.7")
        self.assertIn(f"    {pkg}", result.stderr)
        self.assertIn("ctmux --install", self.calls())
        self.assertIn('[delegate]\nclient = "codex"\n', self.config().read_text())

    def test_claude_only(self):
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        calls = "\n".join(self.calls())
        for absent in (
            "--cask codex",
            "codex login",
            "codex plugin",
            "install-codex",
            "login codex",
        ):
            self.assertNotIn(absent, calls)
        self.assertIn("==> broker Codex install\n    skip: Codex not chosen", result.stderr)
        self.assertIn("agent-pm-broker login claude", calls)
        self.assertNotIn("[delegate]", self.config().read_text())

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
        self.assert_ok(self.again("--ref", "next"))
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
        self.assert_ok(self.again("--ref", "next"))
        self.assertEqual(self.plugin_calls(), [])

    def test_disabled_plugin_is_enabled(self):
        self.assert_ok(self.fresh())
        plugins = Path(self.fakes, "claude/plugins.json")
        plugins.write_text(plugins.read_text().replace("true", "false"))
        self.assert_ok(self.again())
        self.assertEqual(
            self.plugin_calls(),
            [
                "claude plugin enable agent-pm@agent-pm",
                "claude plugin enable autopilot@claude-autopilot",
            ],
        )

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
                f"sudo -A {broker} install-codex 0.163.0",
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
        result = self.fresh("--no-docker")
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
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (CLIENTS_Q, ""),
                (DOCKER_Q, "y"),
                (WRITE_Q, "y"),
                skip,
            ]
        )
        self.assertEqual(result.code, 1)
        self.assertIn("install.sh: Docker Desktop not running", result.stderr)
        self.assertIn(
            "install.sh: Docker Desktop running failed; rerun the same command", result.stderr
        )
        self.assertNotIn("broker-install ", self.calls())
        (self.fakes / "docker_wont_start").unlink()
        self.assert_ok(self.again(answers=[*DOCKER_LOGINS, (WRITE_Q, "y")]))
        self.assertIn("broker-install ", self.calls())

    def test_github_app_key_optional(self):
        self.assert_ok(self.fresh(docker="y"))
        result = self.again()
        self.assert_ok(result)
        self.assertNotRegex(result.tty, PEM_Q)
        self.assertIn("skip: no agent-pm-bot App key", result.stderr)
        (self.fakes / "keychain").mkdir(exist_ok=True)
        (self.fakes / "keychain/agent-pm-bot").write_text("stored by hand")
        self.assert_ok(self.again())
        self.assertIn("agent-pm-broker login github agent-pm-bot", self.calls())
        self.assertEqual(len(self.state()["login_github"]), 64)

    def test_github_app_key_stored_by_pipe(self):
        pem = Path(self.home, "app.pem")
        secret = "-----BEGIN RSA PRIVATE KEY-----SENTINELKEY-----END RSA PRIVATE KEY-----"
        pem.write_text(secret)
        encoded = base64.b64encode(secret.encode()).decode()
        logins = [(TOKEN_Q, TOKEN), (PEM_Q, str(pem)), (r"App ID \(digits\):", "12 -w x")]
        logins.append((r"App ID \(digits\):", "5352992"))
        result = self.fresh(docker="y", logins=logins)
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/agent-pm-bot").read_text(), encoded)
        self.assertEqual((self.fakes / "keychain/agent-pm-bot.account").read_text(), "5352992")
        self.assertIn("security -i", self.calls())
        self.assertIn("agent-pm-broker login github agent-pm-bot", self.calls())
        pem.unlink()
        self.assert_secret_free(encoded, "SENTINELKEY", TOKEN, result=result)

    def test_broker_change_logs_in_again(self):
        self.assert_ok(self.fresh(docker="y"))
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.broker_calls(), [])
        self.assertIn("==> broker\n    skip: installed from this plugin", result.stderr)
        self.assertIn("==> Docker Desktop running\n    skip: broker current", result.stderr)
        versions = Path(self.fakes, "claude/cache/0/broker/internal/codex/versions.go")
        versions.write_text('package codex\n\nconst MinVersion = "0.170.0"\n')
        self.assert_ok(self.again(answers=[(TOKEN_Q, TOKEN)]))
        calls = self.calls()
        self.assertIn("broker-install ", calls)
        self.assertIn("agent-pm-broker login claude", calls)
        self.assertIn(f"sudo -A {self.broker_path()} update-codex 0.170.0", calls)
        self.assertNotIn("agent-pm-broker login codex", calls)

    def test_codex_install_or_update(self):
        self.assert_ok(self.fresh(docker="y"))
        codex = self.broker_path().parent / "codex/state.json"
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
            result = self.again()
            calls = self.calls()
            if action == "busy":
                self.assertEqual(result.code, 1)
                self.assertIn("install.sh: broker busy; rerun", result.stderr)
                continue
            self.assert_ok(result)
            update = f"sudo -A {broker} update-codex 0.163.0"
            self.assertEqual(update in calls, action == "update", status)
            self.assertNotIn(f"sudo -A {broker} install-codex 0.163.0", calls)
            login = "agent-pm-broker login codex" in calls
            self.assertEqual(login, status["state"] != "ready", status)

    def test_opencode_config_and_flags(self):
        flags = ("--clients", "claude,opencode", "--no-docker")
        flags += ("--opencode-provider", "lan", "--opencode-url", "http://10.0.0.5:8080/v1")
        flags += ("--opencode-model", "m1", "--opencode-context", "204800", "--opencode-key-stdin")
        result = self.run_install(
            *flags,
            stdin=KEY + "\n",
            answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        exe = f"{self.agent_pm}/opencode/bin/opencode"
        tiers = "".join(f'{n} = "lan/m1"\n' for n in "1234")
        self.assertTrue(
            self.config()
            .read_text()
            .endswith(
                f'[session.clients.opencode]\nexecutable = "{exe}"\nmodel = "lan/m1"\n\n'
                '[session.clients.opencode.providers.lan]\nbase_url = "http://10.0.0.5:8080/v1"\n'
                f'models = ["m1"]\ncontext = 204800\n\n[session.clients.opencode.tiers]\n{tiers}'
            ),
            self.config().read_text(),
        )
        self.assertEqual(
            (self.state()["opencode_provider"], self.state()["opencode_model"]), ("lan", "m1")
        )
        text = self.config().read_text().replace('3 = "lan/m1"', '3 = "lan/mine"')
        self.config().write_text(text)
        result = self.again("--opencode-model", "m2", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        text = self.config().read_text()
        self.assertIn('models = ["m2"]', text)
        self.assertIn('1 = "lan/m2"', text)
        self.assertIn('3 = "lan/mine"', text)
        self.assertIn('model = "lan/m2"', text)
        self.assertIn("context = 204800", text)
        self.assertNotIn("Which clients", result.tty)
        result = self.again("--opencode-context", "131072", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("context = 131072", self.config().read_text())
        self.assertEqual(self.state()["opencode_context"], "131072")

    def test_opencode_context_asked_when_missing(self):
        """A State from before the context question: the rerun asks it and adds it to the table."""
        self.assert_ok(
            self.fresh(
                "--no-docker",
                clients="claude,opencode",
                opencode=PROVIDER_ANSWERS,
                logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
        )
        state = Path(self.agent_pm, "install/state")
        state.write_text(re.sub(r"(?m)^opencode_context=.*\n", "", state.read_text()))
        self.config().write_text(self.config().read_text().replace("context = 204800\n", ""))
        result = self.again(answers=[(CONTEXT_Q, "204800"), (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("[session.clients.opencode.providers.lan] context = 204800", result.tty)
        self.assertIn("context = 204800", self.config().read_text())

    def test_opencode_flags_refused(self):
        result = self.run_install("--clients", "claude", "--opencode-model", "m", tty=False)
        self.assertEqual(result.code, 2)
        self.assertIn("opencode is not among the clients", result.stderr)
        result = self.run_install("--docker", "--opencode-key-stdin", stdin=KEY + "\n", tty=False)
        self.assertEqual(result.code, 2)
        self.assertIn("the broker reads a provider key only from a terminal", result.stderr)
        self.assertEqual(self.calls(), [])
        result = self.run_install(
            "--no-docker",
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (CLIENTS_Q, "claude,opencode"),
                (PROVIDER_Q, "ollama"),
                (PROVIDER_Q, "lan.1"),
                (PROVIDER_Q, "lan"),
                (URL_Q, 'http://h"/v1'),
                (URL_Q, "http://host/v1"),
                (MODEL_Q, "m"),
                (CONTEXT_Q, "0"),
                (CONTEXT_Q, ""),
                (WRITE_Q, "y"),
                (OPENCODE_LOGIN_Q, "lan"),
                (r"API key:", KEY),
                (WRITE_Q, "y"),
            ],
        )
        self.assert_ok(result)
        self.assertEqual(self.state()["opencode_provider"], "lan")
        self.assertEqual(self.state()["opencode_context"], "65536")
        self.assertIn("context = 65536", self.config().read_text())

    def test_opencode_tmux_interactive(self):
        result = self.fresh(
            "--no-docker",
            clients="claude,opencode",
            opencode=PROVIDER_ANSWERS,
            logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
        )
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("brew install node", calls)
        prefix = f"{self.agent_pm}/opencode"
        self.assertIn(
            f"npm install -g --prefix {prefix} --cache {prefix}/.npm-cache opencode-ai@1.19.3",
            calls,
        )
        self.assertIn(f"node {prefix}/lib/node_modules/opencode-ai/postinstall.mjs", calls)
        self.assertIn("opencode auth login", calls)
        self.assertIn("In opencode auth login pick Other, enter lan", result.stderr)
        self.assertEqual(json.loads(self.auth_json().read_text())["lan"]["key"], KEY)
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("opencode auth login", self.calls())
        self.assertNotIn("npm install", "\n".join(self.calls()))
        self.assert_secret_free(KEY, PASSWORD, result=result)

    def refused_tty(self):
        """Another terminal as /dev/tty, which the Bun-built clients die on, as macOS's kqueue does."""
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        (self.fakes / "kqueue_refuses").write_text(os.ttyname(slave))
        return os.ttyname(slave)

    def test_logins_use_the_terminal_device(self):
        result = self.fresh(
            "--no-docker",
            clients="claude,opencode",
            opencode=PROVIDER_ANSWERS,
            logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            ctty=self.refused_tty(),
        )
        self.assert_ok(result)
        self.assertIn("claude auth login", self.calls())
        self.assertIn("opencode auth login", self.calls())

    def test_broker_logins_use_the_terminal_device(self):
        result = self.fresh(clients="claude", docker="y", ctty=self.refused_tty())
        self.assert_ok(result)
        self.assertIn("claude setup-token", self.calls())
        self.assertIn("agent-pm-broker login claude", self.calls())

    def test_terminal_without_a_device_name(self):
        (self.fakes / "tty_name").write_text("??\n")
        self.assert_ok(self.fresh())
        self.assertIn("claude auth login", self.calls())

    def test_opencode_that_does_not_run_stops(self):
        (self.fakes / "postinstall_broken").write_text("")
        flags = ("--clients", "claude,opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-url", "http://localhost:8080/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        result = self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (WRITE_Q, "y")]
        )
        self.assertEqual(result.code, 1)
        self.assertIn(
            f"install.sh: OpenCode 1.19.3 in {self.agent_pm}/opencode does not run:"
            " opencode --version gave ''",
            result.stderr,
        )
        (self.fakes / "postinstall_broken").unlink()
        result = self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y")]
        )
        self.assert_ok(result)
        self.assertIn("1.19.3", (self.fakes / "opencode_version").read_text())

    def test_opencode_key_from_stdin(self):
        self.auth_json().parent.mkdir(parents=True)
        self.auth_json().write_text('{"other": {"type": "api", "key": "theirs"}}')
        result = self.run_install(
            "--password-stdin",
            "--opencode-key-stdin",
            "--no-docker",
            "--clients",
            "codex,opencode",
            stdin=f"{PASSWORD}\n{KEY}\n",
            answers=[(WRITE_Q, "y"), *PROVIDER_ANSWERS, (WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        data = json.loads(self.auth_json().read_text())
        self.assertEqual(
            data, {"other": {"type": "api", "key": "theirs"}, "lan": {"type": "api", "key": KEY}}
        )
        self.assertEqual(self.auth_json().stat().st_mode & 0o777, 0o600)
        self.assertNotIn("opencode auth login", self.calls())
        self.assertEqual(list(self.auth_json().parent.glob(".auth.json.*")), [])
        self.assert_secret_free(KEY, PASSWORD, result=result)
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertEqual(
            json.loads(self.auth_json().read_text()), {"other": {"type": "api", "key": "theirs"}}
        )
        self.assertEqual(self.auth_json().stat().st_mode & 0o777, 0o600)

    def test_opencode_docker_broker_login(self):
        result = self.fresh(
            clients="claude,opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[
                (TOKEN_Q, TOKEN),
                (PEM_Q, ""),
                (r"OpenCode provider lan API key \(hidden\):", KEY),
            ],
        )
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("agent-pm-broker login opencode lan", calls)
        self.assertLess(
            calls.index("agent-pm-broker login claude"),
            calls.index("agent-pm-broker login opencode lan"),
        )
        self.assertNotIn("opencode auth login", calls)
        self.assertLess(
            result.stderr.index("==> broker\n"), result.stderr.index("==> OpenCode key")
        )
        self.assertFalse(self.auth_json().exists())
        self.assertTrue(self.state()["login_opencode"].endswith(" lan"))
        self.assert_ok(self.again())
        self.assertNotIn("agent-pm-broker login opencode lan", self.calls())
        self.assert_secret_free(KEY, TOKEN, result=result)
        (self.fakes / "keychain/agent-pm-opencode").unlink()
        login = (r"OpenCode provider lan API key \(hidden\):", KEY)
        self.assert_ok(self.again(answers=[login]))
        self.assertIn("agent-pm-broker login opencode lan", self.calls())

    def test_opencode_http_warning(self):
        flags = ("--clients", "claude,opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-model", "m", "--opencode-context", "65536", "--opencode-key-stdin")
        for url, warned in (("http://10.0.0.5:8080/v1", True), ("http://localhost:8080/v1", False)):
            self.config().unlink(missing_ok=True)
            Path(self.agent_pm, "install/state").unlink(missing_ok=True)
            self.auth_json().unlink(missing_ok=True)
            result = self.run_install(
                *flags,
                "--opencode-url",
                url,
                stdin=KEY + "\n",
                answers=[(SUDO_Q, PASSWORD), *([(WRITE_Q, "y")] * (3 if warned else 1))],
            )
            self.assert_ok(result)
            self.assertEqual("sends the API key in plaintext" in result.stderr, warned, url)

    def test_linear_keys_and_board_jobs(self):
        result = self.fresh()
        self.assert_ok(result)
        self.assertIn('skip: no tracker = "linear"', result.stderr)
        self.assertIn("==> board launchd jobs\n    skip: no Linear board", result.stderr)
        linear = (
            '\n[orchestrator]\ntracker = "linear"\n\n[orchestrator.linear]\n'
            'harness_key = "linear-harness"\nmanager_key = "linear-manager"\n\n'
            '[orchestrator.linear.roles.pm]\naccount = "me+pm@example.com"\nkey = "linear-pm"\n'
        )
        self.config().write_text(self.config().read_text() + linear)
        (self.fakes / "keychain").mkdir()
        (self.fakes / "keychain/linear-manager").write_text("already")
        key = r"password data for new item:"
        result = self.again(
            answers=[(key, "k-SENTINEL-harness"), (key, "k-SENTINEL-pm"), (WRITE_Q, "y")]
        )
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/linear-harness").read_text(), "k-SENTINEL-harness")
        self.assertEqual((self.fakes / "keychain/linear-pm").read_text(), "k-SENTINEL-pm")
        calls = self.calls()
        self.assertIn("security add-generic-password -s linear-pm -a me+pm@example.com -w", calls)
        self.assertIn("security add-generic-password -s linear-harness -a linear-harness -w", calls)
        self.assertIn("skip: linear-manager in the keychain", result.stderr)
        agents = Path(self.home, "Library/LaunchAgents")
        for job in ("router", "promote"):
            plist = (agents / f"com.ophis.agent-pm.{job}.plist").read_text()
            self.assertIn(
                f"<string>{self.home}/.agent-pm/bin/agent-pm-orchestrator</string>", plist
            )
            self.assertIn(f"<string>{self.home}/.agent-pm/logs/orchestrator.jsonl</string>", plist)
            self.assertNotIn("/Users/francis", plist)
            self.assertIn(
                f"launchctl bootstrap gui/501 {agents}/com.ophis.agent-pm.{job}.plist", calls
            )
        self.assertTrue(Path(self.agent_pm, "logs").is_dir())
        self.assert_secret_free("k-SENTINEL", result=result)
        mine = agents / "com.ophis.agent-pm.promote.plist"
        mine.write_text("<plist>mine</plist>")
        result = self.again()
        self.assert_ok(result)
        self.assertIn(
            f"skip: {mine} exists; remove it to let install.sh install the job", result.stderr
        )
        self.assertEqual(mine.read_text(), "<plist>mine</plist>")
        self.assertFalse([c for c in self.calls() if c.startswith(("launchctl", "security add"))])

    def ledger(self):
        path = Path(self.agent_pm, "install/added")
        return path.read_text().splitlines() if path.exists() else []

    def full(self):
        return self.fresh(
            clients="claude,codex,opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(TOKEN_Q, TOKEN), (PEM_Q, ""), (r"OpenCode provider lan API key", KEY)],
        )

    def test_ledger_of_a_fresh_install(self):
        shutil.rmtree(self.agent_pm)
        self.assert_ok(self.fresh("--no-docker"))
        h = self.home
        self.assertEqual(
            self.ledger(),
            [
                f"dir\t{h}/.agent-pm",
                f"homebrew\t{self.root}/opt/homebrew",
                "clt",
                f"profile-file\t{h}/.zprofile",
                f'profile\t{h}/.zprofile\teval "$({self.root}/opt/homebrew/bin/brew shellenv)"',
                "formula\tgh",
                "formula\ttmux",
                "formula\tgit",
                "cask\titerm2",
                "config\tcreated",
                f"dir\t{h}/.config/gh",
                f"dir\t{h}/.claude",
                f"file\t{h}/.claude.json",
                f"dir\t{h}/.codex",
                "cask\tclaude-code",
                "cask\tcodex",
                "login\tgh",
                "login\tclaude",
                "login\tcodex",
                "claude-marketplace\tagent-pm",
                "claude-marketplace\tclaude-autopilot",
                "claude-plugin\tagent-pm@agent-pm",
                "claude-plugin\tautopilot@claude-autopilot",
                "codex-marketplace\tagent-pm",
                "codex-marketplace\tclaude-autopilot",
                "codex-plugin\tagent-pm@agent-pm",
                "codex-plugin\tautopilot@claude-autopilot",
                f'profile\t{h}/.zprofile\texport PATH="$HOME/.agent-pm/bin:$PATH"',
            ],
        )

    def test_ledger_with_docker_and_opencode(self):
        self.assert_ok(self.full())
        h, ledger = self.home, self.ledger()
        for record in (
            f"dir\t{h}/.agent-pm/bin",
            f"dir\t{h}/.agent-pm/docker",
            f"dir\t{h}/.agent-pm/opencode",
            f"dir\t{h}/.local/share/opencode",
            f"dir\t{h}/.docker",
            f"dir\t{h}/Library/Group Containers/group.com.docker",
            "formula\tnode",
            "cask\tdocker-desktop",
            "broker",
            "keychain\tagent-pm-claude\tdefault",
            "keychain\tagent-pm-codex\tauth",
            "keychain\tagent-pm-opencode\tlan",
        ):
            self.assertIn(record, ledger)
        self.assertNotIn(f"dir\t{h}/.agent-pm", ledger)
        self.assertNotIn("keychain\tagent-pm-github-users\tagent-pm-bot", ledger)

    def test_rerun_skips_everything(self):
        self.assert_ok(self.full())
        before = self.ledger()
        files = {
            p: p.read_bytes()
            for p in Path(self.home).rglob("*")
            if p.is_file() and "fakes" not in p.parts
        }
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("[y/N]", result.tty)
        self.assertNotRegex(result.tty, r"\? |name:|URL|model:|App ID|Paste")
        changing = (
            "brew install",
            "plugin install",
            "plugin add",
            "marketplace add",
            "broker-install",
        )
        changing += ("security add", "launchctl", "npm install", "setup-token", "-codex ")
        changing += ("auth login", "login codex", "login claude", "login opencode", "codex login")
        changed = [c for c in self.calls() if any(w in c for w in changing)]
        self.assertEqual([c for c in changed if not c.endswith(" status")], [])
        self.assertEqual(self.ledger(), before)
        exempt = {"sudo", "configuration", "plugin directory"}
        steps = re.findall(r"==> (.+)\n(?!    skip:)", result.stderr)
        self.assertEqual([s for s in steps if s not in exempt], [])
        for path, data in files.items():
            if path.name not in ("stdout", "stderr", "calls.log", "env.log"):
                self.assertEqual(path.read_bytes(), data, path)

    def test_failure_then_rerun_completes(self):
        (self.fakes / "fail").write_text("brew install --cask codex")
        result = self.run_install(
            "--no-docker",
            answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (CLIENTS_Q, ""), (WRITE_Q, "y")],
        )
        self.assertEqual(result.code, 1)
        self.assertTrue(
            result.stderr.endswith(
                "install.sh: apps failed; rerun the same command to continue\n" + RELOAD + "\n"
            ),
            result.stderr,
        )
        self.assertIn("cask\tcodex", self.ledger())
        self.assertNotIn("codex plugin", "\n".join(self.calls()))
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn("brew install --cask codex", calls)
        self.assertNotIn("brew install --cask claude-code", calls)
        self.assertIn("codex plugin add agent-pm@agent-pm", calls)
        self.assertEqual(self.ledger().count("cask\tcodex"), 1)

    def test_not_recorded_outside_home(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        self.env["CODEX_HOME"] = str(outside / "codex")
        result = self.fresh("--no-docker")
        self.assert_ok(result)
        self.assertIn(f"not recorded for cleanup: dir {outside}/codex", result.stderr)
        self.assertFalse([r for r in self.ledger() if str(outside) in r])

    def test_created_config_is_not_backed_up(self):
        self.assert_ok(self.fresh("--no-docker"))
        self.assert_ok(
            self.again("--docker", answers=[(WRITE_Q, "y"), (TOKEN_Q, TOKEN), (PEM_Q, "")])
        )
        self.assertIn('default = "docker"', self.config().read_text())
        self.assertFalse(Path(self.agent_pm, "install/config.local.toml.orig").exists())
        self.assertNotIn("config\tbackup", self.ledger())

    def test_existing_config_is_backed_up_once(self):
        mine = '# mine\n[session.hosts]\ndefault = "tmux"\n'
        self.config().write_text(mine)
        self.assert_ok(self.fresh("--docker", "--clients", "claude"))
        self.assert_ok(self.again("--no-docker", answers=[(WRITE_Q, "y")]))
        self.assertEqual(Path(self.agent_pm, "install/config.local.toml.orig").read_text(), mine)
        self.assertEqual(self.ledger().count("config\tbackup"), 1)

    def cleanup(self, *args, answers=(), stdin=None):
        """Without --password-stdin, --yes runs answer sudo's prompt."""
        if stdin is None and "--yes" in args:
            answers = [(SUDO_Q, PASSWORD), *answers]
        return self.run_install(*args, answers=answers, stdin=stdin, script=CLEANUP)

    def snapshot(self):
        skip = {"fakes", "bin"}
        logs = {"stdout", "stderr", "calls.log", "env.log"}
        return {
            p: p.read_bytes()
            for p in Path(self.home).rglob("*")
            if p.is_file() and not skip & set(p.relative_to(self.home).parts) and p.name not in logs
        }

    def test_cleanup_usage_and_nothing_recorded(self):
        result = self.run_install("--help", tty=False, script=CLEANUP)
        self.assertEqual(result.code, 0)
        self.assertIn("usage: install-cleanup.sh [--yes] [--password-stdin]", result.stdout)
        result = self.run_install("--bogus", tty=False, script=CLEANUP)
        self.assertEqual(result.code, 2)
        tty = Tty()
        try:
            with open(tty.path) as stdin:
                proc = subprocess.run(
                    ["bash", str(CLEANUP), "--password-stdin"],
                    env=self.env,
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        finally:
            tty.close()
        self.assertEqual(proc.returncode, 2)
        result = self.cleanup()
        self.assert_ok(result)
        self.assertIn("nothing recorded by install.sh; nothing to remove", result.stderr)
        self.assertNotIn("sudo", "\n".join(self.calls()))

    def test_cleanup_asks_and_declines(self):
        self.assert_ok(self.fresh("--no-docker"))
        before = self.snapshot()
        result = self.cleanup(answers=[(REMOVE_Q, "n")])
        self.assertEqual(result.code, 1)
        self.assertIn("install-cleanup.sh: nothing removed", result.stderr)
        self.assertIn("  formula gh", result.tty)
        self.assertIn("  login claude", result.tty)
        self.assertFalse([c for c in self.calls() if c.startswith("sudo")])
        self.assertEqual(self.snapshot(), before)

    def test_cleanup_rejects_bad_records(self):
        self.assert_ok(self.fresh("--no-docker"))
        ledger = Path(self.agent_pm, "install/added")
        good = ledger.read_text()
        bad = (
            "dir\t/Applications",
            f"dir\t{self.home}",
            f"dir\t{self.home}/x/../..",
            "formula\t--force",
            "homebrew\t/usr/local",
            "keychain\t-x\ty",
            "rm\t-rf",
        )
        for record in bad:
            ledger.write_text(good + record + "\n")
            result = self.run_install("--yes", script=CLEANUP)
            self.assertEqual(result.code, 1, record)
            self.assertIn("not a record install.sh writes; nothing removed", result.stderr)
            self.assertFalse([c for c in self.calls() if c.startswith("sudo")], record)
            self.assertEqual(ledger.read_text(), good + record + "\n")

    def test_cleanup_round_trip(self):
        before = self.snapshot()
        first = self.full()
        self.assert_ok(first)
        calls, ledger, files = self.run_calls(), self.ledger(), self.snapshot()
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.fakes / "keychain").iterdir()), [])
        self.assertEqual(list((self.fakes / "login").iterdir()), [])
        self.assertFalse(self.broker_path().parent.exists())
        self.assertFalse(Path(self.root, "opt/homebrew/bin/brew").exists())
        self.assert_secret_free(PASSWORD, KEY, TOKEN, result=result)
        second = self.full()
        self.assert_ok(second)
        self.assertEqual(self.run_calls(), calls)
        self.assertEqual(self.ledger(), ledger)
        self.assertEqual(second.tty, first.tty)
        self.assertEqual(self.snapshot(), files)

    def preinstalled_homebrew(self):
        """Homebrew with gh and the Command Line Tools, there before install.sh; PATH lacks its bin."""
        prefix = Path(self.root, "opt/homebrew/bin")
        prefix.mkdir(parents=True)
        for app in ("brew", "gh"):
            shutil.copy(self.fakes / "apps" / app, prefix / app)
        (self.fakes / "brew").mkdir()
        (self.fakes / "brew/gh").touch()
        Path(self.root, "Library/Developer/CommandLineTools").mkdir(parents=True)

    def test_cleanup_removes_only_what_was_added(self):
        self.preinstalled_homebrew()
        profile = Path(self.home, ".zprofile")
        profile.write_text("export EDITOR=vim\n")
        mine = '# mine\n[session.hosts]\ndefault = "tmux"\n'
        self.config().write_text(mine)
        (self.fakes / "keychain").mkdir()
        (self.fakes / "keychain/agent-pm-claude").write_text("theirs")
        Path(self.home, ".docker").mkdir()
        Path(self.home, ".docker/config.json").write_text("{}")
        before = self.snapshot()
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        self.assertFalse(
            [r for r in self.ledger() if r.startswith(("homebrew", "clt", "formula\tgh"))]
        )
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.fakes / "brew/gh").exists())
        self.assertTrue((self.fakes / "keychain/agent-pm-claude").exists())
        self.assertNotIn("brew uninstall gh", self.calls())

    def test_cleanup_without_brew_on_path(self):
        self.preinstalled_homebrew()
        self.assert_ok(self.fresh("--no-docker"))
        self.assertNotIn(str(self.root), self.env["PATH"])
        result = self.cleanup("--yes")
        self.assert_ok(result)
        calls = self.calls()
        for done in (
            "codex plugin remove agent-pm@agent-pm",
            "claude plugin uninstall agent-pm@agent-pm",
            "claude plugin marketplace remove agent-pm",
            "gh auth logout --hostname github.com",
            "claude auth logout",
            "codex logout",
            "brew uninstall --cask codex",
            "brew uninstall --cask claude-code",
            "brew uninstall tmux",
        ):
            self.assertIn(done, calls)
        self.assertEqual(list((self.fakes / "login").iterdir()), [])

    def test_cleanup_keeps_a_failed_removal(self):
        self.assert_ok(self.fresh("--no-docker"))
        (self.fakes / "fail").write_text("claude plugin marketplace remove agent-pm")
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1)
        self.assertIn("removing claude-marketplace agent-pm failed", result.stderr)
        self.assertIn("claude-marketplace\tagent-pm", self.ledger())
        self.assertNotIn("codex-plugin\tagent-pm@agent-pm", self.ledger())
        self.assert_ok(self.cleanup("--yes"))
        self.assertFalse(Path(self.agent_pm, "install").exists())

    def board_install(self):
        """A fresh install, then the Linear board set up and the installer run again for its keys and jobs."""
        self.assert_ok(self.fresh("--no-docker"))
        first = self.run_calls()
        linear = (
            '\n[orchestrator]\ntracker = "linear"\n\n[orchestrator.linear]\n'
            'harness_key = "linear-harness"\nmanager_key = "linear-manager"\n'
        )
        self.config().write_text(self.config().read_text() + linear)
        key = r"password data for new item:"
        self.assert_ok(self.again(answers=[(key, "k1"), (key, "k2"), (WRITE_Q, "y")]))
        return first + self.run_calls()

    def test_cleanup_round_trip_with_board(self):
        before = self.snapshot()
        calls = self.board_install()
        files = self.snapshot()
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        uid_calls = [c for c in self.calls() if c.startswith("launchctl bootout")]
        self.assertEqual(len(uid_calls), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.fakes / "keychain").iterdir()), [])
        self.assertEqual(self.board_install(), calls)
        self.assertEqual(self.snapshot(), files)

    def test_cleanup_round_trip_opencode_tmux(self):
        def install():
            result = self.fresh(
                "--no-docker",
                clients="claude,opencode",
                opencode=PROVIDER_ANSWERS,
                logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", KEY)],
            )
            self.assert_ok(result)
            return self.run_calls()

        before = self.snapshot()
        calls = install()
        files = self.snapshot()
        self.assert_ok(self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.auth_json().exists())
        self.assertEqual(install(), calls)
        self.assertEqual(self.snapshot(), files)

    def test_cleanup_keeps_user_profile_lines(self):
        self.assert_ok(self.fresh("--no-docker"))
        profile = Path(self.home, ".zprofile")
        profile.write_text(profile.read_text() + "export EDITOR=vim\n")
        self.assert_ok(self.cleanup("--yes"))
        self.assertEqual(profile.read_text(), "export EDITOR=vim\n")

    def test_cleanup_failure_then_rerun(self):
        self.assert_ok(self.fresh("--no-docker"))
        (self.fakes / "fail").write_text("codex plugin remove agent-pm@agent-pm")
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1)
        self.assertIn("install-cleanup.sh: plugins failed; rerun the same command", result.stderr)
        self.assertIn("codex-plugin\tagent-pm@agent-pm", self.ledger())
        self.assertIn("login\tgh", self.ledger())
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertFalse(Path(self.agent_pm, "install").exists())

    def test_cleanup_missing_tool_keeps_record(self):
        self.assert_ok(self.fresh("--no-docker"))
        Path(self.root, "opt/homebrew/bin/codex").unlink()
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1)
        self.assertIn("codex not found on PATH; its records stay for a rerun", result.stderr)
        self.assertIn("codex-plugin\tagent-pm@agent-pm", self.ledger())
        self.assertIn("cask\tcodex", self.ledger())

    def test_cleanup_handles_every_kind(self):
        install, cleanup = SCRIPT.read_text(), CLEANUP.read_text()
        written = set(re.findall(r"ledger_add ([a-z-]+)", install)) | {"dir", "keychain"}
        listed = re.search(r"for kind in (.*?); do", cleanup, re.S).group(1)
        handled = set(listed.replace("\\\n", " ").split())
        self.assertEqual(written - handled, set())

    def test_cleanup_helpers_match(self):
        install, cleanup = SCRIPT.read_text(), CLEANUP.read_text()
        pattern_line = re.compile(r"^name_pattern=.*$", re.M)
        self.assertEqual(
            pattern_line.search(cleanup).group(0), pattern_line.search(install).group(0)
        )
        for name in SHARED:
            pattern = rf"^{name}\(\) \{{(?:[^\n]*\}}$|\n.*?^\}})"
            ours = re.search(pattern, install, re.S | re.M)
            theirs = re.search(pattern, cleanup, re.S | re.M)
            self.assertIsNotNone(theirs, name)
            self.assertEqual(theirs.group(0), ours.group(0), name)

    def test_profile_without_a_final_newline(self):
        profile = Path(self.home, ".zprofile")
        profile.write_text("export EDITOR=vim")
        self.assert_ok(self.fresh("--no-docker"))
        lines = profile.read_text().splitlines()
        self.assertEqual(lines[0], "export EDITOR=vim")
        self.assertTrue(lines[1].startswith("eval "), lines)
        self.assert_ok(self.again())
        self.assertEqual(len(profile.read_text().splitlines()), 3)
