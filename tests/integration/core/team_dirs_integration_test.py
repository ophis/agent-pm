import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import hermetic
from tests.integration.core import fake_claude, fake_codex
from tests.unit.core.drive_test import NOTE, SID, TEAM, outcome, progress
import repo

DRIVE = os.path.join(hermetic.CORE, "src", "drive.py")
DONE = {"status": "done", "title": "note", "summary": "A note.", "deliverable": "A note.\n"}
# The team dirs lie outside every temp dir (drive.py refuses one there): under the repo's gitignored work/.
TEAMS = os.path.join(os.path.dirname(hermetic.CORE), "work", "team-dirs-tests")
IN_TEMP = repo.under(os.path.realpath(TEAMS), repo.temp_dirs()) is not None


@unittest.skipIf(IN_TEMP, f"{TEAMS} is in a temp dir, where drive.py refuses a team dir")
class Fixture(unittest.TestCase):
    def setUp(self):
        os.makedirs(TEAMS, mode=0o700, exist_ok=True)
        if os.path.realpath(TEAMS) != TEAMS:
            raise RuntimeError(f"{TEAMS} must not contain symlinks")
        os.chmod(TEAMS, 0o700)
        self.team = tempfile.mkdtemp(dir=TEAMS, prefix="team-")
        self.addCleanup(shutil.rmtree, self.team, ignore_errors=True)
        os.chmod(self.team, 0o755)
        for rel, text in TEAM.items():
            path = os.path.join(self.team, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.chmod(os.path.dirname(path), 0o755)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
        self.tasks = os.path.join(self.team, "tasks")
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


class TeamDirs(Fixture):
    def test_a_dry_run(self):
        res = self.drive(*self.run_argv("writer", "note", "--dry-run"))
        self.assertEqual(res.returncode, 0, res.stderr)
        argv = json.loads(res.stdout)["argv"]
        self.assertIn(f"`{self.tasks}`", argv[2])
        self.assertIn(self.tasks, [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"])
        self.assertEqual(os.listdir(self.work), [])

    def test_list(self):
        res = self.drive("--list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"\nwriter  local  {os.path.join(self.team, 'roles', 'writer.md')}\n"
                      f"  note: a short note; always; light.  [{os.path.join(self.tasks, 'note.md')}]\n", res.stdout)

    def test_a_headless_run_reads_its_task_from_its_tasks_dir(self):
        res = self.drive(*self.run_argv("writer", "note"), steps=[
            progress("start", "noting"), {"kind": "read", "path": os.path.join(self.tasks, "note.md")}, outcome(DONE)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("drive.py: status done", res.stderr)
        self.assertIn("[agent-pm-progress:start] the topic", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))
        with open(os.path.join(self.work, "out.md")) as f:
            self.assertEqual(f.read(), "A note.\n")


class CodexTeamDirs(Fixture):
    # its HOME (its recovery store) outside the temp dirs too, as fake_codex_integration_test.py's
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

    def test_a_codex_run_and_its_resume_add_its_tasks_dir(self):
        for extra in ((), ("--resume",)):
            with self.subTest(extra=extra):
                res = self.codex(*extra, steps=[{"kind": "progress", "name": "start", "text": "noting"},
                                                {"kind": "outcome", "outcome": DONE}])
                self.assertEqual(res.returncode, 0, res.stderr)
                with open(self.log) as f:
                    call = json.load(f)
                dirs = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "--add-dir"]
                self.assertIn(self.tasks, map(os.path.realpath, dirs))
                self.assertNotIn(os.path.join(hermetic.CORE, "team", "tasks"), dirs)
                self.assertIn(f"`{self.tasks}`", call["prompt"])
                self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))

    def test_a_read_dir_holding_the_team_dir_is_refused(self):
        with open(os.path.join(self.env["HOME"], ".agent-pm", "core.local.toml"), "a") as f:
            f.write(f'[roles.writer]\nread = ["{self.team}"]\n')
        res = self.codex(steps=[])
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn(f"team_dirs: {self.team} overlaps a dir CodexClient adds {self.team}", res.stderr)
        self.assertFalse(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()
