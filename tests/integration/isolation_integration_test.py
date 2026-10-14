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
workspace = marker.parent / "workspace"
workspace.mkdir()
gate = isolation.Gate(root, sandbox, workspace)
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
(workspace / "owned").write_text("allowed")
summary = marker.parent / "summary"
with gate.reporting(summary):
    summary.write_text("reported")
    try:
        sys.audit("os.kill")
    except RuntimeError:
        pass
    else:
        raise AssertionError("reporting bypassed the process gate")
try:
    marker.write_text("escape")
except RuntimeError:
    pass
else:
    raise AssertionError("reporting bypassed the write gate")
gate.unit = False
bad = [
    ("tmux", ["tmux", "ls"], {}),
    ("tmux", ["tmux", "-S", str(marker), "ls"], {}),
    ("tmux", ["tmux", "-S" + str(marker), "ls"], {"TMUX_TMPDIR": str(sandbox)}),
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
sys.audit("subprocess.Popen", "tmux", ["tmux", "capture-pane", "-p", "-S", "-40"], None, {"TMUX_TMPDIR": str(sandbox)})
sys.audit("subprocess.Popen", "git", ["git", "status"], None, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
print(json.dumps(sorted(blocked)))
"""


class SandboxGates(Case):
    def test_caught_and_background_violations_still_fail_at_stop_test(self):
        script = """import json, os, sys, threading, unittest
from pathlib import Path
import isolation, run_tests

root = Path(sys.argv[1])
__file__ = str(root / "fixture.py")
sandbox = root / "sandbox"
sandbox.mkdir()
os.environ["AGENT_PM_TEST_SANDBOX"] = str(sandbox)
run_tests.GATE = isolation.install(root)
run_tests.GATE.unit_only = True
def swallow(event):
    try:
        sys.audit(event)
    except Exception:
        pass
class Swallowed(unittest.TestCase):
    def test_discovery(self):
        pass
    def test_caught(self):
        swallow("os.kill")
    def test_background(self):
        thread = threading.Thread(target=swallow, args=("socket.bind",))
        thread.start()
        thread.join()
codes = []
swallow("socket.getaddrinfo")
for method in ("test_discovery", "test_caught", "test_background"):
    codes.append(run_tests.run(unittest.TestSuite([Swallowed(method)]), method))
print(json.dumps(codes))
"""
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", script, self.home],
            env=clean_env(None, REPO, str(Path(REPO, ".github"))),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.splitlines()[-1]), [1, 1, 1])
        self.assertIn("test discovery: forbidden socket.getaddrinfo", proc.stderr)
        self.assertIn("Swallowed.test_caught: forbidden os.kill", proc.stderr)
        self.assertIn("Swallowed.test_background: forbidden socket.bind", proc.stderr)

    def test_unit_selection_keeps_external_testcase_classes_under_the_unit_gate(self):
        root = Path(self.home)
        (root / ".github").mkdir()
        for package in (root / "tests", root / "tests/unit"):
            package.mkdir()
            (package / "__init__.py").touch()
        marker = root / "escaped"
        helper = root / "helper.py"
        helper.write_text(
            "import os, unittest\n"
            "class External(unittest.TestCase):\n"
            "    def test_write(self):\n"
            f"        os.mkdir({str(marker)!r})\n"
        )
        (root / "tests/unit/external_test.py").write_text(
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('helper', {str(helper)!r})\n"
            "helper = importlib.util.module_from_spec(spec)\n"
            "sys.modules['helper'] = helper\n"
            "spec.loader.exec_module(helper)\n"
            "External = helper.External\n"
        )
        env = clean_env(None, REPO, str(Path(REPO, ".github")))
        env.pop("RUN_TESTS_STATE", None)
        proc = subprocess.run(
            [sys.executable, "-P", "-S", str(Path(REPO, ".github/run_tests.py")), "Unit", "--unit"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("helper.External.test_write", proc.stderr)
        self.assertIn("write outside temporary directory", proc.stderr)
        self.assertFalse(marker.exists())

    def test_every_filesystem_mutation_is_confined_to_owned_roots(self):
        script = """import os, shutil, sqlite3, sys
from pathlib import Path
import isolation

root = Path(sys.argv[1])
sandbox = root / "sandbox"
sandbox.mkdir()
outside = root / "outside"
outside.mkdir()
source = outside / "source"
source.write_text("original")
directory = outside / "directory"
directory.mkdir()
owned = sandbox / "owned"
owned.write_text("owned")
fd = os.open(source, os.O_RDWR)
dir_fd = os.open(outside, os.O_RDONLY)
gate = isolation.Gate(root, sandbox)
gate.current = "sentinel.filesystem"
sys.addaudithook(gate.audit)
operations = [
    ("mkdir", lambda: os.mkdir(outside / "new")),
    ("rename source", lambda: os.rename(source, sandbox / "renamed")),
    ("rename destination", lambda: os.rename(owned, outside / "renamed")),
    ("replace", lambda: os.replace(owned, source)),
    ("move", lambda: shutil.move(owned, outside / "moved")),
    ("remove", lambda: os.remove(source)),
    ("rmdir", lambda: os.rmdir(directory)),
    ("rmtree", lambda: shutil.rmtree(directory)),
    ("chmod", lambda: os.chmod(source, 0o777)),
    ("chown", lambda: os.chown(source, os.getuid(), os.getgid())),
    ("symlink destination", lambda: os.symlink(owned, outside / "link")),
    ("symlink source", lambda: os.symlink(source, sandbox / "link")),
    ("link source", lambda: os.link(source, sandbox / "hardlink")),
    ("link destination", lambda: os.link(owned, outside / "hardlink")),
    ("truncate", lambda: os.truncate(source, 0)),
    ("ftruncate", lambda: os.ftruncate(fd, 0)),
    ("utime", lambda: os.utime(source, (0, 0))),
    ("sqlite", lambda: sqlite3.connect(str(outside / "db"))),
    ("sqlite URI", lambda: sqlite3.connect((outside / "db").as_uri(), uri=True)),
    ("mkdir dir_fd", lambda: os.mkdir("new", dir_fd=dir_fd)),
    ("remove dir_fd", lambda: os.remove("source", dir_fd=dir_fd)),
    ("chmod fd", lambda: os.fchmod(fd, 0o777)),
]
for name, operation in operations:
    try:
        operation()
    except RuntimeError as error:
        assert "sentinel.filesystem" in str(error), (name, error)
        assert "write outside temporary directory" in str(error), (name, error)
    else:
        raise AssertionError("escaped " + name)
assert source.read_text() == "original"
assert owned.read_text() == "owned"
assert sorted(p.name for p in outside.iterdir()) == ["directory", "source"]
allowed = sandbox / "allowed"
allowed.mkdir()
file = allowed / "file"
file.write_text("ok")
os.chmod(file, 0o600)
os.chown(file, os.getuid(), os.getgid())
os.utime(file, (1, 1))
os.truncate(file, 1)
os.symlink(file, allowed / "link")
os.link(file, allowed / "hardlink")
os.rename(file, allowed / "renamed")
os.replace(allowed / "renamed", file)
os.remove(allowed / "link")
sqlite3.connect(str(allowed / "db")).close()
sqlite3.connect(":memory:").close()
shutil.rmtree(allowed)
os.close(fd)
os.close(dir_fd)
"""
        proc = subprocess.run(
            [sys.executable, "-P", "-S", "-c", script, self.home],
            env=clean_env(None, str(Path(REPO, ".github"))),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

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
                "socket.bind",
                "socket.connect",
                "socket.getaddrinfo",
                "socket.sendto",
                "subprocess.Popen",
            ],
        )
        self.assertFalse(marker.exists())
        self.assertEqual((sandbox / "owned").read_text(), "allowed")
        self.assertEqual(Path(self.home, "workspace/owned").read_text(), "allowed")
        self.assertEqual(Path(self.home, "summary").read_text(), "reported")

    def test_the_launcher_cleans_inherited_environment_and_python_startup(self):
        root = Path(self.home)
        package = root / "unit"
        package.mkdir()
        (package / "__init__.py").touch()
        marker = root / "startup-ran"
        ambient = root / "ambient"
        ambient.mkdir()
        (ambient / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('escaped')\n"
        )
        (package / "environment_test.py").write_text(
            "import os, sys, unittest\n"
            "class Environment(unittest.TestCase):\n"
            "    def test_clean(self):\n"
            "        self.assertNotIn('TMUX', os.environ)\n"
            "        self.assertNotIn('GIT_DIR', os.environ)\n"
            "        self.assertNotIn('GIT_CONFIG_COUNT', os.environ)\n"
            "        self.assertEqual(os.environ['GIT_CONFIG_GLOBAL'], os.devnull)\n"
            "        self.assertEqual(os.environ['GIT_CONFIG_NOSYSTEM'], '1')\n"
            "        self.assertEqual(os.environ['TUI_ATTACH_PREFIX'], '')\n"
            f"        self.assertNotIn({str(ambient)!r}, sys.path)\n"
            "        self.assertTrue(os.environ['PATH'].split(os.pathsep)[0].endswith('blocked-bin'))\n"
            "        self.assertNotEqual(os.environ['HOME'], os.environ['TMPDIR'])\n"
        )
        env = clean_env(None, REPO, str(Path(REPO, ".github")), str(ambient))
        env.pop("RUN_TESTS_STATE", None)
        env.update(
            TMUX=str(marker) + ",1,0",
            GIT_DIR=str(marker),
            GIT_CONFIG_COUNT="1",
            TUI_ATTACH_PREFIX="ambient",
            RUN_TESTS_DURATIONS=str(root / "durations"),
            GITHUB_STEP_SUMMARY=str(root / "step-summary"),
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-P",
                "-S",
                str(Path(REPO, ".github/run_tests.py")),
                "Unit fixture",
                "unit",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            json.loads((root / "durations").read_text())["test"],
            "unit.environment_test.Environment.test_clean",
        )
