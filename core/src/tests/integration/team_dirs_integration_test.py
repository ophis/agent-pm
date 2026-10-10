"""drive.py run as a process with a local `team_dirs` (core/config.toml's comment), fake_claude.py on PATH as `claude`:
a team dir's role and an extended core role run as core's own, through their task view."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(TESTS), TESTS]
REAL_HOME = os.path.realpath(os.path.expanduser("~"))   # read before any test points HOME elsewhere
import hermetic  # noqa: E402
import fake_claude  # noqa: E402
import repo  # noqa: E402
from drive_test import NOTE, SID, TEAM, outcome, progress  # noqa: E402

DRIVE = os.path.join(hermetic.CORE, "src", "drive.py")
DONE = {"status": "done", "title": "note", "summary": "A note.", "deliverable": "A note.\n"}
IN_TEMP = repo.under(REAL_HOME, repo.temp_dirs()) is not None


@unittest.skipIf(IN_TEMP, f"HOME {REAL_HOME} is in a temp dir, where drive.py refuses a team dir")
class TeamDirs(unittest.TestCase):
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
        self.assertEqual(os.listdir(os.path.join(self.work, "tasks")), ["note.md"])
        with open(os.path.join(self.work, "out.md")) as f:
            self.assertEqual(f.read(), "A note.\n")


if __name__ == "__main__":
    unittest.main()
