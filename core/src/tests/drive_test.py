import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402
import report  # noqa: E402

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

    def launch(self, prompt, run, *, params, access):
        Recorder.seen.append(dict(prompt=prompt, run=run, params=params, access=access))
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

    def report(self):
        return f"python3 {CORE}/src/report.py --to {self.work}/.report.jsonl"


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
            "--output-format", "stream-json", "--verbose",
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py prepare --dir {self.work}/src *)",
            f"Bash({self.report()} *)"])
        self.assertIn(f"## Return\n\nReport through `{self.report()}`", launch.argv[2])
        self.assertIn(f"`{self.report()} progress start <what that line asks you to report>`", launch.argv[2])
        self.assertIn(f"`{self.report()} outcome --status <done|needs_input|failed> --title", launch.argv[2])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_engineering_xhigh_and_repo_commands(self):
        argv = self.plan("engineer", "engineering", client="claude").argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools"] + [
            f"Bash(python3 {CORE}/src/repo.py {cmd} --dir {self.work}/src *)" for cmd in ("checkout", "status")]
            + [f"Bash({self.report()} *)"])

    def test_product_design_argv_has_the_prepare_rule(self):
        argv = self.plan("pm", "product-design", client="claude").argv
        self.assertEqual(argv[argv.index("--allowedTools"):], [
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py prepare --dir {self.work}/src *)",
            f"Bash({self.report()} *)"])
        self.assertIn(f"`python3 {CORE}/src/repo.py prepare --dir <Workdir>/src <repo>`", argv[2])

    def test_the_gate_is_pre_approved_verbatim(self):
        gate = "python3 /u/usage.py --below 80"
        argv = self.plan("researcher", "deep-research", client="claude", repo=self.repo,
                         layers=[{"gate": gate}]).argv
        self.assertEqual(argv[argv.index("--allowedTools"):][-1], f"Bash({gate})")
        self.assertIn(f"the gate is `{gate}`", argv[2])

    def test_deep_research_adds_the_methods_dir_light_research_none(self):
        argv = self.plan("researcher", "deep-research", client="claude", repo=self.repo).argv
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"],
                         [os.path.join(CORE, "team", "methods")])
        self.assertNotIn("--add-dir", self.plan(client="claude", repo=self.repo).argv)

    def test_deep_research_names_core_method_files_for_claude_and_by_default(self):
        prompts = [self.plan("researcher", "deep-research", client="claude").argv[2]]
        self.plan("researcher", "deep-research")
        prompts.append(Recorder.seen[-1]["prompt"])
        for name in ("deep-research", "ultracode"):
            path = os.path.join(CORE, "team", "methods", f"{name}.md")
            self.assertTrue(os.path.isfile(path), path)
            for prompt in prompts:
                self.assertIn(f"`{path}`", prompt)

    def test_resume(self):
        argv = self.plan(client="claude", resume=True).argv
        self.assertEqual(argv[3:5], ["--resume", SID])
        self.assertTrue(argv[2].startswith("Resumed run"))

    def test_interactive_is_argv_without_the_headless_flags(self):
        for role, task in (("researcher", "light-research"), ("engineer", "engineering")):
            launch = self.plan(role, task, client="claude", repo=self.repo)
            argv = launch.argv
            rest = [a for i, a in enumerate(argv) if i not in (1, 2)]
            for flag in ("--output-format", "stream-json", "--verbose"):
                rest.remove(flag)
            i = launch.interactive.index("--settings")
            self.assertEqual(launch.interactive[:i] + launch.interactive[i + 2:], ["claude", argv[2], *rest[1:]])

    def test_interactive_adds_the_stop_hook_after_the_config_flags(self):
        launch = self.plan(client="claude")
        i = launch.interactive.index("--settings")
        cmd = f"python3 {CORE}/src/report.py --to {self.work}/.report.jsonl stop"
        self.assertEqual(json.loads(launch.interactive[i + 1]),
                         {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}})

    def test_headless_has_no_settings(self):
        self.assertNotIn("--settings", self.plan(client="claude").argv)

    def test_interactive_resume(self):
        launch = self.plan(client="claude", resume=True)
        self.assertEqual(launch.interactive[2:4], ["--resume", SID])
        self.assertTrue(launch.interactive[1].startswith("Resumed run"))

    def test_other_launches_have_no_interactive_command(self):
        self.assertEqual(clients.Launch(["x"]).interactive, [])
        self.assertEqual(self.plan().interactive, [])

    def test_commands_and_task_rules_become_allowed_tools(self):
        c = claude(roles={"r": {"tasks": {"t": {"allow": ["WebFetch"]}}}})
        access = drive.Access(dirs=[], commands=["make test"])
        argv = c.launch("p", run(), params=PARAMS, access=access).argv
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools", "Bash(make test)", "WebFetch"])

    def test_no_rule_groups_when_empty(self):
        argv = claude().launch("p", run(), params=PARAMS, access=NO_ACCESS).argv
        for flag in ("--add-dir", "--disallowedTools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_braces_in_the_input_survive(self):
        argv = self.plan(client="claude", input="Explain {{x}} templates.").argv
        self.assertTrue(argv[2].rstrip().endswith("Explain {{x}} templates."))

    def test_unmapped_tier(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(tiers={}).launch("p", run(), params=PARAMS, access=NO_ACCESS)
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
        self.assertEqual(seen["access"], drive.Access(dirs=[], commands=[
            f"python3 {CORE}/src/repo.py prepare --dir {self.work}/src *", f"{self.report()} *"]))
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
        acc = drive.access(run(commands=["{{scripts}}/x *"], gate=gate), self.params(), repo=None, scripts="/s",
                           methods="/m")
        report = f"python3 /s/report.py --to {self.work}/.report.jsonl *"
        self.assertEqual(acc.commands, ["/s/x *", report, gate])
        acc = drive.access(run(commands=["x"]), self.params(), repo=None, scripts="/s", methods="/m")
        self.assertEqual(acc.commands, ["x", report])

    def test_repo_entry_binds_to_the_repo_arg(self):
        acc = drive.access(run(write=["repo"], commands=["{{scripts}}/x --dir {{workdir}}/src *"]), self.params(),
                           repo=self.repo, scripts="/s", methods="/m")
        self.assertEqual(acc, drive.Access(dirs=[self.repo], commands=[
            f"/s/x --dir {self.work}/src *", f"python3 /s/report.py --to {self.work}/.report.jsonl *"]))

    def test_methods_fills_read_and_write_entries_and_nothing_else_does(self):
        acc = drive.access(run(read=["{{methods}}"], write=["{{methods}}/out"]), self.params(), repo=None, scripts="/s",
                           methods="/m")
        self.assertEqual(acc.dirs, ["/m", "/m/out"])
        for key in ("read", "write"):
            with self.subTest(key), self.assertRaises(compose.ConfigError) as cm:
                drive.access(run(**{key: ["{{nope}}"]}), self.params(), repo=None, scripts="/s", methods="/m")
            self.assertIn("{{nope}}", str(cm.exception))

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


def methods_copy(skill):
    """A skill's expected copy of core's team/methods/ (path in the skill → text), dotfiles and dot dirs skipped."""
    src = os.path.join(CORE, "team", "methods")
    copy = {}
    for d, dirs, names in os.walk(src):
        dirs[:] = [n for n in dirs if not n.startswith(".")]
        for name in (n for n in names if not n.startswith(".")):
            with open(os.path.join(d, name)) as f:
                copy[os.path.join(skill, "methods", os.path.relpath(os.path.join(d, name), src))] = f.read()
    return copy


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

    def test_a_skill_naming_methods_gets_a_copy_of_core_methods(self):
        r = run(role_title="R", task_title="T", task_summary="Do it.", output={"type": "orchestrator"})
        skill = os.path.join(os.path.abspath("o"), "r-t")
        files = clients.SkillClient({}).export("Follow `${CLAUDE_SKILL_DIR}/methods/x.md`.", r, dest="o").files
        expected = methods_copy(skill)
        self.assertIn(os.path.join(skill, "methods", "deep-research.md"), expected)
        self.assertEqual({p: t for p, t in files.items() if p != os.path.join(skill, "SKILL.md")}, expected)
        self.assertEqual(list(clients.SkillClient({}).export("No methods.", r, dest="o").files),
                         [os.path.join(skill, "SKILL.md")])

    def test_the_methods_copy_keeps_subdirs_and_skips_dotfiles(self):
        src = os.path.join(self.tmp.name, "methods")
        for rel, data in (("a.md", b"a"), ("sub/b.md", b"b"), (".DS_Store", b"\x00\xff"), (".hidden/c.md", b"c"),
                          ("sub/.x", b"x")):
            os.makedirs(os.path.dirname(os.path.join(src, rel)), exist_ok=True)
            with open(os.path.join(src, rel), "wb") as f:
                f.write(data)
        r = run(role_title="R", task_title="T", task_summary="Do it.", output={"type": "orchestrator"})
        skill = os.path.join(os.path.abspath("o"), "r-t")
        with unittest.mock.patch.object(sys.modules["clients.skill"], "CORE_METHODS", src):
            files = clients.SkillClient({}).export("Follow `${CLAUDE_SKILL_DIR}/methods/a.md`.", r, dest="o").files
        methods = os.path.join(skill, "methods")
        self.assertEqual({p: t for p, t in files.items() if p != os.path.join(skill, "SKILL.md")},
                         {os.path.join(methods, "a.md"): "a", os.path.join(methods, "sub", "b.md"): "b"})

    def test_deep_research_skill_names_and_carries_the_methods(self):
        skill = os.path.join(self.tmp.name, "researcher-deep-research")
        files = self.export("researcher", "deep-research").files
        expected = methods_copy(skill)
        for name in ("deep-research", "ultracode"):
            self.assertIn(f"`${{CLAUDE_SKILL_DIR}}/methods/{name}.md`", files[os.path.join(skill, "SKILL.md")])
            self.assertIn(os.path.join(skill, "methods", f"{name}.md"), expected)
        self.assertEqual(sorted(files),
                         sorted([os.path.join(skill, "SKILL.md"), os.path.join(skill, "scripts", "repo.py"), *expected]))
        self.assertEqual({p: files[p] for p in expected}, expected)

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

    def test_main_refuses_a_runner_other_than_headless(self):
        skills = os.path.join(self.tmp.name, "skills")
        for extra in ((), ("--dry-run",)):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                code = drive.main(["--role", "dummy-tester", "--client", "skill", "--out", skills, "--runner", "tui",
                                   *extra], root=CORE)
            self.assertEqual((code, out.getvalue(), os.path.exists(skills)), (2, "", False))
            self.assertIn("drive.py: SkillClient writes files; it takes no --runner tui\n", err.getvalue())


class FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout, self.rc = lines, rc

    def wait(self):
        return self.rc

    def kill(self):
        self.killed = True


def stream(*events):
    return [json.dumps(e) + "\n" for e in events]


def said(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def progress(name, text):
    return {"kind": "progress", "name": name, "text": text}


def outcome(data):
    return {"kind": "outcome", "outcome": data}


def feed(channel, items):
    """The run's stdout: each str a line of it; each dict appended to the channel first, as report.py does."""
    for item in items:
        if isinstance(item, dict):
            drive.append_line(channel, json.dumps(item, ensure_ascii=False) + "\n")
        else:
            yield item


DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
STOP = {"kind": "stop"}


class FakeTui:
    """tui's API for the tui runner. Each status call takes the next step: a list of items appended to the channel
    (the pane runs on), or a pane state (None: gone; an int: dead with that status). No step left fails the test."""
    def __init__(self, channel, steps):
        self.channel, self.steps, self.calls = channel, list(steps), []

    def patch(self, **api):
        """tui's start, status, kill and send replaced by this fake's, or by `api`'s."""
        return unittest.mock.patch.multiple(drive.tui, **{"start": self.start, "status": self.status, "kill": self.kill,
                                                          "send": self.send, **api})

    def start(self, name, argv, **kw):
        self.calls.append(("start", name, argv, kw))

    def status(self, name):
        if not self.steps:
            raise AssertionError("polled after the last step")
        step = self.steps.pop(0)
        if not isinstance(step, list):
            return step
        for item in step:
            drive.append_line(self.channel, json.dumps(item) + "\n")
        return drive.tui.RUNNING

    def kill(self, name):
        self.calls.append(("kill", name))

    def send(self, name, text):
        self.calls.append(("send", name, text))


class ClaudeEvents(unittest.TestCase):
    def kinds(self, *lines):
        return [(e.kind, e.text) for e in claude().events(lines)]

    def test_assistant_text_lines_are_text(self):
        lines = [said("Starting.\n[agent-pm-progress:start] 2 rounds\n\n  \nDone.")]
        self.assertEqual(self.kinds("not json\n", *lines), [
            ("text", "not json"), ("text", "Starting."), ("text", "[agent-pm-progress:start] 2 rounds"), ("text", "Done.")])

    def test_results_and_odd_json_are_skipped(self):
        lines = ["null\n", "3\n", "[]\n", '"text"\n', *stream(
            {"type": "assistant"}, {"type": "assistant", "message": {"content": "x"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}, {"type": "thinking"}, 7]}},
            {"type": "result", "subtype": "success", "result": "bye", "structured_output": DONE})]
        self.assertEqual(self.kinds(*lines), [])


class Handover(unittest.TestCase):
    def test_each_client_names_the_progress_mark(self):
        for c in (claude(), clients.SkillClient({})):
            self.assertIn(f"[{clients.PROGRESS}:<name>]", c.handover(), type(c).__name__)
        self.assertIn("before calling the next tool, send a text message containing only that line",
                      clients.SkillClient({}).handover())
        self.assertNotIn("ultracode", claude().handover())
        self.assertNotIn("progress:budget", claude().handover())

    def test_claude_reports_through_the_report_command_outcome_last(self):
        text = claude().handover()
        self.assertIn("{{report}} progress <name>", text)
        self.assertIn("{{report}} outcome --status", text)
        self.assertIn("last action", text)
        self.assertNotIn("structured output", text)

    def test_the_base_client_returns_no_outcome(self):
        self.assertEqual(clients.Client({}).handover(), "")
        self.assertEqual([e.kind for e in clients.Client({}).events(['{"type": "result"}\n'])], ["text"])


class Schema(unittest.TestCase):
    def test_schema_and_validate_agree(self):
        schema = compose.outcome_schema(CORE)
        self.assertEqual(tuple(schema["properties"]["status"]["enum"]), drive.STATUSES)
        self.assertEqual(set(schema["properties"]), {f.name for f in dataclasses.fields(drive.Outcome)})
        self.assertEqual(schema["properties"]["questions"]["maxItems"], 4)


class Report(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.work)
        self.channel = os.path.join(self.work, ".report.jsonl")

    def report(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = report.main(["--to", self.channel, *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    def lines(self):
        if not os.path.exists(self.channel):
            return []
        with open(self.channel) as f:
            return [json.loads(line) for line in f]

    def test_stop_appends_one_line(self):
        self.assertEqual(self.report("stop"), (0, "report.py: stop reported\n"))
        self.assertEqual(self.lines(), [{"kind": "stop"}])

    def test_report_event_maps_stop(self):
        self.assertEqual(drive.report_event('{"kind": "stop"}'), clients.Event("stop"))

    def test_progress_appends_one_line(self):
        self.assertEqual(self.report("progress", "round-1", "2", "rounds,", "cap 80"), (0, "report.py: progress reported\n"))
        self.assertEqual(self.report("progress", "start", "-x go")[0], 0)
        self.assertEqual(self.lines(), [progress("round-1", "2 rounds, cap 80"), progress("start", "-x go")])

    def test_outcome_from_arguments_the_deliverable_from_its_file(self):
        doc = os.path.join(self.work, "doc.md")
        with open(doc, "w") as f:
            f.write("# Doc\n")
        code, out = self.report("outcome", "--status", "done", "--title", "T", "--summary", "S", "--deliverable", doc)
        self.assertEqual((code, self.lines()), (0, [outcome(DONE)]))
        self.assertIn("outcome reported", out)

    def test_repeatable_questions_and_files(self):
        self.assertEqual(self.report("outcome", "--status", "needs_input", "--title", "T", "--summary", "- a\n- b",
                                     "--question", "Which repo?", "--question", "- Why?", "--file", "a.md",
                                     "--file", "b.md", "--url", "https://x")[0], 0)
        self.assertEqual(self.lines(), [outcome({"status": "needs_input", "title": "T", "summary": "- a\n- b",
                                                 "questions": ["Which repo?", "- Why?"], "url": "https://x",
                                                 "files": ["a.md", "b.md"]})])

    def test_bad_arguments_append_nothing(self):
        base = ["outcome", "--title", "T", "--summary", "S"]
        for argv in (base + ["--status", "maybe"], ["outcome", "--status", "done"], ["note", "x"], [],
                     base + ["--status", "done", "--deliverable", os.path.join(self.work, "no.md")]):
            self.assertNotEqual(self.report(*argv)[0], 0, argv)
        self.assertEqual(self.lines(), [])

    def test_a_fifo_channel_is_refused_not_waited_on(self):
        os.mkfifo(self.channel)
        self.assertEqual(self.report("progress", "x", "y")[0], 1)

    def test_a_deliverable_that_is_not_utf8_is_an_error_line(self):
        doc = os.path.join(self.work, "doc.md")
        with open(doc, "wb") as f:
            f.write(b"\xff\xfe")
        code, _ = self.report("outcome", "--status", "done", "--title", "T", "--summary", "S", "--deliverable", doc)
        self.assertEqual((code, self.lines()), (1, []))

    def test_a_symlinked_channel_is_refused(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")
        os.symlink(target, self.channel)
        self.assertEqual(self.report("progress", "x", "y")[0], 1)
        with open(target) as f:
            self.assertEqual(f.read(), "mine")


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

    def test_the_schema_holds(self):
        self.fails({**DONE, "extra": 1}, "unknown field 'extra'")
        self.fails({**DONE, "title": "x" * 201}, "title is longer than 200")
        self.fails({**DONE, "summary": "x" * 4001}, "summary is longer than 4000")
        self.fails({**DONE, "status": "needs_input", "questions": [3]}, "questions[0] must be text")
        self.fails({**DONE, "files": None}, "files must be a list")
        self.fails({**DONE, "url": None}, "url must be text")

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
    def start(self, items, rc=0, output=None, sinks=None, progress=(), **params):
        """Runs `items` through drive.start: str lines on stdout, dicts (or callables) on the channel."""
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work)
        calls, log = [], io.StringIO()
        p = self.params(**params)

        def popen(argv, **kw):
            calls.append((argv, kw))
            self.proc = FakeProc(feed(p.channel, items), rc)
            return self.proc

        r = drive.start(launch, run(output=output or {"type": "local"}, progress=list(progress)), p, client=claude(), popen=popen,
                        sinks=sinks if sinks is not None else [drive.terminal(log), *drive.default_sinks(p)[1:]])
        return r, calls, log.getvalue()

    def read(self, name):
        with open(os.path.join(self.work, name)) as f:
            return f.read()

    def exists(self, name):
        return os.path.lexists(os.path.join(self.work, name))

    def test_done_run_saves_outcome_deliverable_and_progress(self):
        r, ((argv, kw),), log = self.start([progress("round", "half way"), said("hi"), outcome(DONE)])
        out = os.path.join(self.work, "out.md")
        self.assertEqual((r.returncode, r.outcome.status, r.outcome.url), (0, "done", out))
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake"], self.work, "1"))
        self.assertEqual(self.read("out.md"), "# Doc\n")
        self.assertEqual(json.loads(self.read("outcome.json"))["url"], out)
        self.assertEqual(json.loads(self.read("progress.jsonl")) | {"ts": ""}, {"ts": "", "name": "round", "text": "half way"})
        self.assertEqual(log, "Progress (round): half way\nhi\n")

    def test_stop_events_reach_no_sink(self):
        seen = []
        r, _, _ = self.start([{"kind": "stop"}, progress("round", "x"), {"kind": "stop"}, outcome(DONE)],
                             sinks=[lambda e: seen.append(e.kind)])
        self.assertEqual((r.returncode, seen), (0, ["progress", "outcome"]))

    def test_headless_ignores_stops(self):
        with unittest.mock.patch.object(drive.tui, "send") as send:
            r, _, _ = self.start([*[{"kind": "stop"}] * (drive.STOP_LIMIT + 1), said("still going"), outcome(DONE)])
        self.assertEqual((r.returncode, r.outcome.status, send.called), (0, "done", False))

    def test_progress_arrives_while_stdout_is_quiet(self):
        got = threading.Event()

        def quiet():
            drive.append_line(os.path.join(self.work, ".report.jsonl"), json.dumps(progress("start", "go")) + "\n")
            self.assertTrue(got.wait(5), "no progress event while stdout was quiet")
            yield said("after")

        seen = []

        def sink(event):
            seen.append(event.kind)
            if event.kind == "progress":
                got.set()

        launch = drive.Launch(["fake"], cwd=self.work)
        with unittest.mock.patch.object(drive, "POLL", 0.01):
            drive.start(launch, run(), self.params(), client=claude(), sinks=[sink],
                        popen=lambda argv, **kw: FakeProc(quiet()))
        self.assertEqual(seen, ["progress", "text"])

    def test_the_channel_exists_before_launch(self):
        def items():
            self.assertTrue(os.path.isfile(os.path.join(self.work, ".report.jsonl")))
            yield said("x")
        self.start(items())

    def test_only_lines_appended_after_start_count(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, ".report.jsonl"), "w") as f:
            f.write(json.dumps(progress("start", "old")) + "\n" + json.dumps(outcome(DONE)) + "\n")
        seen = []
        r, _, _ = self.start([said("resumed")], sinks=[seen.append], resume=True)
        self.assertEqual((r.error, [e.kind for e in seen]), ("the run returned no outcome", ["text"]))
        r, _, _ = self.start([outcome({**DONE, "title": "new"})], resume=True)
        self.assertEqual(r.outcome.title, "new")

    def test_the_last_outcome_counts(self):
        r, _, _ = self.start([outcome({**DONE, "title": "first"}), said("fixing"), outcome({**DONE, "title": "real"})])
        self.assertEqual(r.outcome.title, "real")

    def test_malformed_channel_lines_are_skipped(self):
        seen = []
        bad = [{"kind": "progress", "name": "a b", "text": "x"}, {"kind": "progress", "name": "x", "text": " \n "},
               {"kind": "outcome", "outcome": "done"}, {"kind": "x"}]
        r, _, _ = self.start([*bad, progress("ok", "yes")], sinks=[seen.append])
        self.assertEqual([(e.kind, e.name) for e in seen], [("progress", "ok")])
        self.assertIsNone(r.outcome)

    def test_a_multi_line_progress_report_becomes_one_line(self):
        seen = []
        self.start([progress("start", "new build,\n  phase P1")], sinks=[seen.append])
        self.assertEqual([(e.name, e.text) for e in seen if e.kind == "progress"], [("start", "new build, phase P1")])

    def test_a_fifo_at_the_channel_is_refused_not_waited_on(self):
        os.makedirs(self.work)
        os.mkfifo(os.path.join(self.work, ".report.jsonl"))
        with self.assertRaises(OSError):
            self.start([outcome(DONE)])

    def test_garbage_and_partial_lines(self):
        def items():
            with open(os.path.join(self.work, ".report.jsonl"), "a") as f:
                f.write("not json\n[]\n" + json.dumps(progress("a", "1"))[:10])
            yield said("x")
            with open(os.path.join(self.work, ".report.jsonl"), "a") as f:
                f.write(json.dumps(progress("a", "1"))[10:] + "\n")
        seen = []
        self.start(items(), sinks=[seen.append])
        self.assertEqual(sorted((e.kind, e.text) for e in seen), [("progress", "1"), ("text", "x")])

    def test_a_symlinked_channel_is_not_read(self):
        os.makedirs(self.work)
        target = os.path.join(self.tmp.name, "lines")
        with open(target, "w") as f:
            f.write(json.dumps(outcome(DONE)) + "\n")

        def items():
            os.remove(os.path.join(self.work, ".report.jsonl"))
            os.symlink(target, os.path.join(self.work, ".report.jsonl"))
            with open(target, "a") as f:
                f.write(json.dumps(outcome(DONE)) + "\n")
            yield said("x")
        r, _, _ = self.start(items())
        self.assertEqual(r.error, "the run returned no outcome")

    def test_orchestrator_destination_saves_the_deliverable_with_no_url(self):
        r, _, _ = self.start([outcome(DONE)], output={"type": "orchestrator"})
        self.assertEqual((r.outcome.url, self.read("out.md")), ("", "# Doc\n"))

    def test_github_destination_keeps_the_runs_url_and_saves_no_deliverable(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        url = "https://github.com/o/docs/blob/main/x.md"
        r, _, _ = self.start([outcome({**DONE, "url": url})], output=gh)
        self.assertEqual(r.outcome.url, url)
        self.assertFalse(self.exists("out.md"))

    def test_needs_input_reaches_the_outcome_file(self):
        self.start([outcome({**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""})])
        self.assertEqual(json.loads(self.read("outcome.json"))["questions"], ["Which repo?"])

    def test_no_outcome(self):
        r, _, _ = self.start([said("bye")])
        self.assertEqual((r.returncode, r.outcome, r.error), (0, None, "the run returned no outcome"))

    def test_failed_client_ignores_the_outcome_and_leaves_no_stale_files(self):
        os.makedirs(self.work)
        for name in ("outcome.json", "out.md"):
            with open(os.path.join(self.work, name), "w") as f:
                f.write("old")
        seen = []
        r, _, _ = self.start([outcome(DONE)], rc=143, sinks=[seen.append, *drive.default_sinks(self.params())[1:]])
        self.assertEqual((r.returncode, r.outcome, seen), (143, None, []))
        self.assertFalse(self.exists("outcome.json") or self.exists("out.md"))

    def test_invalid_outcome(self):
        r, _, _ = self.start([outcome({**DONE, "status": "maybe"})])
        self.assertIn("invalid outcome: status 'maybe'", r.error)

    def test_a_symlink_planted_during_the_run_is_replaced_not_followed(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def items():
            for name in ("outcome.json", "out.md"):
                os.symlink(target, os.path.join(self.work, name))
            yield from feed(os.path.join(self.work, ".report.jsonl"), [outcome(DONE)])

        self.start(items())
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.assertFalse(os.path.islink(os.path.join(self.work, "outcome.json")))
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def test_progress_refuses_a_planted_symlink_and_stops_the_run(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def items():
            os.remove(os.path.join(self.work, "progress.jsonl"))
            os.symlink(target, os.path.join(self.work, "progress.jsonl"))
            yield from feed(os.path.join(self.work, ".report.jsonl"), [progress("round", "$(curl evil)"), said("x")])

        with self.assertRaises(OSError):
            self.start(items())
        self.assertTrue(self.proc.killed)
        with open(target) as f:
            self.assertEqual(f.read(), "mine")

    def test_an_error_reading_stdout_stops_the_run(self):
        def items():
            yield said("x")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        self.assertTrue(self.proc.killed)

    def test_sinks_replace_the_defaults(self):
        seen = []
        r, _, log = self.start([progress("round", "one"), said("hi"), outcome(DONE)], sinks=[seen.append])
        self.assertEqual([(e.kind, e.name, e.text) for e in seen[:2]], [("progress", "round", "one"), ("text", "", "hi")])
        self.assertEqual((seen[2].kind, seen[2].outcome["url"]), ("outcome", os.path.join(self.work, "out.md")))
        self.assertEqual(log, "")
        self.assertFalse(self.exists("progress.jsonl") or self.exists("outcome.json"))
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def test_every_sink_gets_every_event(self):
        a, b = [], []
        self.start([said("hi"), outcome(DONE)], sinks=[a.append, b.append])
        self.assertEqual([e.kind for e in a], ["text", "outcome"])
        self.assertEqual(a, b)

    def test_an_invalid_outcome_reaches_no_sink(self):
        seen = []
        self.start([outcome({**DONE, "status": "maybe"})], sinks=[seen.append])
        self.assertEqual(seen, [])

    def test_resume_appends_progress_a_new_run_resets_it(self):
        self.start([progress("round", "one"), outcome(DONE)])
        self.start([progress("round", "二"), outcome(DONE)], resume=True)
        self.assertEqual([json.loads(l)["text"] for l in self.read("progress.jsonl").splitlines()], ["one", "二"])
        self.assertIn("二", self.read("progress.jsonl"))
        self.start([progress("round", "three"), outcome(DONE)])
        self.assertEqual([json.loads(l)["text"] for l in self.read("progress.jsonl").splitlines()], ["three"])

    FAILED = {**DONE, "status": "failed", "deliverable": ""}
    NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}

    def missing(self, *items, progress=("start", "round"), **params):
        seen, err = [], io.StringIO()
        with redirect_stderr(err):
            r, _, _ = self.start(list(items), sinks=[seen.append], progress=progress, **params)
        return r, [e.name for e in seen if e.kind == "missing"], err.getvalue(), [e.kind for e in seen]

    def test_done_or_failed_without_start_reports_it_missing(self):
        for o in (DONE, self.FAILED):
            r, missing, err, kinds = self.missing(progress("round", "one"), outcome(o))
            self.assertEqual((r.outcome.status, missing), (o["status"], ["start"]))
            self.assertEqual(err, "drive.py: missing progress mark: start\n")
            self.assertEqual(kinds, ["progress", "missing", "outcome"])

    def test_no_report_when_start_was_seen_needs_input_resumed_or_not_expected(self):
        for items, marks, params in (
                ((progress("start", "go"), outcome(DONE)), ("start", "round"), {}),
                ((outcome(self.NEEDS),), ("start",), {}),
                ((outcome(DONE),), ("start",), {"resume": True}),
                ((outcome(DONE),), ("round",), {}),
                ((outcome(DONE),), (), {})):
            _, missing, err, _ = self.missing(*items, progress=marks, **params)
            self.assertEqual((missing, err), ([], ""), (marks, params))

    def test_no_outcome_reports_nothing(self):
        _, missing, err, _ = self.missing(said("bye"))
        self.assertEqual((missing, err), ([], ""))


class Command(unittest.TestCase):
    def test_each_runner_starts_its_command(self):
        launch = drive.Launch(["h"], interactive=["i"])
        self.assertEqual([drive.command(launch, r, Recorder({})) for r in ("headless", "tui")], [["h"], ["i"]])

    def test_unknown_runner_or_no_command(self):
        for launch, runner, msg in ((drive.Launch(["h"], interactive=["i"]), "gui", "unknown runner 'gui'"),
                                    (drive.Launch(["h"]), "tui", "Recorder has no tui command"),
                                    (drive.Launch([], interactive=["i"]), "headless", "has no headless command")):
            with self.assertRaises(compose.ConfigError) as e:
                drive.command(launch, runner, Recorder({}))
            self.assertIn(msg, str(e.exception))


class HostFailure(Base):
    """drive.start with a runner whose begin or poll raises."""
    def start(self, *, begin=None, poll=None):
        self.stopped = []
        stopped = self.stopped

        class Failing:
            starts = "argv"

            def __init__(self, **kw):
                pass

            def begin(self, argv, **kw):
                if begin:
                    raise begin

            def poll(self, timeout):
                raise poll

            def stop(self):
                stopped.append(True)

        with unittest.mock.patch.dict(drive.RUNNERS, {"failing": Failing}):
            return drive.start(drive.Launch(["x"]), run(), self.params(), client=Recorder({}), runner="failing",
                               sinks=[])

    def test_an_interrupted_begin_stops_the_host(self):
        for error in (KeyboardInterrupt(), SystemExit(1), RuntimeError("x")):
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.start(begin=error)
            self.assertEqual(self.stopped, [True])

    def test_a_runner_error_stops_the_host_and_is_the_result(self):
        for kw in ({"begin": drive.RunnerError("no host")}, {"poll": drive.RunnerError("no host")}):
            with self.subTest(**kw):
                self.assertEqual(self.start(**kw), drive.Result(1, None, "failing: no host"))
                self.assertEqual(self.stopped, [True])

    def test_headless_raises_a_failed_popen(self):
        popen = unittest.mock.Mock(side_effect=FileNotFoundError(2, "No such file or directory", "fake"))
        with self.assertRaises(FileNotFoundError):
            drive.start(drive.Launch(["fake"]), run(), self.params(), client=Recorder({}), sinks=[], popen=popen)


class TuiRunner(Base):
    NAME = f"r-t-{SID[:8]}"

    def start(self, *steps, sinks=None, progress=(), show=None, api=None, **params):
        """drive.start with the tui runner over FakeTui(steps), `api` replacing its functions; stderr in self.err."""
        p = self.params(**params)
        self.fake, self.err, seen = FakeTui(p.channel, steps), io.StringIO(), []
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work, interactive=["claude", "hi"])
        with self.fake.patch(**(api or {})), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(self.err):
            r = drive.start(launch, run(progress=list(progress), show=show), p, client=claude(), runner="tui",
                            sinks=[seen.append] if sinks is None else sinks)
        return r, self.fake.calls, [e.kind for e in seen], self.err.getvalue()

    def main(self, *steps, api=None, extra=()):
        fake, err = FakeTui(os.path.join(self.work, ".report.jsonl"), steps), io.StringIO()
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, "--runner", "tui", *extra]
        with fake.patch(**(api or {})), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(err):
            return drive.main(argv, root=CORE), fake.calls, err.getvalue()

    def test_session_name_and_start_arguments(self):
        _, calls, _, _ = self.start([outcome(DONE)], show="echo {{session}}")
        (_, name, argv, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((name, argv, kw["cwd"]), (self.NAME, ["claude", "hi"], self.work))
        self.assertEqual(kw["show"], "echo {{session}}")
        self.assertEqual((kw["env"]["FAKE"], kw["env"]["PATH"]), ("1", os.environ["PATH"]))

    def test_tui_session_name(self):
        self.assertEqual(drive.tui_session("r", "t", SID), self.NAME)
        self.assertRegex(self.NAME, drive.TUI_SESSION)
        self.assertIsNone(drive.TUI_SESSION.fullmatch("r-t-xyz"))

    def test_layout_reaches_tui_start_and_defaults_to_right_beside_nothing(self):
        for layout, want in ((None, ("right", None)), (drive.Layout("below", "s"), ("below", "s"))):
            p = self.params()
            fake = FakeTui(p.channel, [[outcome(DONE)]])
            launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"])
            with fake.patch(), unittest.mock.patch.object(drive, "POLL", 0):
                drive.start(launch, run(), p, client=claude(), runner="tui", layout=layout, sinks=[])
            (_, _, _, kw), = [c for c in fake.calls if c[0] == "start"]
            self.assertEqual((kw["split"], kw["beside"]), want)

    def test_a_bad_layout_is_a_config_error_before_files_or_channel(self):
        marker = os.path.join(self.work, "marker")
        cases = (("headless", drive.Layout(), "no layout"), ("tui", drive.Layout("left"), "split"),
                 ("tui", drive.Layout(beside="a b"), "beside"), ("tui", drive.Layout(beside=""), "beside"))
        for runner, layout, word in cases:
            with self.subTest(runner=runner, layout=layout):
                p = self.params()
                launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"], files={marker: "x"})
                with self.assertRaisesRegex(drive.ConfigError, word):
                    drive.start(launch, run(), p, client=claude(), runner=runner, layout=layout, sinks=[])
                self.assertFalse(os.path.lexists(marker))
                self.assertFalse(os.path.lexists(p.channel))

    def test_main_split_and_beside_build_the_layout(self):
        seen = []
        real = drive.start

        def start(*a, **kw):
            seen.append(kw["layout"])
            return real(*a, **kw)

        for flags, want in ((["--split", "below", "--beside", "s"], drive.Layout("below", "s")),
                            (["--beside", "s"], drive.Layout("right", "s")), ([], None)):
            with unittest.mock.patch.object(drive, "start", start):
                self.main([outcome(DONE)], extra=flags)
            self.assertEqual(seen.pop(), want)

    def test_main_layout_with_headless_exits_2(self):
        err = io.StringIO()
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, "--split", "below"]
        with redirect_stderr(err), unittest.mock.patch.object(drive, "start") as start:
            self.assertEqual(drive.main(argv, root=CORE), 2)
        self.assertIn("no layout", err.getvalue())
        start.assert_not_called()

    def test_done_on_the_first_outcome_without_waiting_for_a_stop_or_a_kill(self):
        r, calls, kinds, _ = self.start([progress("round", "x")], [outcome(DONE)])
        self.assertEqual((r.returncode, r.outcome.status, kinds), (0, "done", ["progress", "outcome"]))
        self.assertEqual([c[0] for c in calls], ["start"])
        self.assertEqual(self.fake.steps, [])

    def test_a_dead_pane_or_a_gone_session_ends_the_run_without_an_outcome(self):
        for state, result in ((3, (3, None, "the client exited 3")), (0, (0, None, "the run returned no outcome")),
                              (None, (0, None, "the run returned no outcome"))):
            r, calls, _, _ = self.start([progress("round", "x")], state)
            self.assertEqual((r.returncode, r.outcome, r.error), result)
            self.assertEqual([c[0] for c in calls], ["start"])

    def test_main_exits_3_on_a_dead_panes_status_1_on_0_and_0_on_an_outcome(self):
        self.assertEqual([self.main(state)[0] for state in (3, 0)], [3, 1])
        code, (start, *rest), _ = self.main([outcome(DONE)])
        self.assertEqual((code, start[2][0], rest), (0, "claude", []))

    def test_a_sink_raising_kills_the_session(self):
        def sink(event):
            raise RuntimeError("sink")

        with self.assertRaises(RuntimeError):
            self.start([progress("round", "x")], sinks=[sink])
        self.assertEqual(self.fake.calls[-1], ("kill", self.NAME))

    def test_a_failed_kill_is_printed_and_the_drivers_exception_raised(self):
        def sink(event):
            raise RuntimeError("sink")

        kill = unittest.mock.Mock(side_effect=drive.tui.TuiError("gone"))
        with self.assertRaises(RuntimeError):
            self.start([progress("round", "x")], sinks=[sink], api={"kill": kill})
        self.assertEqual(kill.call_args, unittest.mock.call(self.NAME))
        self.assertIn("drive.py: tui: gone\n", self.err.getvalue())

    def test_a_tui_error_on_start_is_the_result(self):
        r, calls, _, _ = self.start(api={"start": unittest.mock.Mock(side_effect=drive.tui.TuiError("no tmux"))})
        self.assertEqual((r, calls), (drive.Result(1, None, "tui: no tmux"), []))

    def test_an_interrupted_start_kills_no_session_it_did_not_start(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(api={"start": unittest.mock.Mock(side_effect=KeyboardInterrupt)})
        self.assertEqual(self.fake.calls, [])

    def test_a_tui_error_reading_the_session_kills_it_and_is_the_result(self):
        status = unittest.mock.Mock(side_effect=drive.tui.TuiError("no server"))
        r, calls, _, _ = self.start(api={"status": status})
        self.assertEqual((r, [c[0] for c in calls]), (drive.Result(1, None, "tui: no server"), ["start", "kill"]))
        code, calls, err = self.main(api={"status": status})
        self.assertEqual((code, [c[0] for c in calls]), (3, ["start", "kill"]))
        self.assertIn("drive.py: tui: no server\n", err)

    def test_a_missing_start_mark_is_reported(self):
        r, _, kinds, err = self.start([progress("round", "x"), outcome(DONE)], progress=("start", "round"))
        self.assertEqual(kinds, ["progress", "missing", "outcome"])
        self.assertIn("drive.py: missing progress mark: start\n", err)

    def test_the_first_stop_without_an_outcome_nudges_once(self):
        r, calls, kinds, _ = self.start([STOP], [STOP], [outcome(DONE), STOP])
        self.assertEqual([c for c in calls if c[0] == "send"], [("send", self.NAME, drive.NUDGE)])
        self.assertEqual((r.outcome.status, kinds), ("done", ["outcome"]))

    def test_it_gives_up_after_stop_limit_counted_stops_and_leaves_the_session(self):
        r, calls, kinds, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT))
        self.assertEqual(r, drive.Result(0, None, "the run returned no outcome"))
        self.assertEqual(([c[0] for c in calls], kinds, self.fake.steps), (["start", "send"], [], []))
        self.assertIn(f"drive.py: no outcome after {drive.STOP_LIMIT} stops; session {self.NAME} left open: "
                      f"tmux attach -t '={self.NAME}'\n", err)
        code, calls, _ = self.main(*[[STOP]] * (1 + drive.STOP_LIMIT))
        self.assertEqual((code, [c[0] for c in calls]), (1, ["start", "send"]))

    def test_progress_resets_the_count(self):
        again = [[STOP]] * (drive.STOP_LIMIT - 1)
        r, calls, _, _ = self.start([STOP], *again, [progress("round", "x")], *again, [outcome(DONE)])
        self.assertEqual((r.outcome.status, [c[0] for c in calls]), ("done", ["start", "send"]))

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        send = unittest.mock.Mock(side_effect=drive.tui.TuiError("no pane"))
        r, _, _, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT), api={"send": send})
        self.assertEqual((r.error, send.call_count), ("the run returned no outcome", 1))
        self.assertIn("drive.py: tui: no pane\n", err)

    def test_start_refuses_a_client_without_the_command_before_anything_starts(self):
        popen = unittest.mock.Mock()
        with unittest.mock.patch.object(drive.tui, "start") as start, self.assertRaises(compose.ConfigError):
            drive.start(drive.Launch(["fake"]), run(), self.params(), client=Recorder({}), runner="tui", popen=popen)
        self.assertEqual((popen.called, start.called, os.path.exists(self.work)), (False, False, False))


