import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import HEADER, STATES as IDS_BY_KEY, TEAM  # noqa: E402
import eng  # noqa: E402
import launch  # noqa: E402
import pipeline  # noqa: E402

CONFIG = HEADER + """human_members = ["me@x.com"]
[projects.p-dr]
role = "researcher"
task = "deep-research"
[projects.p-pd]
prefix = "PRD"
[projects.p-eng]
prefix = "ENG"
role = "engineer"
task = "engineering"
"""
RESEARCHER_ID = 'tasks = ["deep-research"]\naccount = "r@x.com"\nkey = "k-researcher"\n'
ENGINEER_ID = 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = "k-engineer"\n'
REGISTRY = {
    "roles/principles.md": "", "roles/researcher.md": "", "roles/researcher.toml": RESEARCHER_ID,
    "roles/engineer.md": "", "roles/engineer.toml": 'read_only = ["~/playground/private_docs"]\n' + ENGINEER_ID,
    "tasks/deep-research.md": "",
    "tasks/deep-research.toml": 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/playground/private_docs"]\n',
    "tasks/engineering.md": "",
    "tasks/engineering.toml": 'model = "opus"\neffort = "high"\nadd_dirs = ["~/playground/private_docs"]\nrepo_from_issue = true\n',
}
PUSH_RULE = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
SID = "0f0f0f0f-1111-2222-3333-444444444444"
PRIVATE = os.path.expanduser("~/playground/private_docs")
ENG_PY = shlex.quote(os.path.join(pipeline.ROOT, "scripts", "eng.py"))
IDS = f" Team: {TEAM}. States: " + ", ".join(f"{pipeline.STATES[k]}={IDS_BY_KEY[k]}" for k in pipeline.STATES) + "."
FAKE_CLAUDE = """#!/bin/bash
echo "claude says hi"
echo "oops" >&2
exit 3
"""


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
        self.root = os.path.join(self.tmp, "registry")
        for rel, text in REGISTRY.items():
            self.write(rel, text)
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
            f.write(text)

    def run_launch(self, *argv, keychain=None):
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ):
            rc = launch.main(list(argv), sh=lambda cmd, **kw: self.calls.append(cmd), config=self.config,
                             runs=self.runs, logs=self.logs, gql=self.gql, run=self.run, projects=self.projects,
                             root=self.root,
                             keychain=keychain or (lambda s: self.checked.append(s) or s not in self.missing))
            self.path = os.environ["PATH"]
        self.err = err.getvalue()
        return rc

    def args(self, mode="new", project="p-dr"):
        base = ["--issue", "TASK-1", "--url", "https://l/TASK-1", "--project", project, "--sid", SID, "--mode", mode]
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
        os.makedirs(os.path.dirname(path))
        open(path, "w").close()

    def test_session_gets_role_key_service(self):
        self.assertEqual(self.run_launch(*self.args()), 0)
        self.assertEqual(self.checked, ["k-researcher"])
        self.assertIn("LINEAR_KEYCHAIN_SERVICE=k-researcher", self.exports())

    def test_missing_role_key_is_config_error(self):
        self.missing = {"k-researcher"}
        self.make_transcript()
        for mode in ("new", "resume"):
            with self.subTest(mode):
                self.calls = []
                self.assertEqual(self.run_launch(*self.args(mode)), 2)
                self.assertEqual(self.calls, [])
                self.assertRegex(self.plog().splitlines()[-1],
                                 r"^\S+ \S+ config-error TASK-1: no Keychain item for role key k-researcher$")
                self.assertIn("config-error TASK-1", self.err)

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
        for project, key in (("p-dr", "k-researcher"), ("p-eng", "k-engineer")):
            with self.subTest(project):
                self.calls = []
                with mock.patch.object(launch.subprocess, "run", run), \
                        mock.patch.object(eng, "resolve", return_value=eng.Invalid("x")):
                    self.assertEqual(self.run_launch(*self.args(project=project), keychain=launch.has_key), 0)
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
        self.assertEqual(cmd[:7], ["tmux", "new-session", "-d", "-s", "agent-pm", "-c", run_dir])
        self.assertTrue(os.path.isdir(run_dir))
        self.assertEqual(cmd[7:9], ["bash", "-c"])
        self.assertIn("export PATH=/opt/homebrew/bin:", cmd[9])
        self.assertIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=3600000", cmd[9])
        self.assertIn(os.path.join(self.logs, "projects", "deep-research.log"), cmd[9])

    def test_new_prompt_and_flags(self):
        self.run_launch(*self.args())
        self.assertEqual(launch.PRINCIPLES, os.path.join(pipeline.ROOT, "roles", "principles.md"))
        instructions = os.path.join(self.root, "tasks/deep-research.md")
        charter = os.path.join(self.root, "roles/researcher.md")
        root, rd = pipeline.ROOT, os.path.join(self.work, "TASK-1")
        self.assertEqual(self.claude(), [
            "claude", "-p", f"Follow {launch.PRINCIPLES}, your role charter {charter} and the task {instructions} to handle TASK-1 (https://l/TASK-1). "
                            "The runner has already claimed it. Humans: me@x.com. Project: p-dr." + IDS,
            "--session-id", SID, "--model", "opus", "--effort", "xhigh", "--permission-mode", "auto",
            "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", f"{root}/roles", "--add-dir", f"{root}/tasks", "--add-dir", f"{root}/templates", "--add-dir", PRIVATE,
            "--disallowedTools", f"Edit({slashes(root)}/roles/**)", f"Edit({slashes(root)}/tasks/**)",
            f"Edit({slashes(root)}/templates/**)", f"Edit({slashes(rd)}/worktrees/*/.git)"])

    def test_every_project_locked_down(self):
        for project in ("p-dr", "p-eng"):
            self.calls = []
            with mock.patch.object(eng, "resolve", return_value=eng.Invalid("x")):
                self.run_launch(*self.args(project=project))
            argv = self.claude()
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(self.after(argv, "--setting-sources"), ["user"])
            self.assertNotIn(pipeline.ROOT, argv)
            rules = self.after(argv, "--disallowedTools")
            self.assertTrue(rules and all(r.startswith("Edit(//") for r in rules), rules)
            self.assertIn(f"Edit({slashes(pipeline.ROOT)}/roles/**)", rules)
            self.assertIn(f"Edit({slashes(pipeline.ROOT)}/tasks/**)", rules)
            self.assertEqual(f"Edit({slashes(PRIVATE)}/**)" in rules, project == "p-eng")

    def test_deny(self):
        self.assertEqual(launch.deny("/a b/c/**"), "Edit(//a b/c/**)")

    def test_humans_none(self):
        self.write_config(CONFIG.replace('human_members = ["me@x.com"]\n', ""))
        self.run_launch(*self.args())
        self.assertTrue(self.claude()[2].endswith(" Humans: none. Project: p-dr." + IDS))

    def test_humans_lists_every_member(self):
        self.write_config(CONFIG.replace('human_members = ["me@x.com"]', 'human_members = ["me@x.com", "b@x.com"]'))
        self.run_launch(*self.args())
        self.assertTrue(self.claude()[2].endswith(" Humans: me@x.com, b@x.com. Project: p-dr." + IDS))

    def test_resume_prompt(self):
        self.make_transcript()
        self.assertEqual(self.run_launch(*self.args("resume")), 0)
        argv = self.claude()
        instructions = os.path.join(self.root, "tasks/deep-research.md")
        charter = os.path.join(self.root, "roles/researcher.md")
        self.assertEqual(argv[2], f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {launch.PRINCIPLES}, your role charter {charter} "
                                  f"and the task {instructions} first (they may have changed since this session started) and follow the task's resume rule."
                                  " Humans: me@x.com. Project: p-dr." + IDS)
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertEqual(self.calls[0][6], os.path.join(self.work, "TASK-1"))
        self.calls = []
        self.run_launch(*self.args())
        self.assertTrue(self.claude()[2].endswith(IDS))

    def test_resume_without_transcript(self):
        legacy = os.path.join(self.projects, pipeline.escape(self.work), f"{SID}.jsonl")
        os.makedirs(os.path.dirname(legacy))
        open(legacy, "w").close()
        self.assertEqual(self.run_launch(*self.args("resume")), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog(), r"^\S+ \S+ transient TASK-1: .+\n$")
        self.assertIn("transient TASK-1", self.err)

    def test_non_engineering_never_resolves(self):
        with mock.patch.object(eng, "resolve") as resolve:
            self.run_launch(*self.args())
        resolve.assert_not_called()
        self.assertNotIn("AGENT_PM_ISSUE", self.exports())
        self.assertNotIn("Repo check", self.claude()[2])

    def ok(self):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        return eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", "/u/playground/demo", "main", "TASK-1-demo", wt)

    def launch_eng(self, result, mode="new"):
        error = result if isinstance(result, Exception) else None
        with mock.patch.object(eng, "resolve", return_value=result, side_effect=error) as resolve:
            rc = self.run_launch(*self.args(mode, "p-eng"))
        resolve.assert_called_once_with("TASK-1", self.gql, self.run)
        return rc

    def test_engineering_ok(self):
        self.assertEqual(self.launch_eng(self.ok()), 0)
        self.assertIn(" AGENT_PM_ISSUE=TASK-1", self.exports())
        argv = self.claude()
        wt = self.ok().worktree
        self.assertTrue(argv[2].endswith(
            " Humans: me@x.com. Project: p-eng." + IDS + " Repo check: OK ophis/demo, clone /u/playground/demo, default branch main,"
            f" branch TASK-1-demo, worktree {wt}. eng.py: python3 {ENG_PY}."))
        self.assertNotIn("--allowedTools", argv)
        rules = self.after(argv, "--disallowedTools")
        self.assertIn(f"Edit({slashes(PRIVATE)}/**)", rules)

    def test_engineering_ok_fills_allowed_tools(self):
        self.write("tasks/engineering.toml", REGISTRY["tasks/engineering.toml"] + f'allowed_tools = ["{PUSH_RULE}"]\n')
        self.launch_eng(self.ok())
        wt = self.ok().worktree
        self.assertEqual(self.after(self.claude(), "--allowedTools"),
                         [f"Bash(git -c core.hooksPath=/dev/null -C {wt} push -u git@github.com:ophis/demo.git TASK-1-demo)"])

    def test_engineering_invalid_starts_run_to_bounce(self):
        self.assertEqual(self.launch_eng(eng.Invalid("no Repo: line")), 0)
        self.assertTrue(self.claude()[2].endswith(f" Project: p-eng.{IDS} Repo check failed: no Repo: line. eng.py: python3 {ENG_PY}."))
        self.assertNotIn("AGENT_PM_ISSUE", self.exports())

    def test_engineering_resolve_error_is_transient(self):
        self.assertEqual(self.launch_eng(KeyError("title")), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog("engineering"), r"^\S+ \S+ transient TASK-1: resolve: KeyError: 'title'\n$")

    def test_engineering_invalid_on_resume_is_transient(self):
        self.make_transcript()
        self.assertEqual(self.launch_eng(eng.Invalid("no Repo: line"), mode="resume"), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog("engineering"), r"^\S+ \S+ transient TASK-1: no Repo: line\n$")

    def test_engineering_transient(self):
        self.assertEqual(self.launch_eng(eng.Transient("gh api: TimeoutExpired")), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog("engineering"), r"^\S+ \S+ transient TASK-1: gh api: TimeoutExpired\n$")
        self.assertIn("transient TASK-1: gh api: TimeoutExpired", self.err)

    def test_unknown_or_non_runnable_project(self):
        for project in ("p-nope", "p-pd"):
            self.assertEqual(self.run_launch(*self.args(project=project)), 2)
            self.assertIn("not a runnable project", self.err)
        self.assertEqual(self.calls, [])

    def test_script_logs_output_and_end_lines(self):
        self.run_launch(*self.args())
        script = self.calls[0][9]
        self.assertEqual(script.count("$(date"), 2)  # launch line, and one end time for both logs
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        script = script.replace("export PATH=", f"export PATH={bindir}:", 1)
        subprocess.run(["bash", "-c", script], check=True, capture_output=True)
        plog = self.plog().splitlines()
        with open(self.runs) as f:
            runs = f.read().splitlines()
        self.assertRegex(plog[0], r"^\S+ \S+ launch TASK-1 mode=new session=0f0f0f0f-1111-2222-3333-444444444444$")
        self.assertEqual(sorted(plog[1:3]), ["claude says hi", "oops"])
        self.assertRegex(plog[3], r"^\S+ \S+ end TASK-1 session=0f0f0f0f-1111-2222-3333-444444444444 exit=3$")
        self.assertEqual(runs, [plog[3]])

    def test_rejects_unsafe_issue_or_sid(self):
        for i, bad in ((1, "TASK-1$(rm -rf ~)"), (7, "s1; ls")):
            argv = self.args()
            argv[i] = bad
            self.assertEqual(self.run_launch(*argv), 2)
        self.assertEqual(self.calls, [])

    def test_sets_path_for_its_own_calls(self):
        self.run_launch(*self.args())
        self.assertEqual(self.path, pipeline.PATH)

    def test_env_not_inherited(self):
        with mock.patch.dict(os.environ, {"PATH": "/nowhere"}):
            self.run_launch(*self.args())
        self.assertNotIn("/nowhere", self.calls[0][9])

    def with_memory(self):
        mem = os.path.join(self.tmp, "mem")
        os.makedirs(mem)
        self.write("roles/researcher.toml", f'memory = "{mem}"\n' + RESEARCHER_ID)
        return mem

    def test_memory_prompt_and_dir(self):
        mem = self.with_memory()
        self.run_launch(*self.args())
        argv = self.claude()
        self.assertIn(f"The runner has already claimed it. Your role memory: {mem}; your role charter says how to use it."
                      " Humans: me@x.com.", argv[2])
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"][-1], mem)
        self.assertFalse(any(mem in r for r in self.after(argv, "--disallowedTools")))

    def test_memory_on_resume(self):
        mem = self.with_memory()
        self.make_transcript()
        self.run_launch(*self.args("resume"))
        self.assertIn(f"follow the task's resume rule. Your role memory: {mem}; your role charter says how to use it.", self.claude()[2])

    def repo_config(self):
        self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n' + ENGINEER_ID)

    def test_repo_read_only_ok(self):
        self.repo_config()
        self.launch_eng(self.ok())
        rules = self.after(self.claude(), "--disallowedTools")
        rd = os.path.join(self.work, "TASK-1")
        self.assertEqual(rules[-2:], [f"Edit({slashes(self.ok().clone)}/**)", f"Edit({slashes(rd)}/worktrees/**)"])
        self.assertNotIn(f"Edit({slashes(PRIVATE)}/**)", rules)

    def test_repo_read_only_invalid(self):
        self.repo_config()
        self.launch_eng(eng.Invalid("no Repo: line"))
        rules = self.after(self.claude(), "--disallowedTools")
        rd = os.path.join(self.work, "TASK-1")
        self.assertEqual(rules[-1], f"Edit({slashes(rd)}/worktrees/**)")
        self.assertFalse(any("/u/playground/demo" in r for r in rules))

    def test_log_named_after_task(self):
        self.launch_eng(eng.Transient("x"))
        self.assertIn("transient TASK-1: x", self.plog("engineering"))

    def eng_memory(self, mem):
        os.makedirs(mem, exist_ok=True)
        self.write("roles/engineer.toml", f'read_only = ["~/playground/private_docs"]\nmemory = "{mem}"\n' + ENGINEER_ID)

    def ok_at(self, clone):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        return eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", clone, "main", "TASK-1-demo", wt)

    def assert_config_error(self, mem):
        self.assertEqual(self.calls, [])
        last = self.plog("engineering").splitlines()[-1]
        self.assertRegex(last, rf"^\S+ \S+ config-error TASK-1: role memory {re.escape(mem)} overlaps the issue's repo \S+$")
        self.assertIn("config-error TASK-1: role memory", self.err)

    def test_memory_overlapping_repo_is_config_error(self):
        playground = os.path.join(self.tmp, "playground")
        clone = os.path.join(playground, "demo")
        os.makedirs(clone)
        worktrees = os.path.join(self.work, "TASK-1", "worktrees")
        cases = [("at clone", clone, self.ok_at(clone)), ("under clone", os.path.join(clone, "mem"), self.ok_at(clone)),
                 ("ancestor of clone", playground, self.ok_at(clone)),
                 ("under worktrees", os.path.join(worktrees, "mem"), self.ok_at(clone)),
                 ("under worktrees, invalid repo", os.path.join(worktrees, "mem"), eng.Invalid("no Repo: line"))]
        for label, mem, result in cases:
            with self.subTest(label):
                self.calls = []
                self.eng_memory(mem)
                self.assertEqual(self.launch_eng(result), 2)
                self.assert_config_error(mem)

    def test_memory_overlapping_repo_on_resume(self):
        clone = os.path.join(self.tmp, "playground", "demo")
        os.makedirs(clone)
        self.eng_memory(clone)
        self.make_transcript()
        self.assertEqual(self.launch_eng(self.ok_at(clone), mode="resume"), 2)
        self.assert_config_error(clone)

    def test_memory_apart_from_repo_starts(self):
        clone = os.path.join(self.tmp, "playground", "demo")
        os.makedirs(clone)
        mem = os.path.join(self.tmp, "engmem")
        self.eng_memory(mem)
        self.assertEqual(self.launch_eng(self.ok_at(clone)), 0)
        self.assertIn(f"Your role memory: {mem};", self.claude()[2])

    def test_memory_check_skipped_without_repo_step(self):
        mem = os.path.join(self.work, "TASK-1", "worktrees", "mem")
        os.makedirs(mem)
        self.write("roles/researcher.toml", f'memory = "{mem}"\n' + RESEARCHER_ID)
        self.assertEqual(self.run_launch(*self.args()), 0)
        self.assertEqual(len(self.calls), 1)


