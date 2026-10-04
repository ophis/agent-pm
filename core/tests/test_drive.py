import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "11111111-2222-3333-4444-555555555555"
NO_ACCESS = drive.Access(dirs=[], read_only=[], commands=[])
RUN = {"task": "t", "tier": 2, "effort": "high"}


class Recorder(clients.Client):
    """A fake client that needs no config file and records what the driver hands it."""
    needs_config = False
    seen = []

    def launch(self, prompt, run, *, sid, resume, access):
        Recorder.seen.append(dict(prompt=prompt, run=run, sid=sid, resume=resume, access=access))
        return clients.Launch(["fake", sid], {"FAKE": "1"})


def claude(**overrides):
    return clients.ClaudeClient({**clients.load_config("claude", CORE), **overrides})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = os.path.join(self.tmp.name, "work")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        Recorder.seen = []
        patch = unittest.mock.patch.dict(clients.REGISTRY, {"fake": Recorder})
        patch.start()
        self.addCleanup(patch.stop)

    def plan(self, role="researcher", task="light-research", client="fake", **kw):
        args = dict(input="Research X.", out=os.path.join(self.work, "out.md"), workdir=self.work, sid=SID)
        return drive.plan(CORE, role, task, client=client, **{**args, **kw})


class Claude(Base):
    def test_light_research_argv(self):
        launch = self.plan(client="claude", repo=self.repo)
        self.assertEqual(launch.argv[:2], ["claude", "-p"])
        self.assertTrue(launch.argv[2].startswith("# Principles"))
        self.assertEqual(launch.argv[3:], [
            "--session-id", SID, "--model", "opus", "--effort", "high",
            "--permission-mode", "auto", "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", self.repo,
            "--disallowedTools", f"Edit(/{CORE}/**)", f"Edit(/{self.repo}/**)"])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_engineering_xhigh_and_writable_repo(self):
        argv = self.plan("engineer", "engineering", client="claude", repo=self.repo).argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertIn(self.repo, argv)
        self.assertNotIn(f"Edit(/{self.repo}/**)", argv)

    def test_resume(self):
        argv = self.plan(client="claude", resume=True).argv
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertTrue(argv[2].startswith("Resumed run"))

    def test_commands_and_task_rules_become_allowed_tools(self):
        c = claude(tasks={"t": {"allow": ["WebFetch"]}})
        access = drive.Access(dirs=[], read_only=[], commands=["make test"])
        argv = c.launch("p", RUN, sid=SID, resume=False, access=access).argv
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools", "Bash(make test)", "WebFetch"])

    def test_no_rule_groups_when_empty(self):
        argv = claude().launch("p", RUN, sid=SID, resume=False, access=NO_ACCESS).argv
        for flag in ("--add-dir", "--disallowedTools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_braces_in_the_input_survive(self):
        argv = self.plan(client="claude", input="Explain {{x}} templates.").argv
        self.assertTrue(argv[2].rstrip().endswith("Explain {{x}} templates."))

    def test_unmapped_tier(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(tiers={}).launch("p", RUN, sid=SID, resume=False, access=NO_ACCESS)
        self.assertIn("no model for tier 2", str(cm.exception))

    def test_unknown_config_key(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(argv=[])
        self.assertIn("unknown key 'argv'", str(cm.exception))


class Generic(Base):
    def test_driver_hands_the_client_neutral_access(self):
        launch = self.plan(repo=self.repo)
        seen, = Recorder.seen
        self.assertEqual(seen["access"], drive.Access(dirs=[self.repo], read_only=[CORE, self.repo], commands=[]))
        self.assertEqual((seen["sid"], seen["resume"], seen["run"]["task"]), (SID, False, "light-research"))
        self.assertTrue(seen["prompt"].startswith("# Principles"))
        self.assertEqual((launch.argv, launch.env, launch.cwd), (["fake", SID], {"FAKE": "1"}, self.work))

    def test_writable_repo_is_not_read_only(self):
        self.plan("engineer", "engineering", repo=self.repo)
        self.assertEqual(Recorder.seen[0]["access"], drive.Access(dirs=[self.repo], read_only=[CORE], commands=[]))

    def test_output_dir_outside_workdir_is_added(self):
        out = os.path.join(self.tmp.name, "elsewhere", "out.md")
        self.plan(out=out)
        self.assertEqual(Recorder.seen[0]["access"].dirs, [os.path.dirname(out)])

    def test_repo_ignored_without_repo_arg(self):
        self.plan()
        self.assertEqual(Recorder.seen[0]["access"], drive.Access(dirs=[], read_only=[CORE], commands=[]))

    def test_read_dir_overlapping_the_workdir_is_refused(self):
        for repo in (self.work, self.tmp.name):
            with self.assertRaises(compose.ConfigError) as cm:
                self.plan(repo=repo)
            self.assertIn("overlaps", str(cm.exception))

    def test_resume_needs_sid(self):
        with self.assertRaises(compose.ConfigError):
            self.plan(resume=True, sid=None)

    def test_new_session_gets_a_sid(self):
        self.plan(sid=None)
        self.assertRegex(Recorder.seen[0]["sid"], r"^[0-9a-f-]{36}$")

    def test_unknown_client(self):
        with self.assertRaises(compose.ConfigError) as cm:
            self.plan(client="nope")
        self.assertIn("unknown client 'nope'", str(cm.exception))


class Outcome(unittest.TestCase):
    def check(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
        try:
            return drive.outcome(f.name)
        finally:
            os.remove(f.name)

    def test_valid_statuses(self):
        for s in ("done", "needs_input", "failed"):
            self.assertEqual(self.check(f"---\nstatus: {s}\ntitle: t\n---\nbody\n"), s)

    def test_invalid(self):
        self.assertIsNone(self.check("no frontmatter\n"))
        self.assertIsNone(self.check("---\nstatus: maybe\n---\n"))
        self.assertIsNone(self.check("---\ntitle: t\n---\n"))
        self.assertIsNone(drive.outcome("/nonexistent/out.md"))


class Main(Base):
    def run_main(self, *extra, rc=0, write=None):
        calls = []

        def fake(argv, **kw):
            calls.append((argv, kw))
            if write is not None:
                with open(os.path.join(self.work, "out.md"), "w") as f:
                    f.write(write)
            return subprocess.CompletedProcess(argv, rc)

        out, err = io.StringIO(), io.StringIO()
        argv = ["--role", "researcher", "--task", "light-research", "--input", "Research X.",
                "--out", os.path.join(self.work, "out.md"), "--workdir", self.work, "--client", "fake", *extra]
        with redirect_stdout(out), redirect_stderr(err):
            code = drive.main(argv, root=CORE, run=fake)
        return code, out.getvalue(), err.getvalue(), calls

    def test_dry_run_prints_plan_and_runs_nothing(self):
        code, out, _, calls = self.run_main("--dry-run")
        data = json.loads(out)
        self.assertEqual((code, calls), (0, []))
        self.assertEqual((data["argv"][0], data["cwd"], data["env"]), ("fake", self.work, {"FAKE": "1"}))

    def test_done_run_exits_0_and_reports_session(self):
        code, _, err, calls = self.run_main("--sid", SID, write="---\nstatus: done\n---\n")
        self.assertEqual(code, 0)
        self.assertIn(f"session {SID}", err)
        (argv, kw), = calls
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake", SID], self.work, "1"))
        self.assertEqual(kw["env"]["PATH"], os.environ["PATH"])

    def test_missing_output_exits_1(self):
        self.assertEqual(self.run_main()[0], 1)

    def test_client_failure_exits_3(self):
        self.assertEqual(self.run_main(rc=1, write="---\nstatus: done\n---\n")[0], 3)

    def test_config_error_exits_2(self):
        code, _, err, calls = self.run_main("--task", "essay")
        self.assertEqual((code, calls), (2, []))
        self.assertIn("drive.py:", err)


if __name__ == "__main__":
    unittest.main()
