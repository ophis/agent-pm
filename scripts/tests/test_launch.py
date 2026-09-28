import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eng  # noqa: E402
import launch  # noqa: E402
import pipeline  # noqa: E402

CONFIG = """team = "T"
human_members = ["me@x.com"]
[projects.p-dr]
instructions = "stages/deep-research.md"
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
[projects.p-pd]
prefix = "PRD"
[projects.p-eng]
prefix = "ENG"
instructions = "stages/deep-research.md"
model = "opus"
effort = "high"
add_dirs = ["~/playground/private_docs"]
repo_from_issue = true
"""
PUSH_RULE = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
SID = "0f0f0f0f-1111-2222-3333-444444444444"
PRIVATE = os.path.expanduser("~/playground/private_docs")
ENG_PY = shlex.quote(os.path.join(pipeline.ROOT, "scripts", "eng.py"))
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
        self.write_config(CONFIG)
        self.runs = os.path.join(self.tmp, "logs", "runs.log")
        self.logs = os.path.join(self.tmp, "logs")
        self.calls = []
        self.gql, self.run = object(), object()

    def write_config(self, text):
        self.config = os.path.join(self.tmp, "pipeline.toml")
        with open(self.config, "w") as f:
            f.write(text)

    def run_launch(self, *argv):
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ):
            rc = launch.main(list(argv), sh=lambda cmd, **kw: self.calls.append(cmd), config=self.config,
                             runs=self.runs, logs=self.logs, gql=self.gql, run=self.run, projects=self.projects)
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

    def plog(self):
        with open(os.path.join(self.logs, "projects", "deep-research.log")) as f:
            return f.read()

    def make_transcript(self):
        path = pipeline.transcript("TASK-1", SID, self.projects)
        os.makedirs(os.path.dirname(path))
        open(path, "w").close()

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
        instructions = os.path.join(pipeline.ROOT, "stages/deep-research.md")
        root, rd = pipeline.ROOT, os.path.join(self.work, "TASK-1")
        self.assertEqual(self.claude(), [
            "claude", "-p", f"Follow {instructions} to handle TASK-1 (https://l/TASK-1). The runner has already claimed it."
                            " Reviewer: me@x.com. Project: p-dr.",
            "--session-id", SID, "--model", "opus", "--effort", "xhigh", "--permission-mode", "auto",
            "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", f"{root}/stages", "--add-dir", f"{root}/templates", "--add-dir", PRIVATE,
            "--disallowedTools", f"Edit({slashes(root)}/stages/**)",
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
            self.assertIn(f"Edit({slashes(pipeline.ROOT)}/stages/**)", rules)
            self.assertEqual(f"Edit({slashes(PRIVATE)}/**)" in rules, project == "p-eng")

    def test_deny(self):
        self.assertEqual(launch.deny("/a b/c/**"), "Edit(//a b/c/**)")

    def test_reviewer_none(self):
        self.write_config(CONFIG.replace('human_members = ["me@x.com"]\n', ""))
        self.run_launch(*self.args())
        self.assertTrue(self.claude()[2].endswith(" Reviewer: none. Project: p-dr."))

    def test_resume_prompt(self):
        self.make_transcript()
        self.assertEqual(self.run_launch(*self.args("resume")), 0)
        argv = self.claude()
        instructions = os.path.join(pipeline.ROOT, "stages/deep-research.md")
        self.assertEqual(argv[2], f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {instructions} "
                                  "first (it may have changed since this session started) and follow its resume rule."
                                  " Reviewer: me@x.com. Project: p-dr.")
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertEqual(self.calls[0][6], os.path.join(self.work, "TASK-1"))

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
            " Reviewer: me@x.com. Project: p-eng. Repo check: OK ophis/demo, clone /u/playground/demo, default branch main,"
            f" branch TASK-1-demo, worktree {wt}. eng.py: python3 {ENG_PY}."))
        self.assertNotIn("--allowedTools", argv)
        rules = self.after(argv, "--disallowedTools")
        self.assertIn(f"Edit({slashes(PRIVATE)}/**)", rules)

    def test_engineering_ok_fills_allowed_tools(self):
        self.write_config(CONFIG + f"allowed_tools = [{PUSH_RULE!r}]\n".replace("'", '"'))
        self.launch_eng(self.ok())
        wt = self.ok().worktree
        self.assertEqual(self.after(self.claude(), "--allowedTools"),
                         [f"Bash(git -c core.hooksPath=/dev/null -C {wt} push -u git@github.com:ophis/demo.git TASK-1-demo)"])

    def test_engineering_invalid_starts_run_to_bounce(self):
        self.assertEqual(self.launch_eng(eng.Invalid("no Repo: line")), 0)
        self.assertTrue(self.claude()[2].endswith(f" Project: p-eng. Repo check failed: no Repo: line. eng.py: python3 {ENG_PY}."))
        self.assertNotIn("AGENT_PM_ISSUE", self.exports())

    def test_engineering_resolve_error_is_transient(self):
        self.assertEqual(self.launch_eng(KeyError("title")), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog(), r"^\S+ \S+ transient TASK-1: resolve: KeyError: 'title'\n$")

    def test_engineering_invalid_on_resume_is_transient(self):
        self.make_transcript()
        self.assertEqual(self.launch_eng(eng.Invalid("no Repo: line"), mode="resume"), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog(), r"^\S+ \S+ transient TASK-1: no Repo: line\n$")

    def test_engineering_transient(self):
        self.assertEqual(self.launch_eng(eng.Transient("gh api: TimeoutExpired")), 3)
        self.assertEqual(self.calls, [])
        self.assertRegex(self.plog(), r"^\S+ \S+ transient TASK-1: gh api: TimeoutExpired\n$")
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


if __name__ == "__main__":
    unittest.main()
