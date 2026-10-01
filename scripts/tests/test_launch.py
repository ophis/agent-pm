import dataclasses
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import DOCS_CLONE, HEADER, STATES as IDS_BY_KEY, TEAM  # noqa: E402
import eng  # noqa: E402
import launch  # noqa: E402
import pipeline  # noqa: E402
import sessions  # noqa: E402

CONFIG = HEADER + 'human_members = ["me@x.com"]\n'
RESEARCHER_ID = 'tasks = ["deep-research"]\naccount = "r@x.com"\nkey = "k-researcher"\n'
ENGINEER_ID = 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = "k-engineer"\n'
RESEARCHER_TWO = 'tasks = ["deep-research", "quick-scan"]\naccount = "r@x.com"\nkey = "k-researcher"\n'
ENGINEER_TWO = 'read_only = ["{docs_clone}"]\ntasks = ["engineering", "review", "triage"]\naccount = "e@x.com"\nkey = "k-engineer"\n'
REGISTRY = {
    "roles/principles.md": "", "roles/researcher.md": "", "roles/researcher.toml": RESEARCHER_ID,
    "roles/engineer.md": "", "roles/engineer.toml": 'read_only = ["{docs_clone}"]\n' + ENGINEER_ID,
    "tasks/deep-research.md": "",
    "tasks/deep-research.toml": 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["{docs_clone}"]\n',
    "tasks/engineering.md": "",
    "tasks/engineering.toml": 'model = "opus"\neffort = "high"\nadd_dirs = ["{docs_clone}"]\nrepo_from_issue = true\n',
}
MAPPED = "11111111-1111-1111-1111-111111111111"
PUSH_RULE = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
SID = "0f0f0f0f-1111-2222-3333-444444444444"
DOCS = " Docs: {repo}, clone {clone}, branch {branch}."
ENG_PY = shlex.quote(os.path.join(pipeline.ROOT, "scripts", "eng.py"))
RESEARCH_PY = shlex.quote(os.path.join(pipeline.ROOT, "scripts", "research.py"))
IDS = f" Team: {TEAM}. States: " + ", ".join(f"{pipeline.STATES[k]}={IDS_BY_KEY[k]}" for k in pipeline.STATES) + "."
FAKE_CLAUDE = """#!/bin/bash
echo "claude says hi"
echo "oops" >&2
exit 3
"""
FAKE_REGISTER = """#!/bin/bash
printf '%s\\0' "$@" > "{out}/$1.args"
echo "reg $1 $2${{4:+ $4}}"
[ {rc} = 0 ] || echo "reg failed" >&2
exit {rc}
"""
ISO = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$"


def slashes(path):
    return "//" + path.lstrip("/")