class Sinks(unittest.TestCase):
    def test_terminal_strips_control_characters_and_skips_the_outcome(self):
        log = io.StringIO()
        sink = drive.terminal(log)
        sink(clients.Event("text", "a\x1b]52;c;ZXZpbA==\x07b\tc"))
        sink(clients.Event("progress", "x\x1b[2J", name="round"))
        sink(clients.Event("outcome", outcome=DONE))
        sink(clients.Event("missing", name="start"))
        self.assertEqual(log.getvalue(), "a]52;c;ZXZpbA==b\tc\nProgress (round): x[2J\n")


class Save(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "sub", "f.txt")

    def test_writes_replaces_and_makes_the_directory(self):
        drive.save(self.path, "one\n")
        drive.save(self.path, "two\n")
        with open(self.path) as f:
            self.assertEqual(f.read(), "two\n")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["f.txt"])

    def test_a_planted_symlink_is_replaced_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.makedirs(os.path.dirname(self.path))
        os.symlink(outside, self.path)
        drive.save(self.path, "new\n")
        with open(self.path) as f, open(outside) as g:
            self.assertEqual((os.path.islink(self.path), f.read(), g.read()), (False, "new\n", "keep\n"))

    def test_no_temp_is_left_when_the_replace_fails(self):
        drive.save(self.path, "old\n")
        with unittest.mock.patch.object(drive.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                drive.save(self.path, "new\n")
        with open(self.path) as f:
            self.assertEqual((f.read(), os.listdir(os.path.dirname(self.path))), ("old\n", ["f.txt"]))


class Main(Base):
    def run_main(self, *extra, lines=(), rc=0):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return FakeProc(feed(os.path.join(self.work, ".report.jsonl"), lines), rc)

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

    def test_dry_run_prints_the_runners_command(self):
        launch = self.plan("dummy-tester", "echo", client="claude", input="Hello.")
        code, out, _, _ = self.run_main("--dry-run", "--sid", SID, "--runner", "tui")
        self.assertEqual((code, json.loads(out)["argv"]), (0, launch.interactive))

    def test_a_client_without_the_runners_command_exits_2(self):
        for extra in ((), ("--dry-run",)):
            code, out, err, calls = self.run_main("--client", "fake", "--runner", "tui", *extra)
            self.assertEqual((code, out, calls), (2, "", []))
            self.assertIn("drive.py: Recorder has no tui command", err)

    def test_done_run_exits_0_and_reports_session(self):
        code, _, err, calls = self.run_main("--sid", SID, lines=[outcome(DONE)])
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
        self.assertEqual(self.run_main(lines=[outcome(DONE)], rc=1)[0], 3)

    def test_config_error_exits_2(self):
        code, _, err, calls = self.run_main("--task", "essay")
        self.assertEqual((code, calls), (2, []))
        self.assertIn("drive.py:", err)

    def test_resume_end_to_end(self):
        code, _, _, ((argv, _),) = self.run_main("--sid", SID, "--resume", lines=[outcome(DONE)])
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
