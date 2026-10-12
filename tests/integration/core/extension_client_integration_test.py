from tests.integration.core import live_tmux

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[3] / "core"
MANAGER = "mgr"
WORKER = "extension_worker"
MARKER = "EXTENSION_SESSION"
TIMEOUT = 60
WORKER_MODULE = """import sys
from .base import Client, WorkerCommand
class WorkerAdapter(Client):
    session_env = {marker!r}
    def worker(self, spec):
        return WorkerCommand([spec.exe or sys.executable, {script!r}, "resume" if spec.resume else "new", spec.sid,
                              spec.name], env={{"EXTENSION_WORKER": spec.name}})
RUNS = WorkerAdapter.runs
"""
SCRIPT = """import json, os, signal, sys, time
LINGER = 1
def log(what):
    with open(os.environ["EXTENSION_LOG"], "a") as f:
        f.write(json.dumps({"what": what, "pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(),
                            "env": {k: os.environ.get(k) for k in ("EXTENSION_SESSION", "EXTENSION_WORKER")}}) + "\\n")
signal.signal(signal.SIGHUP, signal.SIG_IGN)
log("start")
print("ready", sys.argv[3], flush=True)
try:
    for line in sys.stdin:
        print("got", line.strip(), flush=True)
except OSError:
    pass
time.sleep(LINGER)
log("exit")
"""


def copied_core(case) -> Path:
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    root = Path(os.path.realpath(temp.name))
    shutil.copytree(CORE, root / "core")
    return root


