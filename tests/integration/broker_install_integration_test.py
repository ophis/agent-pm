import os
import shutil
import subprocess
from pathlib import Path

from tests.case import REPO, Case

INSTALL = Path(REPO, "broker/install.sh")
TOOLS = ("sh", "dirname", "mkdir", "cp", "mktemp", "rm", "cat")
FAKE = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$LOG"
if [ "$(basename "$0")" = go ]; then
  while [ "$#" -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done
fi
"""


class Install(Case):
    def setUp(self):
        self.bin = Path(self.home, "bin")
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.log = Path(self.home, "calls.log")

    def fake(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)

    def install(self):
        env = {"HOME": self.home, "PATH": str(self.bin), "LOG": str(self.log)}
        return subprocess.run(
            [str(self.bin / "sh"), str(INSTALL)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_seeds_definition_builds_signs_and_installs(self):
        self.fake("go", "codesign", "sudo")
        (self.bin / "basename").symlink_to(shutil.which("basename"))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        seeded = Path(self.agent_pm, "docker/worker/Dockerfile")
        self.assertEqual(seeded.read_text(), Path(REPO, "broker/worker/Dockerfile").read_text())
        calls = self.log.read_text().splitlines()
        self.assertEqual([c.split()[0] for c in calls], ["go", "codesign", "sudo", "sudo"])
        self.assertTrue(calls[0].startswith("go build -trimpath -o "))
        self.assertIn(
            "codesign --force --sign - --options runtime --identifier com.github.ophis.agent-pm-broker ",
            calls[1],
        )
        self.assertEqual(
            calls[2], "sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/agent-pm"
        )
        self.assertTrue(calls[3].startswith("sudo install -o root -g wheel -m 0755 "))
        self.assertTrue(calls[3].endswith(" /usr/local/libexec/agent-pm/agent-pm-broker"))

    def test_existing_definition_is_never_overwritten(self):
        self.fake("go", "codesign", "sudo")
        (self.bin / "basename").symlink_to(shutil.which("basename"))
        worker = Path(self.agent_pm, "docker/worker")
        worker.mkdir(parents=True)
        (worker / "Dockerfile").write_text("FROM mine\n")
        for _ in range(2):
            result = self.install()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((worker / "Dockerfile").read_text(), "FROM mine\n")
        self.assertEqual(os.listdir(worker), ["Dockerfile"])

    def test_missing_go_fails_naming_go(self):
        self.fake("codesign", "sudo")
        (self.bin / "basename").symlink_to(shutil.which("basename"))
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("go is required", result.stderr)
        self.assertFalse(self.log.exists())
