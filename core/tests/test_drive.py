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
PARAMS = compose.RunParams(input="x", out="o", workdir="w", sid=SID)


def run(**kw):
    return compose.RunConfig(**{"role": "r", "task": "t", "tier": 2, "effort": "high", "output": {"type": "local"}, **kw})


class Recorder(clients.Client):
    """A fake client that needs no config file and records what the driver hands it."""
    needs_config = False
    seen = []

    def launch(self, prompt, run, *, params, access, schema):
        Recorder.seen.append(dict(prompt=prompt, run=run, params=params, access=access, schema=schema))
        return clients.Launch(["fake", params.sid], {"FAKE": "1"}, cwd=os.path.abspath(params.workdir))


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

    def params(self, **kw):
        return compose.RunParams(**{"input": "Research X.", "out": os.path.join(self.work, "out.md"),
                                    "workdir": self.work, "sid": SID, **kw})

    def plan(self, role="researcher", task="light-research", client="fake", repo=None, **params):
        return drive.plan(CORE, clients.get(client, CORE), role, task, params=self.params(**params), repo=repo)[0]


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
            "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(compose.outcome_schema(CORE)),
            "--allowedTools", f"Bash(python3 {CORE}/scripts/repo.py prepare --dir {self.work}/src *)"])
        self.assertIn("## Return\n\nReturn the outcome as your structured output", launch.argv[2])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_engineering_xhigh_and_repo_commands(self):
        argv = self.plan("engineer", "engineering", client="claude").argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools"] + [
            f"Bash(python3 {CORE}/scripts/repo.py {cmd} --dir {self.work}/src *)" for cmd in ("checkout", "status")])

    def test_resume(self):
        argv = self.plan(client="claude", resume=True).argv
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertTrue(argv[2].startswith("Resumed run"))

    def test_commands_and_task_rules_become_allowed_tools(self):
        c = claude(roles={"r": {"tasks": {"t": {"allow": ["WebFetch"]}}}})
        access = drive.Access(dirs=[], commands=["make test"])
        argv = c.launch("p", run(), params=PARAMS, access=access, schema={}).argv
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools", "Bash(make test)", "WebFetch"])

    def test_no_rule_groups_when_empty(self):
        argv = claude().launch("p", run(), params=PARAMS, access=NO_ACCESS, schema={}).argv
        for flag in ("--add-dir", "--disallowedTools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_braces_in_the_input_survive(self):
        argv = self.plan(client="claude", input="Explain {{x}} templates.").argv
        self.assertTrue(argv[2].rstrip().endswith("Explain {{x}} templates."))

    def test_unmapped_tier(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(tiers={}).launch("p", run(), params=PARAMS, access=NO_ACCESS, schema={})
        self.assertIn("no model for tier 2", str(cm.exception))

    def test_role_value_applies_to_its_tasks_and_task_value_wins(self):
        c = claude(allow=["Read"], roles={"r": {"allow": ["WebFetch"], "tasks": {"u": {"allow": ["Grep"]}}}})
        self.assertEqual(c.value(run(), "allow"), ["WebFetch"])
        self.assertEqual(c.value(run(task="u"), "allow"), ["Grep"])
        self.assertEqual(c.value(run(role="x"), "allow"), ["Read"])
        self.assertIsNone(c.value(run(role="x"), "nothing"))

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
        self.assertEqual(seen["access"], drive.Access(dirs=[], commands=[f"python3 {CORE}/scripts/repo.py prepare --dir {self.work}/src *"]))
        self.assertEqual((seen["params"].sid, seen["params"].resume, seen["run"].task), (SID, False, "light-research"))
        self.assertTrue(seen["prompt"].startswith("# Principles"))
        self.assertEqual((launch.argv, launch.env, launch.cwd), (["fake", SID], {"FAKE": "1"}, self.work))

    def test_repo_entry_binds_to_the_repo_arg(self):
        acc = drive.access(run(write=["repo"], commands=["{{scripts}}/x --dir {{workdir}}/src *"]), self.params(),
                           repo=self.repo, scripts="/s")
        self.assertEqual(acc, drive.Access(dirs=[self.repo], commands=[f"/s/x --dir {self.work}/src *"]))

    def test_the_run_never_needs_the_out_dir(self):
        self.plan(out=os.path.join(self.tmp.name, "elsewhere", "out.md"))
        self.assertEqual(Recorder.seen[0]["access"].dirs, [])

    def test_repo_ignored_without_repo_arg(self):
        self.plan("engineer", "engineering")
        self.assertEqual(Recorder.seen[0]["access"].dirs, [])

    def test_resume_needs_sid(self):
        with self.assertRaises(compose.ConfigError):
            self.plan(resume=True, sid=None)

    def test_new_session_gets_a_sid(self):
        self.plan(sid=None)
        self.assertRegex(Recorder.seen[0]["params"].sid, r"^[0-9a-f-]{36}$")

    def test_unknown_client(self):
        with self.assertRaises(compose.ConfigError) as cm:
            self.plan(client="nope")
        self.assertIn("unknown client 'nope'", str(cm.exception))

    def test_plan_and_export_each_need_their_kind_of_client(self):
        with self.assertRaises(compose.ConfigError):
            self.plan(client="skill")
        with self.assertRaises(compose.ConfigError):
            drive.export(CORE, claude(), "dummy-tester", dest=self.tmp.name)


class Skill(Base):
    def export(self, role, task=None, dest=None):
        return drive.export(CORE, clients.get("skill", CORE), role, task, dest=dest or self.tmp.name)

    def skill_text(self, role, task):
        return self.export(role, task).files[os.path.join(self.tmp.name, f"{role}-{task}", "SKILL.md")]

    def test_writes_one_skill_file_and_no_command(self):
        skills = os.path.join(self.tmp.name, "skills")
        launch = self.export("researcher", "light-research", dest=skills)
        self.assertEqual(launch.argv, [])
        skill = os.path.join(skills, "researcher-light-research")
        self.assertEqual(sorted(launch.files), [os.path.join(skill, "SKILL.md"), os.path.join(skill, "scripts", "repo.py")])
        text = launch.files[os.path.join(skill, "SKILL.md")]
        with open(os.path.join(CORE, "scripts", "repo.py")) as f:
            self.assertEqual(launch.files[os.path.join(skill, "scripts", "repo.py")], f.read())
        head, body = text.split("\n---\n", 1)
        self.assertEqual(head.splitlines()[:2], ["---", "name: researcher-light-research"])
        self.assertIn('description: "Quick research on a question', head)
        self.assertTrue(body.lstrip().startswith("# Principles"))
        self.assertIn("Input: $ARGUMENTS", body)
        self.assertIn("`python3 ${CLAUDE_SKILL_DIR}/scripts/repo.py prepare ", body)
        self.assertNotIn(self.tmp.name, body)
        self.assertNotIn(CORE, body)

    def test_skills_without_scripts_get_only_skill_md(self):
        self.assertEqual(list(self.export("pm", "product-design").files),
                         [os.path.join(self.tmp.name, "pm-product-design", "SKILL.md")])

    def test_document_tasks_return_to_the_orchestrator(self):
        for role, task in (("researcher", "light-research"), ("pm", "product-design")):
            text = self.skill_text(role, task)
            self.assertIn("publish, post or save it nowhere. Leave `url` empty.", text)
            self.assertNotIn("ophis/private_docs", text)
            self.assertIn("## Return\n\nEnd with your final reply in this conversation", text)
            self.assertNotIn("Output: ", text)
            self.assertNotIn("## Resume", text)

    def test_pull_request_stays(self):
        text = self.skill_text("engineer", "engineering")
        self.assertIn("gh pr create", text)
        self.assertNotIn("publish, post or save it nowhere", text)

    def test_description_falls_back_to_the_task_heading(self):
        r = run(role_title="R", task_title="T", task_summary="Do it.", output={"type": "orchestrator"})
        (_, text), = clients.SkillClient({}).export("p", r, dest="o").files.items()
        self.assertIn('description: "T as R: Do it."', text)

    def test_every_task_has_a_skill_description(self):
        skill = clients.load_config("skill", CORE)["roles"]
        with open(os.path.join(CORE, "config", "config.toml"), "rb") as f:
            pairs = [(r, t) for r, role in tomllib.load(f)["roles"].items() for t in role.get("tasks", {})]
        for r, t in pairs:
            self.assertTrue(skill.get(r, {}).get("tasks", {}).get(t, {}).get("description"), (r, t))

    def test_every_task_becomes_a_skill(self):
        for role, task in (("researcher", "deep-research"), ("pm", "product-design"), ("engineer", "engineering"),
                           ("dummy-tester", "echo")):
            self.assertNotIn("{{", self.skill_text(role, task))

    def test_main_writes_the_file_and_runs_nothing(self):
        skills = os.path.join(self.tmp.name, "skills")
        calls = []
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = drive.main(["--role", "dummy-tester", "--client", "skill", "--out", skills], root=CORE,
                              popen=lambda *a, **k: calls.append(a))
        path = os.path.join(skills, "dummy-tester-echo", "SKILL.md")
        self.assertEqual((code, calls), (0, []))
        with open(path) as f:
            self.assertTrue(f.read().startswith("---\nname: dummy-tester-echo\n"))
        self.assertIn(f"wrote {path}", err.getvalue())


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout, self.rc = lines, rc

    def wait(self):
        return self.rc


def stream(*events):
    return [json.dumps(e) + "\n" for e in events]


def result(outcome, turns=3):
    return {"type": "result", "subtype": "success", "num_turns": turns, "structured_output": outcome}


def said(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}


class ClaudeEvents(unittest.TestCase):
    def test_text_progress_and_the_last_outcome(self):
        lines = stream(said("Starting.\nProgress: budget 2 rounds"), result(None, 0), result(DONE),
                       {"type": "system", "subtype": "task_updated"})
        events = list(claude().events(["not json\n", *lines]))
        self.assertEqual([(e.kind, e.text) for e in events if e.kind != "outcome"],
                         [("text", "not json"), ("text", "Starting."), ("progress", "budget 2 rounds")])
        self.assertEqual([e.outcome for e in events if e.kind == "outcome"], [DONE])


class Validate(Base):
    def check(self, data, output=None):
        return drive.validate(data, run(output=output or {"type": "local"}), self.params())

    def fails(self, data, msg, output=None):
        with self.assertRaises(drive.InvalidOutcome) as cm:
            self.check(data, output)
        self.assertIn(msg, str(cm.exception))

    def test_valid(self):
        self.assertEqual(self.check(DONE), drive.Outcome("done", "T", "S", deliverable="# Doc\n"))

    def test_status_title_and_questions(self):
        self.fails({**DONE, "status": "maybe"}, "status 'maybe'")
        self.fails({**DONE, "title": "a\nb"}, "one line")
        self.fails({**DONE, "status": "needs_input"}, "1–4 questions")
        self.assertEqual(self.check({**DONE, "questions": ["q"]}).questions, [])

    def test_url_must_fit_the_destination(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        self.assertEqual(self.check({**DONE, "url": "https://github.com/o/docs/blob/main/R/x.md"}, gh).url,
                         "https://github.com/o/docs/blob/main/R/x.md")
        self.fails({**DONE, "url": "https://evil.example/x"}, "url must start with", gh)
        self.fails({**DONE, "url": "file:///etc/passwd"}, "https link", {"type": "pull-request"})
        self.assertEqual(self.check({**DONE, "url": "https://x"}).url, "")

    def test_files_stay_under_the_workdir(self):
        os.makedirs(self.work)
        inside = os.path.join(self.work, "plan.md")
        self.assertEqual(self.check({**DONE, "files": [inside]}).files, [os.path.realpath(inside)])
        self.fails({**DONE, "files": [os.path.expanduser("~/.ssh/id_rsa")]}, "not a .md file under the workdir")
        self.fails({**DONE, "files": [os.path.join(self.work, "..", "x.md")]}, "not a .md file under the workdir")


class Start(Base):
    def start(self, lines, rc=0, output=None, **params):
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work)
        calls, log = [], io.StringIO()

        def popen(argv, **kw):
            calls.append((argv, kw))
            return FakeProc(lines, rc)

        r = drive.start(launch, run(output=output or {"type": "local"}), self.params(**params), client=claude(),
                        popen=popen, log=log)
        return r, calls, log.getvalue()

    def read(self, name):
        with open(os.path.join(self.work, name)) as f:
            return f.read()

    def test_done_run_saves_outcome_deliverable_and_progress(self):
        r, ((argv, kw),), log = self.start(stream(said("Progress: half way"), result(DONE)))
        out = os.path.join(self.work, "out.md")
        self.assertEqual((r.returncode, r.outcome.status, r.outcome.url), (0, "done", out))
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake"], self.work, "1"))
        self.assertEqual(self.read("out.md"), "# Doc\n")
        self.assertEqual(json.loads(self.read("outcome.json"))["url"], out)
        self.assertEqual(json.loads(self.read("progress.jsonl"))["text"], "half way")
        self.assertIn("Progress: half way", log)

    def test_github_destination_keeps_the_runs_url_and_saves_no_deliverable(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        url = "https://github.com/o/docs/blob/main/x.md"
        r, _, _ = self.start(stream(result({**DONE, "url": url})), output=gh)
        self.assertEqual(r.outcome.url, url)
        self.assertFalse(os.path.exists(os.path.join(self.work, "out.md")))

    def test_no_outcome(self):
        r, _, _ = self.start(stream(said("bye")))
        self.assertEqual((r.returncode, r.outcome, r.error), (0, None, "the run returned no outcome"))

    def test_failed_client_ignores_the_outcome(self):
        r, _, _ = self.start(stream(result(DONE)), rc=143)
        self.assertEqual((r.returncode, r.outcome), (143, None))

    def test_invalid_outcome(self):
        r, _, _ = self.start(stream(result({**DONE, "status": "maybe"})))
        self.assertIn("invalid outcome: status 'maybe'", r.error)

    def test_a_stale_outcome_is_removed_first(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, "outcome.json"), "w") as f:
            f.write("{}")
        self.start(stream(said("bye")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "outcome.json")))

    def test_resume_appends_progress(self):
        self.start(stream(said("Progress: one"), result(DONE)))
        self.start(stream(said("Progress: two"), result(DONE)), resume=True)
        self.assertEqual([json.loads(l)["text"] for l in self.read("progress.jsonl").splitlines()], ["one", "two"])


class Main(Base):
    def run_main(self, *extra, lines=(), rc=0):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return FakeProc(list(lines), rc)

        out, err = io.StringIO(), io.StringIO()
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, *extra]
        with redirect_stdout(out), redirect_stderr(err):
            code = drive.main(argv, root=CORE, popen=popen)
        return code, out.getvalue(), err.getvalue(), calls

    def test_dry_run_prints_plan_and_runs_nothing(self):
        code, out, _, calls = self.run_main("--dry-run")
        data = json.loads(out)
        self.assertEqual((code, calls), (0, []))
        self.assertEqual((data["argv"][0], data["cwd"]), ("claude", self.work))

    def test_done_run_exits_0_and_reports_session(self):
        code, _, err, calls = self.run_main("--sid", SID, lines=stream(result(DONE)))
        self.assertEqual(code, 0)
        self.assertIn(f"session {SID}", err)
        self.assertIn("status done", err)
        (argv, kw), = calls
        self.assertEqual((argv[0], kw["cwd"]), ("claude", self.work))
        self.assertEqual(kw["env"]["PATH"], os.environ["PATH"])

    def test_no_outcome_exits_1(self):
        code, _, err, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("no outcome", err)

    def test_client_failure_exits_3(self):
        self.assertEqual(self.run_main(lines=stream(result(DONE)), rc=1)[0], 3)

    def test_config_error_exits_2(self):
        code, _, err, calls = self.run_main("--task", "essay")
        self.assertEqual((code, calls), (2, []))
        self.assertIn("drive.py:", err)

    def test_a_run_client_needs_input_and_workdir(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = drive.main(["--role", "dummy-tester", "--out", "o.md"], root=CORE)
        self.assertEqual(code, 2)
        self.assertIn("needs --input and --workdir", err.getvalue())


if __name__ == "__main__":
    unittest.main()