class Launch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")
        patch = mock.patch.object(pipeline, "WORK", self.work)
        patch.start()
        self.addCleanup(patch.stop)
        self.projects = os.path.join(self.tmp, "projects")
        self.playground = os.path.join(self.tmp, "playground")
        self.root = os.path.join(self.tmp, "registry")
        for rel, text in REGISTRY.items():
            self.write(rel, text)
        self.clone = os.path.join(self.tmp, "notes")
        os.makedirs(self.clone)
        self.ids = IDS + DOCS.format(repo="acme/notes", clone=self.clone, branch="trunk")
        self.write_config(CONFIG)
        self.runs = os.path.join(self.tmp, "logs", "runs.log")
        self.logs = os.path.join(self.tmp, "logs")
        self.calls = []
        self.checked, self.missing = [], set()
        self.gql, self.run = object(), object()

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def write_config(self, text):
        self.config = os.path.join(self.tmp, "pipeline.toml")
        with open(self.config, "w") as f:
            f.write(text.replace(DOCS_CLONE, self.clone))

    def run_launch(self, *argv, keychain=None):
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ):
            rc = launch.main(list(argv), sh=lambda cmd, **kw: self.calls.append(cmd), config=self.config,
                             runs=self.runs, logs=self.logs, gql=self.gql, run=self.run, projects=self.projects,
                             root=self.root, playground=self.playground,
                             keychain=keychain or (lambda s: self.checked.append(s) or s not in self.missing))
            self.path = os.environ["PATH"]
        self.err = err.getvalue()
        return rc

    def args(self, mode="new", assignee="r@x.com", project="p-dr", task=None):
        if task is None:
            task = "engineering" if assignee.lower() == "e@x.com" else "deep-research"
        base = ["--issue", "TASK-1", "--url", "https://l/TASK-1", "--project", project, "--assignee", assignee,
                "--sid", SID, "--mode", mode, "--task", task]
        return base + (["--k", "2"] if mode == "resume" else [])

    def claude(self):
        """The claude argv inside the tmux script."""
        (cmd,) = self.calls
        toks = shlex.split(cmd[9])
        i = toks.index("claude")
        return toks[i:toks.index("<", i)]

    def exports(self):
        return self.calls[0][9].split(";")[0]

    def after(self, argv, flag):
        """Values that follow flag up to the next --option."""
        i = argv.index(flag) + 1
        j = next((k for k in range(i, len(argv)) if argv[k].startswith("--")), len(argv))
        return argv[i:j]

    def plog(self, name="deep-research"):
        with open(os.path.join(self.logs, "projects", f"{name}.log")) as f:
            return f.read()

    def make_transcript(self):
        path = pipeline.transcript("TASK-1", SID, self.projects)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()

    def test_missing_docs_clone_before_repo_step(self):
        os.rmdir(self.clone)
        with mock.patch.object(eng, "resolve") as resolve:
            self.assertEqual(self.run_launch(*self.args(assignee="e@x.com")), 2)
        resolve.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_has_key_never_reads_the_secret(self):
        calls = []
        def run(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=44)
        with mock.patch.object(launch.subprocess, "run", run):
            self.assertFalse(launch.has_key("k-x"))
        self.assertEqual(calls, [(["security", "find-generic-password", "-s", "k-x"],
                                  {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})])

    def test_no_key_in_command_env_or_log(self):
        secret, security = "lin_api_SENTINEL", []
        def run(cmd, **kw):
            if cmd[0] != "security":
                raise AssertionError(cmd)
            security.append(cmd)
            return SimpleNamespace(returncode=0, stdout=secret if "-w" in cmd else "", stderr="")
        for assignee, key in (("r@x.com", "k-researcher"), ("e@x.com", "k-engineer")):
            with self.subTest(assignee):
                self.calls = []
                with mock.patch.object(launch.subprocess, "run", run), \
                        mock.patch.object(eng, "resolve", return_value=eng.Invalid("x")):
                    self.assertEqual(self.run_launch(*self.args(assignee=assignee), keychain=launch.has_key), 0)
                (cmd,) = self.calls
                self.assertFalse([part for part in cmd if secret in part])
                self.assertIn(f"LINEAR_KEYCHAIN_SERVICE={key}", self.exports())
                self.assertNotIn("LINEAR_API_KEY", cmd[9])
        self.assertEqual(sorted(c[-1] for c in security), ["k-engineer", "k-researcher"])
        self.assertFalse([c for c in security if "-w" in c])
        for f in os.listdir(os.path.join(self.logs, "projects")):
            with open(os.path.join(self.logs, "projects", f)) as fh:
                self.assertNotIn(secret, fh.read())

    def test_tmux_session_cwd_and_bash(self):
        self.assertEqual(self.run_launch(*self.args()), 0)
        (cmd,) = self.calls
        run_dir = os.path.join(self.work, "TASK-1")
        self.assertEqual(cmd[:7], ["tmux", "new-session", "-d", "-s", "agent-pm-researcher", "-c", run_dir])
        self.assertTrue(os.path.isdir(run_dir))
        self.assertEqual(cmd[7:9], ["bash", "-c"])
        self.assertIn("export PATH=/opt/homebrew/bin:", cmd[9])
        self.assertIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=3600000", cmd[9])
        self.assertIn(os.path.join(self.logs, "projects", "deep-research.log"), cmd[9])

    def test_tmux_session_per_role(self):
        with mock.patch.object(eng, "resolve", return_value=eng.Invalid("x")):
            self.assertEqual(self.run_launch(*self.args(assignee="E@X.com")), 0)
        (cmd,) = self.calls
        self.assertEqual(cmd[2:5], ["-d", "-s", "agent-pm-engineer"])

    def test_new_prompt_and_flags(self):
        self.run_launch(*self.args())
        self.assertEqual(launch.PRINCIPLES, os.path.join(pipeline.ROOT, "roles", "principles.md"))
        instructions = os.path.join(self.root, "tasks/deep-research.md")
        charter = os.path.join(self.root, "roles/researcher.md")
        root, rd = pipeline.ROOT, os.path.join(self.work, "TASK-1")
        self.assertEqual(self.claude(), [
            "claude", "-p", f"Follow {launch.PRINCIPLES}, your role charter {charter} and the task {instructions} to handle TASK-1 (https://l/TASK-1). "
                            "The runner has already claimed it. Humans: me@x.com. Project: p-dr." + self.ids,
            "--session-id", SID, "--model", "opus", "--effort", "xhigh", "--permission-mode", "auto",
            "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", f"{root}/roles", "--add-dir", f"{root}/tasks", "--add-dir", f"{root}/templates", "--add-dir", self.clone,
            "--disallowedTools", f"Edit({slashes(root)}/roles/**)", f"Edit({slashes(root)}/tasks/**)",
            f"Edit({slashes(root)}/templates/**)", f"Edit({slashes(rd)}/worktrees/*/.git)"])

    def test_humans(self):
        for members, humans in (("", "none"), ('human_members = ["me@x.com", "b@x.com"]\n', "me@x.com, b@x.com")):
            with self.subTest(humans):
                self.calls = []
                self.write_config(CONFIG.replace('human_members = ["me@x.com"]\n', members))
                self.run_launch(*self.args())
                self.assertTrue(self.claude()[2].endswith(f" Humans: {humans}. Project: p-dr." + self.ids))

    def test_resume_prompt(self):
        self.make_transcript()
        self.assertEqual(self.run_launch(*self.args("resume")), 0)
        argv = self.claude()
        instructions = os.path.join(self.root, "tasks/deep-research.md")
        charter = os.path.join(self.root, "roles/researcher.md")
        self.assertEqual(argv[2], f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {launch.PRINCIPLES}, your role charter {charter} "
                                  f"and the task {instructions} first (they may have changed since this session started) and follow the task's resume rule."
                                  " Humans: me@x.com. Project: p-dr." + self.ids)
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertEqual(self.calls[0][6], os.path.join(self.work, "TASK-1"))
        self.calls = []
        self.run_launch(*self.args())
        self.assertTrue(self.claude()[2].endswith(self.ids))

    def ok(self, clone="/u/playground/demo"):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        return eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", clone, "main", "TASK-1-demo", wt)

    def launch_eng(self, result, mode="new"):
        error = result if isinstance(result, Exception) else None
        with mock.patch.object(eng, "resolve", return_value=result, side_effect=error) as resolve:
            rc = self.run_launch(*self.args(mode, "e@x.com", "p-eng"))
        resolve.assert_called_once_with("TASK-1", self.gql, self.run, repos=pipeline.load_config(self.config)["project_repos"])
        return rc

    def test_engineering_ok(self):
        self.write_config(CONFIG + f'[project_repos]\n"{MAPPED}" = "ophis/demo"\n')
        wt = self.ok().worktree
        for ok, src in ((self.ok(), ""), (dataclasses.replace(self.ok(), mapped=True), " (from project mapping)")):
            with self.subTest(mapped=ok.mapped):
                self.calls = []
                with mock.patch.object(eng, "resolve", return_value=ok) as resolve:
                    self.assertEqual(self.run_launch(*self.args("new", "e@x.com", "p-eng")), 0)
                resolve.assert_called_once_with("TASK-1", self.gql, self.run, repos={MAPPED: "ophis/demo"})
                self.assertIn(" AGENT_PM_ISSUE=TASK-1", self.exports())
                argv = self.claude()
                self.assertTrue(argv[2].endswith(
                    " Humans: me@x.com. Project: p-eng." + self.ids + f" Repo check: OK ophis/demo{src}, clone /u/playground/demo,"
                    f" default branch main, branch TASK-1-demo, worktree {wt}. eng.py: python3 {ENG_PY}."))
                self.assertNotIn("--allowedTools", argv)
                self.assertIn(f"Edit({slashes(self.clone)}/**)", self.after(argv, "--disallowedTools"))

    def test_engineering_ok_fills_allowed_tools(self):
        self.write("tasks/engineering.toml", REGISTRY["tasks/engineering.toml"] + f'allowed_tools = ["{PUSH_RULE}"]\n')
        self.launch_eng(self.ok())
        wt = self.ok().worktree
        self.assertEqual(self.after(self.claude(), "--allowedTools"),
                         [f"Bash(git -c core.hooksPath=/dev/null -C {wt} push -u git@github.com:ophis/demo.git TASK-1-demo)"])

    def test_engineering_invalid_starts_run_to_bounce(self):
        self.assertEqual(self.launch_eng(eng.Invalid("no Repo: line")), 0)
        self.assertTrue(self.claude()[2].endswith(f" Project: p-eng.{self.ids} Repo check failed: no Repo: line. eng.py: python3 {ENG_PY}."))
        self.assertNotIn("AGENT_PM_ISSUE", self.exports())

    def test_assignee_not_a_role_account(self):
        for assignee in ("nobody@x.com", ""):
            self.assertEqual(self.run_launch(*self.args(assignee=assignee)), 2)
            self.assertIn("is not a role account", self.err)
        self.assertEqual(self.calls, [])

    def hand_off(self, roles='[roles.researcher]\nnext = "engineer"\n'):
        """Project MAPPED maps to ophis/demo; the engineering task needs a prefix to be a next."""
        self.write("tasks/engineering.toml", REGISTRY["tasks/engineering.toml"] + 'prefix = "ENG"\n')
        self.write_config(CONFIG + roles + f'[project_repos]\n"{MAPPED}" = "ophis/demo"\n')

    def test_project_repo_follows_project_in_prompt(self):
        self.hand_off()
        for project, repo in ((MAPPED, "ophis/demo"), ("p-other", "none")):
            with self.subTest(project=project):
                self.calls = []
                self.assertEqual(self.run_launch(*self.args(project=project)), 0)
                self.assertTrue(self.claude()[2].endswith(f" Humans: me@x.com. Project: {project}. Project repo: {repo}." + self.ids))

    def two_tasks(self):
        self.write("roles/researcher.toml", RESEARCHER_TWO)
        self.write("tasks/quick-scan.md", "")
        self.write("tasks/quick-scan.toml", f'model = "sonnet"\neffort = "low"\nadd_dirs = ["{self.tmp}/extra"]\n')

    def engineer_tasks(self, review_tools=False):
        self.write("roles/engineer.toml", ENGINEER_TWO)
        self.write("tasks/review.md", "")
        tools = f'allowed_tools = ["Bash(git -C {{worktree}} status {{branch}})"]\n' if review_tools else ""
        self.write("tasks/review.toml", 'model = "sonnet"\neffort = "medium"\nadd_dirs = ["{docs_clone}"]\nrepo_from_issue = true\n' + tools)
        self.write("tasks/triage.md", "")
        self.write("tasks/triage.toml", 'model = "haiku"\neffort = "low"\n')

    def test_task_selects_its_files_and_keeps_the_roles(self):
        self.hand_off()
        mem = os.path.join(self.tmp, "mem")
        os.makedirs(mem)
        self.two_tasks()
        self.write("roles/researcher.toml", f'memory = "{mem}"\n' + RESEARCHER_TWO)
        self.assertEqual(self.run_launch(*self.args(project=MAPPED, task="quick-scan")), 0)
        argv = self.claude()
        self.assertIn(f"your role charter {os.path.join(self.root, 'roles/researcher.md')} and the task "
                      f"{os.path.join(self.root, 'tasks/quick-scan.md')} to handle TASK-1", argv[2])
        self.assertNotIn("deep-research", argv[2])
        self.assertIn(f"Your role memory: {mem};", argv[2])
        self.assertIn(f" Project: {MAPPED}. Project repo: ophis/demo.", argv[2])
        self.assertEqual((self.after(argv, "--model"), self.after(argv, "--effort")), (["sonnet"], ["low"]))
        dirs = [argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"]
        self.assertEqual(dirs[3:], [os.path.join(self.tmp, "extra"), mem])
        script = self.calls[0][9]
        self.assertIn(os.path.join(self.logs, "projects", "quick-scan.log"), script)
        self.assertNotIn("deep-research.log", script)
        self.assertEqual(self.checked, ["k-researcher"])
        self.assertIn("LINEAR_KEYCHAIN_SERVICE=k-researcher", self.exports())
        self.assertEqual(self.calls[0][2:5], ["-d", "-s", "agent-pm-researcher"])

    def test_task_decides_the_repo_step_and_allowed_tools(self):
        self.engineer_tasks(review_tools=True)
        wt = self.ok().worktree
        with mock.patch.object(eng, "resolve", return_value=self.ok()) as resolve:
            self.assertEqual(self.run_launch(*self.args(assignee="e@x.com", task="review")), 0)
        resolve.assert_called_once()
        self.assertIn(" AGENT_PM_ISSUE=TASK-1", self.exports())
        self.assertEqual(self.after(self.claude(), "--allowedTools"), [f"Bash(git -C {wt} status TASK-1-demo)"])
        self.assertIn("Repo check: OK", self.claude()[2])
        self.calls = []
        with mock.patch.object(eng, "resolve") as resolve:
            self.assertEqual(self.run_launch(*self.args(assignee="e@x.com", task="triage")), 0)
        resolve.assert_not_called()
        self.assertNotIn("AGENT_PM_ISSUE", self.exports())
        self.assertNotIn("--allowedTools", self.claude())
        self.assertNotIn("Repo check", self.claude()[2])
        self.assertIn(os.path.join(self.logs, "projects", "triage.log"), self.calls[0][9])

    def test_task_not_the_roles_is_config_error_in_the_default_tasks_log(self):
        self.two_tasks()
        for bad in ("engineering", "../x", "nope", ""):
            for mode in ("new", "resume"):
                with self.subTest(task=bad, mode=mode):
                    self.calls, self.checked = [], []
                    self.assertEqual(self.run_launch(*self.args(mode, task=bad)), 2)
                    line = f"config-error TASK-1: task {bad!r} is not one of researcher's tasks (deep-research, quick-scan)"
                    self.assertRegex(self.plog().splitlines()[-1], rf"^\S+ \S+ {re.escape(line)}$")
                    self.assertIn(line, self.err)
                    self.assertEqual((self.calls, self.checked), ([], []))
        self.assertEqual(os.listdir(os.path.join(self.logs, "projects")), ["deep-research.log"])

    def test_task_is_required(self):
        argv = self.args()
        i = argv.index("--task")
        del argv[i:i + 2]
        with self.assertRaises(SystemExit) as e:
            self.run_launch(*argv)
        self.assertEqual((e.exception.code, self.calls), (2, []))

    def register(self, rc=0):
        """Patches REGISTER with a fake that saves each call's argv to <tmp>/<start|end>.args and prints `reg <verb> <issue> [rc]`."""
        fake = os.path.join(self.tmp, "reg")
        with open(fake, "w") as f:
            f.write(FAKE_REGISTER.format(out=self.tmp, rc=rc))
        os.chmod(fake, 0o755)
        return mock.patch.object(launch, "REGISTER", [fake])

    def execute(self):
        """Runs the tmux script with a fake claude (exit 3) in tmp; returns the script's exit status."""
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        script = self.calls[0][9].replace("export PATH=", f"export PATH={bindir}:", 1)
        return subprocess.run(["bash", "-c", script], cwd=self.tmp, capture_output=True).returncode

    def registered(self, verb):
        """The fake REGISTER's argv for verb, consumed so a later call cannot reuse it."""
        path = os.path.join(self.tmp, f"{verb}.args")
        with open(path, "rb") as f:
            args = f.read().decode().split("\0")[:-1]
        os.remove(path)
        return args

    def logs_after_run(self, name="deep-research"):
        with open(self.runs) as f:
            return self.plog(name).splitlines(), f.read().splitlines()

    def test_script_logs_output_and_end_lines(self):
        with self.register():
            self.run_launch(*self.args())
        self.assertEqual(self.calls[0][9].count("$(date"), 2)  # launch line, and one end time for both logs
        self.assertEqual(self.execute(), 0)
        plog, runs = self.logs_after_run()
        self.assertRegex(plog[0], r"^\S+ \S+ launch TASK-1 mode=new session=0f0f0f0f-1111-2222-3333-444444444444$")
        self.assertEqual(plog[1], "reg start TASK-1")
        self.assertEqual(sorted(plog[2:4]), ["claude says hi", "oops"])
        self.assertRegex(plog[4], r"^\S+ \S+ end TASK-1 session=0f0f0f0f-1111-2222-3333-444444444444 exit=3$")
        self.assertEqual(plog[5:], ["reg end TASK-1 3"])
        self.assertEqual(runs, [plog[4]])

    def test_failing_registry_leaves_the_end_lines(self):
        with self.register(rc=1):
            self.run_launch(*self.args())
        self.assertEqual(self.execute(), 1)
        plog, runs = self.logs_after_run()
        self.assertEqual(plog[1:3], ["reg start TASK-1", "reg failed"])
        self.assertEqual(sorted(plog[3:5]), ["claude says hi", "oops"])
        self.assertRegex(plog[5], r"^\S+ \S+ end TASK-1 session=0f0f0f0f-1111-2222-3333-444444444444 exit=3$")
        self.assertEqual(plog[6:], ["reg end TASK-1 3", "reg failed"])
        self.assertEqual(runs, [plog[5]])

    def test_registry_gets_the_record(self):
        rd = os.path.join(self.work, "TASK-1")
        researcher, engineer = ({"sid": SID, "cwd": rd, "key": key} for key in ("k-researcher", "k-engineer"))
        self.two_tasks()
        self.make_transcript()
        cases = [
            ("new", lambda: self.run_launch(*self.args()), researcher),
            ("resume", lambda: self.run_launch(*self.args("resume")), researcher),
            ("other task", lambda: self.run_launch(*self.args(task="quick-scan")), researcher),
            ("engineering ok", lambda: self.launch_eng(self.ok()), engineer),
            ("engineering invalid", lambda: self.launch_eng(eng.Invalid("x")), engineer)]
        for name, start_run, expected in cases:
            with self.subTest(name):
                self.calls = []
                with self.register():
                    self.assertEqual(start_run(), 0)
                self.assertEqual(self.execute(), 0)
                start = self.registered("start")
                self.assertEqual(start[:2], ["start", "TASK-1"])
                self.assertEqual(self.registered("end"), ["end", "TASK-1", start[2], "3"])
                rec = json.loads(start[2])
                self.assertRegex(rec.pop("started_at"), ISO)
                self.assertEqual(rec, expected)

    def test_rejects_unsafe_issue_or_sid(self):
        for flag, bad in (("--issue", "TASK-1$(rm -rf ~)"), ("--sid", "s1; ls")):
            with self.subTest(flag):
                argv = self.args()
                argv[argv.index(flag) + 1] = bad
                self.assertEqual(self.run_launch(*argv), 2)
                issue, sid = argv[argv.index("--issue") + 1], argv[argv.index("--sid") + 1]
                self.assertEqual(self.err, f"launch.py: bad issue or session id: {issue} {sid}\n")
                self.assertEqual(self.calls, [])

    def test_sets_path_for_its_own_calls(self):
        self.run_launch(*self.args())
        self.assertEqual(self.path, pipeline.PATH)

    def test_memory_prompt_and_dir(self):
        mem = os.path.join(self.tmp, "mem")
        os.makedirs(mem)
        self.write("roles/researcher.toml", f'memory = "{mem}"\n' + RESEARCHER_ID)
        self.make_transcript()
        for mode, before in (("new", "The runner has already claimed it."), ("resume", "follow the task's resume rule.")):
            with self.subTest(mode):
                self.calls = []
                self.run_launch(*self.args(mode))
                argv = self.claude()
                self.assertIn(f"{before} Your role memory: {mem}; your role charter says how to use it. Humans: me@x.com.", argv[2])
                self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"][-1], mem)
                self.assertFalse(any(mem in r for r in self.after(argv, "--disallowedTools")))

    def test_repo_read_only(self):
        self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n' + ENGINEER_ID)
        rd = slashes(os.path.join(self.work, "TASK-1"))
        for result, clone in ((self.ok(), [f"Edit({slashes(self.ok().clone)}/**)"]), (eng.Invalid("no Repo: line"), [])):
            with self.subTest(type(result).__name__):
                self.calls = []
                self.launch_eng(result)
                self.assertEqual(self.after(self.claude(), "--disallowedTools")[3:],
                                 [f"Edit({rd}/worktrees/*/.git)", *clone, f"Edit({rd}/worktrees/**)"])

    def eng_memory(self, mem):
        os.makedirs(mem, exist_ok=True)
        self.write("roles/engineer.toml", f'read_only = ["{{docs_clone}}"]\nmemory = "{mem}"\n' + ENGINEER_ID)

    def test_run_that_does_not_start(self):
        clone, worktrees = os.path.join(self.playground, "demo"), os.path.join(self.work, "TASK-1", "worktrees")
        os.makedirs(clone)
        transcript = pipeline.transcript("TASK-1", SID, self.projects)
        new, resume = (lambda: self.run_launch(*self.args())), (lambda: self.run_launch(*self.args("resume")))
        no_key, no_docs = (lambda: self.missing.add("k-researcher")), (lambda: os.rmdir(self.clone))
        key = "config-error TASK-1: no Keychain item for role key k-researcher"
        docs = f"config-error TASK-1: docs clone {self.clone} is not a directory"
        memory = "config-error TASK-1: role memory {} overlaps the issue's repo {}"
        under_clone, under_worktrees = os.path.join(clone, "mem"), os.path.join(worktrees, "mem")
        cases = [
            ("no role key", no_key, new, 2, "deep-research", key),
            ("no role key, resume", no_key, resume, 2, "deep-research", key),
            ("no docs clone", no_docs, new, 2, "deep-research", docs),
            ("no docs clone, resume", no_docs, resume, 2, "deep-research", docs),
            ("role key checked before the docs clone", lambda: (no_key(), no_docs()), new, 2, "deep-research", key),
            ("no transcript", lambda: os.remove(transcript), resume, 3, "deep-research",
             f"transient TASK-1: no transcript to resume at {transcript}"),
            ("resolve error", None, lambda: self.launch_eng(KeyError("title")), 3, "engineering",
             "transient TASK-1: resolve: KeyError: 'title'"),
            ("invalid repo on resume", None, lambda: self.launch_eng(eng.Invalid("no Repo: line"), "resume"), 3, "engineering",
             "transient TASK-1: no Repo: line"),
            ("transient repo step", None, lambda: self.launch_eng(eng.Transient("gh api: TimeoutExpired")), 3, "engineering",
             "transient TASK-1: gh api: TimeoutExpired"),
            ("memory at the clone", lambda: self.eng_memory(clone), lambda: self.launch_eng(self.ok(clone)), 2, "engineering",
             memory.format(clone, clone)),
            ("memory at the clone, resume", lambda: self.eng_memory(clone), lambda: self.launch_eng(self.ok(clone), "resume"), 2,
             "engineering", memory.format(clone, clone)),
            ("memory under the clone", lambda: self.eng_memory(under_clone), lambda: self.launch_eng(self.ok(clone)), 2,
             "engineering", memory.format(under_clone, clone)),
            ("memory above the clone", lambda: self.eng_memory(self.playground), lambda: self.launch_eng(self.ok(clone)), 2,
             "engineering", memory.format(self.playground, clone)),
            ("memory under worktrees", lambda: self.eng_memory(under_worktrees), lambda: self.launch_eng(self.ok(clone)), 2,
             "engineering", memory.format(under_worktrees, worktrees)),
            ("memory under worktrees, invalid repo", lambda: self.eng_memory(under_worktrees),
             lambda: self.launch_eng(eng.Invalid("no Repo: line")), 2, "engineering", memory.format(under_worktrees, worktrees))]
        for name, setup, start, rc, log, line in cases:
            with self.subTest(name):
                shutil.rmtree(self.logs, ignore_errors=True)
                os.makedirs(self.clone, exist_ok=True)
                self.write("roles/engineer.toml", REGISTRY["roles/engineer.toml"])
                self.calls, self.missing = [], set()
                self.make_transcript()
                if setup:
                    setup()
                with mock.patch.object(sessions, "base") as base:
                    self.assertEqual(start(), rc)
                base.assert_not_called()
                self.assertEqual(self.calls, [])
                self.assertRegex(self.err, rf"^\S+ \S+ {re.escape(line)}\n\Z")
                self.assertEqual(self.plog(log), self.err)

    def test_memory_apart_from_repo_starts(self):
        clone = os.path.join(self.playground, "demo")
        os.makedirs(clone)
        mem = os.path.join(self.tmp, "engmem")
        self.eng_memory(mem)
        self.assertEqual(self.launch_eng(self.ok(clone)), 0)
        self.assertIn(f"Your role memory: {mem};", self.claude()[2])

    def test_memory_check_skipped_without_repo_step(self):
        mem = os.path.join(self.work, "TASK-1", "worktrees", "mem")
        os.makedirs(mem)
        self.write("roles/researcher.toml", f'memory = "{mem}"\n' + RESEARCHER_ID)
        self.assertEqual(self.run_launch(*self.args()), 0)
        self.assertEqual(len(self.calls), 1)

    def read_repo(self):
        """deep-research gets read_repo; project MAPPED maps to ophis/demo."""
        self.hand_off(roles="")
        self.write("tasks/deep-research.toml", REGISTRY["tasks/deep-research.toml"] + "read_repo = true\n")

    def test_read_repo_extras_on_new_and_resume(self):
        self.read_repo()
        self.make_transcript()
        rd = os.path.join(self.work, "TASK-1")
        clone = f"Edit({slashes(self.playground)}/demo/**)"
        for mode in ("new", "resume"):
            for project, repo, mapped in ((MAPPED, "ophis/demo", [clone]), ("p-other", "none", [])):
                with self.subTest(mode=mode, project=project):
                    self.calls = []
                    self.assertEqual(self.run_launch(*self.args(mode, project=project)), 0)
                    argv = self.claude()
                    self.assertTrue(argv[2].endswith(
                        f" Humans: me@x.com. Project: {project}. Project repo: {repo}." + self.ids + f" research.py: python3 {RESEARCH_PY}."))
                    self.assertIn(" AGENT_PM_ISSUE=TASK-1", self.exports())
                    self.assertEqual(self.after(argv, "--disallowedTools")[3:], [
                        f"Edit({slashes(rd)}/worktrees/*/.git)", f"Edit({slashes(rd)}/src/**)",
                        f"Edit({slashes(pipeline.ROOT)}/scripts/**)", *mapped])
                    self.assertEqual(argv[-2:], ["--allowedTools", f"Bash(python3 {RESEARCH_PY} prepare)"])

    def test_read_repo_quotes_the_script_path(self):
        self.read_repo()
        root = os.path.join(self.tmp, "a b")
        with mock.patch.object(launch, "ROOT", root):
            self.assertEqual(self.run_launch(*self.args()), 0)
        path = shlex.quote(os.path.join(root, "scripts", "research.py"))
        argv = self.claude()
        self.assertTrue(argv[2].endswith(f" research.py: python3 {path}."))
        self.assertEqual(argv[-1], f"Bash(python3 {path} prepare)")

    def test_read_repo_run_dir_inside_the_mapped_clone(self):
        self.read_repo()
        clone = os.path.join(self.playground, "demo")
        for d in (".git", ".claude", "roles", "scripts", "work"):
            os.makedirs(os.path.join(clone, d))
        open(os.path.join(clone, "README.md"), "w").close()
        link = os.path.join(self.tmp, "link")
        os.symlink(clone, link)
        c = slashes(clone)
        for work in (os.path.join(clone, "work"), os.path.join(link, "work")):
            with self.subTest(work=work), mock.patch.object(pipeline, "WORK", work), mock.patch.object(launch, "ROOT", clone):
                self.calls = []
                self.assertEqual(self.run_launch(*self.args(project=MAPPED)), 0)
                rd = slashes(os.path.join(work, "TASK-1"))
                self.assertEqual(self.after(self.claude(), "--disallowedTools"), [
                    f"Edit({c}/roles/**)", f"Edit({c}/tasks/**)", f"Edit({c}/templates/**)", f"Edit({rd}/worktrees/*/.git)",
                    f"Edit({rd}/src/**)", f"Edit({c}/scripts/**)", f"Edit({c}/.claude)", f"Edit({c}/.claude/**)",
                    f"Edit({c}/.git)", f"Edit({c}/.git/**)", f"Edit({c}/README.md)", f"Edit({c}/roles)", f"Edit({c}/scripts)"])

    def test_task_without_read_repo_gets_no_extras(self):
        self.read_repo()
        self.two_tasks()
        os.makedirs(os.path.join(self.playground, "demo"))
        self.assertEqual(self.run_launch(*self.args(project=MAPPED, task="quick-scan")), 0)
        argv = self.claude()
        self.assertTrue(argv[2].endswith(f" Humans: me@x.com. Project: {MAPPED}." + self.ids))
        self.assertNotIn("AGENT_PM_ISSUE", self.exports())
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(self.after(argv, "--disallowedTools")[3:], [f"Edit({slashes(os.path.join(self.work, 'TASK-1'))}/worktrees/*/.git)"])

    def test_project_repo_once_for_read_repo_and_repo_hand_off(self):
        self.read_repo()
        self.hand_off()
        self.assertEqual(self.run_launch(*self.args(project=MAPPED)), 0)
        prompt = self.claude()[2]
        self.assertEqual(prompt.count("Project repo:"), 1)
        self.assertTrue(prompt.endswith(f" Project: {MAPPED}. Project repo: ophis/demo." + self.ids + f" research.py: python3 {RESEARCH_PY}."))


class RealConfig(unittest.TestCase):
    """Each task of the repo's pipeline.toml, command for command."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")
        patch = mock.patch.object(pipeline, "WORK", self.work)
        patch.start()
        self.addCleanup(patch.stop)
        self.cfg = pipeline.load_config()

    def launch(self, role, task, repo):
        calls = []
        argv = ["--issue", "TASK-1", "--url", "https://l/TASK-1", "--project", "p-x",
                "--assignee", pipeline.registry()[0][role].account, "--sid", SID, "--mode", "new", "--task", task]
        with redirect_stderr(io.StringIO()), mock.patch.dict(os.environ), \
                mock.patch.object(eng, "resolve", return_value=repo):
            rc = launch.main(argv, sh=lambda cmd, **kw: calls.append(cmd), runs=os.path.join(self.tmp, "runs.log"),
                             logs=os.path.join(self.tmp, "logs"), gql=object(), run=object(), projects=os.path.join(self.tmp, "projects"),
                             keychain=lambda s: True, docs_ok=lambda p: True, playground=os.path.join(self.tmp, "playground"))
        self.assertEqual(rc, 0)
        (cmd,) = calls
        toks = shlex.split(cmd[9])
        i = toks.index("claude")
        return toks[i:toks.index("<", i)], cmd[9]

    def expected(self, role, task, effort, extra_deny=(), tail="", project_repo=" Project repo: none.", allowed=()):
        root, rd, cfg = pipeline.ROOT, os.path.join(self.work, "TASK-1"), self.cfg
        real = (f" Team: {cfg['team']}. States: " + ", ".join(f"{pipeline.STATES[k]}={cfg['states'][k]}" for k in pipeline.STATES) + "."
                + DOCS.format(**cfg["docs"]))
        return [
            "claude", "-p",
            f"Follow {root}/roles/principles.md, your role charter {root}/roles/{role}.md and the task {root}/tasks/{task}.md"
            " to handle TASK-1 (https://l/TASK-1). The runner has already claimed it."
            f" Humans: {', '.join(cfg['human_members'])}. Project: p-x.{project_repo}" + real + tail,
            "--session-id", SID, "--model", "opus", "--effort", effort, "--permission-mode", "auto",
            "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", f"{root}/roles", "--add-dir", f"{root}/tasks", "--add-dir", f"{root}/templates", "--add-dir", cfg["docs"]["clone"],
            "--disallowedTools", f"Edit({slashes(root)}/roles/**)", f"Edit({slashes(root)}/tasks/**)",
            f"Edit({slashes(root)}/templates/**)", f"Edit({slashes(rd)}/worktrees/*/.git)", *extra_deny,
            *(["--allowedTools", *allowed] if allowed else [])]

    def research(self, task, effort):
        rd = os.path.join(self.work, "TASK-1")
        return self.expected("researcher", task, effort,
                             extra_deny=[f"Edit({slashes(rd)}/src/**)", f"Edit({slashes(pipeline.ROOT)}/scripts/**)"],
                             tail=f" research.py: python3 {RESEARCH_PY}.", allowed=[f"Bash(python3 {RESEARCH_PY} prepare)"])

    def test_every_task(self):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        ok = eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", "/u/playground/demo", "main", "TASK-1-demo", wt)
        engineering = self.expected(
            "engineer", "engineering", "xhigh", extra_deny=[f"Edit({slashes(self.cfg['docs']['clone'])}/**)"], project_repo="",
            tail=" Repo check: OK ophis/demo, clone /u/playground/demo, default branch main,"
                 f" branch TASK-1-demo, worktree {wt}. eng.py: python3 {ENG_PY}.")
        cases = [("researcher", "deep-research", None, self.research("deep-research", "ultracode"), True),
                 ("researcher", "light-research", None, self.research("light-research", "high"), True),
                 ("pm", "product-design", None, self.expected("pm", "product-design", "high"), False),
                 ("engineer", "engineering", ok, engineering, True)]
        for role, task, repo, argv, issue_env in cases:
            with self.subTest(task):
                got, script = self.launch(role, task, repo)
                self.assertEqual(got, argv)
                self.assertIn(os.path.join(self.tmp, "logs", "projects", f"{task}.log"), script)
                exports = script.split(";")[0]
                self.assertIn(f"LINEAR_KEYCHAIN_SERVICE=linear-api-key-{role}", exports)
                self.assertEqual(" AGENT_PM_ISSUE=TASK-1" in exports, issue_env)


if __name__ == "__main__":
    unittest.main()
