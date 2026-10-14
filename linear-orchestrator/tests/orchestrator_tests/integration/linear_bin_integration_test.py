"""bin/agent-pm-linear run by absolute path from a temp cwd, no PYTHONPATH, a minimal PATH."""

import os
import re
import shutil
import subprocess
import sys
import tempfile

from orchestrator_tests.case import REPO, Case

BIN = os.path.join(REPO, "bin", "agent-pm-linear")
MINIMAL = "/usr/bin:/bin"
COMMANDS = [
    "router",
    "promote",
    "prune",
    "status",
    "show",
    "close",
    "attach-command",
    "resume",
    "writeback",
]


def system_python_ok() -> bool:
    try:
        done = subprocess.run(
            ("/usr/bin/python3", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return False
    return done.returncode == 0


class Command(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

    def minimal(self):
        """PATH is exactly /usr/bin:/bin where that python3 is 3.11+ (CI); elsewhere (macOS 3.9) a
        directory holding a python3 link to this interpreter comes first.
        """
        if system_python_ok():
            return MINIMAL
        own = os.path.join(self.tmp, "python")
        os.makedirs(own, exist_ok=True)
        if not os.path.lexists(link := os.path.join(own, "python3")):
            os.symlink(sys.executable, link)
        return f"{own}:{MINIMAL}"

    def run_bin(self, path, *argv, bin_path=BIN):
        return subprocess.run(
            (bin_path, *argv),
            cwd=self.tmp,
            env={"HOME": self.home, "PATH": path},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_help_lists_the_subcommands(self):
        done = self.run_bin(self.minimal(), "--help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(re.search(r"\{([a-z,-]+)\}", done.stdout)[1].split(","), COMMANDS)

    def test_router_help_exits_0(self):
        done = self.run_bin(self.minimal(), "router", "--help")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertIn("agent-pm-linear router", done.stdout)

    def test_it_sets_pythonpath_to_four_roots_and_puts_its_dir_first_on_path(self):
        repo = os.path.join(self.tmp, "repo")
        bin_dir = os.path.join(repo, "bin")
        os.makedirs(bin_dir)
        copy = os.path.join(bin_dir, "agent-pm-linear")
        shutil.copy2(BIN, copy)
        probe = os.path.join(bin_dir, "python3")
        with open(probe, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$PATH" "$PYTHONPATH" "$*"\n')
        os.chmod(probe, 0o755)
        done = self.run_bin(MINIMAL, "status", "TASK-1", bin_path=copy)
        self.assertEqual(done.returncode, 0, done.stderr)
        roots = ":".join(
            os.path.join(repo, p, "src")
            for p in ("utils", "session", "delegate", "linear-orchestrator")
        )
        self.assertEqual(
            done.stdout.splitlines(),
            [f"{bin_dir}:{MINIMAL}", roots, "-P -m agent_pm.linear_orchestrator.cli status TASK-1"],
        )