class RealConfig(unittest.TestCase):
    """NFR-1: the three runs of the repo's pipeline.toml, command for command."""
    DR, PD, ENG = ("03495382-48f7-4280-a11c-4375df80a561", "ba0738ba-ade7-4525-8d79-1b9944334e74",
                   "ddbff8bf-b633-4b8c-9272-d1d5ee923747")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")
        patch = mock.patch.object(pipeline, "WORK", self.work)
        patch.start()
        self.addCleanup(patch.stop)
        self.projects = os.path.join(self.tmp, "projects")
        self.calls = []
        self.humans = pipeline.load_config()["human_members"]

    def launch(self, project, mode="new", repo=None):
        argv = ["--issue", "TASK-1", "--url", "https://l/TASK-1", "--project", project, "--sid", SID, "--mode", mode]
        if mode == "resume":
            argv += ["--k", "2"]
            path = pipeline.transcript("TASK-1", SID, self.projects)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        with redirect_stderr(io.StringIO()), mock.patch.dict(os.environ), \
                mock.patch.object(eng, "resolve", return_value=repo):
            rc = launch.main(argv, sh=lambda cmd, **kw: self.calls.append(cmd), runs=os.path.join(self.tmp, "runs.log"),
                             logs=os.path.join(self.tmp, "logs"), gql=object(), run=object(), projects=self.projects,
                             keychain=lambda s: True)
        self.assertEqual(rc, 0)
        (cmd,) = self.calls
        toks = shlex.split(cmd[9])
        i = toks.index("claude")
        return toks[i:toks.index("<", i)], cmd[9]

    def expected(self, project, role, task, effort, extra_deny=(), tail=""):
        root, rd = pipeline.ROOT, os.path.join(self.work, "TASK-1")
        humans, cfg = self.humans, pipeline.load_config()
        real = f" Team: {cfg['team']}. States: " + ", ".join(f"{pipeline.STATES[k]}={cfg['states'][k]}" for k in pipeline.STATES) + "."
        return [
            "claude", "-p",
            f"Follow {root}/roles/principles.md, your role charter {root}/roles/{role}.md and the task {root}/tasks/{task}.md"
            " to handle TASK-1 (https://l/TASK-1). The runner has already claimed it."
            f" Humans: {', '.join(humans)}. Project: {project}." + real + tail,
            "--session-id", SID, "--model", "opus", "--effort", effort, "--permission-mode", "auto",
            "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", f"{root}/roles", "--add-dir", f"{root}/tasks", "--add-dir", f"{root}/templates", "--add-dir", PRIVATE,
            "--disallowedTools", f"Edit({slashes(root)}/roles/**)", f"Edit({slashes(root)}/tasks/**)",
            f"Edit({slashes(root)}/templates/**)", f"Edit({slashes(rd)}/worktrees/*/.git)", *extra_deny]

    def test_deep_research(self):
        argv, script = self.launch(self.DR)
        self.assertEqual(argv, self.expected(self.DR, "researcher", "deep-research", "xhigh"))
        self.assertIn(os.path.join(self.tmp, "logs", "projects", "deep-research.log"), script)
        self.assertIn("LINEAR_KEYCHAIN_SERVICE=linear-api-key-researcher", script.split(";")[0])

    def test_product_design(self):
        argv, script = self.launch(self.PD)
        self.assertEqual(argv, self.expected(self.PD, "pm", "product-design", "high"))
        self.assertIn(os.path.join(self.tmp, "logs", "projects", "product-design.log"), script)
        self.assertIn("LINEAR_KEYCHAIN_SERVICE=linear-api-key-pm", script.split(";")[0])

    def test_engineering(self):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        ok = eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", "/u/playground/demo", "main", "TASK-1-demo", wt)
        argv, script = self.launch(self.ENG, repo=ok)
        self.assertEqual(argv, self.expected(
            self.ENG, "engineer", "engineering", "xhigh", extra_deny=[f"Edit({slashes(PRIVATE)}/**)"],
            tail=" Repo check: OK ophis/demo, clone /u/playground/demo, default branch main,"
                 f" branch TASK-1-demo, worktree {wt}. eng.py: python3 {ENG_PY}."))
        self.assertIn(" AGENT_PM_ISSUE=TASK-1", script.split(";")[0])
        self.assertIn("LINEAR_KEYCHAIN_SERVICE=linear-api-key-engineer", script.split(";")[0])
        self.assertIn(os.path.join(self.tmp, "logs", "projects", "engineering.log"), script)

    def test_real_config_resume_prompt(self):
        argv, _ = self.launch(self.DR, mode="resume")
        root = pipeline.ROOT
        self.assertEqual(argv[2].split(" Humans:")[0],
                         f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {root}/roles/principles.md,"
                         f" your role charter {root}/roles/researcher.md and the task {root}/tasks/deep-research.md first"
                         " (they may have changed since this session started) and follow the task's resume rule.")
        self.assertEqual(argv[3:5], ["--resume", SID])


if __name__ == "__main__":
    unittest.main()