class ExtensionClientIntegration(unittest.TestCase):
    def setUp(self):
        self.root = copied_core(self)
        self.core = self.root / "core"
        self.home = self.root / "home"
        self.home.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.env = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(self.core / "src")}
        (self.core / "src" / "clients" / "extension.py").write_text(
            "from .base import Client, Launch\n"
            "class CustomAdapter(Client):\n"
            "    keys = frozenset({'banner'})\n"
            "    def handover(self):\n"
            "        return self.config['banner']\n"
            "    def launch(self, prompt, run, *, params, access):\n"
            "        return Launch(argv=['extension-runner', '--tier', str(run.tier), '--effort', run.effort, prompt],\n"
            "                      cwd=access.cwd)\n"
            "RUNS = CustomAdapter.runs\n"
        )

    def config(self, text):
        with (self.core / "config.toml").open("a") as f:
            f.write("\n" + text)

    def drive(self, client):
        return subprocess.run(
            [
                sys.executable,
                str(self.core / "src" / "drive.py"),
                "--client",
                client,
                "--role",
                "dummy-tester",
                "--task",
                "echo",
                "--input",
                "Hello.",
                "--out",
                str(self.work / "out.md"),
                "--workdir",
                str(self.work),
                "--dry-run",
            ],
            cwd=self.work,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_extension_module_and_config_support_dry_run_capability_catalog_and_resolution(self):
        self.config(
            '[clients.extension]\nimplementation = "CustomAdapter"\ntier = 4\nbanner = "Extension client handover."\n'
        )
        result = self.drive("extension")
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads(result.stdout)
        self.assertEqual(launch["argv"][:5], ["extension-runner", "--tier", "4", "--effort", "low"])
        self.assertIn("Extension client handover.", launch["argv"][5])
        self.assertIn("# Input\n\nHello.", launch["argv"][5])

        script = (
            "import json, sys, clients, compose; "
            "root = sys.argv[1]; client = clients.get('extension', root); "
            "print(json.dumps({'handover': client.handover(), "
            "'tier': compose.resolve(root, client, 'dummy-tester', 'echo').tier, "
            "'catalog': clients.catalog('extension', root)}))"
        )
        api = subprocess.run(
            [sys.executable, "-c", script, str(self.core)],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(api.returncode, 0, api.stderr)
        data = json.loads(api.stdout)
        self.assertEqual(data["handover"], "Extension client handover.")
        self.assertEqual(data["tier"], 4)
        self.assertEqual(list(data["catalog"]), ["researcher", "pm", "engineer", "dummy-tester"])
        self.assertIn("echo", data["catalog"]["dummy-tester"])

    def test_configured_client_without_module_fails_before_launch(self):
        self.config('[clients.absent]\nimplementation = "AbsentClient"\n')
        result = self.drive("absent")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("client 'absent' has no module clients.absent", result.stderr)

    def test_module_without_config_table_fails_before_launch(self):
        result = self.drive("extension")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("no [clients.extension] table", result.stderr)


class ExtensionWorkerIntegration(unittest.TestCase):
    def setUp(self):
        self.root = copied_core(self)
        core = self.root / "core"
        self.crew = str(core / "skills" / "tmux" / "scripts" / "crew.py")
        script = self.root / "worker.py"
        script.write_text(SCRIPT)
        (core / "src" / "clients" / f"{WORKER}.py").write_text(
            WORKER_MODULE.format(marker=MARKER, script=str(script))
        )
        with (core / "config.toml").open("a") as f:
            f.write(f'\n[clients.{WORKER}]\nimplementation = "WorkerAdapter"\n')
        home = self.root / "home"
        (home / ".agent-pm").mkdir(parents=True)
        self.roster = home / ".agent-pm" / "managers" / MANAGER / "roster.json"
        self.work = str(self.root / "work")
        os.mkdir(self.work)
        self.log = self.root / "log.jsonl"
        self.server = live_tmux.Server(self, str(home))
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        self.ok(self.crew_py("attach"))

    def crew_py(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = self.server.env(**self.server.inside(MANAGER), EXTENSION_LOG=str(self.log), **extra)
        return subprocess.run(
            [sys.executable, self.crew, *args],
            env=env,
            cwd=self.work,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )

    def ok(self, res: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        self.assertEqual(
            res.returncode, 0, f"{res.args}\nstdout: {res.stdout}\nstderr: {res.stderr}"
        )
        return res

    def start(self, *args: str, **extra: str) -> str:
        out = self.ok(
            self.crew_py("start", "w1", "--manager", MANAGER, "--cwd", self.work, *args, **extra)
        ).stdout
        name, _, sid = out.strip().partition(" ")
        self.assertEqual(name, "w1", out)
        return sid

    def logged(self, count: int) -> list[dict]:
        def read():
            entries = (
                [json.loads(line) for line in self.log.read_text().splitlines()]
                if self.log.exists()
                else []
            )
            return entries if len(entries) >= count else None

        return live_tmux.wait(read, what=f"{count} entries in {self.log}")

    def pane_pid(self) -> int:
        return int(
            self.ok(self.server.tmux("display-message", "-p", "-t", "=w1:", "#{pane_pid}")).stdout
        )

    def resume(self) -> list[str]:
        return json.loads(self.roster.read_text())["entries"]["w1"]["resume"]

    def test_client_flag_starts_its_worker_and_a_restart_waits_for_the_old_one(self):
        sid = self.start("--client", WORKER)
        (begun,) = self.logged(1)
        self.assertEqual(
            (begun["what"], begun["argv"], begun["cwd"]), ("start", ["new", sid, "w1"], self.work)
        )
        self.assertEqual(begun["env"], {MARKER: None, "EXTENSION_WORKER": "w1"})
        self.assertEqual(self.pane_pid(), begun["pid"])
        self.assertEqual(
            {k: self.server.option("w1", k) for k in ("@client", "@exe", "@env")},
            {"@client": WORKER, "@exe": sys.executable, "@env": "{}"},
        )
        self.assertEqual(
            self.resume(),
            [
                sys.executable,
                self.crew,
                "start",
                "w1",
                "--manager",
                MANAGER,
                "--client",
                WORKER,
                "--resume",
                sid,
                "--cwd",
                self.work,
            ],
        )
        self.ok(self.crew_py("restart", "w1"))
        _, ended, resumed = self.logged(3)
        self.assertEqual((ended["what"], ended["pid"]), ("exit", begun["pid"]))
        self.assertEqual((resumed["what"], resumed["argv"]), ("start", ["resume", sid, "w1"]))
        self.assertEqual(self.pane_pid(), resumed["pid"])

    def test_its_session_marker_makes_it_the_default(self):
        sid = self.start(**{MARKER: "1"})
        (begun,) = self.logged(1)
        self.assertEqual(begun["env"], {MARKER: None, "EXTENSION_WORKER": "w1"})
        self.assertEqual(self.pane_pid(), begun["pid"])
        self.assertEqual(self.server.option("w1", "@client"), WORKER)
        self.assertEqual(self.resume()[6:10], ["--client", WORKER, "--resume", sid])


if __name__ == "__main__":
    unittest.main()
