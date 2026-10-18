import os
import shutil
import subprocess
from pathlib import Path

from tests.case import REPO, Case

BROKER = Path(REPO, "broker")
TOOLS = ("sh", "dirname", "basename", "mkdir", "cp", "mktemp", "rm", "cat", "grep")
SIGN = "codesign --force --sign - --options runtime --identifier com.github.ophis.agent-pm-broker "
FAKE = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$LOG"
case "$(basename "$0") $*" in
  "go build"*) while [ "$#" -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done ;;
  "codesign -dv "*) echo "CodeDirectory v=20500 size=1 $FLAGS hashes=1" >&2 ;;
  "codesign -d --entitlements"*) echo "$ENTITLEMENTS" ;;
esac
"""


class Install(Case):
    def setUp(self):
        self.bin = Path(self.home, "bin")
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.log = Path(self.home, "calls.log")
        self.plugin = Path(self.home, "plugin")
        (self.plugin / "broker/worker").mkdir(parents=True)
        for name in ("install.sh", "build.sh", "with-go-state.sh", "worker/Dockerfile"):
            shutil.copy(BROKER / name, self.plugin / "broker" / name)
        self.env = {
            "HOME": self.home,
            "PATH": str(self.bin),
            "LOG": str(self.log),
            "FLAGS": "flags=0x10002(adhoc,runtime)",
            "ENTITLEMENTS": "Executable=/x",
        }

    def fake(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)

    def run_script(self, name, *args, **env):
        return subprocess.run(
            [str(self.bin / "sh"), str(self.plugin / "broker" / name), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_build_script_builds_and_signs(self):
        self.fake("go", "codesign")
        out = Path(self.home, "out/agent-pm-broker")
        out.parent.mkdir()
        result = self.run_script("build.sh", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                f"go build -trimpath -o {out} .",
                f"{SIGN}{out}",
                f"codesign --verify --strict {out}",
                f"codesign -dv {out}",
                f"codesign -d --entitlements - {out}",
            ],
        )

    def test_build_script_rejects_a_binary_without_the_hardened_runtime(self):
        self.fake("go", "codesign")
        result = self.run_script("build.sh", str(Path(self.home, "b")), FLAGS="flags=0x2(adhoc)")
        self.assertEqual(result.returncode, 1)
        self.assertIn("lacks the hardened runtime", result.stderr)

    def test_build_script_rejects_get_task_allow(self):
        self.fake("go", "codesign")
        result = self.run_script(
            "build.sh", str(Path(self.home, "b")), ENTITLEMENTS="com.apple.security.get-task-allow"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("get-task-allow", result.stderr)

    def test_seeds_definition_builds_signs_and_installs(self):
        self.fake("go", "codesign", "sudo")
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        seeded = Path(self.agent_pm, "docker/worker/Dockerfile")
        self.assertEqual(seeded.read_text(), (BROKER / "worker/Dockerfile").read_text())
        calls = self.calls()
        self.assertEqual([c.split()[0] for c in calls], ["go"] + ["codesign"] * 4 + ["sudo"] * 2)
        self.assertTrue(calls[0].startswith("go build -trimpath -o "))
        self.assertTrue(calls[1].startswith(SIGN))
        self.assertEqual(
            calls[5], "sudo install -d -o root -g wheel -m 0755 /usr/local/libexec/agent-pm"
        )
        self.assertTrue(calls[6].startswith("sudo install -o root -g wheel -m 0755 "))
        self.assertTrue(calls[6].endswith(" /usr/local/libexec/agent-pm/agent-pm-broker"))

    def test_existing_definition_is_never_overwritten(self):
        self.fake("go", "codesign", "sudo")
        worker = Path(self.agent_pm, "docker/worker")
        worker.mkdir(parents=True)
        (worker / "Dockerfile").write_text("FROM mine\n")
        for _ in range(2):
            result = self.run_script("install.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((worker / "Dockerfile").read_text(), "FROM mine\n")
        self.assertEqual(os.listdir(worker), ["Dockerfile"])

    def test_missing_go_fails_naming_go(self):
        self.fake("codesign", "sudo")
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("go is required", result.stderr)
        self.assertEqual(self.calls(), [])
