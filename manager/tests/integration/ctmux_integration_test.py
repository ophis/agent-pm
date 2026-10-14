"""ctmux (manager/skills/tmux/scripts/ctmux): its tmux calls through a fake tmux, its new session on
a private tmux server with the shell under agent-pm-crew hold, and its standalone installed copy.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import uuid

from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held

from manager.tests.case import REPO, Case
from session.tests import live_tmux

CTMUX = os.path.join(REPO, "manager", "skills", "tmux", "scripts", "ctmux")
USAGE = "usage: ctmux <session-name> | ctmux --install"
FAKE = """#!{python} -I
import json, os, sys
with open(os.environ["CTMUX_LOG"], "a") as f:
    f.write(json.dumps([os.getcwd(), os.getpid(), *sys.argv[1:]]) + "\\n")
code = {{"has": "HAS_RC", "start-server": "START_RC", "-CC": "ATTACH_RC"}}.get(sys.argv[1], "")
sys.exit(int(os.environ.get(code, "0")))
"""
PROXY = """#!{python} -I
import json, os, sys
if sys.argv[1:2] == ["-CC"]:
    with open({log!r}, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    sys.exit(0)
os.execv({program!r}, [{program!r}, "-f", os.devnull, *sys.argv[1:]])
"""
SHELL = """#!/bin/sh
printf '%s\\n' "$(pwd -P)" "$*" "$AGENT_PM_MANAGER" "$AGENT_PM_HOLDER_ID" \\
  "$AGENT_PM_HOLDER_GENERATION" > "$0.tmp" && mv "$0.tmp" "$0.log"
exec cat
"""
START = [
    "start-server",
    ";",
    "set",
    "-g",
    "mouse",
    "on",
    ";",
    "set",
    "-g",
    "history-limit",
    "50000",
    ";",
    "set",
    "-s",
    "extended-keys",
    "on",
    ";",
    "set",
    "-s",
    "terminal-features[99]",
    "xterm*:extkeys",
    ";",
    "new",
    "-d",
    "-s",
]
OPTIONS = (
    ("-g", "mouse"),
    ("-g", "history-limit"),
    ("-s", "extended-keys"),
    ("-s", "terminal-features"),
)


def created(name: str, cwd: str, shell: str) -> list[str]:
    hold = ["agent-pm-crew", "hold", f"--manager={name}", f"--session={name}", "--", shell, "-l"]
    return [*START, name, "-c", cwd, *hold]


def executable(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


class FakeTmux(Case):
    def setUp(self):
        self.bin = os.path.join(self.home, "bin")
        self.cwd = os.path.realpath(os.path.join(self.home, "a b'c$d"))
        self.log = os.path.join(self.home, "tmux.log")
        os.mkdir(self.bin)
        os.mkdir(self.cwd)
        executable(os.path.join(self.bin, "tmux"), FAKE.format(python=sys.executable))
        self.dst = os.path.join(self.agent_pm, "bin", "ctmux")
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.home,
            "SHELL": "/bin/zsh",
            "TMUX_TMPDIR": os.path.join(self.home, "tmux"),
            "CTMUX_LOG": self.log,
        }

    def ctmux(self, *args, script=CTMUX, has_rc=1, **codes):
        if os.path.exists(self.log):
            os.unlink(self.log)
        env = {**self.env, "HAS_RC": str(has_rc), **{k: str(v) for k, v in codes.items()}}
        process = subprocess.Popen(
            (script, *args),
            cwd=self.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = process.communicate(timeout=30)
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as f:
                calls = [json.loads(line) for line in f]
        return process, out, err, calls

    def test_a_missing_or_empty_name_is_a_usage_error_without_tmux(self):
        for args in ((), ("",)):
            with self.subTest(args=args):
                process, _, err, calls = self.ctmux(*args)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(USAGE, err)
                self.assertEqual(calls, [])

    def test_an_existing_session_is_only_attached(self):
        _, _, _, calls = self.ctmux("s1", has_rc=0)
        self.assertEqual(
            [call[2:] for call in calls], [["has", "-t", "=s1"], ["-CC", "attach", "-t", "=s1"]]
        )

    def test_an_absent_session_starts_from_home_in_the_callers_directory_with_its_shell_held(self):
        process, _, err, calls = self.ctmux("s1")
        self.assertEqual((process.returncode, err), (0, ""))
        self.assertEqual(
            [call[2:] for call in calls],
            [
                ["has", "-t", "=s1"],
                created("s1", self.cwd, "/bin/zsh"),
                ["-CC", "attach", "-t", "=s1"],
            ],
        )
        self.assertEqual(calls[1][0], self.home)

    def test_with_an_empty_shell_the_held_command_is_bin_sh(self):
        self.env["SHELL"] = ""
        _, _, _, calls = self.ctmux("s1")
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/sh"))

    def test_names_with_spaces_quotes_and_dollars_stay_single_words(self):
        name = "m a'n$x"
        _, _, _, calls = self.ctmux(name)
        self.assertEqual(
            [call[2:] for call in calls],
            [
                ["has", "-t", f"={name}"],
                created(name, self.cwd, "/bin/zsh"),
                ["-CC", "attach", "-t", f"={name}"],
            ],
        )

    def test_only_the_first_argument_names_the_session(self):
        _, _, _, calls = self.ctmux("s1", "extra", "--install")
        self.assertEqual(
            [call[2:] for call in calls],
            [
                ["has", "-t", "=s1"],
                created("s1", self.cwd, "/bin/zsh"),
                ["-CC", "attach", "-t", "=s1"],
            ],
        )

    def test_a_failed_create_still_attaches_and_the_attach_status_wins(self):
        process, _, _, calls = self.ctmux("s1", START_RC=1, ATTACH_RC=7)
        self.assertEqual(process.returncode, 7)
        self.assertEqual([call[2] for call in calls], ["has", "start-server", "-CC"])

    def test_the_attach_replaces_ctmux_and_its_status_is_ctmux_s(self):
        process, _, _, calls = self.ctmux("s1", has_rc=0, ATTACH_RC=5)
        self.assertEqual(process.returncode, 5)
        self.assertEqual(calls[-1][1], process.pid)

    def test_the_script_names_no_runtime(self):
        with open(CTMUX, "rb") as f:
            text = f.read().lower()
        self.assertNotIn(b"claude", text)
        self.assertNotIn(b"codex", text)

    def test_install_copies_exact_bytes_mode_755_and_prints_the_path_without_tmux(self):
        with open(CTMUX, "rb") as f:
            source = f.read()
        for script in (CTMUX, self.dst):
            with self.subTest(script=script):
                process, out, err, calls = self.ctmux("--install", script=script)
                self.assertEqual((process.returncode, out, err), (0, self.dst + "\n", ""))
                self.assertEqual(stat.S_IMODE(os.stat(self.dst).st_mode), 0o755)
                with open(self.dst, "rb") as f:
                    self.assertEqual(f.read(), source)
                self.assertEqual(calls, [])

    def test_the_installed_copy_runs_without_its_source(self):
        source = os.path.join(self.home, "plugin")
        os.mkdir(source)
        copy = shutil.copy2(CTMUX, os.path.join(source, "ctmux"))
        process, out, _, _ = self.ctmux("--install", script=copy)
        self.assertEqual((process.returncode, out), (0, self.dst + "\n"))
        shutil.rmtree(source)
        process, _, err, calls = self.ctmux("s1", script=self.dst)
        self.assertEqual((process.returncode, err), (0, ""))
        self.assertEqual(calls[1][2:], created("s1", self.cwd, "/bin/zsh"))

    def test_an_install_failure_exits_nonzero_without_a_path(self):
        os.rmdir(self.agent_pm)
        with open(self.agent_pm, "w", encoding="utf-8"):
            pass
        process, out, _, calls = self.ctmux("--install")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((out, calls), ("", []))


class PrivateServer(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        tools = os.path.join(self.home, "tools")
        python = os.path.join(self.home, "python")
        self.cwd = os.path.realpath(os.path.join(self.home, "caller dir"))
        for directory in (tools, python, self.cwd):
            os.mkdir(directory)
        os.symlink(sys.executable, os.path.join(python, "python3"))
        self.attach_log = os.path.join(self.home, "attach.log")
        self.proxy = executable(
            os.path.join(tools, "tmux"),
            PROXY.format(python=sys.executable, log=self.attach_log, program=self.server.program),
        )
        self.shell = executable(os.path.join(self.home, "shell"), SHELL)
        self.env = {
            "PATH": os.pathsep.join((tools, os.path.join(REPO, "bin"), python, "/usr/bin", "/bin")),
            "HOME": self.home,
            "SHELL": self.shell,
            "TMUX_TMPDIR": self.server.root,
        }

    def ctmux(self, name):
        return subprocess.run(
            (CTMUX, name),
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def attaches(self):
        with open(self.attach_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def shell_report(self):
        try:
            with open(self.shell + ".log", encoding="utf-8") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return None

    def roster(self, name):
        return Roster(os.path.join(self.agent_pm, "managers", name))

    def show(self, scope, option):
        return self.server.tmux("show-options", scope + "v", option).stdout.strip()

    def state(self):
        sessions = self.server.tmux("list-sessions", "-F", "#{session_name} #{session_path}")
        options = [self.show(scope, option) for scope, option in OPTIONS]
        return sorted(sessions.stdout.splitlines()), options

    def test_a_new_session_holds_the_lease_for_its_shell_in_the_callers_directory(self):
        done = self.ctmux("s1")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches(), [["-CC", "attach", "-t", "=s1"]])
        self.assertEqual(
            [self.show(scope, option) for scope, option in OPTIONS[:3]], ["on", "50000", "on"]
        )
        self.assertIn("xterm*:extkeys", self.show("-s", "terminal-features"))
        cwd, args, manager, holder, generation = live_tmux.wait(
            self.shell_report, what="the held shell's report"
        )
        self.assertEqual((cwd, args, manager, generation), (self.cwd, "-l", "s1", "1"))
        self.assertEqual((str(uuid.UUID(holder)), uuid.UUID(holder).version), (holder, 4))
        with self.assertRaisesRegex(Held, "held by s1"), self.roster("s1").hold(None):
            pass
        self.server.tmux("send-keys", "-t", "=s1:", "C-d")
        live_tmux.wait(lambda: self.free(self.roster("s1")), what="the lease to end with the shell")

    def test_an_existing_session_is_attached_untouched_and_names_match_exactly(self):
        for name in ("s1", "s1-x"):
            subprocess.run(
                (self.proxy, "new-session", "-d", "-s", name, *live_tmux.IDLE),
                cwd=self.home,
                env=self.env,
                timeout=30,
                check=True,
            )
        before = self.state()
        done = self.ctmux("s1")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches(), [["-CC", "attach", "-t", "=s1"]])
        self.assertEqual(self.state(), before)
        self.assertFalse(os.path.exists(os.path.join(self.agent_pm, "managers")))
        done = self.ctmux("s1-")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(self.attaches()[-1], ["-CC", "attach", "-t", "=s1-"])
        self.assertEqual(
            live_tmux.wait(self.shell_report, what="the held shell's report")[2], "s1-"
        )
        names = self.server.tmux("list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertEqual(sorted(names), ["s1", "s1-", "s1-x"])

    @staticmethod
    def free(roster):
        try:
            with roster.hold(None):
                return True
        except Held:
            return False
