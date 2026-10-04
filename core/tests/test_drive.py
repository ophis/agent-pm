import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "11111111-2222-3333-4444-555555555555"
NO_ACCESS = drive.Access(dirs=[], commands=[])
RUN = {"role": "r", "task": "t", "tier": 2, "effort": "high"}


class Recorder(clients.Client):
    """A fake client that needs no config file and records what the driver hands it."""
    needs_config = False
    seen = []

    def launch(self, prompt, run, *, sid, resume, access, out):
        Recorder.seen.append(dict(prompt=prompt, run=run, sid=sid, resume=resume, access=access, out=out))
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
    def test_no_deny_rules(self):
        for role, task in (("researcher", "light-research"), ("engineer", "engineering")):
            self.assertNotIn("--disallowedTools", self.plan(role, task, client="claude", repo=self.repo).argv)

    def test_light_research_argv(self):
        launch = self.plan(client="claude", repo=self.repo)
        self.assertEqual(launch.argv[:2], ["claude", "-p"])
        self.assertTrue(launch.argv[2].startswith("# Principles"))
        self.assertEqual(launch.argv[3:], [
            "--session-id", SID, "--model", "opus", "--effort", "high",
            "--permission-mode", "auto", "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", self.repo])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_engineering_xhigh_and_writable_repo(self):
        argv = self.plan("engineer", "engineering", client="claude", repo=self.repo).argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertIn(self.repo, argv)

    def test_resume(self):
        argv = self.plan(client="claude", resume=True).argv
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertTrue(argv[2].startswith("Resumed run"))

    def test_commands_and_task_rules_become_allowed_tools(self):
        c = claude(roles={"r": {"tasks": {"t": {"allow": ["WebFetch"]}}}})
        access = drive.Access(dirs=[], commands=["make test"])
        argv = c.launch("p", RUN, sid=SID, resume=False, access=access, out="o").argv
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools", "Bash(make test)", "WebFetch"])

    def test_no_rule_groups_when_empty(self):
        argv = claude().launch("p", RUN, sid=SID, resume=False, access=NO_ACCESS, out="o").argv
        for flag in ("--add-dir", "--disallowedTools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_braces_in_the_input_survive(self):
        argv = self.plan(client="claude", input="Explain {{x}} templates.").argv
        self.assertTrue(argv[2].rstrip().endswith("Explain {{x}} templates."))

    def test_unmapped_tier(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(tiers={}).launch("p", RUN, sid=SID, resume=False, access=NO_ACCESS, out="o")
        self.assertIn("no model for tier 2", str(cm.exception))

    def test_role_value_applies_to_its_tasks_and_task_value_wins(self):
        c = claude(allow=["Read"], roles={"r": {"allow": ["WebFetch"], "tasks": {"u": {"allow": ["Grep"]}}}})
        self.assertEqual(c.value({"role": "r", "task": "t"}, "allow"), ["WebFetch"])
        self.assertEqual(c.value({"role": "r", "task": "u"}, "allow"), ["Grep"])
        self.assertEqual(c.value({"role": "x", "task": "t"}, "allow"), ["Read"])
        self.assertIsNone(c.value({"role": "x", "task": "t"}, "nothing"))

    def test_claude_tier_override_changes_the_model(self):
        with unittest.mock.patch.object(clients, "load_config",
                                        lambda name, root: {**clients.base.load_config(name, root), "tier": 3}):
            argv = self.plan(client="claude").argv
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_unknown_config_key(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(argv=[])
        self.assertIn("unknown key 'argv'", str(cm.exception))


class Generic(Base):
    def test_driver_hands_the_client_neutral_access(self):
        launch = self.plan(repo=self.repo)
        seen, = Recorder.seen
        self.assertEqual(seen["access"], drive.Access(dirs=[self.repo], commands=[]))
        self.assertEqual((seen["sid"], seen["resume"], seen["run"]["task"]), (SID, False, "light-research"))
        self.assertTrue(seen["prompt"].startswith("# Principles"))
        self.assertEqual((launch.argv, launch.env, launch.cwd), (["fake", SID], {"FAKE": "1"}, self.work))

    def test_writable_repo_is_reachable(self):
        self.plan("engineer", "engineering", repo=self.repo)
        self.assertEqual(Recorder.seen[0]["access"], drive.Access(dirs=[self.repo], commands=[]))

    def test_output_dir_outside_workdir_is_added(self):
        out = os.path.join(self.tmp.name, "elsewhere", "out.md")
        self.plan(out=out)
        self.assertEqual(Recorder.seen[0]["access"].dirs, [os.path.dirname(out)])

    def test_repo_ignored_without_repo_arg(self):
        self.plan()
        self.assertEqual(Recorder.seen[0]["access"], drive.Access(dirs=[], commands=[]))

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


class Skill(Base):
    def test_writes_one_skill_file_and_no_command(self):
        skills = os.path.join(self.tmp.name, "skills")
        launch = drive.plan(CORE, "researcher", "light-research", client="skill", input=None, out=skills, workdir=None)
        self.assertEqual(launch.argv, [])
        (path, text), = launch.files.items()
        self.assertEqual(path, os.path.join(skills, "researcher-light-research", "SKILL.md"))
        head, body = text.split("\n---\n", 1)
        self.assertEqual(head.splitlines()[:2], ["---", "name: researcher-light-research"])
        self.assertIn('description: "Quick research on a question', head)
        self.assertTrue(body.lstrip().startswith("# Principles"))
        self.assertIn("Input: $ARGUMENTS", body)
        self.assertNotIn(self.tmp.name, body)

    def skill_text(self, role, task):
        launch = drive.plan(CORE, role, task, client="skill", input=None, out=self.tmp.name, workdir=None)
        (_, text), = launch.files.items()
        return text

    def test_document_tasks_return_to_the_orchestrator(self):
        for role, task in (("researcher", "light-research"), ("pm", "product-design")):
            text = self.skill_text(role, task)
            self.assertIn("Return the deliverable to your orchestrator", text)
            self.assertNotIn("ophis/private_docs", text)
            self.assertIn("Output: your final reply to the orchestrator", text)

    def test_pull_request_stays(self):
        text = self.skill_text("engineer", "engineering")
        self.assertIn("gh pr create", text)
        self.assertNotIn("Return the deliverable to your orchestrator", text)

    def test_client_entries_replace_neutral_values(self):
        run = {"role": "r", "task": "t", "tier": 2, "effort": "high", "read": [], "write": [], "commands": [],
               "templates": [], "output": {"type": "github", "repo": "o/d"}}
        self.assertEqual(drive.override(clients.SkillClient({}), dict(run)), run)
        c = clients.SkillClient({"effort": "low", "roles": {"r": {"tasks": {"t": {"tier": 3, "output": {"type": "orchestrator"}}}}}})
        got = drive.override(c, dict(run))
        self.assertEqual((got["tier"], got["effort"], got["output"]), (3, "low", {"type": "orchestrator"}))

    def test_invalid_override_is_a_config_error(self):
        run = {"role": "r", "task": "t", "tier": 2, "effort": "high", "output": {"type": "local"}}
        with self.assertRaises(compose.ConfigError):
            drive.override(clients.SkillClient({"tier": 9}), run)

    def test_description_falls_back_to_the_task_heading(self):
        run = {"role": "r", "task": "t", "role_title": "R", "task_title": "T", "task_summary": "Do it.",
               "output": {"type": "orchestrator"}}
        (_, text), = clients.SkillClient({}).launch("p", run, sid=None, resume=False, access=NO_ACCESS, out="o").files.items()
        self.assertIn('description: "T as R: Do it."', text)

    def test_every_task_has_a_skill_description(self):
        skill = clients.load_config("skill", CORE)["roles"]
        with open(os.path.join(CORE, "config.toml"), "rb") as f:
            pairs = [(r, t) for r, role in tomllib.load(f)["roles"].items() for t in role.get("tasks", {})]
        for r, t in pairs:
            self.assertTrue(skill.get(r, {}).get("tasks", {}).get(t, {}).get("description"), (r, t))

    def test_every_task_becomes_a_skill(self):
        for role, task in (("researcher", "deep-research"), ("pm", "product-design"), ("engineer", "engineering"),
                           ("dummy-tester", "echo")):
            launch = drive.plan(CORE, role, task, client="skill", input=None, out=self.tmp.name, workdir=None)
            (path, text), = launch.files.items()
            self.assertTrue(path.endswith(f"{role}-{task}/SKILL.md"))
            self.assertNotIn("{{", text)

    def test_main_writes_the_file_and_runs_nothing(self):
        skills = os.path.join(self.tmp.name, "skills")
        calls = []
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = drive.main(["--role", "dummy-tester", "--client", "skill", "--out", skills], root=CORE,
                              run=lambda *a, **k: calls.append(a))
        path = os.path.join(skills, "dummy-tester-echo", "SKILL.md")
        self.assertEqual((code, calls), (0, []))
        with open(path) as f:
            self.assertTrue(f.read().startswith("---\nname: dummy-tester-echo\n"))
        self.assertIn(f"wrote {path}", err.getvalue())

    def test_claude_still_needs_input_and_workdir(self):
        with self.assertRaises(compose.ConfigError) as cm:
            drive.plan(CORE, "dummy-tester", client="claude", input=None, out="o.md", workdir=None)
        self.assertIn("needs --input", str(cm.exception))


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
