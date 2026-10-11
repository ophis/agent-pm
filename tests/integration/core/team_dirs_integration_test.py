"""drive.py run as a process with a local `team_dirs` (core/config.toml's comment), fake_claude.py on PATH as `claude`
(fake_codex.py as `codex`): a team dir's role and an extended core role run as core's own, through their task view."""
import json
import os
import shutil
import pwd
import subprocess
import sys
import tempfile
import unittest

from tests import hermetic
from tests.integration.core import fake_claude, fake_codex
from tests.unit.core.drive_test import NOTE, SID, TEAM, outcome, progress
import repo

REAL_HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)   # hermetic points HOME at a temp dir
DRIVE = os.path.join(hermetic.CORE, "src", "drive.py")
DONE = {"status": "done", "title": "note", "summary": "A note.", "deliverable": "A note.\n"}
IN_TEMP = repo.under(REAL_HOME, repo.temp_dirs()) is not None


class Fixture(unittest.TestCase):
    """A team dir (drive_test.TEAM) as the hermetic local config's `team_dirs`, fake_claude.py on PATH."""
    def setUp(self):
        # The team dir lies outside every temp dir (drive.py refuses one there), so under the real HOME.
        self.team = os.path.realpath(tempfile.mkdtemp(dir=REAL_HOME, prefix=".agent-pm-team-test-"))
        self.addCleanup(shutil.rmtree, self.team, ignore_errors=True)
        os.chmod(self.team, 0o755)
        for rel, text in TEAM.items():
            path = os.path.join(self.team, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.chmod(os.path.dirname(path), 0o755)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
        local = hermetic.home(self)
        with open(os.path.join(local, "core.local.toml"), "w") as f:
            f.write(f"team_dirs = {json.dumps([self.team])}\n")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.realpath(tmp.name)
        self.work, bin_dir = os.path.join(root, "work"), os.path.join(root, "bin")
        for d in (self.work, bin_dir):
            os.mkdir(d)
        self.scenario = os.path.join(root, "scenario.json")
        self.env = {"PATH": fake_claude.install(bin_dir), "HOME": os.environ["HOME"], "PYTHONUTF8": "1",
                    fake_claude.ENV: self.scenario}

    def drive(self, *argv, steps=()) -> subprocess.CompletedProcess:
        with open(self.scenario, "w") as f:
            json.dump({"steps": list(steps)}, f)
        return subprocess.run([sys.executable, DRIVE, *argv], cwd=self.work, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def run_argv(self, role, task, *extra):
        return ["--role", role, "--task", task, "--input", "Write X.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, "--sid", SID, *extra]


@unittest.skipIf(IN_TEMP, f"HOME {REAL_HOME} is in a temp dir, where drive.py refuses a team dir")
class TeamDirs(Fixture):
    def test_dry_runs(self):
        for role, task in (("writer", "note"), ("pm", "one-pager")):
            with self.subTest(role=role):
                res = self.drive(*self.run_argv(role, task, "--dry-run"))
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(f"`{os.path.join(self.work, 'tasks')}`", json.loads(res.stdout)["argv"][2])
        self.assertEqual(os.listdir(self.work), [])

    def test_list(self):
        res = self.drive("--list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"\nwriter  local  {os.path.join(self.team, 'roles', 'writer.md')}\n", res.stdout)
        self.assertIn(f"\n  one-pager: a one-page brief; when asked; light.  "
                      f"[{os.path.join(self.team, 'tasks', 'one-pager.md')}]\n", res.stdout)

    def test_a_headless_run_reads_its_task_from_the_view(self):
        res = self.drive(*self.run_argv("writer", "note"),
                         steps=[progress("start", "noting"), {"kind": "read", "path": "tasks/note.md"}, outcome(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("drive.py: status done", res.stderr)
        self.assertIn("[agent-pm-progress:start] the topic", res.stderr)
        with open(os.path.join(self.work, "tasks", "note.md")) as f:
            self.assertEqual(f.read(), NOTE)
        self.assertEqual(sorted(os.listdir(os.path.join(self.work, "tasks"))), [".agent-pm-view", "note.md"])
        with open(os.path.join(self.work, "out.md")) as f:
            self.assertEqual(f.read(), "A note.\n")


@unittest.skipIf(IN_TEMP, f"HOME {REAL_HOME} is in a temp dir, where drive.py refuses a team dir")
class CodexTeamDirs(Fixture):
    """The same team dir through `--client codex`, whose --add-dir dirs it can write: its HOME (its recovery store)
    outside the temp dirs too, as fake_codex_integration_test.py's."""
    def setUp(self):
        super().setUp()
        homes = os.path.join(os.path.dirname(hermetic.CORE), "work", "fake-codex-homes")
        os.makedirs(homes, mode=0o700, exist_ok=True)
        home = tempfile.TemporaryDirectory(prefix="home-", dir=homes)
        self.addCleanup(home.cleanup)
        os.makedirs(os.path.join(home.name, ".agent-pm"))
        shutil.copy(os.path.join(os.environ["HOME"], ".agent-pm", "core.local.toml"),
                    os.path.join(home.name, ".agent-pm", "core.local.toml"))
        self.log = os.path.join(os.path.dirname(self.scenario), "codex-call.json")
        self.env |= {"PATH": fake_codex.install(os.path.join(os.path.dirname(self.scenario), "bin")),
                     "HOME": home.name, fake_codex.ENV: self.scenario}

    def codex(self, *extra, steps):
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "log": self.log}, f)
        return subprocess.run([sys.executable, DRIVE, "--client", "codex", *self.run_argv("writer", "note"), *extra],
                              cwd=self.work, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=60)

    def test_a_codex_run_and_its_resume_read_the_task_view(self):
        view = os.path.join(self.work, "tasks")
        for extra in ((), ("--resume",)):
            with self.subTest(extra=extra):
                res = self.codex(*extra, steps=[{"kind": "progress", "name": "start", "text": "noting"},
                                                {"kind": "outcome", "outcome": DONE}])
                self.assertEqual(res.returncode, 0, res.stderr)
                with open(self.log) as f:
                    call = json.load(f)
                dirs = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "--add-dir"]
                self.assertIn(os.path.realpath(view), map(os.path.realpath, dirs))
                self.assertNotIn(os.path.join(hermetic.CORE, "team", "tasks"), dirs)
                self.assertIn(f"`{view}`", call["prompt"])
                with open(os.path.join(view, "note.md")) as f:
                    self.assertEqual(f.read(), NOTE)
                self.assertEqual(sorted(os.listdir(view)), [".agent-pm-view", "note.md"])

    def test_a_read_dir_holding_the_team_dir_is_refused(self):
        with open(os.path.join(self.env["HOME"], ".agent-pm", "core.local.toml"), "a") as f:
            f.write(f'[roles.writer]\nread = ["{self.team}"]\n')
        res = self.codex(steps=[])
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn(f"team_dirs: {self.team} overlaps a dir CodexClient can write {self.team}", res.stderr)
        self.assertFalse(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()
