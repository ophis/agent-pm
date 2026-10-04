import dataclasses
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

    def plan(self, role="researcher", task="light-research", client="fake", repo=None, layers=(), **params):
        return drive.plan(CORE, clients.get(client, CORE), role, task, params=self.params(**params), repo=repo,
                          layers=layers)[0]


class Claude(Base):
    def test_no_deny_rules(self):
        for role, task in (("researcher", "light-research"), ("engineer", "engineering")):
            self.assertNotIn("--disallowedTools", self.plan(role, task, client="claude", repo=self.repo).argv)

    def test_light_research_argv(self):
        launch = self.plan(client="claude", repo=self.repo)
        self.assertEqual(launch.argv[:2], ["claude", "-p"])
        self.assertTrue(launch.argv[2].startswith("# Guide"))
        self.assertEqual(launch.argv[3:], [
            "--session-id", SID, "--model", "opus", "--effort", "high",
            "--permission-mode", "auto", "--setting-sources", "user", "--strict-mcp-config",
            "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(compose.outcome_schema(CORE)),
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py prepare --dir {self.work}/src *)"])
        self.assertIn("## Return\n\nReturn the outcome as your structured output", launch.argv[2])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_engineering_xhigh_and_repo_commands(self):
        argv = self.plan("engineer", "engineering", client="claude").argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools"] + [
            f"Bash(python3 {CORE}/src/repo.py {cmd} --dir {self.work}/src *)" for cmd in ("checkout", "status")])

    def test_product_design_argv_has_the_prepare_rule(self):
        argv = self.plan("pm", "product-design", client="claude").argv
        self.assertEqual(argv[argv.index("--allowedTools"):], [
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py prepare --dir {self.work}/src *)"])
        self.assertIn(f"`python3 {CORE}/src/repo.py prepare --dir <Workdir>/src <repo>`", argv[2])

    def test_the_gate_is_pre_approved_verbatim(self):
        gate = "python3 /u/usage.py --below 80"
        argv = self.plan("researcher", "deep-research", client="claude", repo=self.repo,
                         layers=[{"gate": gate}]).argv
        self.assertEqual(argv[argv.index("--allowedTools"):][-1], f"Bash({gate})")
        self.assertIn(f"the gate is `{gate}`", argv[2])

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
        self.assertEqual(seen["access"], drive.Access(dirs=[], commands=[f"python3 {CORE}/src/repo.py prepare --dir {self.work}/src *"]))
        self.assertEqual((seen["params"].sid, seen["params"].resume, seen["run"].task), (SID, False, "light-research"))
        self.assertTrue(seen["prompt"].startswith("# Guide"))
        self.assertEqual((launch.argv, launch.env, launch.cwd), (["fake", SID], {"FAKE": "1"}, self.work))

    def test_layers_apply_after_the_clients_config(self):
        client = Recorder({"tier": 3, "effort": "low"})
        drive.plan(CORE, client, "researcher", "light-research", params=self.params())
        drive.plan(CORE, client, "researcher", "light-research", params=self.params(), layers=[{"tier": 4}, {"tier": 1}])
        plain, layered = (s["run"] for s in Recorder.seen)
        self.assertEqual((plain.tier, plain.effort), (3, "low"))
        self.assertEqual((layered.tier, layered.effort), (1, "low"))

    def test_access_appends_the_gate_after_the_filled_commands(self):
        gate = "python3 /u/usage.py {{workdir}} *"
        acc = drive.access(run(commands=["{{scripts}}/x *"], gate=gate), self.params(), repo=None, scripts="/s")
        self.assertEqual(acc.commands, ["/s/x *", gate])
        self.assertEqual(drive.access(run(commands=["x"]), self.params(), repo=None, scripts="/s").commands, ["x"])

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
        with open(os.path.join(CORE, "src", "repo.py")) as f:
            self.assertEqual(launch.files[os.path.join(skill, "scripts", "repo.py")], f.read())
        head, body = text.split("\n---\n", 1)
        self.assertEqual(head.splitlines()[:2], ["---", "name: researcher-light-research"])
        self.assertIn('description: "Quick research on a question', head)
        self.assertTrue(body.lstrip().startswith("# Guide"))
        self.assertIn("Input: $ARGUMENTS", body)
        self.assertIn("`python3 ${CLAUDE_SKILL_DIR}/scripts/repo.py prepare ", body)
        self.assertNotIn(self.tmp.name, body)
        self.assertNotIn(CORE, body)

    def test_skills_without_scripts_get_only_skill_md(self):
        r = run(role_title="R", task_title="T", task_summary="Do it.", output={"type": "orchestrator"})
        self.assertEqual(list(clients.SkillClient({}).export("No scripts.", r, dest="o").files),
                         [os.path.join(os.path.abspath("o"), "r-t", "SKILL.md")])

    def test_product_design_skill_gets_repo_py(self):
        skill = os.path.join(self.tmp.name, "pm-product-design")
        files = self.export("pm", "product-design").files
        self.assertEqual(sorted(files), [os.path.join(skill, "SKILL.md"), os.path.join(skill, "scripts", "repo.py")])
        self.assertIn("`python3 ${CLAUDE_SKILL_DIR}/scripts/repo.py prepare --dir <Workdir>/src <repo>`",
                      files[os.path.join(skill, "SKILL.md")])

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

    def kill(self):
        self.killed = True


def stream(*events):
    return [json.dumps(e) + "\n" for e in events]


def result(outcome, turns=3):
    return {"type": "result", "subtype": "success", "num_turns": turns, "structured_output": outcome}


def said(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}


class ClaudeEvents(unittest.TestCase):
    def kinds(self, *lines):
        return [(e.kind, e.name, e.text) for e in claude().events(lines) if e.kind != "outcome"]

    def outcomes(self, *lines):
        return [e.outcome for e in claude().events(lines) if e.kind == "outcome"]

    def test_text_and_progress_lines(self):
        lines = stream(said("Starting.\n- [agent-pm-progress:start]  2 rounds, cap 80\n\n"
                            "* `[agent-pm-progress:round_1]` one\n**[agent-pm-progress:x-y]** \n"
                            "Progress: not a report\nsee [agent-pm-progress:x] mid-line"))
        self.assertEqual(self.kinds("not json\n", *lines), [
            ("text", "", "not json"), ("text", "", "Starting."), ("progress", "start", "2 rounds, cap 80"),
            ("progress", "round_1", "one"), ("progress", "x-y", ""), ("text", "", "Progress: not a report"),
            ("text", "", "see [agent-pm-progress:x] mid-line")])

    def test_every_structured_result_is_an_outcome_the_driver_keeps_the_last(self):
        lines = stream(result(None, 0), result(DONE), {"type": "system", "subtype": "task_updated"})
        self.assertEqual(self.outcomes(*lines), [DONE])

    def test_odd_json_is_skipped(self):
        lines = ["null\n", "3\n", "[]\n", '"text"\n', *stream(
            {"type": "assistant"}, {"type": "assistant", "message": {"content": "x"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}, {"type": "thinking"}, 7]}},
            result("a string"), result(["a", "list"]))]
        self.assertEqual((self.kinds(*lines), self.outcomes(*lines)), ([], []))


