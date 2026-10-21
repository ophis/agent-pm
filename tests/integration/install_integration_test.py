import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections import namedtuple
from pathlib import Path

from agent_pm.orchestrator.engine import config as orchestrator_config

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
    "stty",
)
FAKED = ("uname", "id", "sudo", "curl", "security", "open", "launchctl", "shasum")
FAKED += ("xcode-select", "ps", "sw_vers", "lipo", "pgrep", "pkill", "defaults")
APPS = ("brew", "gh", "tmux", "git", "go", "claude", "codex", "docker", "agent-pm-broker")
APPS += ("npm", "node", "opencode")
PASSWORD = "pw-SENTINEL-sudo"
TOKEN = "sk-ant-oat01-SENTINEL-token"
KEY = "sk-SENTINEL-opencode-key"
OLD, NEW = "1" * 40, "2" * 40
SPEC = "agent-pm@git+https://github.com/ophis/agent-pm.git#main"
API_CALL = (
    "curl -fsSL -H Accept: application/vnd.github.sha"
    " https://api.github.com/repos/ophis/agent-pm/commits/main"
)
MENU_Q = r"Choose what to install \(up/down move, space ticks, Enter confirms\):"
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
SUDO_Q = r"Password:"
WRITE_Q = r"Write it\? \[y/N\]"
TOKEN_Q = r"Paste the setup-token \(hidden\):"
LEAVE_Q = r"Press Enter to leave this screen:"
SCREEN_ON, SCREEN_OFF = "\x1b[?1049h\x1b[H\x1b[2J", "\x1b[H\x1b[2J\x1b[?1049l"
PEM_Q = r"private key \(\.pem; [^)]*\), empty to skip:"
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
    "terminal",
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
ITERM_DIRS = (
    "Application Support/iTerm2",
    "Application Support/iTerm",
    "Caches/com.googlecode.iterm2",
    "HTTPStorages/com.googlecode.iterm2",
    "WebKit/com.googlecode.iterm2",
    "Saved Application State/com.googlecode.iterm2.savedState",
    "Containers/com.googlecode.iterm2.iTermFileProvider",
    "Containers/iTermAI",
    "Application Scripts/com.googlecode.iterm2.iTermFileProvider",
)
ITERM_FILES = (
    "Preferences/com.googlecode.iterm2.plist",
    "Preferences/com.googlecode.iterm2.private.plist",
    "Cookies/com.googlecode.iterm2.binarycookies",
    "HTTPStorages/com.googlecode.iterm2.binarycookies",
)
KEY_Q = r"password data for new item:"
SLOTS = ("harness", "manager", "researcher", "pm", "engineer")
STATES = ("Backlog", "Todo", "In Progress", "In Review", "Handoff", "Done", "Canceled")


def uuid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def label(n, name, parent=None):
    return {
        "id": uuid(n),
        "name": name,
        "isGroup": parent is None and name in ("Tasks", "Agent"),
        "parent": {"id": uuid(parent)} if parent else None,
    }


LINEAR = {
    "keys": {f"k-SENTINEL-{slot}": f"{slot}@example.com" for slot in SLOTS},
    "teams": [
        {
            "id": uuid(1),
            "key": "ENG",
            "name": "Engineering",
            "states": [{"id": uuid(10 + i), "name": name} for i, name in enumerate(STATES)],
            "members": [
                *(
                    {"email": f"{slot}@example.com", "active": True}
                    for slot in SLOTS
                    if slot != "manager"
                ),
                {"email": "me@example.com", "active": True},
                {"email": "gone@example.com", "active": False},
            ],
            "projects": [{"id": uuid(30), "name": "Widget"}],
        }
    ],
    "labels": [
        label(40, "Tasks"),
        label(41, "build", 40),
        label(42, "light-build", 40),
        label(43, "Agent"),
        label(44, "claude", 43),
        label(45, "bug"),
    ],
}
PROJECT_Q = r"GitHub repo of Linear project Widget \(<owner>/<name>, empty to skip\):"
DOCS_Q = r"GitHub repo for the board's documents, research reports and PRDs \(<owner>/<name>\):"
DOCS = ((DOCS_Q, "ophis/docs"), (r"Its branch \[main\]:", ""))
BOARD = ((PROJECT_Q, "ophis/widget"), *DOCS, (WRITE_Q, "y"), (WRITE_Q, "y"))
STATE_KEYS = ("backlog", "todo", "in_progress", "in_review", "handoff", "done", "canceled")
HAND_CONFIG = (
    f'[orchestrator]\ntracker = "linear"\n\n[orchestrator.linear]\nteam = "{uuid(1)}"\n'
    f'harness_key = "linear-harness"\nmanager_key = "linear-manager"\ntask_label_group = "{uuid(40)}"\n\n'
    "[orchestrator.linear.states]\n"
    + "".join(f'{key} = "{uuid(10 + i)}"\n' for i, key in enumerate(STATE_KEYS))
    + "".join(
        f'\n[orchestrator.linear.roles.{role}]\naccount = "{account}"\nkey = "linear-{role}"\n'
        for role, account in (
            ("researcher", "researcher@example.com"),
            ("pm", "me+pm@example.com"),
            ("engineer", "engineer@example.com"),
        )
    )
    + "".join(
        f'\n[delegate.roles.{role}.output]\ntype = "github"\nrepo = "ophis/docs"\nbranch = "main"\ndir = "{d}"\n'
        for role, d in (("researcher", "Research/"), ("pm", "Product Design/"))
    )
)
Result = namedtuple("Result", "code stdout stderr tty")
ITEMS = ("claude", "codex", "opencode", "docker", "board")


