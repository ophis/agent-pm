"""A local `team_dirs` read by the Composer in a child process (child.Case): a team dir's role composes with its tasks
dir to read, never added; its catalog row; a read dir holding the team dir composes unadded, a write dir refused."""

import json
import os
import re
import shutil
import tempfile
import unittest

from agent_pm.delegate.composer.prompt import RESUME, RETURN
from agent_pm.utils.paths import temp_dirs, under

from delegate.tests.case import REPO
from delegate.tests.integration.composer import child

# outside every temp dir (check_team_dirs refuses one there): the repo's gitignored work/
TEAMS = os.path.join(REPO, "work", "new-team-dirs-tests")
IN_TEMP = under(os.path.realpath(TEAMS), temp_dirs()) is not None
CORE_TASKS = os.path.join(REPO, "delegate", "src", "agent_pm", "delegate", "team", "tasks")
MARK = "[agent-pm-progress:start] the topic"
NOTE = (
    f"# Note\n\n## Steps\n\n1. **Report progress:**\n   {MARK}\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)
WRITER = (
    "# Writer\n\nYou write notes.\n\n## Tasks\n\n"
    "Pick the task below that fits the input; unsure → `note` (`<tasks>/note.md`).\n\n"
    "- `note` (`<tasks>/note.md`): a short note; always; light.\n"
)
TEAM = {"roles/writer.md": WRITER, "tasks/note.md": NOTE}
TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")
CATALOG = """from agent_pm.utils.config_file import read_project
team = composer.team(composer.TEAM, composer.team_dirs())
composer.check_team_dirs(team.dirs, writes=[])
team.check_all(read_project("delegate"))
params = composer.RunParams(input="", out=os.path.join(sys.argv[1], "out.md"), workdir=sys.argv[1])
rows = {}
for role, tasks in composer.catalog(team).items():
    run = composer.resolve(role, team=team)
    composer.render(run, params, team=team)
    rows[role] = [run.output["type"], team.source(team.lookup("roles", role)),
                  {t: [what, team.source(team.lookup("tasks", t))] for t, what in tasks.items()}]
print(json.dumps(rows))
"""


def make_team(case: unittest.TestCase) -> str:
    """A team dir of TEAM's files under TEAMS, removed after `case`'s test."""
    os.makedirs(TEAMS, mode=0o700, exist_ok=True)
    if os.path.realpath(TEAMS) != TEAMS:
        raise RuntimeError(f"{TEAMS} must not contain symlinks")
    os.chmod(TEAMS, 0o700)
    team = tempfile.mkdtemp(dir=TEAMS, prefix="team-")
    case.addCleanup(shutil.rmtree, team, ignore_errors=True)
    os.chmod(team, 0o755)
    for rel, text in TEAM.items():
        path = os.path.join(team, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o644)
    return team


@unittest.skipIf(IN_TEMP, f"{TEAMS} is in a temp dir, where check_team_dirs refuses a team dir")
class TeamDirs(child.Case):
    def setUp(self):
        super().setUp()
        self.team = make_team(self)
        self.tasks = os.path.join(self.team, "tasks")
        self.local(f"team_dirs = {json.dumps([self.team])}\n")

    def composed(self, **req) -> dict:
        got = self.compose("writer", "note", **req)
        self.assertNotIn("error", got)
        return got

    def test_a_team_dirs_role_composes_with_its_tasks_dir(self):
        got = self.composed()
        self.assertEqual(TASKS_PARAM.search(got["prompt"])[1], self.tasks)
        self.assertEqual((got["intent"]["dirs"], got["intent"]["reads"]), ([], [self.tasks]))
        self.assertEqual(os.listdir(self.work), [])

    def test_the_catalog_lists_its_role_after_cores_with_its_sources(self):
        rows = self.child(CATALOG, self.work)
        self.assertEqual(list(rows), ["researcher", "pm", "engineer", "dummy-tester", "writer"])
        note = ["a short note; always; light.", os.path.join(self.tasks, "note.md")]
        self.assertEqual(
            rows["writer"], ["local", os.path.join(self.team, "roles", "writer.md"), {"note": note}]
        )
        self.assertEqual({row[1] for role, row in rows.items() if role != "writer"}, {"built-in"})
        self.assertEqual(os.listdir(self.work), [])

    def test_the_prompt_leads_to_its_task_file_in_its_tasks_dir(self):
        got = self.composed()
        prompt = got["prompt"]
        self.assertIn("**Your task**: `note` (`<tasks>/note.md`).", prompt)
        tasks = TASKS_PARAM.search(prompt)[1]
        with open(os.path.join(tasks, "note.md"), encoding="utf-8") as f:
            self.assertIn(f"\n   {MARK}\n", f.read())
        self.assertIn(tasks, got["intent"]["reads"])
        self.assertIn(f"\n{RETURN}\n", prompt)
        self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))

    def test_a_run_and_its_resume_read_its_tasks_dir_unadded(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                got = self.composed(resume=resume)
                dirs, reads = got["intent"]["dirs"], got["intent"]["reads"]
                self.assertIn(self.tasks, map(os.path.realpath, reads))
                self.assertNotIn(self.tasks, map(os.path.realpath, dirs))
                self.assertNotIn(CORE_TASKS, dirs + reads)
                self.assertIn(f"`{self.tasks}`", got["prompt"])
                self.assertEqual(got["prompt"].startswith(RESUME), resume)
                self.assertFalse(os.path.exists(os.path.join(self.work, "tasks")))

    def test_a_read_dir_holding_the_team_dir_composes_unadded(self):
        self.local(f"[roles.writer]\nread = [{json.dumps(self.team)}]\n", "a")
        got = self.composed()
        self.assertEqual(
            (got["intent"]["dirs"], got["intent"]["reads"]), ([], [self.tasks, self.team])
        )

    def test_a_write_dir_holding_the_team_dir_is_refused(self):
        self.local(f"[roles.writer]\nwrite = [{json.dumps(self.team)}]\n", "a")
        got = self.compose("writer", "note")
        self.assertEqual(
            got,
            {
                "error": f"team_dirs: {self.team} overlaps a dir the run adds {self.team}: the agent "
                "run can write there"
            },
        )


if __name__ == "__main__":
    unittest.main()
