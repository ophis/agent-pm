"""Exercise irreversible audit hooks in disposable interpreters before real state is reached."""

import json
import subprocess
import sys
from pathlib import Path

from tests.case import REPO, Case
from utils.tests.sandbox import clean_env

SCRIPT = """import json, os, subprocess, sys
from pathlib import Path
import isolation

root, sandbox, marker = map(Path, sys.argv[1:])
gate = isolation.Gate(root, sandbox)
gate.current = "sentinel.test"
sys.addaudithook(gate.audit)
blocked = []
for event in isolation.DENIED:
    try:
        sys.audit(event)
    except RuntimeError as error:
        assert "sentinel.test" in str(error) and "forbidden " + event in str(error)
        assert "File " in str(error)
        blocked.append(event)
try:
    subprocess.Popen([sys.executable, "-I", "-c", "raise SystemExit(99)"])
except RuntimeError as error:
    assert "forbidden subprocess.Popen" in str(error)
else:
    raise AssertionError("unit process escaped")
try:
    marker.write_text("escape")
except RuntimeError as error:
    assert "write outside temporary directory" in str(error)
else:
    raise AssertionError("unit write escaped")
(sandbox / "owned").write_text("allowed")
gate.unit = False
bad = [
    ("tmux", ["tmux", "ls"], {}),
    ("tmux", ["tmux", "-S", str(marker), "ls"], {}),
    ("tmux", ["tmux", "ls"], {"TMUX_TMPDIR": str(sandbox), "TMUX": str(marker) + ",1,0"}),
    ("git", ["git", "status"], {"GIT_DIR": str(marker)}),
]
for exe, argv, env in bad:
    try:
        sys.audit("subprocess.Popen", exe, argv, None, env)
    except RuntimeError:
        pass
    else:
        raise AssertionError("integration tool escaped")
sys.audit("subprocess.Popen", "tmux", ["tmux", "-S", str(sandbox / "socket"), "ls"], None, {})
sys.audit("subprocess.Popen", "tmux", ["tmux", "ls"], None, {"TMUX_TMPDIR": str(sandbox)})
sys.audit("subprocess.Popen", "git", ["git", "status"], None, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
print(json.dumps(sorted(blocked)))
"""


class SandboxGates(Case):
    def test_hooks_reject_unit_effects_and_unisolated_integration_tools(self):
        sandbox = Path(self.home, "sandbox")
        sandbox.mkdir()
        marker = Path(self.home, "outside")
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", SCRIPT, REPO, str(sandbox), str(marker)],
            env=clean_env(None, str(Path(REPO, ".github"))),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            [
                "os.exec",
                "os.fork",
                "os.forkpty",
                "os.kill",
                "os.killpg",
                "os.posix_spawn",
                "os.system",
                "socket.connect",
                "subprocess.Popen",
            ],
        )
        self.assertFalse(marker.exists())
        self.assertEqual((sandbox / "owned").read_text(), "allowed")