def menu(want, have=("claude", "codex")):
    """The checklist keys that turn the ticked items `have` into `want`: space where they differ, down to the
    next item; the answer's newline confirms."""
    return "".join((" " if (i in want) != (i in have) else "") + "\x1b[B" for i in ITEMS)


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
        (self.fakes / "commit").write_text(OLD)
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
        flow = "orchestrator/src/agent_pm/orchestrator/engine/flow"
        (self.pkg_src / flow).mkdir(parents=True)
        charters = "delegate/src/agent_pm/delegate/team/roles"
        (self.pkg_src / charters).mkdir(parents=True)
        # copyfile: CI makes the shipped source read-only, and the cleanup must delete these copies
        for source in [Path(REPO, flow, "workflow.toml"), *Path(REPO, charters).glob("*.md")]:
            shutil.copyfile(source, self.pkg_src / source.relative_to(REPO))
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
        kept = {
            self.fakes / "keychain",
            self.fakes / "broker/claude_token",
            self.fakes / "linear.json",
        }
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

    def fresh(self, *args, clients="", docker="n", opencode=(), logins=None, ctty=None, then=()):
        """A first run answering every question: brew profile line, clients, Docker, the
        OpenCode provider, the config, the Docker logins, the PATH line, then `then` (the board's).
        """
        chosen = args[args.index("--clients") + 1] if "--clients" in args else clients
        if logins is None and (docker == "y" or "--docker" in args):
            logins = [(TOKEN_Q, TOKEN)] if "claude" in (chosen or "claude") else []
            logins.append((PEM_Q, ""))
        answers = [(SUDO_Q, PASSWORD), (WRITE_Q, "y")]
        if "--clients" not in args or not {"--docker", "--no-docker"} & set(args):
            on = "--docker" in args or ("--no-docker" not in args and docker == "y")
            have = ("claude", "codex", *(("board",) if "--board" in args else ()))
            if "--clients" in args:
                have = chosen.split(",")
            if "--docker" in args:
                have = (*have, "docker")
            want = (*(chosen or "claude,codex").split(","), *(("docker",) if on else ()))
            want = (*want, *(("board",) if "--board" in args else ()))
            answers.append((MENU_Q, menu(want, have)))
        plugin = [(WRITE_Q, "y")] if "opencode" in (chosen or "") else []
        answers += [*opencode, (WRITE_Q, "y"), *plugin, *(logins or ()), (WRITE_Q, "y"), *then]
        return self.run_install(*args, answers=answers, ctty=ctty)

    def again(self, *args, answers=()):
        """A rerun: the checklist, unless flags answer it, confirmed as the last run left it."""
        flagged = "--clients" in args and {"--docker", "--no-docker"} & set(args)
        shown = [] if flagged else [(MENU_Q, "")]
        return self.run_install(*args, answers=[(SUDO_Q, PASSWORD), *shown, *answers])

    def config(self):
        return Path(self.agent_pm, "config.local.toml")

    def linear(self, **changes):
        """The fake Linear workspace: LINEAR with `changes`."""
        (self.fakes / "linear.json").write_text(json.dumps({**LINEAR, **changes}))

    def keys(self, *slots):
        """The answers pasting each slot's key at security's prompt."""
        return [(KEY_Q, f"k-SENTINEL-{slot}") for slot in slots]

    def configured(self, text, then=(), board="linear"):
        """A first run with Board ticked over a config.local.toml of `text` that already has the
        configuration: brew profile line, the checklist, the PATH line, then `then`."""
        Path(self.agent_pm).mkdir(parents=True, exist_ok=True)
        self.config().write_text(text)
        ticked = ("claude", "codex", "board")
        answers = [
            (SUDO_Q, PASSWORD),
            (WRITE_Q, "y"),
            (MENU_Q, menu(ticked, ticked)),
            (WRITE_Q, "y"),
        ]
        return self.run_install("--board", board, "--launchd", answers=[*answers, *then])

    def plugin_calls(self):
        return [c for c in self.calls() if " plugin " in c and " list " not in c]

    def broker_calls(self):
        own = {"sudo -n true", "sudo -n -v", "sudo -k", "sudo -S -p  -v", "sudo -A -E -- true"}
        wanted = ("broker-", "sudo", "agent-pm-broker")
        return [
            c
            for c in self.calls()
            if c.startswith(wanted)
            and c not in own
            and "status" not in c
            and "/Docker.app/" not in c
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
        self.assertLess(tty.index("Write it?"), tty.index("Choose what to install"))
        self.assertLess(tty.index("Choose what to install"), tty.index("config.local.toml:"))
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
            "Welcome onboard, and congrats: your crew is ready.",
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

    def test_checklist(self):
        """j/k move too; no client ticked asks again; a first run leaves the board unticked, which skips its steps."""
        keys = " j jjj"
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, keys),
                (r"Tick at least one client\.", "kkkk "),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.state()["clients"], self.state()["docker"], self.state()["board"]),
            ("codex", "no", "no"),
        )
        self.assertIn("  [ ] Board: local or Linear", result.tty)
        self.assertIn("==> board launchd jobs\n    skip: board not chosen", result.stderr)
        self.assertIn("==> Linear keys\n    skip: board not chosen", result.stderr)

    def test_checklist_skipped_by_flags(self):
        result = self.fresh("--clients", "claude", "--no-docker", "--no-board")
        self.assert_ok(result)
        self.assertNotRegex(result.tty, MENU_Q)
        self.assertEqual(self.state()["board"], "no")
        self.assertEqual(
            self.run_install("--board", "linear", "--launchd", "--no-board", tty=False).code, 2
        )

    def test_answers_kept_and_flags_override(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertEqual((self.state()["clients"], self.state()["docker"]), ("claude", "yes"))
        self.assertEqual(self.state()["board"], "no")
        self.assertNotIn("brew install --cask codex", self.calls())
        result = self.again()
        self.assert_ok(result)
        lines = (
            "> [x] Claude Code",
            "  [ ] Codex",
            "  [ ] OpenCode",
            "  [x] Docker workers",
            "  [ ] Board",
        )
        for line in lines:
            self.assertIn(line, result.tty)
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
        self.assertIn("> [ ] Claude Code\r\n\r\x1b[K  [x] Codex", result.tty)
        self.assertEqual((self.state()["clients"], self.state()["docker"]), ("codex", "yes"))

    def test_config_template_and_edit(self):
        self.assert_ok(self.fresh("--docker", "--clients", "codex"))
        self.assertEqual(
            self.config().read_text(),
            "# Written by install.sh. Keys and defaults: each layer's packaged config.toml "
            "(its `# local:` lines);\n# the Linear board: docs/board.md#setup.\n"
            '[session.hosts]\ndefault = "docker"\n\n[delegate]\nclient = "codex"\n\n'
            '[session.hosts.docker]\nplugins = []\n\n[delegate.delegates]\nharness_host = "tmux"\n',
        )
        cases = (
            (
                '# mine\n[session.hosts]\ndefault = "tmux" # x\n\n[other]\nx = 1\n',
                '# mine\n[session.hosts]\ndefault = "docker"\n\n[other]\nx = 1\n\n'
                '[delegate]\nclient = "codex"\n\n[session.hosts.docker]\nplugins = []\n\n'
                '[delegate.delegates]\nharness_host = "tmux"\n',
            ),
            (
                '[delegate.delegates]\n\n[session.hosts]\ndefault = "docker"\n[delegate]\nclient = "codex"\n',
                '[delegate.delegates]\nharness_host = "tmux"\n\n[session.hosts]\ndefault = "docker"\n'
                '[delegate]\nclient = "codex"\n\n[session.hosts.docker]\nplugins = []\n',
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
                (MENU_Q, ""),
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
        self.assertNotIn("api.github.com", calls)

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
        self.assertIn(
            "skip: no GitHub App key: create the App by docs/docker-host.md#create-the-github-app",
            result.stderr,
        )
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

    def test_docker_not_running_fails(self):
        (self.fakes / "docker_wont_start").touch()
        (self.bin / "sleep").unlink()
        executable(self.bin / "sleep", "#!/bin/sh\n")
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(("claude", "codex", "docker"))),
                (WRITE_Q, "y"),
            ]
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(self.calls().count("docker info"), 102)
        self.assertIn(
            "install.sh: Docker Desktop did not start in 5 minutes: check its window, then rerun",
            result.stderr,
        )
        self.assertIn(
            "install.sh: Docker Desktop running failed; rerun the same command", result.stderr
        )
        self.assertNotIn("broker-install ", self.calls())
        (self.fakes / "docker_wont_start").unlink()
        self.assert_ok(self.again(answers=[*DOCKER_LOGINS, (WRITE_Q, "y")]))
        self.assertIn("broker-install ", self.calls())

    def test_docker_first_start_unattended(self):
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        calls = self.calls()
        install = f"sudo -A {self.root}/Applications/Docker.app/Contents/MacOS/install"
        self.assertLess(
            calls.index(f"{install} --accept-license --user=tester"), calls.index("open -a Docker")
        )
        self.assertEqual(
            json.loads(self.docker_settings().read_text()),
            {"UseVirtualizationFrameworkRosetta": False, "DisplayedOnboarding": True},
        )
        self.assertIn(
            "Docker Desktop 4.96.0 for arm64, macOS 14.0+: matches arm64, macOS 26.7.1",
            result.stderr,
        )
        self.assertNotIn("press Enter", result.tty)
        self.assertEqual(self.state()["docker_license"], "accepted")

    def test_docker_cli_not_in_usr_local_bin(self):
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertNotIn("[session.hosts.docker]", self.config().read_text())
        Path(self.root, "usr/local/bin/docker").unlink()
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertTrue(
            self.config()
            .read_text()
            .endswith(f'[session.hosts.docker]\ndocker = "{self.root}/opt/homebrew/bin/docker"\n')
        )
        self.assertNotIn("[session.hosts.docker] docker", self.again().tty)

    def test_docker_settings_kept(self):
        self.docker_settings().parent.mkdir(parents=True)
        self.docker_settings().write_text(
            '{"UseVirtualizationFrameworkRosetta": true, "Theme": "dark"}'
        )
        self.assert_ok(self.fresh(clients="claude", docker="y"))
        self.assertEqual(
            json.loads(self.docker_settings().read_text()),
            {
                "UseVirtualizationFrameworkRosetta": True,
                "Theme": "dark",
                "DisplayedOnboarding": True,
            },
        )

    def docker_settings(self):
        return Path(self.home, "Library/Group Containers/group.com.docker/settings-store.json")

    def assert_docker_build_refused(self, marker, text, said):
        (self.fakes / marker).write_text(text)
        result = self.run_install(
            "--docker", "--clients", "claude", answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 2]
        )
        self.assertEqual(result.code, 1)
        self.assertIn(f"install.sh: {said}", result.stderr)
        self.assertNotIn("open -a Docker", self.calls())

    def test_docker_build_for_another_cpu(self):
        said = "Docker Desktop 4.96.0 is built for x86_64, not this Mac's arm64"
        self.assert_docker_build_refused("docker_arch", "x86_64", said)

    def test_docker_build_for_a_newer_macos(self):
        said = "Docker Desktop 4.96.0 needs macOS 27.0 or later, not 26.7.1"
        self.assert_docker_build_refused("docker_min_os", "27.0", said)

    def test_github_app_key_optional(self):
        self.assert_ok(self.fresh(docker="y"))
        result = self.again()
        self.assert_ok(result)
        self.assertNotRegex(result.tty, PEM_Q)
        self.assertIn(
            "skip: no GitHub App key: create the App by docs/docker-host.md#create-the-github-app",
            result.stderr,
        )
        (self.fakes / "keychain").mkdir(exist_ok=True)
        (self.fakes / "keychain/agent-pm-bot").write_text("stored by hand")
        result = self.again(answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertIn("agent-pm-broker login github agent-pm-bot", self.calls())
        self.assertEqual(len(self.state()["login_github"]), 64)
        self.assertEqual(self.state()["github_app"], "agentpm-test-bot")
        self.assertIn('[session.hosts.docker.github] app = "agentpm-test-bot"', result.tty)
        self.assertTrue(
            self.config()
            .read_text()
            .endswith('[session.hosts.docker.github]\napp = "agentpm-test-bot"\n')
        )
        result = self.again()
        self.assert_ok(result)
        self.assertNotIn("login github", "\n".join(self.calls()))

    def test_github_app_key_stored_by_the_broker(self):
        """The broker creates the App key item from the .pem path, so the key is on no command line."""
        pem = Path(self.home, "app.pem")
        secret = "-----BEGIN RSA PRIVATE KEY-----SENTINELKEY-----END RSA PRIVATE KEY-----"
        pem.write_text(secret)
        logins = [(TOKEN_Q, TOKEN), (PEM_Q, str(pem)), (r"App ID \(digits\):", "12 -w x")]
        logins += [(r"App ID \(digits\):", "0123"), (r"App ID \(digits\):", "5352992")]
        result = self.fresh(docker="y", logins=[*logins, (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/agent-pm-bot").read_text(), secret)
        self.assertEqual((self.fakes / "keychain/agent-pm-bot.account").read_text(), "5352992")
        calls = self.calls()
        login = f"agent-pm-broker login github agent-pm-bot --key-file {pem} --app-id 5352992"
        self.assertIn(login, calls)
        self.assertFalse(
            [c for c in calls if c.startswith(("security -i", "security add-", "base64"))]
        )
        self.assertIn("keychain\tagent-pm-bot\t5352992", self.ledger())
        self.assertEqual(self.state()["github_app"], "agentpm-test-bot")
        pem.unlink()
        self.assert_secret_free("SENTINELKEY", TOKEN, result=result)

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
            answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 4],
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
                (MENU_Q, menu(("claude", "opencode"))),
                (PROVIDER_Q, "ollama"),
                (PROVIDER_Q, "lan.1"),
                (PROVIDER_Q, "lan"),
                (URL_Q, 'http://h"/v1'),
                (URL_Q, "http://host/v1"),
                (MODEL_Q, "m"),
                (CONTEXT_Q, "0"),
                (CONTEXT_Q, ""),
                (WRITE_Q, "y"),
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

    def test_setup_token_on_a_cleared_alternate_screen(self):
        """The token setup-token prints and its paste stay on the alternate screen, cleared before leaving it."""
        result = self.fresh(clients="claude", docker="y")
        self.assert_ok(result)
        self.assertEqual((result.tty.count(SCREEN_ON), result.tty.count(SCREEN_OFF)), (1, 1))
        on = result.tty.index(SCREEN_ON)
        off = result.tty.index(SCREEN_OFF)
        self.assertLess(on, off)
        shown = [m.start() for m in re.finditer(re.escape(TOKEN), result.tty)]
        self.assertTrue(shown)
        self.assertTrue(all(on < at < off for at in shown), result.tty)
        self.assertRegex(result.tty[on:off], TOKEN_Q)
        self.assertIn("    setup-token saved in the broker; its screen is cleared\n", result.stderr)
        self.assertEqual((self.fakes / "broker/claude_token").read_text(), TOKEN)
        self.assert_secret_free(TOKEN, result=result)

    def test_setup_token_failure_leaves_the_alternate_screen(self):
        """A failed setup-token or broker login waits for Enter on that screen, then leaves it and stops."""
        failed = "install.sh: broker login: Claude failed; rerun the same command to continue\n"
        self.assert_ok(self.fresh("--no-docker", clients="claude"))
        config = [(WRITE_Q, "y")]
        for call in ("claude setup-token", "agent-pm-broker login claude"):
            with self.subTest(call=call):
                (self.fakes / "fail").write_text(call)
                result = self.again("--docker", answers=[*config, (LEAVE_Q, "")])
                config = []
                self.assertEqual(result.code, 1, result.stderr)
                on = result.tty.index(SCREEN_ON)
                self.assertGreater(result.tty.index(LEAVE_Q.replace("\\", ""), on), on)
                self.assertTrue(result.tty.endswith(SCREEN_OFF), result.tty[-200:])
                self.assertEqual(result.stderr.count(failed), 1, result.stderr)
                self.assertNotIn("setup-token saved in the broker", result.stderr)
                self.assertNotIn("login_claude", self.state())

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
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (WRITE_Q, "y")]
        )
        self.assert_ok(result)
        self.assertIn("1.19.3", (self.fakes / "opencode_version").read_text())

    def opencode_only(self):
        flags = ("--clients", "opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-url", "http://localhost:8080/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        return self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 4]
        )

    def opencode_rerun(self, *answers):
        """A rerun of opencode_only's install: the checklist confirmed, then `answers`."""
        return self.run_install(
            "--password-stdin", stdin=PASSWORD + "\n", answers=[(MENU_Q, ""), *answers]
        )

    def opencode_package(self):
        return Path(
            self.home,
            ".cache/opencode/packages/agent-pm@git+https:/github.com/ophis/agent-pm.git#main",
            "node_modules/agent-pm",
        )

    def test_opencode_only(self):
        result = self.opencode_only()
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn(f"opencode plugin -g -f {SPEC}", calls)
        self.assertIn(
            "curl -fsSL https://raw.githubusercontent.com/ophis/agent-pm/main/broker/internal/launcher/versions.go",
            calls,
        )
        self.assertFalse([c for c in calls if c.startswith(("claude", "codex"))])
        self.assertIn("ctmux --install", calls)
        self.assertIn('[delegate]\nclient = "opencode"\n', self.config().read_text())
        conf = json.loads(Path(self.home, ".config/opencode/opencode.jsonc").read_text())
        self.assertEqual(conf["plugin"], [SPEC])
        self.assertIn(f"    {self.opencode_package()}\n", result.stderr)
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), OLD)
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn("> [ ] Claude Code", result.tty)
        self.assertNotIn("opencode plugin", "\n".join(self.calls()))
        self.assertIn(f"==> OpenCode plugin\n    skip: {SPEC} at {OLD[:12]}\n", result.stderr)

    def test_opencode_rerun_downloads_new_commit(self):
        self.assert_ok(self.opencode_only())
        (self.fakes / "commit").write_text(NEW)
        (self.pkg_src / "NEW").write_text("")
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn(f"opencode plugin -g -f {SPEC}", self.calls())
        self.assertIn(
            f"    {SPEC}: main is at {NEW[:12]}, OpenCode's copy at {OLD[:12]}: downloading it again\n",
            result.stderr,
        )
        package = self.opencode_package()
        self.assertEqual((package / "COMMIT").read_text(), NEW)
        self.assertTrue((package / "NEW").exists())
        self.assertEqual(self.state()["opencode_plugin_commit"], NEW)
        self.assertIn(f"    {package}\n", result.stderr)

    def test_opencode_new_commit_reinstalls_broker(self):
        key_q = r"OpenCode provider lan API key \(hidden\):"
        result = self.fresh(
            clients="opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(PEM_Q, ""), (key_q, KEY)],
        )
        self.assert_ok(result)
        self.assertIn("broker-install ", self.calls())
        (self.fakes / "commit").write_text(NEW)
        self.versions.write_text('package codex\n\nconst MinVersion = "0.170.0"\n')
        result = self.again(answers=[(key_q, KEY)])
        self.assert_ok(result)
        calls = self.calls()
        self.assertIn(f"opencode plugin -g -f {SPEC}", calls)
        self.assertIn("broker-install ", calls)
        self.assertNotIn("ctmux --install", calls)
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), NEW)

    def test_opencode_rerun_without_recorded_commit(self):
        """An install from before install.sh recorded the plugin's commit downloads it once more."""
        self.assert_ok(self.opencode_only())
        state = Path(self.agent_pm, "install/state")
        lines = state.read_text().splitlines(keepends=True)
        state.write_text("".join(x for x in lines if not x.startswith("opencode_plugin_commit=")))
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn(
            f"    {SPEC}: main is at {OLD[:12]}, OpenCode's copy at unknown: downloading it again\n",
            result.stderr,
        )
        self.assertIn(f"opencode plugin -g -f {SPEC}", self.calls())
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)

    def test_opencode_unresolvable_ref_stops(self):
        self.assert_ok(self.opencode_only())
        (self.fakes / "fail").write_text(API_CALL)
        failed = self.opencode_rerun()
        (self.fakes / "commit").write_text("main\n")
        garbled = self.opencode_rerun()
        for result in (failed, garbled):
            self.assertEqual(result.code, 1)
            self.assertIn(
                "install.sh: cannot resolve main on github.com/ophis/agent-pm\n", result.stderr
            )
            self.assertIn(
                "install.sh: OpenCode plugin failed; rerun the same command to continue",
                result.stderr,
            )
        self.assertNotIn("opencode plugin", "\n".join(self.calls()))
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), OLD)
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)

    def test_opencode_failed_download_keeps_no_commit(self):
        self.assert_ok(self.opencode_only())
        (self.fakes / "commit").write_text(NEW)
        (self.fakes / "fail").write_text(f"opencode plugin -g -f {SPEC}")
        result = self.opencode_rerun()
        self.assertEqual(result.code, 1)
        self.assertIn(
            "install.sh: OpenCode plugin failed; rerun the same command to continue", result.stderr
        )
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)
        self.assertFalse(self.opencode_package().exists())
        result = self.opencode_rerun()
        self.assert_ok(result)
        self.assertIn("OpenCode's copy at unknown: downloading it again", result.stderr)
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), NEW)
        self.assertEqual(self.state()["opencode_plugin_commit"], NEW)

    def test_opencode_plugin_declined_keeps_cache(self):
        self.assert_ok(self.opencode_only())
        Path(self.home, ".config/opencode/opencode.jsonc").write_text('{"plugin": []}')
        (self.fakes / "commit").write_text(NEW)
        result = self.opencode_rerun((WRITE_Q, "n"))
        self.assert_ok(result)
        self.assertIn("==> OpenCode plugin\n    skip: declined\n", result.stderr)
        self.assertNotIn("opencode plugin", "\n".join(self.calls()))
        self.assertEqual((self.opencode_package() / "COMMIT").read_text(), OLD)
        self.assertEqual(self.state()["opencode_plugin_commit"], OLD)

    def test_opencode_ref_outside_cache_deletes_nothing(self):
        victim = Path(self.home, ".cache/opencode/victim")
        victim.mkdir(parents=True)
        ref = "../../../../../../victim"
        flags = ("--ref", ref, "--clients", "opencode", "--no-docker", "--opencode-provider", "lan")
        flags += ("--opencode-url", "http://localhost:8080/v1", "--opencode-model", "m")
        flags += ("--opencode-context", "65536", "--opencode-key-stdin")
        result = self.run_install(
            *flags, stdin=KEY + "\n", answers=[(SUDO_Q, PASSWORD), *[(WRITE_Q, "y")] * 2]
        )
        self.assertEqual(result.code, 1)
        spec = f"agent-pm@git+https://github.com/ophis/agent-pm.git#{ref}"
        self.assertIn(f"install.sh: {spec}: not an OpenCode package dir\n", result.stderr)
        self.assertTrue(victim.is_dir())
        calls = "\n".join(self.calls())
        self.assertNotIn("opencode plugin", calls)
        self.assertNotIn("api.github.com", calls)

    def test_docker_without_claude_stages_no_plugins(self):
        """The broker stages plugins from Claude Code's install record; Codex or OpenCode alone has none."""
        result = self.fresh("--clients", "codex", "--docker", logins=[(PEM_Q, "")])
        self.assert_ok(result)
        self.assertIn("[session.hosts.docker]\nplugins = []\n", self.config().read_text())

    def test_cleanup_round_trip_opencode_only(self):
        conf = Path(self.home, ".config/opencode")
        conf.mkdir(parents=True)
        (conf / "opencode.json").write_text('{"theme": "mine"}')
        before = self.snapshot()
        self.assert_ok(self.opencode_only())
        self.assertIn("agent-pm@git+", (conf / "opencode.json").read_text())
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)

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
            answers=[(WRITE_Q, "y"), *PROVIDER_ANSWERS, *[(WRITE_Q, "y")] * 3],
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

    def test_opencode_empty_key_asked_again_or_skipped(self):
        key_q = r"OpenCode provider lan API key \(hidden\):"
        again = r"No lan key stored\. Try again\? \[y/N\]"
        result = self.fresh(
            clients="claude,opencode",
            docker="y",
            opencode=PROVIDER_ANSWERS,
            logins=[(TOKEN_Q, TOKEN), (PEM_Q, ""), (key_q, ""), (again, "y"), (key_q, KEY)],
        )
        self.assert_ok(result)
        self.assertEqual(self.calls().count("agent-pm-broker login opencode lan"), 2)
        self.assertEqual((self.fakes / "keychain/agent-pm-opencode").read_text(), KEY)
        self.assertIn(
            'The first worker that reaches http://10.0.0.5:8080/v1 makes macOS ask "Allow Docker to'
            ' find devices on local networks?": click Allow',
            result.stderr,
        )
        (self.fakes / "keychain/agent-pm-opencode").unlink()
        result = self.again(answers=[(key_q, ""), (again, "n")])
        self.assert_ok(result)
        self.assertIn("skip: no lan key: rerun install.sh, or run", result.stderr)

    def test_opencode_tmux_empty_key_skipped(self):
        result = self.fresh(
            "--no-docker",
            clients="claude,opencode",
            opencode=PROVIDER_ANSWERS,
            logins=[(OPENCODE_LOGIN_Q, "lan"), (r"API key:", ""), (r"Try again\? \[y/N\]", "n")],
        )
        self.assert_ok(result)
        self.assertIn("skip: no lan key: rerun install.sh, or run", result.stderr)
        self.assertFalse(self.auth_json().exists())

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
                answers=[(SUDO_Q, PASSWORD), *([(WRITE_Q, "y")] * (4 if warned else 1))],
            )
            self.assert_ok(result)
            self.assertEqual("sends the API key in plaintext" in result.stderr, warned, url)

    def test_linear_keys_and_board_jobs(self):
        """A board config completed by hand: only the missing keys are asked, then the jobs installed."""
        result = self.fresh()
        self.assert_ok(result)
        self.assertIn("==> board launchd jobs\n    skip: board not chosen", result.stderr)
        self.config().write_text(self.config().read_text() + "\n" + HAND_CONFIG)
        (self.fakes / "keychain").mkdir(exist_ok=True)
        (self.fakes / "keychain/linear-manager").write_text("k-SENTINEL-manager")
        self.linear()
        result = self.again(
            "--board",
            "linear",
            "--launchd",
            answers=[*self.keys("harness", "researcher", "pm", "engineer"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/linear-harness").read_text(), "k-SENTINEL-harness")
        self.assertEqual((self.fakes / "keychain/linear-pm").read_text(), "k-SENTINEL-pm")
        calls = self.calls()
        self.assertIn("security add-generic-password -s linear-pm -a me+pm@example.com -w", calls)
        self.assertIn("security add-generic-password -s linear-harness -a linear-harness -w", calls)
        self.assertIn("skip: linear-manager in the keychain", result.stderr)
        self.assertIn(
            "==> Linear board configuration\n    skip: has the Linear board configuration",
            result.stderr,
        )
        agents = Path(self.home, "Library/LaunchAgents")
        for job in ("router", "promote"):
            plist = (agents / f"com.ophis.agent-pm.{job}.plist").read_text()
            self.assertIn(
                f"<string>{self.home}/.agent-pm/bin/agent-pm-orchestrator</string>", plist
            )
            self.assertIn(f"<string>{self.home}/.agent-pm/logs/orchestrator.jsonl</string>", plist)
            self.assertFalse({"@HOME@", "@PATH@"} & set(re.findall(r"@[A-Z]+@", plist)))
            path = re.search(r"<key>PATH</key>\s*<string>([^<]*)</string>", plist).group(1)
            self.assertEqual(
                path,
                f"{self.bin}:{self.root}/opt/homebrew/bin:{self.agent_pm}/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            )
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

    def test_board_keys_stored_and_verified(self):
        self.linear()
        result = self.fresh("--board", "linear", "--launchd", then=[*self.keys(*SLOTS), *BOARD])
        self.assert_ok(result)
        for slot in SLOTS:
            self.assertEqual(
                (self.fakes / f"keychain/linear-{slot}").read_text(), f"k-SENTINEL-{slot}"
            )
        calls = self.calls()
        self.assertIn("security add-generic-password -s linear-pm -a linear-pm -w", calls)
        self.assertIn(
            "Linear key linear-pm (pm account): paste its API key at security's prompt (nothing echoes)",
            result.stderr,
        )
        viewers = [c for c in calls if c.startswith("curl -q ") and "viewer" in c]
        self.assertEqual(len(viewers), 2 * len(SLOTS))  # each new key's check, then the plan's
        self.assertIn("keychain\tlinear-pm\tlinear-pm", self.ledger())
        self.assert_secret_free("k-SENTINEL", result=result)
        self.assertNotIn("k-SENTINEL", result.tty)

    def test_board_refused_key_is_deleted(self):
        self.linear()
        result = self.fresh(
            "--board",
            "linear",
            "--launchd",
            then=[*self.keys("harness", "manager"), (KEY_Q, "k-SENTINEL-wrong")],
        )
        self.assertNotEqual(result.code, 0)
        self.assertIn(
            "Linear refused the key in linear-researcher, or cannot be reached: rerun to paste it again",
            result.stderr,
        )
        self.assertFalse((self.fakes / "keychain/linear-researcher").exists())
        self.assertTrue((self.fakes / "keychain/linear-manager").exists())
        self.assert_secret_free("k-SENTINEL", result=result)
        self.assertNotIn("k-SENTINEL", result.tty)

    def assert_no_linear(self, result, *, keychain=True):
        self.assertNotRegex(result.tty, KEY_Q)
        self.assertFalse([c for c in self.calls() if c.startswith("curl -q")])
        if keychain:
            self.assertFalse(
                [c for c in self.calls() if c.startswith("security") and "linear" in c]
            )

    def assert_jobs(self, installed):
        for job in ("router", "promote"):
            target = Path(self.home, f"Library/LaunchAgents/com.ophis.agent-pm.{job}.plist")
            self.assertEqual(target.exists(), installed)
            bootstraps = [c for c in self.calls() if c == f"launchctl bootstrap gui/501 {target}"]
            self.assertEqual(bool(bootstraps), installed)

    def test_board_local_with_jobs(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "local"
        )
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        before = self.config().read_text()
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.config().read_text(), before)
        self.assertEqual(
            (self.state()["board_tracker"], self.state()["board_jobs"]), ("local", "yes")
        )
        self.assertNotRegex(result.tty, WRITE_Q)
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        self.assert_no_linear(result)

    def test_board_local_without_jobs(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--no-launchd",
            then=[(WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "local"
        )
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.state()["board_jobs"], "no")
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_local_tracker_inferred(self):
        self.assert_ok(self.fresh())
        self.config().write_text(
            self.config().read_text() + '\n[orchestrator]\ntracker = "local"\n'
        )
        state = Path(self.agent_pm, "install/state")
        state.write_text(state.read_text().replace("board=no", "board=yes"))
        result = self.again("--launchd", answers=[(WRITE_Q, "y")])
        self.assert_ok(result)
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        self.assertEqual(self.state()["board_tracker"], "local")
        self.assertNotIn("Board tracker [local/linear]", result.tty)
        state.write_text(
            "\n".join(
                line
                for line in state.read_text().splitlines()
                if not line.startswith("board_jobs=")
            )
            + "\n"
        )
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.state()["board_jobs"], "yes")
        self.assert_no_linear(result)

    def test_board_switch_from_linear_to_local(self):
        result = self.configured(HAND_CONFIG, board="local", then=[(WRITE_Q, "y"), (WRITE_Q, "y")])
        self.assert_ok(result)
        data = tomllib.loads(self.config().read_text())["orchestrator"]
        self.assertEqual(data["tracker"], "local")
        self.assertEqual(data["linear"]["team"], uuid(1))
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)
        before = self.config().read_text()
        result = self.again("--no-board", "--no-launchd")
        self.assert_ok(result)
        self.assertEqual(self.config().read_text(), before)
        self.assert_no_linear(result)
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        for job in ("router", "promote"):
            self.assertTrue(
                Path(self.home, f"Library/LaunchAgents/com.ophis.agent-pm.{job}.plist").exists()
            )

    def test_board_local_declined(self):
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "local",
            "--launchd",
            then=[(WRITE_Q, "n")],
        )
        self.assert_ok(result)
        self.assertNotIn("orchestrator", tomllib.loads(self.config().read_text()))
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)
        result = self.again(answers=[(WRITE_Q, "y"), (WRITE_Q, "y")])
        self.assert_ok(result)
        self.assert_jobs(installed=True)
        self.assert_no_linear(result)

    def test_board_switch_from_local_to_linear(self):
        self.linear()
        result = self.configured(
            HAND_CONFIG.replace('tracker = "linear"', 'tracker = "local"'),
            then=[*self.keys(*SLOTS), (PROJECT_Q, "ophis/widget"), (WRITE_Q, "y"), (WRITE_Q, "y")],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "linear"
        )
        orchestrator_config.load()
        self.assert_jobs(installed=True)
        self.assert_secret_free("k-SENTINEL", result=result)

    def test_board_linear_without_jobs(self):
        self.linear()
        result = self.fresh(
            "--clients",
            "claude",
            "--no-docker",
            "--board",
            "linear",
            "--no-launchd",
            then=[*self.keys(*SLOTS), *BOARD[:-1]],
        )
        self.assert_ok(result)
        self.assertEqual(
            tomllib.loads(self.config().read_text())["orchestrator"]["tracker"], "linear"
        )
        orchestrator_config.load()
        self.assert_jobs(installed=False)
        self.assert_secret_free("k-SENTINEL", result=result)
        result = self.again()
        self.assert_ok(result)
        self.assert_jobs(installed=False)
        self.assert_no_linear(result, keychain=False)

    def test_board_none_leaves_tracker_and_jobs_alone(self):
        self.assert_ok(self.fresh("--clients", "claude", "--no-docker", "--no-board"))
        self.assertNotIn("orchestrator", tomllib.loads(self.config().read_text()))
        self.assert_jobs(installed=False)
        result = self.again()
        self.assert_ok(result)
        self.assertEqual(self.state()["board"], "no")
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_interactive_tracker_and_jobs(self):
        result = self.run_install(
            answers=[
                (SUDO_Q, PASSWORD),
                (WRITE_Q, "y"),
                (MENU_Q, menu(("claude", "codex", "board"))),
                (r"Board tracker \[local/linear\] \[local\]:", "invalid"),
                (r"Board tracker \[local/linear\] \[local\]:", ""),
                (r"Install board launchd jobs\? \[y/N\]", "n"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
                (WRITE_Q, "y"),
            ]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.state()["board_tracker"], self.state()["board_jobs"]), ("local", "no")
        )
        self.assert_jobs(installed=False)
        self.assert_no_linear(result)

    def test_board_flags_reject_invalid_choices(self):
        for args in (
            ("--board",),
            ("--board", "yes"),
            ("--launchd", "--no-launchd"),
            ("--board", "local", "--no-board"),
        ):
            result = self.run_install(*args, tty=False)
            self.assertEqual(result.code, 2, args)
            self.assertEqual(self.calls(), [])

    def test_board_custom_workflow_roles(self):
        self.linear(keys={**LINEAR["keys"], "k-SENTINEL-reviewer": "reviewer@example.com"})
        Path(self.home, "wf.toml").write_text(
            "".join(
                f'[[stage]]\nrole = "{role}"\ninput = "build"\n\n'
                for role in ("researcher", "engineer", "reviewer")
            )
        )
        slots = ("harness", "manager", "researcher", "engineer", "reviewer")
        board = [(PROJECT_Q, ""), (WRITE_Q, "y"), (WRITE_Q, "y")]  # no document role: no docs repo
        result = self.configured(
            '[orchestrator]\nworkflow = "~/wf.toml"\n', then=[*self.keys(*slots), *board]
        )
        self.assert_ok(result)
        self.assertEqual(
            (self.fakes / "keychain/linear-reviewer").read_text(), "k-SENTINEL-reviewer"
        )
        self.assertFalse([c for c in self.calls() if "linear-pm" in c])
        roles = self.board_config()["roles"]
        self.assertEqual(sorted(roles), ["engineer", "researcher", "reviewer"])
        self.assertEqual(
            roles["reviewer"], {"account": "reviewer@example.com", "key": "linear-reviewer"}
        )

    def board_config(self):
        return tomllib.loads(self.config().read_text())["orchestrator"]["linear"]

    def assert_board_stopped(self, result, said):
        self.assertNotEqual(result.code, 0)
        self.assertIn(f"install.sh: {said}", result.stderr)
        self.assertNotIn("[orchestrator", self.config().read_text())
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])
        self.assert_secret_free("k-SENTINEL", result=result)

    def test_board_fresh_install(self):
        """Keys, the config from Linear (shown before writing) and the jobs; a rerun redoes none of it."""
        self.linear(labels=[*LINEAR["labels"], label(46, "design", 40)])
        result = self.fresh("--board", "linear", "--launchd", then=[*self.keys(*SLOTS), *BOARD])
        self.assert_ok(result)
        data = tomllib.loads(self.config().read_text())
        self.assertEqual(data["orchestrator"]["tracker"], "linear")
        linear = data["orchestrator"]["linear"]
        self.assertEqual(linear["team"], uuid(1))
        self.assertEqual(linear["states"], {k: uuid(10 + i) for i, k in enumerate(STATE_KEYS)})
        self.assertEqual(linear["task_label_group"], uuid(40))
        self.assertEqual(linear["task_labels"], {"build": uuid(41), "light-build": uuid(42)})
        self.assertEqual(linear["agent_label_group"], uuid(43))
        self.assertEqual(linear["agent_labels"], {"claude": uuid(44)})
        self.assertEqual(
            (linear["harness_key"], linear["manager_key"]), ("linear-harness", "linear-manager")
        )
        self.assertEqual(linear["human_members"], ["me@example.com"])
        self.assertEqual(
            linear["roles"],
            {r: {"account": f"{r}@example.com", "key": f"linear-{r}"} for r in SLOTS[2:]},
        )
        self.assertEqual(linear["project_repos"], {uuid(30): "ophis/widget"})
        self.assertIn(
            "    Tasks label design: not a task of the board's roles; left out", result.stderr
        )
        outputs = {r: data["delegate"]["roles"][r]["output"] for r in ("researcher", "pm")}
        docs = {"type": "github", "repo": "ophis/docs", "branch": "main"}
        self.assertEqual(
            outputs,
            {"researcher": {**docs, "dir": "Research/"}, "pm": {**docs, "dir": "Product Design/"}},
        )
        orchestrator_config.load()  # the router's config load accepts it (HOME is this test's)
        shown = result.tty.index('[orchestrator.linear] team = "' + uuid(1) + '"')
        self.assertLess(shown, result.tty.index("Library/LaunchAgents:"))
        agents = Path(self.home, "Library/LaunchAgents")
        for job in ("router", "promote"):
            self.assertTrue((agents / f"com.ophis.agent-pm.{job}.plist").is_file())
            self.assertIn(
                f"launchctl bootstrap gui/501 {agents}/com.ophis.agent-pm.{job}.plist", self.calls()
            )
        self.assert_secret_free("k-SENTINEL", result=result)
        self.assertNotIn("k-SENTINEL", result.tty)
        config = self.config().read_text()
        result = self.again()
        self.assert_ok(result)
        for slot in SLOTS:
            self.assertIn(f"skip: linear-{slot} in the keychain", result.stderr)
        for said in ("has the Linear board configuration", "router installed", "promote installed"):
            self.assertIn(f"skip: {said}", result.stderr)
        changing = ("curl -q", "security add", "launchctl")
        self.assertFalse([c for c in self.calls() if c.startswith(changing)])
        self.assertEqual(self.config().read_text(), config)

    def test_board_config_declined(self):
        self.linear()
        board = [*self.keys(*SLOTS), (PROJECT_Q, ""), *DOCS, (WRITE_Q, "n")]
        result = self.fresh("--board", "linear", "--launchd", then=board)
        self.assert_ok(result)
        self.assertIn("skip: declined", result.stderr)
        self.assertNotIn("[orchestrator", self.config().read_text())
        self.assertIn(
            "==> board launchd jobs\n    skip: board tracker not configured", result.stderr
        )
        self.assertFalse([c for c in self.calls() if c.startswith("launchctl")])

    def test_board_fills_only_missing(self):
        self.linear()
        mine = (
            f'[orchestrator.linear]\nteam = "{uuid(1)}"\nharness_key = "my-harness"\n\n'
            f'[orchestrator.linear.task_labels]\nbuild = "{uuid(99)}"\n'
        )
        result = self.configured(mine, then=[*self.keys(*SLOTS), *BOARD])
        self.assert_ok(result)
        self.assertEqual((self.fakes / "keychain/my-harness").read_text(), "k-SENTINEL-harness")
        linear = self.board_config()
        self.assertEqual((linear["team"], linear["harness_key"]), (uuid(1), "my-harness"))
        self.assertEqual(linear["task_labels"], {"build": uuid(99), "light-build": uuid(42)})
        self.assertFalse([c for c in self.calls() if "teams(" in c])

    def test_board_docs_repo_reused(self):
        self.linear()
        mine = '[delegate.roles.pm.output]\ntype = "github"\nrepo = "me/docs"\nbranch = "trunk"\ndir = "PRD/"\n'
        board = [(PROJECT_Q, ""), (WRITE_Q, "y"), (WRITE_Q, "y")]
        result = self.configured(mine, then=[*self.keys(*SLOTS), *board])
        self.assert_ok(result)
        roles = tomllib.loads(self.config().read_text())["delegate"]["roles"]
        self.assertEqual(roles["pm"]["output"]["dir"], "PRD/")
        self.assertEqual(
            roles["researcher"]["output"],
            {"type": "github", "repo": "me/docs", "branch": "trunk", "dir": "Research/"},
        )
        self.assertNotRegex(result.tty, DOCS_Q)

    def test_board_team_asked_among_several(self):
        ops = {**LINEAR["teams"][0], "id": uuid(2), "key": "OPS", "projects": []}
        self.linear(teams=[LINEAR["teams"][0], ops])
        team_q = r"Linear team for the board \(ENG, OPS\):"
        board = [(team_q, "NOPE"), (team_q, "OPS"), *DOCS, (WRITE_Q, "y"), (WRITE_Q, "y")]
        result = self.fresh("--board", "linear", "--launchd", then=[*self.keys(*SLOTS), *board])
        self.assert_ok(result)
        self.assertEqual(self.board_config()["team"], uuid(2))

    def test_board_missing_state_stops(self):
        team = LINEAR["teams"][0]
        states = [s for s in team["states"] if s["name"] != "Handoff"]
        self.linear(teams=[{**team, "states": states}])
        result = self.fresh("--board", "linear", "--launchd", then=self.keys(*SLOTS))
        self.assert_board_stopped(
            result, "Linear team ENG lacks states: Handoff (docs/credentials.md#linear-presets)"
        )

    def test_board_without_tasks_group_stops(self):
        self.linear(labels=[x for x in LINEAR["labels"] if x["name"] != "Tasks"])
        result = self.fresh("--board", "linear", "--launchd", then=self.keys(*SLOTS))
        self.assert_board_stopped(
            result,
            "Linear team ENG has no Tasks label group (docs/credentials.md#linear-presets)",
        )

    def test_board_same_account_stops(self):
        self.linear(keys={**LINEAR["keys"], "k-SENTINEL-pm": "researcher@example.com"})
        result = self.fresh("--board", "linear", "--launchd", then=self.keys(*SLOTS))
        self.assert_board_stopped(
            result, "the keys in linear-researcher and linear-pm are the same Linear account"
        )

    def test_board_existing_bad_key_kept(self):
        (self.fakes / "keychain").mkdir()
        (self.fakes / "keychain/linear-harness").write_text("k-SENTINEL-stale")
        self.linear()
        result = self.fresh("--board", "linear", "--launchd", then=self.keys(*SLOTS[1:]))
        self.assert_board_stopped(
            result,
            "Linear refused the key in linear-harness, or cannot be reached: fix that keychain item, then rerun",
        )
        self.assertEqual((self.fakes / "keychain/linear-harness").read_text(), "k-SENTINEL-stale")

    def test_board_hostile_linear_names_stop(self):
        """Linear's strings are checked before they reach the terminal or the config."""
        team = LINEAR["teams"][0]
        hostile = "W\x1b]52;c;aGk=\x07"
        self.linear(teams=[{**team, "projects": [{"id": uuid(30), "name": hostile}]}])
        result = self.fresh("--board", "linear", "--launchd", then=self.keys(*SLOTS))
        self.assert_board_stopped(result, f"Linear returned an invalid project name ({uuid(30)})")
        members = [*team["members"], {"email": "x\x1b[2J@example.com", "active": True}]
        self.linear(teams=[{**team, "members": members}])
        again = self.again()
        self.assert_board_stopped(again, "Linear returned an invalid member email")
        for text in (result.stdout, result.stderr, result.tty, again.stderr, again.tty):
            self.assertNotIn("\x1b]52", text)
            self.assertNotIn("\x1b[2J@", text)

    def test_board_quoted_label_written(self):
        quoted, astral = 'a" = "b', "\U0001f916 bot"
        labels = [*LINEAR["labels"], label(46, quoted, 43), label(47, astral, 43)]
        self.linear(labels=labels)
        result = self.fresh("--board", "linear", "--launchd", then=[*self.keys(*SLOTS), *BOARD])
        self.assert_ok(result)
        agents = self.board_config()["agent_labels"]
        self.assertEqual((agents[quoted], agents[astral]), (uuid(46), uuid(47)))

    def test_board_unreadable_workflow_stops(self):
        result = self.configured('[orchestrator]\nworkflow = "~/missing.toml"\n')
        self.assertNotEqual(result.code, 0)
        self.assertIn(
            f"install.sh: {self.home}/missing.toml: cannot read its [[stage]] roles", result.stderr
        )
        self.assertFalse([c for c in self.calls() if c.startswith(("security add", "launchctl"))])

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
                f"dir\t{h}/.config",
                f"dir\t{h}/.config/iterm2",
                *(f"dir\t{h}/Library/{path}" for path in ITERM_DIRS),
                *(f"file\t{h}/Library/{path}" for path in ITERM_FILES),
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
            answers=[(SUDO_Q, PASSWORD), (WRITE_Q, "y"), (MENU_Q, ""), (WRITE_Q, "y")],
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

    def cleanup(self, *args, answers=(), stdin=None, ctty=None):
        """Without --password-stdin, --yes runs answer sudo's prompt."""
        if stdin is None and "--yes" in args:
            answers = [(SUDO_Q, PASSWORD), *answers]
        return self.run_install(*args, answers=answers, stdin=stdin, script=CLEANUP, ctty=ctty)

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

    def test_cleanup_logs_claude_out_on_a_kqueue_refused_tty(self):
        """claude auth logout dies on /dev/tty, as macOS's kqueue refuses it; the terminal's own device works."""
        self.assert_ok(self.fresh("--no-docker", clients="claude"))
        self.assertTrue((self.fakes / "login/claude").exists())
        result = self.cleanup("--yes", ctty=self.refused_tty())
        self.assert_ok(result)
        self.assertIn("claude auth logout", self.calls())
        self.assertNotIn("claude is still logged in", result.stderr)
        self.assertFalse((self.fakes / "login/claude").exists())
        self.assertIn("Removed what install.sh added.", result.stderr)

    def test_cleanup_round_trip(self):
        before = self.snapshot()
        first = self.full()
        self.assert_ok(first)
        calls, ledger, files = self.run_calls(), self.ledger(), self.snapshot()
        result = self.cleanup("--yes", "--password-stdin", stdin=PASSWORD + "\n")
        self.assert_ok(result)
        self.assertNotIn("docker-uninstall", self.calls())
        self.assertIn("brew uninstall --cask docker-desktop", self.calls())
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

    def test_cleanup_round_trip_leaves_no_docker_files(self):
        """Docker Desktop's and iTerm2's own files go; a shell rc there before keeps its own lines."""
        bashrc = Path(self.home, ".bashrc")
        bashrc.write_text("export EDITOR=vim\n")
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        library = Path(self.home, "Library")
        written = [
            library / "Application Support/Docker Desktop/state.json",
            library / "Preferences/com.docker.docker.plist",
            Path(self.home, ".zshrc"),
        ]
        self.assertTrue(all(path.exists() for path in written))
        self.assertIn("by Docker Desktop", bashrc.read_text())
        for path in ("Preferences/com.googlecode.iterm2.plist", "Application Support/iTerm2/x"):
            (library / path).parent.mkdir(parents=True, exist_ok=True)
            (library / path).write_text("iTerm2")
        self.assertIn(f"profile-file\t{self.home}/.zshrc", self.ledger())
        self.assertIn(f"docker-block\t{bashrc}", self.ledger())
        result = self.cleanup("--yes")
        self.assert_ok(result)
        for path in [*written, library / "Preferences/com.googlecode.iterm2.plist"]:
            self.assertFalse(path.exists(), path)
        self.assertFalse((library / "Application Support/Docker Desktop").exists())
        self.assertFalse((library / "Application Support/iTerm2").exists())
        self.assertEqual(bashrc.read_text(), "export EDITOR=vim\n")
        self.assertIn("defaults delete com.docker.docker", self.calls())
        self.assertIn("defaults delete com.googlecode.iterm2", self.calls())

    def test_cleanup_stops_docker_processes(self):
        """What still runs from Docker.app after its cask is gone ends; nothing else is signalled."""
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        agent = (
            f"{self.root}/Applications/Docker.app/Contents/Resources/cli-plugins/docker-agent serve"
        )
        others = [f"{self.root}/Applications/DockerXapp/Contents/MacOS/x", f"/bin/sh {agent}"]
        processes = self.fakes / "processes"
        self.assertEqual(processes.read_text().splitlines(), [agent])
        processes.write_text("".join(f"{line}\n" for line in [agent, *others]))
        result = self.cleanup("--yes")
        self.assert_ok(result)
        self.assertEqual(processes.read_text().splitlines(), others)
        calls = self.calls()
        kill = next(c for c in calls if c.startswith("pkill -TERM -f "))
        self.assertLess(calls.index("brew uninstall --cask docker-desktop"), calls.index(kill))
        self.assertNotIn("pkill -KILL", "\n".join(calls))

    def test_cleanup_fails_on_an_unkillable_docker_process(self):
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        (self.fakes / "unkillable").write_text((self.fakes / "processes").read_text())
        result = self.cleanup("--yes")
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            "install-cleanup.sh: Docker Desktop processes still running: 1\n", result.stderr
        )
        self.assertIn("pkill -KILL", "\n".join(self.calls()))
        self.assertIn("cask\tdocker-desktop", self.ledger())

    def test_cleanup_needs_full_disk_access_for_a_container(self):
        """macOS lets only Full Disk Access remove an app container: without it nothing is removed."""
        self.assert_ok(self.fresh("--docker", logins=[(TOKEN_Q, TOKEN), (PEM_Q, "")]))
        container = Path(self.home, "Library/Containers/com.docker.docker")
        (container / "locked").mkdir(parents=True)
        (container / "locked/data").write_text("")
        ledger = Path(self.agent_pm, "install/added")
        recorded = ledger.read_bytes()
        result = self.cleanup()
        self.assertEqual(result.code, 1, result.stderr)
        self.assertEqual(
            result.stderr,
            f"install-cleanup.sh: macOS lets only Full Disk Access remove {container}: give the"
            " terminal Full Disk Access (System Settings › Privacy & Security), reopen it, then"
            " rerun; nothing removed (take the access back once cleanup has finished)\n",
        )
        self.assertNotRegex(result.tty, REMOVE_Q)
        self.assertEqual(ledger.read_bytes(), recorded)
        self.assertTrue((container / "locked/data").exists())
        self.assertEqual([c for c in self.calls() if not c.startswith(("uname", "ps "))], [])
        tcc = Path(self.home, "Library/Application Support/com.apple.TCC/TCC.db")
        tcc.parent.mkdir(parents=True)
        tcc.write_text("granted")
        locked = container / "locked"
        locked.chmod(0o500)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o700))
        result = self.cleanup(answers=[(REMOVE_Q, "y"), (SUDO_Q, PASSWORD)])
        self.assertEqual(result.code, 1, result.stderr)
        self.assertIn(
            f"install-cleanup.sh: cannot remove {container}; its record stays", result.stderr
        )
        self.assertIn(f"dir\t{container}", ledger.read_text().splitlines())
        self.assertIn("docker-uninstall", self.calls())
        self.assertLess(
            self.calls().index("docker-uninstall"),
            self.calls().index("brew uninstall --cask docker-desktop"),
        )
        locked.chmod(0o700)
        result = self.cleanup(answers=[(REMOVE_Q, "y"), (SUDO_Q, PASSWORD)])
        self.assert_ok(result)
        self.assertTrue(result.stderr.endswith("Removed what install.sh added.\n"), result.stderr)
        self.assertFalse(container.exists())
        self.assertFalse(Path(self.agent_pm, "install").exists())
        self.assertFalse(Path(self.home, ".zprofile").exists())

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
        Path(self.home, ".zshrc").write_text(
            "# The following lines have been added by Docker Desktop to enable Docker CLI"
            " completions.\n# End of Docker Desktop section.\n"
        )
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
        self.linear()
        board = [*self.keys(*SLOTS), (PROJECT_Q, ""), *DOCS, (WRITE_Q, "y"), (WRITE_Q, "y")]
        self.assert_ok(self.again("--board", "linear", "--launchd", answers=board))
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
