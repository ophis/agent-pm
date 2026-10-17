"""Isolated shipped-runtime fixture installations, with no repository or real cache mutation."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from session.tests import live_tmux
from session.tests.case import REPO, Case
from session.tests.integration.clients.codex import fake_tui
from utils.tests.sandbox import project_roots


def install(destination: Path) -> Path:
    destination.mkdir()
    for source in project_roots(REPO):
        root = Path(source)
        shutil.copytree(
            root,
            destination / root.parent.name / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    shutil.copytree(Path(REPO) / "bin", destination / "bin")
    for folder, _dirs, _files in os.walk(destination):
        os.chmod(folder, os.stat(folder).st_mode | stat.S_IWUSR)
    return destination


class UpgradeCase(Case):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home, self.work = self.root / "home", self.root / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.a, self.b = install(self.root / "A"), install(self.root / "B")
        self.server = live_tmux.Server(self, str(self.home))
        self.server.start("mgr")
        fake_tui.install(self.server.bin)
        self.log, self.scenario, self.gate = (
            self.root / name for name in ("calls", "scenario", "gate")
        )
        self.env = self.server.env(
            FAKE_LOG=str(self.log),
            FAKE_SCENARIO=str(self.scenario),
            CODEX_HOME=str(self.home / ".codex"),
        )
        self.snapshots = self.home / ".agent-pm/installations"

    def cli(self, installation, command, *args):
        result = subprocess.run(
            (str(installation / "bin/agent-pm-session"), command, *map(str, args)),
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def rows(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def hooks(self):
        return [row for row in self.rows() if "hook" in row]