class Handover(unittest.TestCase):
    def test_each_client_names_the_progress_mark(self):
        for c in (claude(), clients.SkillClient({})):
            self.assertIn(f"[{clients.PROGRESS}:<name>]", c.handover(), type(c).__name__)
        self.assertIn("structured output", claude().handover())
        for c in (claude(), clients.SkillClient({})):
            self.assertIn("before calling the next tool, send a text message containing only that line",
                          c.handover(), type(c).__name__)
        self.assertNotIn("ultracode", claude().handover())
        self.assertNotIn("progress:budget", claude().handover())

    def test_the_base_client_returns_no_outcome(self):
        self.assertEqual(clients.Client({}).handover(), "")
        self.assertEqual([e.kind for e in clients.Client({}).events(['{"type": "result"}\n'])], ["text"])


class Schema(unittest.TestCase):
    def test_schema_and_validate_agree(self):
        schema = compose.outcome_schema(CORE)
        self.assertEqual(tuple(schema["properties"]["status"]["enum"]), drive.STATUSES)
        self.assertEqual(set(schema["properties"]), {f.name for f in dataclasses.fields(drive.Outcome)})
        self.assertEqual(schema["properties"]["questions"]["maxItems"], 4)


class Validate(Base):
    def check(self, data, output=None):
        return drive.validate(data, run(output=output or {"type": "local"}), self.params())

    def fails(self, data, msg, output=None):
        with self.assertRaises(drive.InvalidOutcome) as cm:
            self.check(data, output)
        self.assertIn(msg, str(cm.exception))

    def touch(self, *parts, text="x"):
        path = os.path.join(self.work, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_valid(self):
        self.assertEqual(self.check({**DONE, "title": "  T  "}), drive.Outcome("done", "T", "S", deliverable="# Doc\n"))

    def test_shape(self):
        self.fails([DONE], "not an object")
        self.fails({**DONE, "status": "maybe"}, "status 'maybe'")
        self.fails({**DONE, "summary": 3}, "must be text")
        self.fails({**DONE, "deliverable": ["x"]}, "must be text")
        self.fails({**DONE, "files": "x.md"}, "must be lists")

    def test_title(self):
        for title in ("", "  ", "a\nb", "a\x1b[2Jb", 5):
            self.fails({**DONE, "title": title}, "printable", )

    def test_questions(self):
        ask = {**DONE, "status": "needs_input"}
        self.fails(ask, "1–4 questions")
        self.fails({**ask, "questions": ["q"] * 5}, "1–4 questions")
        self.assertEqual(self.check({**ask, "questions": ["q?"]}).questions, ["q?"])
        self.assertEqual(self.check({**DONE, "questions": ["q"]}).questions, [])

    def test_done_needs_the_deliverable_when_it_comes_back(self):
        for output in ({"type": "local"}, {"type": "orchestrator"}):
            self.fails({**DONE, "deliverable": " "}, "must carry the deliverable", output)
        self.assertEqual(self.check({**DONE, "deliverable": ""}, {"type": "pull-request"}).deliverable, "")
        self.assertEqual(self.check({**DONE, "status": "failed", "deliverable": ""}).status, "failed")

    def test_url_must_fit_the_destination(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        ok = "https://github.com/o/docs/blob/main/R/x.md"
        self.assertEqual(self.check({**DONE, "url": ok}, gh).url, ok)
        for bad in ("https://evil.example/o/docs/blob/main/x.md", "https://github.com/o/docs/blob/dev/x.md"):
            self.fails({**DONE, "url": bad}, "url must start with", gh)
        for bad in (ok + "/../../../../x/y", "https://github.com/o/docs/blob/main/%2e%2e/%2e%2e/x", ok + "\n)[x](y",
                    ok + " x", "http://github.com/o/docs/blob/main/x.md", 5):
            self.fails({**DONE, "url": bad}, "plain https link", gh)
        pr = {"type": "pull-request"}
        for good in ("https://github.com/o/n/pull/7", "https://ghe.io/o/n/compare/main...b?expand=1", "https://github.com/o/n/tree/b"):
            self.assertEqual(self.check({**DONE, "url": good}, pr).url, good)
        self.fails({**DONE, "url": "https://github.com/o/n/issues/1"}, "pull request, compare or tree", pr)
        self.assertEqual(self.check({**DONE, "url": "https://x"}).url, "")

    def test_files_are_single_link_md_files_under_the_workdir(self):
        plan = self.touch("docs", "plan.md")
        self.assertEqual(self.check({**DONE, "files": [plan, "docs/plan.md"]}).files, [os.path.realpath(plan)] * 2)
        os.makedirs(self.work + "2")
        sibling = os.path.join(self.work + "2", "x.md")
        with open(sibling, "w") as f:
            f.write("x")
        outside = os.path.join(self.tmp.name, "secret.md")
        with open(outside, "w") as f:
            f.write("key")
        os.symlink(outside, os.path.join(self.work, "link.md"))
        os.link(outside, os.path.join(self.work, "hard.md"))
        for bad in (self.touch("notes.txt"), os.path.join(self.work, "missing.md"), sibling, "link.md", "hard.md",
                    os.path.join(self.work, "..", "secret.md"), os.path.expanduser("~/.ssh/id_rsa"), 7):
            self.fails({**DONE, "files": [bad]}, "single-link .md file under the workdir")


class Start(Base):
    def start(self, lines, rc=0, output=None, sinks=None, progress=(), **params):
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work)
        calls, log = [], io.StringIO()

        def popen(argv, **kw):
            calls.append((argv, kw))
            self.proc = FakeProc(lines, rc)
            return self.proc

        p = self.params(**params)
        r = drive.start(launch, run(output=output or {"type": "local"}, progress=list(progress)), p, client=claude(), popen=popen,
                        sinks=sinks if sinks is not None else [drive.terminal(log), *drive.default_sinks(p)[1:]])
        return r, calls, log.getvalue()

    def read(self, name):
        with open(os.path.join(self.work, name)) as f:
            return f.read()

    def exists(self, name):
        return os.path.lexists(os.path.join(self.work, name))

    def test_done_run_saves_outcome_deliverable_and_progress(self):
        r, ((argv, kw),), log = self.start(stream(said("[agent-pm-progress:round] half way"), result(DONE)))
        out = os.path.join(self.work, "out.md")
        self.assertEqual((r.returncode, r.outcome.status, r.outcome.url), (0, "done", out))
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake"], self.work, "1"))
        self.assertEqual(self.read("out.md"), "# Doc\n")
        self.assertEqual(json.loads(self.read("outcome.json"))["url"], out)
        self.assertEqual(json.loads(self.read("progress.jsonl")) | {"ts": ""}, {"ts": "", "name": "round", "text": "half way"})
        self.assertIn("Progress (round): half way", log)

    def test_orchestrator_destination_saves_the_deliverable_with_no_url(self):
        r, _, _ = self.start(stream(result(DONE)), output={"type": "orchestrator"})
        self.assertEqual((r.outcome.url, self.read("out.md")), ("", "# Doc\n"))

    def test_github_destination_keeps_the_runs_url_and_saves_no_deliverable(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        url = "https://github.com/o/docs/blob/main/x.md"
        r, _, _ = self.start(stream(result({**DONE, "url": url})), output=gh)
        self.assertEqual(r.outcome.url, url)
        self.assertFalse(self.exists("out.md"))

    def test_needs_input_reaches_the_outcome_file(self):
        self.start(stream(result({**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""})))
        self.assertEqual(json.loads(self.read("outcome.json"))["questions"], ["Which repo?"])

    def test_resumed_stream_keeps_the_last_outcome(self):
        r, _, _ = self.start(stream(result(None, 0), result({**DONE, "title": "real"}), {"type": "system"}))
        self.assertEqual(r.outcome.title, "real")

    def test_no_outcome(self):
        r, _, _ = self.start(stream(said("bye")))
        self.assertEqual((r.returncode, r.outcome, r.error), (0, None, "the run returned no outcome"))

    def test_failed_client_ignores_the_outcome_and_leaves_no_stale_files(self):
        os.makedirs(self.work)
        for name in ("outcome.json", "out.md"):
            with open(os.path.join(self.work, name), "w") as f:
                f.write("old")
        seen = []
        r, _, _ = self.start(stream(result(DONE)), rc=143, sinks=[seen.append, *drive.default_sinks(self.params())[1:]])
        self.assertEqual((r.returncode, r.outcome, seen), (143, None, []))
        self.assertFalse(self.exists("outcome.json") or self.exists("out.md"))

    def test_invalid_outcome(self):
        r, _, _ = self.start(stream(result({**DONE, "status": "maybe"})))
        self.assertIn("invalid outcome: status 'maybe'", r.error)

    def test_a_symlink_planted_during_the_run_is_replaced_not_followed(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def lines():
            for name in ("outcome.json", "out.md"):
                os.symlink(target, os.path.join(self.work, name))
            yield from stream(result(DONE))

        self.start(lines())
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.assertFalse(os.path.islink(os.path.join(self.work, "outcome.json")))
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def test_progress_refuses_a_planted_symlink_and_stops_the_run(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def lines():
            os.remove(os.path.join(self.work, "progress.jsonl"))
            os.symlink(target, os.path.join(self.work, "progress.jsonl"))
            yield from stream(said("[agent-pm-progress:round] $(curl evil)"))

        with self.assertRaises(OSError):
            self.start(lines())
        self.assertTrue(self.proc.killed)
        with open(target) as f:
            self.assertEqual(f.read(), "mine")

    def test_sinks_replace_the_defaults(self):
        seen = []
        r, _, log = self.start(stream(said("hi\n[agent-pm-progress:round] one"), result(DONE)), sinks=[seen.append])
        self.assertEqual([(e.kind, e.name, e.text) for e in seen[:2]], [("text", "", "hi"), ("progress", "round", "one")])
        self.assertEqual((seen[2].kind, seen[2].outcome["url"]), ("outcome", os.path.join(self.work, "out.md")))
        self.assertEqual(log, "")
        self.assertFalse(self.exists("progress.jsonl") or self.exists("outcome.json"))
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def test_every_sink_gets_every_event(self):
        a, b = [], []
        self.start(stream(said("hi"), result(DONE)), sinks=[a.append, b.append])
        self.assertEqual([e.kind for e in a], ["text", "outcome"])
        self.assertEqual(a, b)

    def test_an_invalid_outcome_reaches_no_sink(self):
        seen = []
        self.start(stream(result({**DONE, "status": "maybe"})), sinks=[seen.append])
        self.assertEqual(seen, [])

    def test_resume_appends_progress_a_new_run_resets_it(self):
        self.start(stream(said("[agent-pm-progress:round] one"), result(DONE)))
        self.start(stream(said("[agent-pm-progress:round] 二"), result(DONE)), resume=True)
        self.assertEqual([json.loads(l)["text"] for l in self.read("progress.jsonl").splitlines()], ["one", "二"])
        self.assertIn("二", self.read("progress.jsonl"))
        self.start(stream(said("[agent-pm-progress:round] three"), result(DONE)))
        self.assertEqual([json.loads(l)["text"] for l in self.read("progress.jsonl").splitlines()], ["three"])


    FAILED = {**DONE, "status": "failed", "deliverable": ""}
    NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}

    def missing(self, *events, progress=("start", "round"), **params):
        seen, err = [], io.StringIO()
        with redirect_stderr(err):
            r, _, _ = self.start(stream(*events), sinks=[seen.append], progress=progress, **params)
        return r, [e.name for e in seen if e.kind == "missing"], err.getvalue(), [e.kind for e in seen]

    def test_done_or_failed_without_start_reports_it_missing(self):
        for outcome in (DONE, self.FAILED):
            r, missing, err, kinds = self.missing(said("[agent-pm-progress:round] one"), result(outcome))
            self.assertEqual((r.outcome.status, missing), (outcome["status"], ["start"]))
            self.assertEqual(err, "drive.py: missing progress mark: start\n")
            self.assertEqual(kinds, ["progress", "missing", "outcome"])

    def test_no_report_when_start_was_seen_needs_input_resumed_or_not_expected(self):
        for events, progress, params in (
                ((said("[agent-pm-progress:start] go"), result(DONE)), ("start", "round"), {}),
                ((result(self.NEEDS),), ("start",), {}),
                ((result(DONE),), ("start",), {"resume": True}),
                ((result(DONE),), ("round",), {}),
                ((result(DONE),), (), {})):
            _, missing, err, _ = self.missing(*events, progress=progress, **params)
            self.assertEqual((missing, err), ([], ""), (progress, params))

    def test_no_outcome_reports_nothing(self):
        _, missing, err, _ = self.missing(said("bye"))
        self.assertEqual((missing, err), ([], ""))


class Sinks(unittest.TestCase):
    def test_terminal_strips_control_characters_and_skips_the_outcome(self):
        log = io.StringIO()
        sink = drive.terminal(log)
        sink(clients.Event("text", "a\x1b]52;c;ZXZpbA==\x07b\tc"))
        sink(clients.Event("progress", "x\x1b[2J", name="round"))
        sink(clients.Event("outcome", outcome=DONE))
        sink(clients.Event("missing", name="start"))
        self.assertEqual(log.getvalue(), "a]52;c;ZXZpbA==b\tc\nProgress (round): x[2J\n")


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

    def test_resume_end_to_end(self):
        code, _, _, ((argv, _),) = self.run_main("--sid", SID, "--resume", lines=stream(result(DONE)))
        self.assertEqual((code, argv[3:5]), (0, ["--resume", SID]))
        self.assertTrue(argv[2].startswith("Resumed run"))
        self.assertEqual(self.run_main("--resume")[0], 2)

    def test_a_run_client_needs_input_and_workdir(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = drive.main(["--role", "dummy-tester", "--out", "o.md"], root=CORE)
        self.assertEqual(code, 2)
        self.assertIn("needs --input and --workdir", err.getvalue())


if __name__ == "__main__":
    unittest.main()
