import dataclasses
import errno
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402
import report  # noqa: E402

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SID = "11111111-2222-3333-4444-555555555555"
NO_ACCESS = drive.Access(dirs=[], commands=[])
PARAMS = compose.RunParams(input="x", out="o", workdir="w", sid=SID)
TASKS = os.path.join(CORE, "team", "tasks")
METHODS = os.path.join(CORE, "team", "methods")
INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3"}


def run(**kw):
    return compose.RunConfig(**{"role": "r", "tier": 2, "effort": "high", "output": {"type": "local"}, **kw})


class Recorder(clients.Client):
    """A fake client that needs no config file and records what the driver hands it."""
    needs_config = False
    seen = []

    def launch(self, prompt, run, *, params, access):
        Recorder.seen.append(dict(prompt=prompt, run=run, params=params, access=access))
        return clients.Launch(["fake", params.sid], {"FAKE": "1"}, cwd=access.cwd)


def claude(**overrides):
    return clients.ClaudeClient({**clients.load_config("claude", CORE), **overrides})


def own(session="mgr"):
    """A tmux fake answering every call with `session`, as `display-message` asks; its calls are in .calls."""
    def proc(argv, **kw):
        proc.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{session}\n", "")

    proc.calls = []
    return proc


class Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(unittest.mock.patch.dict(os.environ))   # the suite may run inside tmux
        for key in INSIDE:
            os.environ.pop(key, None)
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

    def plan(self, role="researcher", task="light-research", client="fake", repo=None, layers=(), cwd=None, root=CORE,
             **params):
        return drive.plan(root, clients.get(client, root), role, task, params=self.params(**params), repo=repo,
                          layers=layers, cwd=cwd or self.work)[0]

    def report(self):
        return f"python3 {CORE}/src/report.py --to {self.work}/run.jsonl"


class ClientConfig(unittest.TestCase):
    def test_the_real_tables(self):
        self.assertLessEqual({"flags", "tiers"}, set(clients.load_config("claude", CORE)))
        self.assertIn("roles", clients.load_config("skill", CORE))
        self.assertFalse(os.path.exists(os.path.join(CORE, "config")))

    def test_the_local_file_goes_on_top(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "config.toml"), "w") as f:
            f.write('[clients.claude]\nflags = ["-a"]\n[clients.claude.tiers]\n1 = "x"\n2 = "y"\n')
        with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
            f.write('[clients.claude]\nflags = ["-b"]\n[clients.claude.tiers]\n2 = "z"\n')
        self.assertEqual(clients.load_config("claude", tmp.name), {"flags": ["-b"], "tiers": {"1": "x", "2": "z"}})

    def test_missing_or_bad_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.toml")
        with self.assertRaises(compose.ConfigError) as cm:
            clients.load_config("claude", tmp.name)
        self.assertIn(path, str(cm.exception))
        for text in ("tier = 2\n", "clients = 1\n", "[clients]\nclaude = 1\n", "[clients.skill]\n"):
            with open(path, "w") as f:
                f.write(text)
            with self.subTest(text):
                with self.assertRaises(compose.ConfigError) as cm:
                    clients.load_config("claude", tmp.name)
                self.assertIn("[clients.claude]", str(cm.exception))


class Claude(Base):
    def test_no_deny_rules(self):
        for role, task in (("researcher", "light-research"), ("engineer", "build")):
            self.assertNotIn("--disallowedTools", self.plan(role, task, client="claude", repo=self.repo).argv)

    def test_the_prompt_names_the_file_the_agent_writes_the_deliverable_to(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere", "out.md")
        for layers in ([{"output": {"type": "local"}}], [{"output": {"type": "orchestrator"}}]):
            for params, want in (({}, "<Workdir>/out.md"), ({"out": elsewhere}, "<Workdir>/tmp/deliverable.md")):
                with self.subTest(layers=layers, params=params):
                    launch = self.plan(client="claude", repo=self.repo, layers=layers, **params)
                    self.assertIn(f"Write the deliverable to `{want}` and return that file as the outcome's "
                                  "`deliverable`", launch.argv[2])

    def test_light_research_argv(self):
        launch = self.plan(client="claude", repo=self.repo)
        self.assertEqual(launch.argv[:2], ["claude", "-p"])
        self.assertTrue(launch.argv[2].startswith("# Guide"))
        self.assertEqual(launch.argv[3:], [
            "--session-id", SID, "--model", "opus", "--effort", "high",
            "--permission-mode", "auto", "--strict-mcp-config", "--setting-sources", "user",
            "--output-format", "stream-json", "--verbose", "--settings", '{"enabledPlugins": {"agent-pm@agent-pm": false}}',
            "--add-dir", TASKS, "--add-dir", METHODS,
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py worktree --dir {self.work}/src *)",
            f"Bash({self.report()} *)"])
        self.assertIn("\n- `report`: `python3 <scripts>/report.py --to <Workdir>/run.jsonl`\n", launch.argv[2])
        self.assertIn("## Return\n\nReport through `report`, never in a reply:", launch.argv[2])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_build_xhigh_and_repo_commands(self):
        argv = self.plan("engineer", None, client="claude").argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools"] + [
            f"Bash(python3 {CORE}/src/repo.py {cmd} --dir {self.work}/src *)" for cmd in ("worktree", "status")]
            + [f"Bash({self.report()} *)"])

    def test_product_design_argv_has_the_worktree_rule(self):
        argv = self.plan("pm", "product-design", client="claude").argv
        self.assertEqual(argv[argv.index("--allowedTools"):], [
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py worktree --dir {self.work}/src *)",
            f"Bash({self.report()} *)"])
        self.assertIn("`python3 <scripts>/repo.py worktree --dir <Workdir>/src --branch <branch> [--name <checkout>] "
                      "<repo>`", argv[2])

    def test_the_gate_is_pre_approved_verbatim(self):
        gate = "python3 /u/usage.py --below 80"
        argv = self.plan("researcher", "deep-research", client="claude", repo=self.repo,
                         layers=[{"gate": gate}]).argv
        self.assertEqual(argv[argv.index("--allowedTools"):][-1], f"Bash({gate})")
        self.assertIn(f"\n- `<gate>`: `{gate}`\n", argv[2])

    def test_every_run_reads_the_tasks_dir_a_researchers_the_methods_dir_too(self):
        for role, task, dirs in (("researcher", None, [TASKS, METHODS]), ("researcher", "deep-research", [TASKS, METHODS]),
                                 ("researcher", "light-research", [TASKS, METHODS]), ("engineer", None, [TASKS]),
                                 ("dummy-tester", "echo", [TASKS])):
            argv = self.plan(role, task, client="claude", repo=self.repo).argv
            self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"], dirs, (role, task))

    def test_deep_research_names_core_method_files_for_claude_and_by_default(self):
        prompts = [self.plan("researcher", "deep-research", client="claude").argv[2]]
        self.plan("researcher", "deep-research")
        prompts.append(Recorder.seen[-1]["prompt"])
        for prompt in prompts:
            self.assertIn(f"\n- `<methods>`: `{METHODS}`\n", prompt)
            for name in ("deep-research", "ultracode"):
                self.assertTrue(os.path.isfile(os.path.join(METHODS, f"{name}.md")), name)
                self.assertIn(f"`<methods>/{name}.md`", prompt)

    def test_interactive_is_argv_without_the_headless_flags(self):
        for role, task in (("researcher", "light-research"), ("engineer", "build")):
            launch = self.plan(role, task, client="claude", repo=self.repo)
            argv = launch.argv
            j = argv.index("--settings")
            rest = [a for i, a in enumerate(argv) if i not in (1, 2, j, j + 1)]
            for flag in ("--output-format", "stream-json", "--verbose"):
                rest.remove(flag)
            interactive = list(launch.interactive)
            for flag in ("--settings", "--name"):
                i = interactive.index(flag)
                del interactive[i:i + 2]
            self.assertEqual(interactive, ["claude", argv[2], *rest[1:]])

    def test_interactive_names_the_claude_session_after_the_tui_session(self):
        for resume in (False, True):
            for prefix, name in ((None, f"researcher-{SID[:8]}"), ("p-1", f"p-1-{SID[:8]}")):
                with self.subTest(resume=resume, prefix=prefix):
                    launch = self.plan(client="claude", resume=resume, prefix=prefix)
                    i = launch.interactive.index("--name")
                    self.assertEqual((launch.interactive.count("--name"), launch.interactive[i + 1]), (1, name))
                    self.assertNotIn("--name", launch.argv)

    def test_interactive_adds_the_stop_hook_to_the_settings(self):
        launch = self.plan(client="claude")
        i = launch.interactive.index("--settings")
        cmd = f"python3 {CORE}/src/report.py --to {self.work}/run.jsonl stop --pending background_tasks"
        self.assertEqual(json.loads(launch.interactive[i + 1]),
                         {"enabledPlugins": {"agent-pm@agent-pm": False},
                          "hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}})

    def test_agent_runs_keep_this_plugin_out(self):
        with open(os.path.join(CORE, ".claude-plugin", "plugin.json")) as f, \
                open(os.path.join(CORE, "..", ".claude-plugin", "marketplace.json")) as g:
            plugin = f"{json.load(f)['name']}@{json.load(g)['name']}"
        launch = self.plan(client="claude")
        tui = drive.tui_claude.with_hooks(launch.interactive, "/e")
        for argv in (launch.argv, launch.interactive, tui):
            self.assertEqual(argv.count("--settings"), 1)
            self.assertEqual(json.loads(argv[argv.index("--settings") + 1])["enabledPlugins"], {plugin: False})

    def test_interactive_resume(self):
        launch = self.plan(client="claude", resume=True)
        self.assertEqual(launch.interactive[2:4], ["--resume", SID])
        self.assertTrue(launch.interactive[1].startswith("Resumed agent run"))

    def test_other_launches_have_no_interactive_command(self):
        self.assertEqual(clients.Launch(["x"]).interactive, [])
        self.assertEqual(self.plan().interactive, [])

    def test_commands_and_role_rules_become_allowed_tools(self):
        c = claude(roles={"r": {"allow": ["WebFetch"]}})
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

    def test_setting_sources_in_flags_is_refused(self):
        for flag in ("--setting-sources", "--setting-sources=user,project"):
            with self.subTest(flag), self.assertRaises(compose.ConfigError) as cm:
                claude(flags=["--verbose", flag, "user"])
            self.assertIn("--setting-sources is the driver's", str(cm.exception))

    def test_role_value_beats_the_global(self):
        c = claude(allow=["Read"], roles={"r": {"allow": ["WebFetch"]}})
        self.assertEqual(c.value(run(), "allow"), ["WebFetch"])
        self.assertEqual(c.value(run(task="u"), "allow"), ["WebFetch"])
        self.assertEqual(c.value(run(role="x"), "allow"), ["Read"])
        self.assertIsNone(c.value(run(role="x"), "nothing"))

    def test_claude_tier_override_changes_the_model(self):
        with unittest.mock.patch.object(clients, "load_config",
                                        lambda name, root: {**clients.base.load_config(name, root), "tier": 3}):
            argv = self.plan(client="claude").argv
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_transcript_and_resume_command_follow_the_cwd(self):
        self.assertEqual(clients.claude.transcript("/a/b.c d", SID, "/p"), f"/p/-a-b-c-d/{SID}.jsonl")
        launch = self.plan(client="claude")
        self.assertEqual((launch.resume, launch.transcript),
                         (f"cd {self.work} && claude --resume {SID}", clients.claude.transcript(self.work, SID)))
        with redirect_stderr(io.StringIO()):
            launch = self.plan(client="claude", cwd=self.repo)
        self.assertEqual(launch.resume, f"cd {self.repo} && claude --resume {SID} --add-dir {self.work}")
        self.assertEqual(launch.transcript, clients.claude.transcript(self.repo, SID))

    def test_unknown_config_key(self):
        with self.assertRaises(compose.ConfigError) as cm:
            claude(argv=[])
        self.assertIn("unknown key 'argv'", str(cm.exception))


class Generic(Base):
    def test_driver_hands_the_client_neutral_access(self):
        launch = self.plan(repo=self.repo)
        seen, = Recorder.seen
        self.assertEqual(seen["access"], drive.Access(dirs=[TASKS, METHODS], commands=[
            f"python3 {CORE}/src/repo.py worktree --dir {self.work}/src *", f"{self.report()} *"], cwd=self.work))
        self.assertEqual((seen["params"].sid, seen["params"].resume, seen["run"].task), (SID, False, "light-research"))
        self.assertTrue(seen["prompt"].startswith("# Guide"))
        self.assertEqual((launch.argv, launch.env, launch.cwd), (["fake", SID], {"FAKE": "1"}, self.work))

    def test_layers_apply_after_the_clients_config(self):
        client = Recorder({"tier": 3, "effort": "low"})
        drive.plan(CORE, client, "researcher", "light-research", params=self.params(), cwd=self.work)
        drive.plan(CORE, client, "researcher", "light-research", params=self.params(), layers=[{"tier": 4}, {"tier": 1}],
                   cwd=self.work)
        plain, layered = (s["run"] for s in Recorder.seen)
        self.assertEqual((plain.tier, plain.effort), (3, "low"))
        self.assertEqual((layered.tier, layered.effort), (1, "low"))

    def test_access_appends_the_gate_after_the_filled_commands(self):
        gate = "python3 /u/usage.py {{workdir}} *"
        acc = drive.access(run(commands=["{{scripts}}/x *"], gate=gate), self.params(), repo=None, scripts="/s",
                           methods="/m", tasks="/t")
        report = f"python3 /s/report.py --to {self.work}/run.jsonl *"
        self.assertEqual(acc.commands, ["/s/x *", report, gate])
        acc = drive.access(run(commands=["x"]), self.params(), repo=None, scripts="/s", methods="/m", tasks="/t")
        self.assertEqual(acc.commands, ["x", report])

    def test_repo_entry_binds_to_the_repo_arg(self):
        acc = drive.access(run(write=["repo"], commands=["{{scripts}}/x --dir {{workdir}}/src *"]), self.params(),
                           repo=self.repo, scripts="/s", methods="/m", tasks="/t")
        self.assertEqual(acc, drive.Access(dirs=["/t", self.repo], commands=[
            f"/s/x --dir {self.work}/src *", f"python3 /s/report.py --to {self.work}/run.jsonl *"], cwd=self.work))

    def test_methods_fills_read_and_write_entries_and_nothing_else_does(self):
        acc = drive.access(run(read=["{{methods}}", "/t"], write=["{{methods}}/out"]), self.params(), repo=None,
                           scripts="/s", methods="/m", tasks="/t")
        self.assertEqual(acc.dirs, ["/t", "/m", "/m/out"])
        for key in ("read", "write"):
            with self.subTest(key), self.assertRaises(compose.ConfigError) as cm:
                drive.access(run(**{key: ["{{nope}}"]}), self.params(), repo=None, scripts="/s", methods="/m", tasks="/t")
            self.assertIn("{{nope}}", str(cm.exception))

    def test_the_run_never_needs_the_out_dir(self):
        self.plan(out=os.path.join(self.tmp.name, "elsewhere", "out.md"))
        self.assertEqual(Recorder.seen[0]["access"].dirs, [TASKS, METHODS])

    def test_repo_ignored_without_repo_arg(self):
        self.plan("engineer", None)
        self.assertEqual(Recorder.seen[0]["access"].dirs, [TASKS])

    def test_unknown_client(self):
        with self.assertRaises(compose.ConfigError) as cm:
            self.plan(client="nope")
        self.assertIn("unknown client 'nope'", str(cm.exception))

    def test_plan_sets_the_configs_status_line(self):
        self.assertIs(self.plan().status_line, False)
        path = os.path.join(hermetic.home(self), "core.local.toml")
        for text, want in (("status_line = true\n", True), ("status_line = false\n", False)):
            with open(path, "w") as f:
                f.write(text)
            self.assertIs(self.plan().status_line, want, text)
        with open(path, "w") as f:
            f.write('status_line = "on"\n')
        with self.assertRaisesRegex(compose.ConfigError, "status_line: want true or false"):
            self.plan()

    def test_plan_sets_the_configs_workers_per_column(self):
        self.assertIsNone(self.plan().per_column)
        path = os.path.join(hermetic.home(self), "core.local.toml")
        with open(path, "w") as f:
            f.write("workers_per_column = 2\n")
        self.assertEqual(self.plan().per_column, 2)
        with open(path, "w") as f:
            f.write("workers_per_column = 0\n")
        with self.assertRaisesRegex(compose.ConfigError, "workers_per_column: want an integer from 1 to 9999"):
            self.plan()

    def test_plan_and_inline_each_need_their_kind_of_client(self):
        with self.assertRaises(compose.ConfigError):
            self.plan(client="skill")
        with self.assertRaises(compose.ConfigError):
            drive.inline(CORE, claude(), "dummy-tester")


class Cwd(Base):
    """drive.place: the run's cwd and whether its project settings load."""
    def setUp(self):
        super().setUp()
        self.trusted = os.path.realpath(os.path.join(self.tmp.name, "trusted"))
        self.other = os.path.realpath(os.path.join(self.tmp.name, "other"))
        for d in (self.trusted, self.other):
            os.makedirs(d)
            with open(os.path.join(d, ".mcp.json"), "w") as f:
                f.write("{}")
        self.local = os.path.join(hermetic.home(self), "core.local.toml")
        self.read = [os.path.join(self.tmp.name, "core", "team", d) for d in ("tasks", "methods")]

    def core(self, *trusted):
        """A core root: the committed config.toml, and the local file listing `trusted` as trusted_dirs."""
        root = os.path.join(self.tmp.name, "core")
        os.makedirs(root, exist_ok=True)
        for d in ("team", "output"):
            if not os.path.lexists(os.path.join(root, d)):
                os.symlink(os.path.join(CORE, d), os.path.join(root, d))
        shutil.copy(os.path.join(CORE, compose.CONFIG), root)
        with open(self.local, "w") as f:
            f.write(f"trusted_dirs = {json.dumps(list(trusted))}\n")
        return root

    def launch(self, *trusted, cwd=None, layers=(), **params):
        """(Launch, stderr) of light-research for claude, trusted_dirs `trusted`."""
        err = io.StringIO()
        with redirect_stderr(err):
            launch = self.plan(client="claude", root=self.core(*trusted), cwd=cwd, layers=layers, **params)
        return launch, err.getvalue()

    @staticmethod
    def flag(argv, name):
        return [argv[i + 1] for i, a in enumerate(argv) if a == name]

    def test_unset_is_the_callers_current_directory(self):
        err = io.StringIO()
        with redirect_stderr(err):
            launch, _ = drive.plan(CORE, claude(), "researcher", "light-research", params=self.params())
        self.assertEqual((launch.cwd, launch.project), (os.getcwd(), False))
        self.assertEqual(self.flag(launch.argv, "--add-dir"), [self.work, TASKS, METHODS])
        self.assertEqual(err.getvalue(), f"drive.py: cwd {os.getcwd()} is not in trusted_dirs: its project settings are off\n")

    def test_the_run_key_wins_over_the_default_and_expands_tilde(self):
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            launch, _ = self.launch(cwd=self.trusted, layers=[{"roles": {"researcher": {"cwd": "~/other"}}}])
        self.assertEqual(launch.cwd, os.path.join(self.tmp.name, "other"))

    def test_a_trusted_cwd_loads_project_settings_and_its_mcp_config(self):
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.trusted, link)
        for entry, cwd in ((self.trusted, self.trusted), (link, self.trusted), (self.tmp.name, self.trusted)):
            with self.subTest(entry=entry):
                launch, err = self.launch(entry, cwd=cwd)
                for argv in (launch.argv, launch.interactive):
                    self.assertEqual(self.flag(argv, "--setting-sources"), ["user,project,local"])
                    self.assertEqual(self.flag(argv, "--mcp-config"), [os.path.join(cwd, ".mcp.json")])
                    self.assertEqual(self.flag(argv, "--add-dir"), [self.work, *self.read])
                    self.assertIn("--strict-mcp-config", argv)
                self.assertEqual((launch.cwd, launch.project, err), (cwd, True, ""))

    def test_a_trusted_cwd_without_mcp_json_gets_no_mcp_config(self):
        os.remove(os.path.join(self.trusted, ".mcp.json"))
        launch, _ = self.launch(self.trusted, cwd=self.trusted)
        self.assertEqual(self.flag(launch.argv, "--mcp-config"), [])

    def test_an_untrusted_cwd_gets_user_settings_and_a_notice(self):
        launch, err = self.launch(self.trusted, cwd=self.other)
        self.assertEqual((self.flag(launch.argv, "--setting-sources"), self.flag(launch.argv, "--mcp-config")), (["user"], []))
        self.assertEqual((launch.cwd, launch.project, self.flag(launch.argv, "--add-dir")),
                         (self.other, False, [self.work, *self.read]))
        self.assertEqual(err, f"drive.py: cwd {self.other} is not in trusted_dirs: its project settings are off\n")

    def test_the_workdir_as_cwd_has_no_notice_and_adds_no_workdir(self):
        launch, err = self.launch()
        self.assertEqual((launch.cwd, err, self.flag(launch.argv, "--add-dir")), (self.work, "", self.read))
        self.assertEqual(self.flag(launch.argv, "--setting-sources"), ["user"])

    def test_a_trusted_cwd_in_the_workdir_is_refused(self):
        sub = os.path.join(self.work, "sub")
        os.makedirs(sub)
        for cwd in (self.work, sub):
            with self.subTest(cwd=cwd), self.assertRaises(compose.ConfigError) as cm:
                self.launch(self.tmp.name, cwd=cwd)
            self.assertIn(f"cwd {cwd} is in trusted_dirs and in the workdir", str(cm.exception))
        launch, _ = self.launch(cwd=sub)
        self.assertEqual((launch.cwd, launch.project), (sub, False))

    def test_a_cwd_that_is_no_directory_or_no_path_is_a_config_error(self):
        for cwd, want in ((os.path.join(self.tmp.name, "nope"), "is not a directory"), ("rel/dir", "absolute or ~ path")):
            with self.subTest(cwd=cwd), self.assertRaises(compose.ConfigError) as cm:
                self.launch(cwd=self.other, layers=[{"cwd": cwd}])
            self.assertIn(want, str(cm.exception))

    def test_a_bad_trusted_dirs_is_a_config_error(self):
        with self.assertRaises(compose.ConfigError) as cm:
            self.launch("rel/dir")
        self.assertIn("trusted_dirs", str(cm.exception))

    def write_record(self, *entries):
        """<workdir>/run.jsonl holding one `session` event per entry."""
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "run.jsonl"), "w") as f:
            f.writelines(json.dumps({"ts": "t", "kind": "session", **e}) + "\n" for e in entries)

    def test_a_resume_runs_in_the_cwd_its_start_recorded(self):
        new, _ = self.launch(self.trusted, cwd=self.trusted)
        p = self.params()
        with redirect_stderr(io.StringIO()):
            drive.start(new, run(), p, client=claude(), sinks=[],
                        popen=lambda argv, **kw: FakeProc(feed(p.channel, [outcome(DONE)])))
        launch, err = self.launch(self.trusted, cwd=self.other, layers=[{"cwd": self.work}], resume=True)
        self.assertEqual((launch.cwd, launch.project, err), (self.trusted, True, ""))

    def test_a_resume_reuses_the_recorded_cwd_and_project(self):
        self.write_record({"sid": SID, "cwd": self.trusted, "project": True})
        launch, err = self.launch(self.trusted, cwd=self.other, layers=[{"cwd": self.work}], resume=True)
        self.assertEqual((launch.cwd, launch.project, err), (self.trusted, True, ""))
        self.assertEqual(self.flag(launch.argv, "--resume"), [SID])
        self.write_record({"sid": SID, "cwd": self.trusted, "project": False})
        launch, _ = self.launch(self.trusted, resume=True)
        self.assertEqual((launch.cwd, self.flag(launch.argv, "--setting-sources")), (self.trusted, ["user"]))

    def test_a_resume_whose_cwd_is_no_longer_trusted_is_refused(self):
        self.write_record({"sid": SID, "cwd": self.trusted, "project": True})
        with self.assertRaises(compose.ConfigError) as cm:
            self.launch(resume=True)
        self.assertIn(f"cwd {self.trusted} is no longer in trusted_dirs", str(cm.exception))

    def test_a_resume_without_its_session_in_the_record_runs_in_the_workdir(self):
        for entries in ((), ({"sid": "other", "cwd": self.trusted, "project": True},),
                        ({"sid": SID, "cwd": "rel", "project": True},), ({"sid": SID, "cwd": self.trusted, "project": 1},)):
            with self.subTest(entries=entries):
                self.write_record(*entries)
                launch, err = self.launch(self.trusted, cwd=self.trusted, resume=True)
                self.assertEqual((launch.cwd, launch.project, err), (self.work, False, ""))

    def test_prompt_paths_stay_absolute_whatever_the_cwd_and_an_input_naming_a_file_is_text(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, "input.md"), "w") as f:
            f.write("Research X.")
        here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, here)
        for cwd in (self.trusted, self.other):
            with self.subTest(cwd=cwd):
                launch, _ = self.launch(self.trusted, cwd=cwd, input="work/input.md", workdir="work")
                work = os.path.join(os.getcwd(), "work")
                self.assertIn(f"\n- `<Workdir>`: `{work}`\n", launch.argv[2])
                self.assertTrue(launch.argv[2].endswith("\n# Input\n\nwork/input.md\n"))


class Skill(Base):
    def text(self, role, task=None):
        return drive.inline(CORE, clients.get("skill", CORE), role, task)

    def test_a_destination_has_no_deliverable_file_line(self):
        for role in ("researcher", "pm", "dummy-tester"):
            text = self.text(role)
            self.assertNotIn("Write the deliverable to", text, role)
            self.assertIn("publish, post or save it nowhere else. Leave `url` empty.", text, role)

    def test_prints_the_prompt_with_this_cores_paths(self):
        text = self.text("pm")
        self.assertTrue(text.startswith("# Guide"))
        self.assertIn(f"\n- `<scripts>`: `{CORE}/src`\n", text)
        self.assertIn("`python3 <scripts>/repo.py worktree --dir <Workdir>/src --branch <branch> [--name <checkout>] "
                      "<repo>`", text)
        for placeholder in ("${CLAUDE_SKILL_DIR}", "$ARGUMENTS", "{{"):
            self.assertNotIn(placeholder, text)

    def test_parameters_and_input_replace_the_tail(self):
        text = self.text("pm")
        self.assertIn(f"\n# Parameters\n\n{compose.RULE}\n\n- `<Workdir>`: the dir `mktemp -d` prints, run once at the "
                      "start and reused for this invocation\n- `<scripts>`: ", text)
        self.assertNotIn("- `report`:", text)
        self.assertTrue(text.endswith("\n# Input\n\nGiven with this prompt.\n"))
        for tail in ("\n---\n\nInput:", "Workdir: "):
            self.assertNotIn(tail, text)

    def test_a_researcher_names_core_methods(self):
        text = self.text("researcher")
        self.assertIn(f"\n- `<methods>`: `{METHODS}`\n", text)
        for name in ("deep-research", "ultracode"):
            self.assertTrue(os.path.isfile(os.path.join(METHODS, f"{name}.md")), name)
            self.assertIn(f"`<methods>/{name}.md`", text)

    def test_document_roles_return_to_the_orchestrator(self):
        for role, task in (("researcher", None), ("researcher", "light-research"), ("pm", None), ("dummy-tester", None)):
            text = self.text(role, task)
            self.assertIn("publish, post or save it nowhere else. Leave `url` empty.", text)
            self.assertNotIn("ophis/private_docs", text)
            self.assertIn("## Return\n\nEnd with your final reply in this conversation", text)
            self.assertNotIn("Output: ", text)

    def test_pull_request_stays(self):
        for task in (None, "build", "light-build"):
            text = self.text("engineer", task)
            self.assertIn("gh pr create", text, task)
            self.assertNotIn("publish, post or save it nowhere", text, task)

    def test_the_engineers_resume_stays(self):
        self.assertIn("\n- **Resume**: run [Engineer › Repo](#repo) steps 1–2 again, then "
                      "[Engineer › Repo › Merge](#repo)", self.text("engineer"))

    def test_every_run_renders(self):
        for role, task in (("researcher", None), ("researcher", "deep-research"), ("pm", None), ("engineer", None),
                           ("engineer", "light-build"), ("dummy-tester", None), ("dummy-tester", "echo")):
            self.assertNotIn("{{", self.text(role, task))

    def test_main_prints_the_prompt_and_runs_nothing(self):
        calls = []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = drive.main(["--client", "skill", "--role", "dummy-tester"], root=CORE,
                              popen=lambda *a, **k: calls.append(a))
        self.assertEqual((code, calls, err.getvalue()), (0, [], ""))
        self.assertEqual(out.getvalue(), self.text("dummy-tester"))

    def test_main_names_a_given_task_else_the_run_picks_one(self):
        for extra, want in (((), "**Your task**: pick it from your charter's Tasks section"),
                            (("--task", "light-build"), "**Your task**: `light-build` (`<tasks>/light-build.md`).")):
            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                self.assertEqual(drive.main(["--client", "skill", "--role", "engineer", *extra], root=CORE), 0)
            self.assertIn(want, out.getvalue().split("\n# Principles\n", 1)[0])

    def test_an_old_skill_output_table_exits_2(self):
        with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
            f.write('[clients.skill.roles.researcher.tasks.deep-research.output]\ntype = "orchestrator"\n')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            self.assertEqual(drive.main(["--client", "skill", "--role", "researcher"], root=CORE), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("[clients.skill.roles.researcher]", err.getvalue())

    def test_main_refuses_a_runner_other_than_headless(self):
        for extra in ((), ("--dry-run",)):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                code = drive.main(["--client", "skill", "--role", "dummy-tester", "--runner", "tui", *extra], root=CORE)
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn("drive.py: SkillClient prints a prompt; it takes no --runner tui\n", err.getvalue())

    def test_main_takes_the_client_once(self):
        calls = []
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            drive.main(["--client", "skill", "--role", "dummy-tester", "--client", "claude", "--input", "x", "--out",
                        "o.md", "--workdir", self.work], root=CORE, popen=lambda *a, **k: calls.append(a))
        self.assertEqual((cm.exception.code, calls), (2, []))


class RoleRuns(Base):
    """drive.py runs a role; --task, when given, names one of its index."""
    def main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--input", "Build X.", "--out", os.path.join(self.work, "out.md"), "--workdir", self.work, "--sid", SID,
                "--dry-run", *extra]
        with redirect_stdout(out), redirect_stderr(err), unittest.mock.patch.object(drive, "start") as start:
            code = drive.main(argv, root=CORE)
        start.assert_not_called()
        return code, json.loads(out.getvalue()) if code == 0 else out.getvalue(), err.getvalue()

    def test_an_engineer_run_needs_no_task(self):
        code, shown, _ = self.main("--role", "engineer")
        argv = shown["argv"]
        self.assertEqual((code, argv[argv.index("--effort") + 1]), (0, "xhigh"))
        self.assertIn("gh pr create", argv[2])
        self.assertIn("**Your task**: pick it from your charter's Tasks section", argv[2].split("\n# Principles\n")[0])
        self.assertIn(TASKS, [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"])

    def test_a_given_task_is_named_in_the_prompt(self):
        code, shown, _ = self.main("--role", "researcher", "--task", "light-research")
        self.assertEqual(code, 0)
        self.assertIn("**Your task**: `light-research` (`<tasks>/light-research.md`).",
                      shown["argv"][2].split("\n# Principles\n")[0])

    def test_the_tui_session_is_named_after_the_role(self):
        code, shown, _ = self.main("--role", "engineer", "--runner", "tui")
        argv = shown["argv"]
        self.assertEqual((code, argv[argv.index("--name") + 1]), (0, f"engineer-{SID[:8]}"))

    def test_a_task_off_the_roles_index_exits_2(self):
        code, out, err = self.main("--role", "engineer", "--task", "deep-research")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "drive.py: task 'deep-research' is not one of engineer's tasks (build, light-build)\n")

    def test_old_keys_in_the_core_local_file_exit_2(self):
        path = os.path.join(hermetic.home(self), "core.local.toml")
        for local in ('[roles.engineer.tasks.build]\neffort = "max"\n', '[roles.engineer]\ndefault_task = "build"\n'):
            with self.subTest(local=local):
                with open(path, "w") as f:
                    f.write(local)
                code, out, err = self.main("--role", "engineer")
                self.assertEqual((code, out), (2, ""))
                self.assertIn("[roles.engineer]", err)
                self.assertNotIn("unknown key", err)

    def test_a_resume_prompt_continues_its_task_and_names_none(self):
        self.plan("engineer", None, resume=True)
        prompt = Recorder.seen[-1]["prompt"]
        self.assertTrue(prompt.startswith(compose.RESUME + "# Guide\n"))
        self.assertIn("Continue the task this session already picked or was given; never pick it again.", prompt)
        self.assertIn("**Your task**: pick it from", prompt.split("\n# Principles\n")[0])


class FakeProc:
    def __init__(self, lines, rc=0, stderr=()):
        self.stdout, self.rc, self.stderr = lines, rc, stderr

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
    """The agent run's stdout: each str a line of it; each dict appended to the channel first, as report.py does."""
    for item in items:
        if isinstance(item, dict):
            drive.append_line(channel, json.dumps(item, ensure_ascii=False) + "\n")
        else:
            yield item


DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
STOP = {"kind": "stop"}
PENDING = {"kind": "stop", "pending": 2}


class FakeTui:
    """tui's API for the tui runner. Each status call takes the next step: a list of items appended to the channel
    (the pane runs on), or a pane state (None: gone; an int: dead with that status); a (t, step) pair first sets the
    fake clock, which starts at 0, to t seconds. No step left fails the test."""
    def __init__(self, channel, steps):
        self.channel, self.steps, self.calls, self.now = channel, list(steps), [], 0

    def patch(self, **api):
        """tui's start, status, kill and send replaced by this fake's, or by `api`'s; time.monotonic by its clock."""
        stack = ExitStack()
        stack.enter_context(unittest.mock.patch.multiple(
            drive.tui_claude, **{"start": self.start, "status": self.status, "kill": self.kill, "send": self.send, **api}))
        stack.enter_context(unittest.mock.patch.object(time, "monotonic", lambda: self.now))
        return stack

    def start(self, name, argv, **kw):
        self.calls.append(("start", name, argv, kw))

    def status(self, name):
        if not self.steps:
            raise AssertionError("polled after the last step")
        step = self.steps.pop(0)
        if isinstance(step, tuple):
            self.now, step = step
        if not isinstance(step, list):
            return step
        for item in step:
            drive.append_line(self.channel, json.dumps(item) + "\n")
        return drive.tui_claude.RUNNING

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
        self.assertIn("`report progress <name>", text)
        self.assertIn("`report outcome --status", text)
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
        self.channel = os.path.join(self.work, "run.jsonl")

    def report(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = report.main(["--to", self.channel, *argv])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    def raw(self):
        if not os.path.exists(self.channel):
            return []
        with open(self.channel) as f:
            return [json.loads(line) for line in f]

    def lines(self):
        """The lines without their `ts`."""
        return [{k: v for k, v in line.items() if k != "ts"} for line in self.raw()]

    def stop_line(self, stdin, *argv):
        """The one line `report.py stop ARGV` appends with `stdin` as sys.stdin."""
        before = len(self.lines())
        with unittest.mock.patch("sys.stdin", stdin):
            self.assertEqual(self.report("stop", *argv), (0, "report.py: stop reported\n"))
        lines = self.lines()
        self.assertEqual(len(lines), before + 1)
        return lines[-1]

    def test_stop_pending_counts_the_list_at_key_on_stdin(self):
        for tasks, n in (([{}, {}], 2), ([], 0)):
            with self.subTest(n=n):
                stdin = io.StringIO(json.dumps({"background_tasks": tasks}))
                self.assertEqual(self.stop_line(stdin, "--pending", "background_tasks"), {"kind": "stop", "pending": n})

    def test_stop_pending_without_a_list_at_key_is_a_plain_stop(self):
        undecodable = io.TextIOWrapper(io.BytesIO(b'{"background_tasks": [\xff\xfe]}'), encoding="utf-8")
        for stdin in (io.StringIO(""), io.StringIO("not json"), io.StringIO("[1]"), io.StringIO("{}"),
                      io.StringIO('{"background_tasks": 3}'), undecodable):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.stop_line(stdin, "--pending", "background_tasks"), STOP)

    def test_stop_without_pending_reads_no_stdin(self):
        class Unreadable:
            def read(self, *args):
                raise AssertionError("stdin was read")
        self.assertEqual(self.stop_line(Unreadable()), STOP)

    def test_report_event_maps_a_stops_pending_count(self):
        self.assertEqual(drive.report_event('{"kind": "stop"}'), clients.Event("stop"))
        self.assertEqual(drive.report_event('{"kind": "stop", "pending": 2}'), clients.Event("stop", pending=2))
        self.assertEqual(drive.report_event('{"kind": "stop", "pending": 0}'), clients.Event("stop"))
        for pending in ("true", "-1", '"2"', "1.5"):
            with self.subTest(pending=pending):
                self.assertEqual(drive.report_event('{"kind": "stop", "pending": %s}' % pending), clients.Event("stop"))

    def test_every_line_starts_with_an_iso_ts_with_offset_then_the_kind(self):
        doc = os.path.join(self.work, "doc.md")
        with open(doc, "w") as f:
            f.write("# Doc\n")
        self.assertEqual(self.report("progress", "start", "go")[0], 0)
        self.assertEqual(self.report("outcome", "--status", "done", "--title", "T", "--summary", "S",
                                     "--deliverable", doc)[0], 0)
        with unittest.mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(self.report("stop")[0], 0)
        lines = self.raw()
        self.assertEqual([list(line)[:2] for line in lines],
                         [["ts", "kind"]] * 3)
        self.assertEqual([line["kind"] for line in lines], ["progress", "outcome", "stop"])
        for line in lines:
            self.assertIsNotNone(datetime.fromisoformat(line["ts"]).utcoffset(), line["ts"])
        self.assertEqual(lines[1]["outcome"]["deliverable"], "# Doc\n")

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
    def setUp(self):
        super().setUp()
        quiet = redirect_stderr(io.StringIO())   # e.g. the missing start mark of a run that reports none
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def start(self, items, rc=0, output=None, sinks=None, stderr=(), **params):
        """Runs `items` through drive.start: str lines on stdout, dicts (or callables) on the channel; `stderr`'s
        lines on stderr."""
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work)
        calls, log = [], io.StringIO()
        p = self.params(**params)

        def popen(argv, **kw):
            calls.append((argv, kw))
            self.proc = FakeProc(feed(p.channel, items), rc, stderr)
            return self.proc

        r = drive.start(launch, run(output=output or {"type": "local"}), p, client=claude(), popen=popen,
                        sinks=sinks if sinks is not None else [drive.terminal(log)])
        return r, calls, log.getvalue()

    def read(self, name):
        with open(os.path.join(self.work, name)) as f:
            return f.read()

    def events(self):
        """The run's record, <workdir>/run.jsonl, one dict per line."""
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def last(self, kind):
        return [e for e in self.events() if e["kind"] == kind][-1]

    def exists(self, name):
        return os.path.lexists(os.path.join(self.work, name))

    def test_done_run_saves_outcome_deliverable_and_progress(self):
        r, ((argv, kw),), log = self.start([progress("round", "half way"), said("hi"), outcome(DONE)])
        out = os.path.join(self.work, "out.md")
        self.assertEqual((r.returncode, r.outcome.status, r.outcome.url), (0, "done", out))
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake"], self.work, "1"))
        self.assertEqual(self.read("out.md"), "# Doc\n")
        self.assertEqual(self.last("result")["outcome"]["url"], out)
        self.assertEqual([(e["name"], e["text"]) for e in self.events() if e["kind"] == "progress"],
                         [("round", "half way")])
        self.assertEqual(log, "Progress (round): half way\nhi\n")

    def test_run_env_drops_the_parent_session_keys(self):
        parent = {"CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_JOB_DIR": "/j", "CLAUDECODE": "1"}
        with unittest.mock.patch.dict(os.environ, parent):
            _, ((_, kw),), _ = self.start([outcome(DONE)])
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", kw["env"])
        self.assertNotIn("CLAUDE_JOB_DIR", kw["env"])
        self.assertEqual((kw["env"]["CLAUDECODE"], kw["env"]["FAKE"]), ("1", "1"))

    def test_pwd_is_the_runs_cwd(self):
        with unittest.mock.patch.dict(os.environ, {"PWD": "/stale"}):
            _, ((_, kw),), _ = self.start([outcome(DONE)])
        self.assertEqual((kw["cwd"], kw["env"]["PWD"]), (self.work, self.work))

    def test_stop_events_reach_no_sink(self):
        seen = []
        r, _, _ = self.start([{"kind": "stop"}, progress("start", "x"), {"kind": "stop"}, outcome(DONE)],
                             sinks=[lambda e: seen.append(e.kind)])
        self.assertEqual((r.returncode, seen), (0, ["progress", "outcome"]))

    def test_headless_ignores_stops(self):
        with unittest.mock.patch.object(drive.tui_claude, "send") as send:
            r, _, _ = self.start([*[{"kind": "stop"}] * (drive.STOP_LIMIT + 1), said("still going"), outcome(DONE)])
        self.assertEqual((r.returncode, r.outcome.status, send.called), (0, "done", False))

    def test_progress_arrives_while_stdout_is_quiet(self):
        got = threading.Event()

        def quiet():
            drive.append_line(os.path.join(self.work, "run.jsonl"), json.dumps(progress("start", "go")) + "\n")
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

    def test_stderr_lines_are_events_and_a_nonzero_exit_is_the_error(self):
        seen = []

        def popen(argv, **kw):
            return FakeProc([], 1, stderr=["boom\n", "\n", "bang\n"])

        launch = drive.Launch(["fake"], cwd=self.work)
        r = drive.start(launch, run(), self.params(), client=claude(), popen=popen, sinks=[seen.append])
        self.assertEqual((r.returncode, r.error), (1, "the client exited 1"))
        self.assertEqual([(e.kind, e.text) for e in seen], [("stderr", "boom"), ("stderr", ""), ("stderr", "bang")])
        errs = [e for e in self.events() if e["kind"] == "stderr"]
        self.assertEqual([list(e) for e in errs], [["ts", "kind", "text"]] * 3)
        self.assertEqual([e["text"] for e in errs], ["boom", "", "bang"])
        self.assertEqual(self.last("result")["error"], "the client exited 1")

    def test_stderr_is_recorded_between_the_reports_it_arrived_with(self):
        r, _, log = self.start([progress("start", "go"), said("hi"), outcome(DONE)], stderr=["warn\n"])
        self.assertEqual(r.returncode, 0)
        kinds = [e["kind"] for e in self.events()]
        self.assertEqual(sorted(kinds), sorted(["input", "session", "progress", "stderr", "outcome", "result", "end"]))
        self.assertEqual(sorted(log.splitlines()), ["Progress (start): go", "hi", "warn"])

    def test_the_run_ends_only_after_both_streams_do(self):
        out_done = threading.Event()

        def stdout():
            yield said("hi")
            out_done.set()

        def stderr():
            self.assertTrue(out_done.wait(5), "stdout never ended")
            time.sleep(0.1)   # past the stdout sentinel
            yield "late\n"

        launch = drive.Launch(["fake"], cwd=self.work)
        seen = []
        drive.start(launch, run(), self.params(), client=claude(), sinks=[seen.append],
                    popen=lambda argv, **kw: FakeProc(stdout(), 1, stderr()))
        self.assertEqual([(e.kind, e.text) for e in seen], [("text", "hi"), ("stderr", "late")])
        self.assertEqual([e["text"] for e in self.events() if e["kind"] == "stderr"], ["late"])

    def test_both_pipes_are_taken_as_text_with_bad_bytes_replaced(self):
        _, ((_, kw),), _ = self.start([outcome(DONE)])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"], kw["text"], kw["errors"]),
                         (subprocess.DEVNULL, subprocess.PIPE, subprocess.PIPE, True, "replace"))

    def test_an_error_reading_stderr_stops_the_run(self):
        def stderr():
            yield "x\n"
            raise KeyboardInterrupt

        launch = drive.Launch(["fake"], cwd=self.work)
        with self.assertRaises(KeyboardInterrupt):
            drive.start(launch, run(), self.params(), client=claude(), sinks=[],
                        popen=lambda argv, **kw: FakeProc(iter(()), 0, stderr()))

    def test_the_stderr_record_failing_never_stops_the_run(self):
        real = drive.append_line

        def append_line(path, line):
            if '"kind": "stderr"' in line:
                raise OSError(errno.ENOSPC, "full")
            real(path, line)

        seen = []
        with unittest.mock.patch.object(drive, "append_line", append_line):
            r, _, _ = self.start([progress("start", "go"), outcome(DONE)], sinks=[seen.append], stderr=["warn\n"])
        self.assertEqual((r.returncode, r.outcome.status, sorted(e.kind for e in seen)),
                         (0, "done", ["outcome", "progress", "stderr"]))
        self.assertFalse([e for e in self.events() if e["kind"] == "stderr"])

    def test_the_channel_exists_before_launch(self):
        def items():
            self.assertTrue(os.path.isfile(os.path.join(self.work, "run.jsonl")))
            yield said("x")
        self.start(items())

    def test_only_lines_appended_after_start_count(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, "run.jsonl"), "w") as f:
            f.write(json.dumps(progress("start", "old")) + "\n" + json.dumps(outcome(DONE)) + "\n")
        seen = []
        r, _, _ = self.start([said("resumed")], sinks=[seen.append], resume=True)
        self.assertEqual((r.error, [e.kind for e in seen]), ("the agent run returned no outcome", ["text"]))
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
        self.start([progress("start", "first build\n  of the PRD")], sinks=[seen.append])
        self.assertEqual([(e.name, e.text) for e in seen if e.kind == "progress"], [("start", "first build of the PRD")])

    def test_a_fifo_at_the_channel_is_refused_not_waited_on(self):
        os.makedirs(self.work)
        os.mkfifo(os.path.join(self.work, "run.jsonl"))
        with self.assertRaises(OSError):
            self.start([outcome(DONE)])

    def test_garbage_and_partial_lines(self):
        def items():
            with open(os.path.join(self.work, "run.jsonl"), "a") as f:
                f.write("not json\n[]\n" + json.dumps(progress("a", "1"))[:10])
            yield said("x")
            with open(os.path.join(self.work, "run.jsonl"), "a") as f:
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
            os.remove(os.path.join(self.work, "run.jsonl"))
            os.symlink(target, os.path.join(self.work, "run.jsonl"))
            with open(target, "a") as f:
                f.write(json.dumps(outcome(DONE)) + "\n")
            yield said("x")
        r, _, _ = self.start(items())
        self.assertEqual(r.error, "the agent run returned no outcome")

    def test_orchestrator_destination_saves_the_deliverable_with_no_url(self):
        r, _, _ = self.start([outcome(DONE)], output={"type": "orchestrator"})
        self.assertEqual((r.outcome.url, self.read("out.md")), ("", "# Doc\n"))

    def test_github_destination_keeps_the_runs_url_and_saves_no_deliverable(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        url = "https://github.com/o/docs/blob/main/x.md"
        r, _, _ = self.start([outcome({**DONE, "url": url})], output=gh)
        self.assertEqual(r.outcome.url, url)
        self.assertFalse(self.exists("out.md"))

    def test_needs_input_reaches_the_record(self):
        self.start([outcome({**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""})])
        self.assertEqual(self.last("result")["outcome"]["questions"], ["Which repo?"])

    def test_no_outcome(self):
        r, _, _ = self.start([said("bye")])
        self.assertEqual((r.returncode, r.outcome, r.error), (0, None, "the agent run returned no outcome"))

    def test_failed_client_ignores_the_outcome_and_leaves_no_stale_outcome(self):
        self.start([outcome(DONE)])
        seen = []
        r, _, _ = self.start([outcome(DONE)], rc=143, sinks=[seen.append])
        self.assertEqual((r.returncode, r.outcome, seen), (143, None, []))
        self.assertFalse(self.exists("out.md"))
        self.assertEqual(self.last("result")["error"], "the client exited 143")

    def test_invalid_outcome_is_the_error_and_reaches_no_sink(self):
        seen = []
        r, _, _ = self.start([outcome({**DONE, "status": "maybe"})], sinks=[seen.append])
        self.assertIn("invalid outcome: status 'maybe'", r.error)
        self.assertEqual(seen, [])

    def test_a_symlink_planted_at_out_during_the_run_is_replaced_not_followed(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def items():
            os.symlink(target, os.path.join(self.work, "out.md"))
            yield from feed(os.path.join(self.work, "run.jsonl"), [progress("round", "x"), outcome(DONE)])

        self.start(items())
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.assertEqual(self.last("result")["outcome"]["status"], "done")
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def spy_save(self):
        patch = unittest.mock.patch.object(drive, "save", wraps=drive.save)
        self.addCleanup(patch.stop)
        return patch.start()

    def test_a_deliverable_the_agent_wrote_to_out_is_not_saved_again(self):
        save, out, written = self.spy_save(), os.path.join(self.work, "out.md"), []

        def items():
            with open(out, "w") as f:
                f.write("# Doc\n")
            written.append(os.stat(out))
            yield from feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)])

        for dest in ("local", "orchestrator"):
            with self.subTest(dest):
                r, _, _ = self.start(items(), output={"type": dest})
                st = os.stat(out)
                self.assertEqual((st.st_ino, st.st_mtime_ns), (written[-1].st_ino, written[-1].st_mtime_ns))
                self.assertEqual((save.called, self.read("out.md")), (False, "# Doc\n"))
                self.assertEqual((r.outcome.url, r.outcome.deliverable), (out if dest == "local" else "", "# Doc\n"))

    def test_a_deliverable_from_another_file_is_saved_to_out(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        other = os.path.join(self.work, "tmp", "deliverable.md")

        def items():
            os.makedirs(os.path.dirname(other))
            with open(other, "w") as f:
                f.write("# Doc\n")
            yield from feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)])

        r, _, _ = self.start(items())
        self.assertEqual([(str(c.args[0]), c.args[1]) for c in save.call_args_list], [(out, "# Doc\n")])
        self.assertEqual((r.outcome.url, self.read("out.md"), self.read("tmp/deliverable.md")), (out, "# Doc\n", "# Doc\n"))

    def test_an_out_that_differs_from_the_deliverable_is_replaced(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        for written in ("", "# Do", "# Doc", "# Doc\n\n", "# Doc\nmore", "# Dac\n"):
            def items():
                with open(out, "w") as f:
                    f.write(written)
                yield from feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)])

            with self.subTest(written=written):
                save.reset_mock()
                self.start(items())
                self.assertEqual((save.call_count, self.read("out.md")), (1, "# Doc\n"))

    def test_a_stale_out_never_stands_for_the_deliverable(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        os.makedirs(self.work)
        with open(out, "w") as f:
            f.write("# Doc\n")
        seen = []
        launch = drive.Launch(["fake"], cwd=self.work)
        p = self.params()
        drive.start(launch, run(), p, client=claude(), sinks=[], begun=lambda: seen.append(os.path.lexists(out)),
                    popen=lambda argv, **kw: FakeProc(feed(p.channel, [outcome(DONE)])))
        self.assertEqual((seen, save.call_count, self.read("out.md")), ([False], 1, "# Doc\n"))

    def test_a_resume_keeps_the_draft_its_session_wrote_to_out(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        os.makedirs(self.work)
        with open(out, "w") as f:
            f.write("# Draft\n")
        seen = []
        launch = drive.Launch(["fake"], cwd=self.work)
        p = self.params(resume=True)
        drive.start(launch, run(), p, client=claude(), sinks=[], begun=lambda: seen.append(self.read("out.md")),
                    popen=lambda argv, **kw: FakeProc(feed(p.channel, [outcome({**DONE, "deliverable": "# Draft\n"})])))
        self.assertEqual((seen, save.call_count, self.read("out.md")), (["# Draft\n"], 0, "# Draft\n"))

    def test_a_fifo_at_out_is_replaced_not_waited_on(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")

        def items():
            os.mkfifo(out)
            yield from feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)])

        self.start(items())
        self.assertEqual((save.call_count, stat.S_ISREG(os.lstat(out).st_mode), self.read("out.md")), (1, True, "# Doc\n"))

    def test_a_symlink_at_out_is_replaced_even_when_its_target_holds_the_deliverable(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("# Doc\n")

        def items():
            os.symlink(target, out)
            yield from feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)])

        self.start(items())
        self.assertEqual((save.call_count, os.path.islink(out), self.read("out.md")), (1, False, "# Doc\n"))
        with open(target) as f:
            self.assertEqual(f.read(), "# Doc\n")

    def test_a_record_replaced_by_a_symlink_is_not_followed_and_the_run_still_ends(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def items():
            os.remove(os.path.join(self.work, "run.jsonl"))
            os.symlink(target, os.path.join(self.work, "run.jsonl"))
            yield said("x")

        err = io.StringIO()
        with redirect_stderr(err):
            r, _, _ = self.start(items())
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.assertEqual(r, drive.Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(len(re.findall(r"^drive\.py: .*run\.jsonl", err.getvalue(), re.M)), 2, err.getvalue())

    def test_a_symlink_at_the_record_stops_the_run_before_it_starts(self):
        os.makedirs(self.work)
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")
        os.symlink(target, os.path.join(self.work, "run.jsonl"))
        with self.assertRaises(OSError):
            self.start([outcome(DONE)])
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.assertFalse(hasattr(self, "proc"))

    def test_an_error_reading_stdout_stops_the_run(self):
        def items():
            yield said("x")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        self.assertTrue(self.proc.killed)

    def test_sinks_replace_the_defaults(self):
        seen = []
        r, _, log = self.start([progress("start", "one"), said("hi"), outcome(DONE)], sinks=[seen.append])
        self.assertEqual([(e.kind, e.name, e.text) for e in seen[:2]], [("progress", "start", "one"), ("text", "", "hi")])
        self.assertEqual((seen[2].kind, seen[2].outcome["url"]), ("outcome", os.path.join(self.work, "out.md")))
        self.assertEqual(log, "")
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def test_every_sink_gets_every_event(self):
        a, b = [], []
        self.start([said("hi"), outcome(DONE)], sinks=[a.append, b.append])
        self.assertEqual([e.kind for e in a], ["text", "missing", "outcome"])
        self.assertEqual(a, b)

    def recorded(self, launch, items=(), **params):
        """drive.start of `launch` over `items`; (result, the record's events when `begun` ran, its events after)."""
        p, seen = self.params(**params), []
        r = drive.start(launch, run(), p, client=claude(), sinks=[], begun=lambda: seen.append(self.events()),
                        popen=lambda argv, **kw: FakeProc(feed(p.channel, items)))
        return r, seen[0], self.events()

    @staticmethod
    def bare(event):
        return {k: v for k, v in event.items() if k != "ts"}

    def test_the_record_is_input_session_the_reports_result_and_end(self):
        launch = drive.Launch(["fake"], cwd=self.repo, transcript="/p/x.jsonl", resume="cd x && resume", project=True)
        r, before, after = self.recorded(launch, [progress("start", "go"), outcome(DONE)])
        self.assertEqual([e["kind"] for e in before], ["input", "session"])
        self.assertEqual([e["kind"] for e in after], ["input", "session", "progress", "outcome", "result", "end"])
        mine = after[:2] + after[4:]
        self.assertEqual([list(e) for e in mine], [["ts", "kind", "text"],
                                                   ["ts", "kind", "sid", "cwd", "project", "transcript", "resume"],
                                                   ["ts", "kind", "outcome"], ["ts", "kind", "sid", "rc"]])
        for e in mine:
            self.assertIsNotNone(datetime.fromisoformat(e["ts"]).utcoffset(), e["ts"])
        kept = {k: v for k, v in dataclasses.asdict(r.outcome).items() if k != "deliverable"}
        self.assertEqual([self.bare(e) for e in mine], [
            {"kind": "input", "text": "Research X."},
            {"kind": "session", "sid": SID, "cwd": self.repo, "project": True, "transcript": "/p/x.jsonl",
             "resume": "cd x && resume"},
            {"kind": "result", "outcome": kept}, {"kind": "end", "sid": SID, "rc": 0}])
        self.assertEqual((kept["url"], r.outcome.deliverable), (os.path.join(self.work, "out.md"), "# Doc\n"))
        self.assertEqual(sorted(os.listdir(self.work)), ["out.md", "run.jsonl"])
        self.assertEqual(drive.session(self.work, SID), after[1])

    def test_the_input_is_recorded_as_given_and_the_cwd_defaults_to_the_workdir(self):
        _, before, _ = self.recorded(drive.Launch(["fake"]), input="  Answers:\n- B\n")
        self.assertEqual((before[0]["text"], before[1]["cwd"]), ("  Answers:\n- B\n", self.work))

    def test_a_run_without_a_valid_outcome_records_its_error(self):
        for items, rc in (([said("bye")], 0), ([outcome(DONE)], 143), ([outcome({**DONE, "status": "maybe"})], 0)):
            with self.subTest(items=items, rc=rc):
                r, _, _ = self.start(items, rc=rc)
                result, end = self.events()[-2:]
                self.assertTrue(r.error)
                self.assertEqual((self.bare(result), self.bare(end)),
                                 ({"kind": "result", "error": r.error}, {"kind": "end", "sid": SID, "rc": rc}))

    def test_a_resume_appends_its_input_and_session(self):
        self.recorded(drive.Launch(["fake"], cwd=self.repo), [progress("start", "go"), outcome(self.NEEDS)])
        self.recorded(drive.Launch(["fake"], cwd=self.repo), [outcome(DONE)], resume=True, input="Use B.")
        events = self.events()
        self.assertEqual([e["kind"] for e in events], ["input", "session", "progress", "outcome", "result", "end",
                                                       "input", "session", "outcome", "result", "end"])
        self.assertEqual([e["text"] for e in events if e["kind"] == "input"], ["Research X.", "Use B."])
        self.assertEqual([e["outcome"]["status"] for e in events if e["kind"] == "result"], ["needs_input", "done"])

    def test_the_driver_events_are_no_reports(self):
        os.makedirs(self.work)
        tail = drive.Tail(os.path.join(self.work, "run.jsonl"))
        lines = [{"ts": "t", "kind": "input", "text": "x"},
                 {"ts": "t", "kind": "session", "sid": SID, "cwd": "/", "project": False},
                 {"ts": "t", "kind": "result", "outcome": DONE}, {"ts": "t", "kind": "end", "sid": SID, "rc": 0}]
        for line in lines:
            drive.append_line(os.path.join(self.work, "run.jsonl"), json.dumps(line) + "\n")
        self.assertEqual(list(tail()), [])

    def test_a_result_line_the_agent_appends_is_no_outcome(self):
        r, _, _ = self.start([{"kind": "result", "outcome": DONE}])
        self.assertEqual(r.error, "the agent run returned no outcome")

    def test_the_record_ends_when_the_run_raises(self):
        def items():
            yield said("x")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        result, end = self.events()[-2:]
        self.assertEqual(self.bare(result), {"kind": "result", "error": "stopped: KeyboardInterrupt"})
        self.assertEqual(self.bare(end), {"kind": "end", "sid": SID, "rc": 1})

    def test_the_closing_writes_never_mask_the_runs_exception(self):
        def items():
            os.remove(os.path.join(self.work, "run.jsonl"))
            os.mkdir(os.path.join(self.work, "run.jsonl"))
            yield said("x")
            raise KeyboardInterrupt
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        self.assertEqual(len(re.findall(r"^drive\.py: .*run\.jsonl", err.getvalue(), re.M)), 2, err.getvalue())

    FAILED = {**DONE, "status": "failed", "deliverable": ""}
    NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}

    def missing(self, *items, **params):
        seen, err = [], io.StringIO()
        with redirect_stderr(err):
            r, _, _ = self.start(list(items), sinks=[seen.append], **params)
        return r, [e.name for e in seen if e.kind == "missing"], err.getvalue(), [e.kind for e in seen]

    def test_done_or_failed_without_start_reports_it_missing(self):
        for o in (DONE, self.FAILED):
            r, missing, err, kinds = self.missing(progress("round", "one"), outcome(o))
            self.assertEqual((r.outcome.status, missing), (o["status"], ["start"]))
            self.assertEqual(err, "drive.py: missing progress mark: start\n")
            self.assertEqual(kinds, ["progress", "missing", "outcome"])

    def test_no_report_when_start_was_seen_needs_input_or_resumed(self):
        for items, params in (((progress("start", "go"), outcome(DONE)), {}), ((outcome(self.NEEDS),), {}),
                              ((outcome(DONE),), {"resume": True})):
            _, missing, err, _ = self.missing(*items, **params)
            self.assertEqual((missing, err), ([], ""), (items, params))

    def test_no_outcome_reports_nothing(self):
        _, missing, err, _ = self.missing(said("bye"))
        self.assertEqual((missing, err), ([], ""))


class Session(Base):
    """drive.session: a session's entry in <workdir>/run.jsonl."""
    def write(self, *lines):
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "run.jsonl"), "wb") as f:
            f.writelines((line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n" for line in lines)

    def test_the_latest_valid_session_event_of_the_sid_wins(self):
        first = {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": False}
        latest = {**first, "cwd": "/b", "project": True}
        self.write(first, latest, b"not json", b"[]", b"\xff\xfe", b'{"kind": "session"', {**first, "kind": "input"},
                   {**first, "sid": "other"}, {**first, "cwd": "rel"}, {**first, "cwd": "/x\x1b[2J"},
                   {**first, "cwd": 3}, {**first, "project": 1}, {k: v for k, v in first.items() if k != "project"})
        self.assertEqual(drive.session(self.work, SID), latest)

    def test_none_without_a_valid_session_event(self):
        self.assertIsNone(drive.session(self.work, SID))
        self.write(b"", {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": "yes"})
        self.assertIsNone(drive.session(self.work, SID))

    def test_a_symlink_or_fifo_record_is_not_read(self):
        os.makedirs(self.work)
        target = os.path.join(self.tmp.name, "planted.jsonl")
        with open(target, "w") as f:
            f.write(json.dumps({"kind": "session", "sid": SID, "cwd": "/", "project": True}) + "\n")
        path = os.path.join(self.work, "run.jsonl")
        os.symlink(target, path)
        self.assertIsNone(drive.session(self.work, SID))
        os.remove(path)
        os.mkfifo(path)
        self.assertIsNone(drive.session(self.work, SID))


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
                with open(os.path.join(self.work, "run.jsonl")) as f:
                    result, end = [json.loads(line) for line in f][-2:]
                self.assertEqual((result["error"], end["rc"]), ("failing: no host", 1))

    def test_an_exception_out_of_the_loop_ends_the_record_stopped(self):
        for error, rc, text in ((SystemExit(129), 129, "stopped: SystemExit: 129"),
                                (SystemExit("bye"), 1, "stopped: SystemExit: bye"),
                                (KeyboardInterrupt(), 1, "stopped: KeyboardInterrupt"),
                                (RuntimeError("one\ntwo"), 1, "stopped: RuntimeError: one two")):
            with self.subTest(error=error):
                with self.assertRaises(type(error)) as cm:
                    self.start(poll=error)
                self.assertIs(cm.exception, error)
                with open(os.path.join(self.work, "run.jsonl")) as f:
                    events = [json.loads(line) for line in f]
                self.assertEqual([e["kind"] for e in events[-4:]], ["input", "session", "result", "end"])
                self.assertEqual({k: events[-2][k] for k in ("kind", "error")}, {"kind": "result", "error": text})
                self.assertEqual({k: events[-1][k] for k in ("kind", "sid", "rc")},
                                 {"kind": "end", "sid": SID, "rc": rc})

    def test_headless_raises_a_failed_popen(self):
        popen = unittest.mock.Mock(side_effect=FileNotFoundError(2, "No such file or directory", "fake"))
        with self.assertRaises(FileNotFoundError):
            drive.start(drive.Launch(["fake"]), run(), self.params(), client=Recorder({}), sinks=[], popen=popen)


class TuiRunner(Base):
    NAME = f"r-{SID[:8]}"

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)

    def start(self, *steps, sinks=None, show=None, api=None, **params):
        """drive.start with the tui runner over FakeTui(steps), `api` replacing its functions; stderr in self.err."""
        p = self.params(**params)
        self.fake, self.err, seen = FakeTui(p.channel, steps), io.StringIO(), []
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work, interactive=["claude", "hi"])
        with self.fake.patch(**(api or {})), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(self.err):
            r = drive.start(launch, run(show=show), p, client=claude(), runner="tui",
                            sinks=[seen.append] if sinks is None else sinks)
        return r, self.fake.calls, [e.kind for e in seen], self.err.getvalue()

    def main(self, *steps, api=None, extra=(), **kw):
        fake, err = FakeTui(os.path.join(self.work, "run.jsonl"), steps), io.StringIO()
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, "--runner", "tui", *extra]
        with fake.patch(**(api or {})), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(err):
            return drive.main(argv, root=CORE, **kw), fake.calls, err.getvalue()

    def test_session_name_and_start_arguments(self):
        _, calls, _, _ = self.start([outcome(DONE)], show="echo {{session}}")
        (_, name, argv, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((name, argv, kw["cwd"]), (self.NAME, ["claude", "hi"], self.work))
        self.assertEqual(kw["template"], "echo {{session}}")
        self.assertEqual((kw["env"]["FAKE"], kw["env"]["PATH"]), ("1", os.environ["PATH"]))

    def test_pwd_is_the_runs_cwd(self):
        with unittest.mock.patch.dict(os.environ, {"PWD": "/stale"}):
            _, calls, _, _ = self.start([outcome(DONE)])
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((kw["cwd"], kw["env"]["PWD"]), (self.work, self.work))

    def test_tui_session_name(self):
        self.assertIs(drive.tui_session, compose.tui_session)
        self.assertEqual(drive.tui_session("r", SID), self.NAME)
        self.assertEqual(drive.tui_session("r", SID, prefix="engineer-TASK-1"), "engineer-TASK-1-11111111")
        self.assertEqual(drive.tui_session("r", SID, prefix=None), self.NAME)

    def test_tui_session_matches_the_prefix_and_sid_shape_only(self):
        for name, prefix in ((self.NAME, "r"), ("engineer-TASK-1-11111111", "engineer-TASK-1"), ("A_b-0-deadbeef", "A_b-0")):
            self.assertEqual(drive.TUI_SESSION.fullmatch(name)["prefix"], prefix)
        for name in ("r-t-xyz", "r-t-1111111", "r-t-111111111", "-11111111", "11111111", "r t-11111111", "r-t-11111111\n"):
            self.assertIsNone(drive.TUI_SESSION.fullmatch(name), name)

    def launched(self, layout=None, status_line=False, prefix=None, per_column=None, **kw):
        """The keyword arguments of the one tui_claude.start call of drive.start with the tui runner, and its name."""
        p = self.params(prefix=prefix)
        fake = FakeTui(p.channel, [[outcome(DONE)]])
        launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"], status_line=status_line,
                              per_column=per_column)
        with fake.patch(), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(io.StringIO()):
            drive.start(launch, run(), p, client=claude(), runner="tui", layout=layout, sinks=[], **kw)
        (_, name, _, kw), = [c for c in fake.calls if c[0] == "start"]
        return name, kw

    def test_layout_reaches_tui_start_and_defaults_to_the_automatic_stack(self):
        for layout, want in ((None, (None, None, None)), (drive.Layout(), (None, None, None)),
                             (drive.Layout("below", "s"), ("below", "s", None)),
                             (drive.Layout(opener="w0t0p0:AB-12"), (None, None, "w0t0p0:AB-12")),
                             (drive.Layout("right", None, "mine"), ("right", None, "mine"))):
            with self.subTest(layout=layout):
                _, kw = self.launched(layout)
                self.assertEqual((kw["split"], kw["split_from"], kw["opener"]), want)

    def test_the_launchs_status_line_reaches_tui_start(self):
        for status_line in (False, True):
            self.assertIs(self.launched(status_line=status_line)[1]["status_line"], status_line)

    def test_the_launchs_per_column_reaches_tui_start(self):
        for per_column in (None, 2):
            self.assertEqual(self.launched(per_column=per_column)[1]["per_column"], per_column)

    def test_prefix_names_the_session_and_events_reach_tui_start(self):
        name, kw = self.launched(prefix="engineer-TASK-1", events="/tmp/ev.log")
        self.assertEqual((name, kw["events"]), ("engineer-TASK-1-11111111", "/tmp/ev.log"))
        name, kw = self.launched()
        self.assertEqual((name, kw["events"]), (self.NAME, None))

    def test_a_kill_after_a_prefixed_start_names_the_prefixed_session(self):
        def sink(event):
            raise RuntimeError("sink")

        p = self.params(prefix="p-1")
        fake = FakeTui(p.channel, [[progress("round", "x")]])
        launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"])
        with fake.patch(), unittest.mock.patch.object(drive, "POLL", 0), self.assertRaises(RuntimeError):
            drive.start(launch, run(), p, client=claude(), runner="tui", sinks=[sink])
        self.assertEqual(fake.calls[-1], ("kill", "p-1-11111111"))

    def test_the_tui_session_has_the_name_its_command_gives_claude(self):
        for prefix in (None, "engineer-TASK-1"):
            with self.subTest(prefix=prefix):
                p, c = self.params(prefix=prefix), claude()
                launch, r = drive.plan(CORE, c, "researcher", "light-research", params=p, cwd=self.work)
                fake = FakeTui(p.channel, [[outcome(DONE)]])
                with fake.patch(), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(io.StringIO()):
                    drive.start(launch, r, p, client=c, runner="tui", sinks=[])
                (_, name, argv, _), = [call for call in fake.calls if call[0] == "start"]
                self.assertEqual(argv[argv.index("--name") + 1], name)

    def test_a_bad_layout_is_a_config_error_before_the_workdir_or_channel(self):
        cases = (("headless", {"layout": drive.Layout()}, "no layout"), ("tui", {"layout": drive.Layout("left")}, "split"),
                 ("tui", {"layout": drive.Layout(split_from="a b")}, "split_from"),
                 ("tui", {"layout": drive.Layout(split_from="")}, "split_from"),
                 ("tui", {"layout": drive.Layout(opener="a b")}, "opener"),
                 ("tui", {"layout": drive.Layout(opener="")}, "opener"),
                 ("tui", {"layout": drive.Layout(opener="a:b:c")}, "opener"),
                 ("headless", {"prefix": "p"}, "prefix"), ("headless", {"events": "/tmp/ev.log"}, "events"))
        for runner, kw, word in cases:
            with self.subTest(runner=runner, **kw):
                p = self.params(prefix=kw.pop("prefix", None))
                launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"])
                with self.assertRaisesRegex(drive.ConfigError, word):
                    drive.start(launch, run(), p, client=claude(), runner=runner, sinks=[], **kw)
                self.assertFalse(os.path.lexists(self.work))
                self.assertFalse(os.path.lexists(p.channel))

    def test_a_layout_with_no_split_and_a_good_opener_is_accepted(self):
        for layout in (drive.Layout(), drive.Layout(opener="mine"), drive.Layout(opener="w0t0p0:AB-12"),
                       drive.Layout("below", "s", "mine")):
            drive.check_layout("tui", layout)

    def test_main_split_and_split_from_build_the_layout(self):
        seen = []
        real = drive.start

        def start(*a, **kw):
            seen.append(kw["layout"])
            return real(*a, **kw)

        for flags, want in ((["--split", "below", "--split-from", "s"], drive.Layout("below", "s")),
                            (["--split-from", "s"], drive.Layout(None, "s")), (["--split", "right"], drive.Layout("right")),
                            ([], None)):
            with unittest.mock.patch.object(drive, "start", start):
                self.main([outcome(DONE)], extra=flags)
            self.assertEqual(seen.pop(), want)

    def test_main_prefix_and_events_reach_start_and_the_tui_session(self):
        seen = []
        real = drive.start

        def start(*a, **kw):
            seen.append((a[2].prefix, kw["events"]))
            return real(*a, **kw)

        for flags, want in (([], (None, None)), (["--prefix", "engineer-TASK-1", "--events", "/tmp/ev.log"],
                                                  ("engineer-TASK-1", "/tmp/ev.log"))):
            with unittest.mock.patch.object(drive, "start", start):
                code, calls, _ = self.main([outcome(DONE)], extra=flags)
            self.assertEqual((code, seen.pop()), (0, want))
        (_, name, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertRegex(name, r"engineer-TASK-1-[0-9a-f]{8}")
        self.assertEqual(kw["events"], "/tmp/ev.log")

    def test_main_without_events_the_tui_run_uses_the_manager_directorys(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = own("mgr")
        code, calls, _ = self.main([outcome(DONE)], proc=proc)
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        want = os.path.join(home, "managers", "mgr", "events")
        self.assertEqual((code, kw["events"], len(proc.calls)), (0, want, 1))
        self.assertEqual(stat.S_IMODE(os.stat(want).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(want)).st_mode), 0o700)

    def test_main_manager_beats_tmux_and_events_beats_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = own("mgr")
        _, calls, _ = self.main([outcome(DONE)], extra=["--manager", "other"], proc=proc)
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((kw["events"], proc.calls), (os.path.join(home, "managers", "other", "events"), []))
        shutil.rmtree(os.path.join(home, "managers"))
        _, calls, _ = self.main([outcome(DONE)], extra=["--manager", "other", "--events", "/tmp/ev.log"], proc=proc)
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((kw["events"], os.path.exists(os.path.join(home, "managers"))), ("/tmp/ev.log", False))

    def test_main_outside_tmux_without_manager_the_tui_run_has_no_events(self):
        home = hermetic.home(self)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, calls, _ = self.main([outcome(DONE)], proc=proc)
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((code, kw["events"], os.listdir(home)), (0, None, []))

    def test_main_a_bad_manager_exits_2_before_anything_starts(self):
        home = hermetic.home(self)
        os.symlink(self.tmp.name, os.path.join(home, "managers"))
        os.environ.update(INSIDE)
        bad = f"drive.py: manager directory {home}/managers: not a directory owned by you\n"
        cases = ((["--manager", "a/b"], own(), "drive.py: manager 'a/b': want [A-Za-z0-9_-]+\n", ((), ("--dry-run",))),
                 ([], own("a b"), "drive.py: own tmux session 'a b': want [A-Za-z0-9_-]+; give --manager <name>\n",
                  ((), ("--dry-run",))),
                 (["--manager", "m1"], own(), bad, ((),)))   # a dry run creates nothing, so checks nothing
        for extra, proc, want, runs in cases:
            for dry in runs:
                with self.subTest(extra=extra, dry=dry):
                    code, calls, err = self.main(extra=[*extra, *dry], proc=proc)
                    self.assertEqual((code, calls, os.path.exists(self.work)), (2, [], False))
                    self.assertTrue(err.endswith(want), err)
        self.assertEqual(os.listdir(self.tmp.name), ["repo"])

    def test_main_manager_needs_the_tui_runner_or_detach_before_anything_resolves(self):
        os.environ.update(INSIDE)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        base = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work]
        cases = ([], ["--dry-run"], ["--runner", "headless"], ["--client", "skill"])
        for extra in cases:
            with self.subTest(extra=extra):
                err, out = io.StringIO(), io.StringIO()
                with redirect_stderr(err), redirect_stdout(out), unittest.mock.patch.object(drive, "start") as start:
                    self.assertEqual(drive.main([*base, *extra, "--manager", "m1"], root=CORE, proc=proc), 2)
                self.assertEqual((err.getvalue(), out.getvalue()), ("drive.py: --manager needs --runner tui or --detach\n", ""))
                start.assert_not_called()
                proc.assert_not_called()
                self.assertFalse(os.path.exists(self.work))

    def test_main_a_bad_prefix_or_headless_prefix_or_events_exits_2_before_anything_starts(self):
        base = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work]
        cases = ((["--runner", "tui", "--prefix", "a b"], "prefix"), (["--runner", "tui", "--prefix", ""], "prefix"),
                 (["--prefix", "p"], "prefix"), (["--events", "/tmp/ev.log"], "events"),
                 (["--runner", "headless", "--prefix", "p", "--dry-run"], "prefix"),
                 (["--events", "/tmp/ev.log", "--dry-run"], "events"))
        for extra, word in cases:
            with self.subTest(extra=extra):
                err = io.StringIO()
                with redirect_stderr(err), unittest.mock.patch.object(drive, "start") as start:
                    self.assertEqual(drive.main([*base, *extra], root=CORE), 2)
                self.assertIn(word, err.getvalue())
                start.assert_not_called()
                self.assertFalse(os.path.exists(self.work))

    def test_main_dry_run_with_a_prefix_exits_0_and_starts_nothing(self):
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, "--runner", "tui", "--prefix", "engineer-TASK-1", "--events", "/tmp/ev.log",
                "--dry-run"]
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), unittest.mock.patch.object(drive, "start") as start:
            self.assertEqual(drive.main(argv, root=CORE), 0)
        self.assertEqual(json.loads(out.getvalue())["argv"][0], "claude")
        start.assert_not_called()

    def test_main_layout_with_headless_exits_2(self):
        err = io.StringIO()
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, "--split", "below"]
        with redirect_stderr(err), unittest.mock.patch.object(drive, "start") as start:
            self.assertEqual(drive.main(argv, root=CORE), 2)
        self.assertIn("no layout", err.getvalue())
        start.assert_not_called()

    def test_done_on_the_first_outcome_without_waiting_for_a_stop_or_a_kill(self):
        r, calls, kinds, _ = self.start([progress("start", "x")], [outcome(DONE)])
        self.assertEqual((r.returncode, r.outcome.status, kinds), (0, "done", ["progress", "outcome"]))
        self.assertEqual([c[0] for c in calls], ["start"])
        self.assertEqual(self.fake.steps, [])

    def test_a_dead_pane_or_a_gone_session_ends_the_run_without_an_outcome(self):
        for state, result in ((3, (3, None, "the client exited 3")), (0, (0, None, "the agent run returned no outcome")),
                              (None, (0, None, "the agent run returned no outcome"))):
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

        kill = unittest.mock.Mock(side_effect=drive.tui_claude.TuiError("gone"))
        with self.assertRaises(RuntimeError):
            self.start([progress("round", "x")], sinks=[sink], api={"kill": kill})
        self.assertEqual(kill.call_args, unittest.mock.call(self.NAME))
        self.assertIn("drive.py: tui: gone\n", self.err.getvalue())

    def test_a_tui_error_on_start_is_the_result(self):
        r, calls, _, _ = self.start(api={"start": unittest.mock.Mock(side_effect=drive.tui_claude.TuiError("no tmux"))})
        self.assertEqual((r, calls), (drive.Result(1, None, "tui: no tmux"), []))

    def test_an_interrupted_start_kills_no_session_it_did_not_start(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(api={"start": unittest.mock.Mock(side_effect=KeyboardInterrupt)})
        self.assertEqual(self.fake.calls, [])

    def test_a_tui_error_reading_the_session_kills_it_and_is_the_result(self):
        status = unittest.mock.Mock(side_effect=drive.tui_claude.TuiError("no server"))
        r, calls, _, _ = self.start(api={"status": status})
        self.assertEqual((r, [c[0] for c in calls]), (drive.Result(1, None, "tui: no server"), ["start", "kill"]))
        code, calls, err = self.main(api={"status": status})
        self.assertEqual((code, [c[0] for c in calls]), (3, ["start", "kill"]))
        self.assertIn("drive.py: tui: no server\n", err)

    def test_a_missing_start_mark_is_reported(self):
        r, _, kinds, err = self.start([progress("round", "x"), outcome(DONE)])
        self.assertEqual(kinds, ["progress", "missing", "outcome"])
        self.assertIn("drive.py: missing progress mark: start\n", err)

    def test_the_first_stop_without_an_outcome_nudges_once(self):
        r, calls, kinds, _ = self.start([STOP], [STOP], [outcome(DONE), STOP])
        self.assertEqual([c for c in calls if c[0] == "send"], [("send", self.NAME, drive.NUDGE)])
        self.assertEqual((r.outcome.status, kinds), ("done", ["missing", "outcome"]))

    def test_it_gives_up_after_stop_limit_counted_stops_and_leaves_the_session(self):
        r, calls, kinds, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT))
        self.assertEqual(r, drive.Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(([c[0] for c in calls], kinds, self.fake.steps), (["start", "send"], [], []))
        self.assertIn(f"drive.py: no outcome after {drive.STOP_LIMIT} stops; session {self.NAME} left open: "
                      f"tmux attach -t '={self.NAME}'\n", err)
        code, calls, _ = self.main(*[[STOP]] * (1 + drive.STOP_LIMIT))
        self.assertEqual((code, [c[0] for c in calls]), (1, ["start", "send"]))

    def test_the_left_open_line_takes_the_attach_prefix(self):
        for env, prefix in (({}, ""), ({"TUI_ATTACH_PREFIX": " "}, ""),
                            ({"TUI_ATTACH_PREFIX": "docker exec -it box"}, "docker exec -it box ")):
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                *_, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT))
                self.assertIn(f"left open: {prefix}tmux attach -t '={self.NAME}'\n", err)

    def test_progress_resets_the_count(self):
        again = [[STOP]] * (drive.STOP_LIMIT - 1)
        r, calls, _, _ = self.start([STOP], *again, [progress("round", "x")], *again, [outcome(DONE)])
        self.assertEqual((r.outcome.status, [c[0] for c in calls]), ("done", ["start", "send"]))

    def test_pending_stops_are_ignored_however_many(self):
        r, calls, _, _ = self.start(*[[PENDING]] * (2 + drive.STOP_LIMIT), [outcome(DONE)])
        self.assertEqual((r.returncode, r.outcome.status, [c[0] for c in calls]), (0, "done", ["start"]))
        self.assertEqual(self.fake.steps, [])

    def test_pending_stops_between_others_do_not_count_or_nudge(self):
        steps = [[PENDING], [PENDING], [STOP], *[[PENDING], [STOP]] * drive.STOP_LIMIT]
        r, calls, _, err = self.start(*steps)
        self.assertEqual((r.error, [c[0] for c in calls], self.fake.steps),
                         ("the agent run returned no outcome", ["start", "send"], []))
        self.assertIn(f"drive.py: no outcome after {drive.STOP_LIMIT} stops; session {self.NAME} left open: "
                      f"tmux attach -t '={self.NAME}'\n", err)

    def test_it_gives_up_when_no_progress_comes_for_wait_limit_and_leaves_the_session(self):
        steps = [(drive.WAIT_LIMIT, []), (drive.WAIT_LIMIT + 1, [])]
        r, calls, kinds, err = self.start(*steps)
        self.assertEqual(r, drive.Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(([c[0] for c in calls], kinds, self.fake.steps), (["start"], [], []))
        self.assertIn(f"drive.py: no outcome 2 h after the last progress report; session {self.NAME} left open: "
                      f"tmux attach -t '={self.NAME}'\n", err)
        code, calls, _ = self.main(*steps)
        self.assertEqual((code, [c[0] for c in calls]), (1, ["start"]))

    def test_exactly_wait_limit_of_quiet_is_not_over_it(self):
        r, _, _, err = self.start((drive.WAIT_LIMIT, []), [outcome(DONE)])
        self.assertEqual((r.outcome.status, err), ("done", "drive.py: missing progress mark: start\n"))

    def test_progress_restarts_the_quiet_clock(self):
        p = drive.WAIT_LIMIT - 10
        r, _, _, _ = self.start((p, [progress("round", "x")]), (drive.WAIT_LIMIT + 10, []), (p + drive.WAIT_LIMIT, []),
                                (p + drive.WAIT_LIMIT + 1, []))
        self.assertEqual((r.error, self.fake.steps), ("the agent run returned no outcome", []))

    def test_pending_stops_do_not_restart_the_quiet_clock(self):
        r, _, _, _ = self.start((drive.WAIT_LIMIT - 10, [PENDING]), (drive.WAIT_LIMIT + 1, []))
        self.assertEqual((r.error, self.fake.steps), ("the agent run returned no outcome", []))

    def test_an_outcome_in_the_poll_the_quiet_limit_passes_wins(self):
        r, calls, _, err = self.start((drive.WAIT_LIMIT + 1, [outcome(DONE)]))
        self.assertEqual((r.returncode, r.outcome.status, [c[0] for c in calls], err),
                         (0, "done", ["start"], "drive.py: missing progress mark: start\n"))

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        send = unittest.mock.Mock(side_effect=drive.tui_claude.TuiError("no pane"))
        r, _, _, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT), api={"send": send})
        self.assertEqual((r.error, send.call_count), ("the agent run returned no outcome", 1))
        self.assertIn("drive.py: tui: no pane\n", err)

    def test_start_refuses_a_client_without_the_command_before_anything_starts(self):
        popen = unittest.mock.Mock()
        with unittest.mock.patch.object(drive.tui_claude, "start") as start, self.assertRaises(compose.ConfigError):
            drive.start(drive.Launch(["fake"]), run(), self.params(), client=Recorder({}), runner="tui", popen=popen)
        self.assertEqual((popen.called, start.called, os.path.exists(self.work)), (False, False, False))


class Detach(Base):
    DRIVER = f"dummy-tester-{SID[:8]}-drive"

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for k in ("TMUX", "TMUX_PANE", "TUI_ATTACH_PREFIX"):
            os.environ.pop(k, None)
        os.environ.update(ITERM_SESSION_ID="w0t0p0:AB-12", TERM_PROGRAM="iTerm.app")   # the opener, with no tmux call
        self.events = os.path.join(self.tmp.name, "w.events")
        for s in drive.SIGNALS:   # a driver's main sets them and leaves them blocked
            self.addCleanup(signal.signal, s, signal.getsignal(s))
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, signal.pthread_sigmask(signal.SIG_BLOCK, []))
        for s in drive.SIGNALS:   # first: one left pending is dropped as the mask is restored
            self.addCleanup(signal.signal, s, signal.SIG_IGN)

    def argv(self, *extra):
        return ["--role", "dummy-tester", "--input", "Hello.",
                "--out", os.path.join(self.work, "out.md"), "--workdir", self.work, "--sid", SID, *extra]

    def outer(self, *extra, rc=0, proc=None, events=True):
        """drive.main --detach (--events self.events unless not `events`), tmux through `proc`, else a fake that takes
        the handover file and names the caller's session mgr; (exit code, stderr, the tmux calls, the handover)."""
        calls, handover = [], {}

        def fake(argv, **kw):
            calls.append(argv)
            if argv[1] == "display-message":
                return subprocess.CompletedProcess(argv, 0, "mgr\n", "")
            if argv[1] == "new-session" and rc == 0:
                with open(argv[-1]) as f:
                    handover.update(json.load(f))
                os.unlink(argv[-1])
            return subprocess.CompletedProcess(argv, rc, "", "duplicate session: x\n" if rc else "")

        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = drive.main(self.argv("--detach", *(("--events", self.events) if events else ()), *extra), root=CORE,
                              proc=proc or fake)
        return code, err.getvalue(), calls, handover

    def inner(self, *steps, argv=None, api=None, **kw):
        """drive.main as the driver of a tui run over FakeTui(steps); (exit code, its calls)."""
        fake = FakeTui(os.path.join(self.work, "run.jsonl"), steps)
        argv = argv or self.argv("--runner", "tui", "--events", self.events, "--driver", "d-drive")
        with fake.patch(**(api or {})), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(io.StringIO()):
            return drive.main(argv, root=CORE, **kw), fake.calls

    def lines(self):
        with open(self.events) as f:
            lines = f.readlines()
        for line in lines:
            self.assertRegex(line, r"^\d\d:\d\d:\d\d ")
        return [line[9:] for line in lines]

    def test_the_driver_session_never_matches_a_tui_session_or_the_reserved_name(self):
        reserved = re.compile(r"[a-z][a-z0-9-]*-[A-Z][A-Z0-9]*-\d+-[0-9a-f]{8}")   # <role>-<ID>-<8 hex>
        self.assertTrue(reserved.fullmatch(drive.tui_session("r", SID, "engineer-TASK-1")))
        for prefix, want in ((None, f"r-{SID[:8]}-drive"), ("engineer-TASK-1", f"engineer-TASK-1-{SID[:8]}-drive")):
            name = drive.driver_session("r", SID, prefix)
            self.assertEqual(name, want)
            self.assertTrue(drive.tui_claude.NAME.fullmatch(name))
            self.assertIsNone(drive.TUI_SESSION.fullmatch(name))
            self.assertIsNone(reserved.fullmatch(name))

    def test_detach_needs_an_events_file_and_a_client_that_runs_before_anything_starts(self):
        home = hermetic.home(self)
        skill, no = ["--client", "skill", "--role", "dummy-tester"], "SkillClient prints a prompt; it takes no --detach"
        need = "--detach needs --events or --manager"
        cases = ((self.argv("--detach"), need), (self.argv("--detach", "--dry-run"), need),
                 (self.argv("--detach", "--runner", "tui"), need),
                 (self.argv("--detach", "--runner", "tui", "--dry-run"), need),
                 ([*skill, "--detach", "--events", self.events], no),
                 ([*skill, "--driver", "d", "--events", self.events], no))
        for argv, want in cases:
            with self.subTest(argv=argv):
                err, out, proc = io.StringIO(), io.StringIO(), unittest.mock.Mock()
                with redirect_stderr(err), redirect_stdout(out):
                    self.assertEqual(drive.main(argv, root=CORE, proc=proc), 2)
                self.assertTrue(err.getvalue().endswith(f"drive.py: {want}\n"), err.getvalue())
                self.assertEqual(out.getvalue(), "")
                proc.assert_not_called()
                self.assertFalse(os.path.exists(self.events))
                self.assertFalse(os.path.exists(self.work))
                self.assertEqual(os.listdir(home), [])

    def test_dry_run_prints_the_plan_and_the_driver_and_starts_nothing(self):
        for runner in ("tui", "headless"):   # with --detach, headless takes --events
            with self.subTest(runner=runner):
                out, proc = io.StringIO(), unittest.mock.Mock()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    code = drive.main(self.argv("--runner", runner, "--detach", "--events", self.events, "--dry-run"),
                                      root=CORE, proc=proc)
                data = json.loads(out.getvalue())
                self.assertEqual((code, data["argv"][0], data["driver"]), (0, "claude", self.DRIVER))
                proc.assert_not_called()
                self.assertFalse(os.path.exists(self.events))
                self.assertFalse(os.path.exists(self.work))

    def test_detach_starts_this_command_as_the_driver_in_its_session_and_exits(self):
        driver, tui = f"p-{SID[:8]}-drive", f"p-{SID[:8]}"
        code, err, (argv,), handover = self.outer("--runner", "tui", "--prefix", "p")
        self.assertEqual(code, 0)
        self.assertEqual(argv, ["tmux", "new-session", "-d", "-e", "ITERM_SESSION_ID=w0t0p0:AB-12", "-s", driver,
                                sys.executable, "-I", "-c", drive.tui_claude.EXEC, argv[-1]])
        self.assertFalse(os.path.exists(os.path.dirname(argv[-1])))
        self.assertEqual(handover["argv"], [
            sys.executable, os.path.abspath(drive.__file__), "--role=dummy-tester", "--input=Hello.",
            f"--out={self.work}/out.md", f"--workdir={self.work}", f"--sid={SID}", "--runner=tui", "--prefix=p",
            f"--events={self.events}", "--opener=w0t0p0:AB-12", f"--driver={driver}"])
        self.assertEqual(handover["cwd"], os.getcwd())
        self.assertEqual(handover["block"], [signal.SIGHUP, signal.SIGTERM, signal.SIGINT])
        self.assertEqual(handover["env"]["PATH"], os.environ["PATH"])
        self.assertNotIn("ITERM_SESSION_ID", handover["env"])   # the pane's, from -e
        self.assertEqual(err, f"drive.py: cwd {os.getcwd()} is not in trusted_dirs: its project settings are off\n"
                              f"drive.py: session {SID}\n"
                              f"drive.py: driver session {driver}: tmux attach -t '={driver}'\n"
                              f"drive.py: tui session {tui}: tmux attach -t '={tui}'\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.work))
        code, calls = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        (_, name, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((code, name, kw["opener"], kw["events"]), (0, tui, "w0t0p0:AB-12", self.events))
        self.assertEqual(self.lines(), [f"{driver} outcome done\n"])

    def test_without_events_the_driver_gets_the_manager_directorys_not_a_manager(self):
        home = hermetic.home(self)
        want = os.path.join(home, "managers", "mgr", "events")
        os.environ.update(INSIDE)
        for runner in ("tui", "headless"):
            with self.subTest(runner=runner):
                code, _, _, handover = self.outer("--runner", runner, "--manager", "mgr", events=False)
                self.assertEqual(code, 0)
                self.assertEqual([a for a in handover["argv"] if a.startswith(("--events", "--manager"))],
                                 [f"--events={want}"])
                self.assertEqual(stat.S_IMODE(os.stat(want).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(want)).st_mode), 0o700)
                os.unlink(want)
        code, _, _, handover = self.outer("--runner", "tui", events=False)   # the caller's tmux session: mgr
        self.assertEqual((code, f"--events={want}" in handover["argv"]), (0, True))
        code, _ = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        self.assertEqual(code, 0)
        with open(want) as f:
            self.assertEqual([line[9:] for line in f], [f"{self.DRIVER} outcome done\n"])

    def test_manager_beats_the_callers_tmux_session_and_events_beats_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        code, _, calls, handover = self.outer("--runner", "headless", "--manager", "other", events=False)
        self.assertEqual((code, [c[1] for c in calls]), (0, ["new-session"]))   # no display-message
        self.assertIn(f"--events={home}/managers/other/events", handover["argv"])
        self.assertTrue(os.path.isfile(os.path.join(home, "managers", "other", "events")))
        self.assertFalse(os.path.exists(os.path.join(home, "managers", "mgr")))
        shutil.rmtree(os.path.join(home, "managers"))
        code, _, _, handover = self.outer("--runner", "headless", "--manager", "other")
        self.assertEqual(code, 0)
        self.assertEqual([a for a in handover["argv"] if a.startswith("--events")], [f"--events={self.events}"])
        self.assertFalse(os.path.exists(os.path.join(home, "managers")))

    def test_a_driver_never_resolves_a_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, calls = self.inner([outcome(DONE)], argv=self.argv("--runner", "tui", "--driver", "d-drive"), proc=proc)
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((code, kw["events"], os.listdir(home)), (0, None, []))

    def test_stdin_input_reaches_the_driver_as_text(self):
        with unittest.mock.patch.object(sys, "stdin", io.StringIO("# Echo\nthis")):
            code, _, _, handover = self.outer("--input", "-")
        self.assertEqual((code, handover["argv"][3]), (0, "--input=# Echo\nthis"))

    def test_stdin_text_dash_reaches_the_driver_as_text_not_its_stdin(self):
        with unittest.mock.patch.object(sys, "stdin", io.StringIO("-")):
            code, _, _, handover = self.outer("--runner", "tui", "--input", "-")
        self.assertEqual((code, handover["argv"][3]), (0, "--input=-"))
        stdin = unittest.mock.Mock()
        with unittest.mock.patch.object(sys, "stdin", stdin):
            code, calls = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        stdin.read.assert_not_called()
        (_, _, argv, _), = [c for c in calls if c[0] == "start"]
        self.assertTrue(argv[1].endswith("\n# Input\n\n-\n"), argv[1][-40:])
        self.assertEqual((code, self.lines()), (0, [f"{self.DRIVER} outcome done\n"]))

    def test_without_an_opener_the_driver_gets_no_iterm_pane_and_headless_no_tui_line(self):
        os.environ["TERM_PROGRAM"] = "vscode"   # $ITERM_SESSION_ID left over from another terminal
        for runner in ("tui", "headless"):
            with self.subTest(runner=runner):
                code, err, (argv,), handover = self.outer("--runner", runner)
                self.assertEqual((code, argv[4]), (0, "ITERM_SESSION_ID="))
                self.assertEqual([a for a in handover["argv"] if a.startswith(("--opener", "--events"))],
                                 [f"--events={self.events}"])
                self.assertEqual("tui session" in err, runner == "tui")

    def test_a_split_from_no_terminal_shows_exits_2_before_anything_starts(self):
        for rc, out, want in ((0, "", "no terminal shows tmux session s"), (1, "", "tmux: can't find session: s")):
            with self.subTest(want=want):
                calls = []

                def proc(argv, **kw):
                    calls.append(argv[1])
                    return subprocess.CompletedProcess(argv, rc, out, "can't find session: s\n" if rc else "")

                code, err, _, _ = self.outer("--runner", "tui", "--split-from", "s", proc=proc)
                self.assertEqual((code, calls), (2, ["list-clients"]))
                self.assertTrue(err.endswith(f"drive.py: {want}\n"), err)
                self.assertFalse(os.path.exists(self.events))

    def test_a_failed_tmux_start_exits_3(self):
        code, err, calls, _ = self.outer("--runner", "tui", rc=1)
        self.assertEqual((code, [c[1] for c in calls]), (3, ["new-session"]))
        self.assertTrue(err.endswith("drive.py: tmux: duplicate session: x\n"), err)
        self.assertFalse(os.path.exists(os.path.dirname(calls[0][-1])))

    def test_a_handover_nobody_takes_kills_the_session(self):
        calls = []

        def proc(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(drive.RunnerError, "session d did not start"):
            drive.detach("d", [sys.executable], cwd=self.tmp.name, env={}, iterm="", proc=proc, sleep=lambda s: None)
        self.assertEqual([c[1:] for c in calls[1:]], [["kill-session", "-t", "=d"]])
        self.assertFalse(os.path.exists(os.path.dirname(calls[0][-1])))

    def test_an_argv_item_holding_nul_raises_before_the_handover_or_tmux(self):
        proc = unittest.mock.Mock()
        with unittest.mock.patch.object(drive.tempfile, "mkdtemp") as mkdtemp, \
                self.assertRaisesRegex(drive.RunnerError, "an argv item holds a NUL character"):
            drive.detach("d", [sys.executable, "--input=a\0b"], cwd=self.tmp.name, env={}, iterm="", proc=proc)
        proc.assert_not_called()
        mkdtemp.assert_not_called()

    def test_an_argv_item_with_a_lone_surrogate_raises_before_the_handover_or_tmux(self):
        proc = unittest.mock.Mock()
        with unittest.mock.patch.object(drive.tempfile, "mkdtemp") as mkdtemp, \
                self.assertRaisesRegex(drive.RunnerError, "an argv item cannot be encoded"):
            drive.detach("d", [sys.executable, "--input=a\ud800b"], cwd=self.tmp.name, env={}, iterm="", proc=proc)
        proc.assert_not_called()
        mkdtemp.assert_not_called()

    def test_the_driver_appends_one_outcome_line_when_it_ends(self):
        needs = {"status": "needs_input", "title": "T", "summary": "S", "questions": ["Which?"]}
        cases = (([outcome(DONE)], 0, "done"), ([outcome(needs)], 0, "needs_input"),
                 ([outcome({"status": "failed", "title": "T", "summary": "S"})], 0, "failed"),
                 ([outcome({"status": "done"})], 1, "error"), (0, 1, "error"), (3, 3, "error"))
        for step, code, word in cases:
            with self.subTest(word=word, step=step):
                self.assertEqual(self.inner(step)[0], code)
                self.assertEqual(self.lines(), [f"d-drive outcome {word}\n"])
                os.unlink(self.events)

    def test_a_driver_that_cannot_start_its_run_appends_error(self):
        code, calls = self.inner(argv=self.argv("--task", "essay", "--events", self.events, "--driver", "d-drive"))
        self.assertEqual((code, calls), (2, []))
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])

    def test_a_headless_driver_appends_only_its_outcome(self):
        def popen(argv, **kw):
            return FakeProc(feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)]))

        with redirect_stderr(io.StringIO()):
            code = drive.main(self.argv("--events", self.events, "--driver", "d-drive"), root=CORE, popen=popen)
        self.assertEqual((code, self.lines()), (0, ["d-drive outcome done\n"]))

    def test_a_signal_ends_the_driver_with_one_error_line_and_kills_its_tui_session(self):
        killed = []

        def status(name):
            self.assertTrue(callable(signal.getsignal(signal.SIGHUP)), "no handler: SIGHUP would end the tests")
            os.kill(os.getpid(), signal.SIGHUP)   # tmux kill-session
            return drive.tui_claude.RUNNING

        def kill(name):
            killed.append(name)
            os.kill(os.getpid(), signal.SIGTERM)   # another, while it stops: blocked

        with self.assertRaises(SystemExit) as cm:
            self.inner(api={"status": status, "kill": kill})
        self.assertEqual((cm.exception.code, killed), (129, [f"dummy-tester-{SID[:8]}"]))
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])

    def test_a_signal_sent_before_the_driver_handles_them_waits_and_ends_it_with_one_error_line(self):
        signal.pthread_sigmask(signal.SIG_BLOCK, drive.SIGNALS)   # as detach hands the driver over
        os.kill(os.getpid(), signal.SIGHUP)   # tmux kill-session as it starts
        with self.assertRaises(SystemExit) as cm:
            self.inner([outcome(DONE)])
        self.assertEqual(cm.exception.code, 129)
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])
        self.assertFalse(os.path.exists(self.work))

    def test_a_signal_as_the_driver_ends_still_leaves_its_outcome_line(self):
        sigmask, sent = signal.pthread_sigmask, []

        def block_after_a_signal(how, signals):
            if how == signal.SIG_BLOCK and not sent:
                sent.append(how)
                os.kill(os.getpid(), signal.SIGTERM)   # before the driver's end blocks them
            return sigmask(how, signals)

        with unittest.mock.patch.object(signal, "pthread_sigmask", block_after_a_signal), \
                self.assertRaises(SystemExit) as cm:
            self.inner([outcome(DONE)])
        self.assertEqual((cm.exception.code, sent), (143, [signal.SIG_BLOCK]))
        self.assertEqual(self.lines(), ["d-drive outcome done\n"])


class Sinks(unittest.TestCase):
    def test_terminal_strips_control_characters_and_skips_the_outcome(self):
        log = io.StringIO()
        sink = drive.terminal(log)
        sink(clients.Event("text", "a\x1b]52;c;ZXZpbA==\x07b\tc"))
        sink(clients.Event("progress", "x\x1b[2J", name="round"))
        sink(clients.Event("stderr", "w\x1b[2Jarn"))
        sink(clients.Event("outcome", outcome=DONE))
        sink(clients.Event("missing", name="start"))
        self.assertEqual(log.getvalue(), "a]52;c;ZXZpbA==b\tc\nProgress (round): x[2J\nw[2Jarn\n")


class Stamp(unittest.TestCase):
    def test_is_local_iso_8601_with_offset_in_seconds(self):
        text = drive.stamp()
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertIsNotNone(datetime.fromisoformat(text).utcoffset())


class AppendLine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "f.jsonl")

    def content(self):
        with open(self.path, "rb") as f:
            return f.read()

    def test_a_megabyte_line_is_one_os_write(self):
        line = "x" * 1_000_000 + "\n"
        with unittest.mock.patch.object(os, "write", wraps=os.write) as write:
            drive.append_line(self.path, line)
        self.assertEqual((write.call_count, self.content()), (1, line.encode()))

    def test_a_short_write_continues_with_the_rest(self):
        real = os.write
        with unittest.mock.patch.object(os, "write", side_effect=lambda fd, data: real(fd, bytes(data[:3]))) as write:
            drive.append_line(self.path, "héllo wörld\n")
        self.assertEqual((write.call_count, self.content()), (5, "héllo wörld\n".encode()))

    def test_appends_after_what_is_there_and_an_empty_text_creates_the_file(self):
        drive.append_line(self.path, "")
        drive.append_line(self.path, "one\n")
        drive.append_line(self.path, "two\n")
        self.assertEqual(self.content(), b"one\ntwo\n")

    def test_created_owner_only(self):
        drive.append_line(self.path, "x\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_a_symlink_is_refused(self):
        target = os.path.join(self.dir, "target")
        with open(target, "w") as f:
            f.write("keep\n")
        os.symlink(target, self.path)
        with self.assertRaises(OSError):
            drive.append_line(self.path, "x\n")
        with open(target) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_a_fifo_is_refused_even_with_a_reader(self):
        os.mkfifo(self.path)
        reader = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        with self.assertRaises(OSError) as e:
            drive.append_line(self.path, "x\n")
        self.assertEqual(e.exception.errno, errno.EINVAL)

    def test_a_lone_surrogate_is_written_as_a_replacement_character(self):
        drive.append_line(self.path, json.dumps({"text": "a\ud800b"}, ensure_ascii=False) + "\n")
        self.assertEqual(self.content(), b'{"text": "a?b"}\n')
        self.assertEqual(json.loads(self.content()), {"text": "a?b"})


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
    def run_main(self, *extra, lines=(), rc=0, **kw):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return FakeProc(feed(os.path.join(self.work, "run.jsonl"), lines), rc)

        out, err = io.StringIO(), io.StringIO()
        argv = ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.", "--out", os.path.join(self.work, "out.md"),
                "--workdir", self.work, *extra]
        with redirect_stdout(out), redirect_stderr(err):
            code = drive.main(argv, root=CORE, popen=popen, **kw)
        return code, out.getvalue(), err.getvalue(), calls

    def test_dry_run_prints_plan_and_runs_nothing(self):
        code, out, err, calls = self.run_main("--dry-run")
        data = json.loads(out)
        self.assertEqual((code, calls), (0, []))
        self.assertEqual((data["argv"][0], data["cwd"]), ("claude", os.getcwd()))
        self.assertIn(f"drive.py: cwd {os.getcwd()} is not in trusted_dirs: its project settings are off\n", err)
        self.assertFalse(os.path.exists(self.work))

    def test_dry_run_prints_the_runners_command(self):
        launch = self.plan("dummy-tester", "echo", client="claude", input="Hello.", cwd=os.getcwd())
        code, out, _, _ = self.run_main("--dry-run", "--sid", SID, "--runner", "tui")
        self.assertEqual((code, json.loads(out)["argv"]), (0, launch.interactive))

    def test_dry_run_tui_argv_names_the_claude_session_after_the_tui_session(self):
        code, out, _, _ = self.run_main("--dry-run", "--sid", SID, "--runner", "tui", "--prefix", "p")
        argv = json.loads(out)["argv"]
        self.assertEqual((code, argv[argv.index("--name") + 1]), (0, f"p-{SID[:8]}"))

    def test_dry_run_shows_the_events_file_given_or_the_manager_directorys_and_creates_nothing(self):
        home = hermetic.home(self)
        want = os.path.join(home, "managers", "mgr", "events")
        os.environ.update(INSIDE)
        proc = own("mgr")
        cases = (("--runner", "tui"), ("--detach",), ("--runner", "tui", "--detach"), ("--detach", "--manager", "mgr"))
        for extra in cases:
            with self.subTest(extra=extra):
                code, out, _, _ = self.run_main("--dry-run", *extra, proc=proc)
                self.assertEqual((code, json.loads(out)["events"]), (0, want))
                self.assertEqual(os.listdir(home), [])
        code, out, _, _ = self.run_main("--dry-run", "--runner", "tui", "--manager", "other", proc=proc)
        self.assertEqual((code, json.loads(out)["events"]), (0, os.path.join(home, "managers", "other", "events")))
        for extra in (("--runner", "tui"), ("--detach",)):
            code, out, _, _ = self.run_main("--dry-run", *extra, "--events", "/tmp/ev.log", "--manager", "other", proc=proc)
            self.assertEqual((code, json.loads(out)["events"]), (0, "/tmp/ev.log"))
        self.assertEqual(os.listdir(home), [])

    def test_dry_run_shows_no_events_when_there_is_none(self):
        home = hermetic.home(self)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, out, _, _ = self.run_main("--dry-run", "--runner", "tui", proc=proc)   # outside tmux, no --manager
        self.assertEqual((code, "events" in json.loads(out)), (0, False))
        os.environ.update(INSIDE)
        code, out, _, _ = self.run_main("--dry-run", proc=proc)   # headless: no events, no resolution
        self.assertEqual((code, "events" in json.loads(out), os.listdir(home)), (0, False, []))

    def test_a_headless_run_never_resolves_a_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, _, _, calls = self.run_main(lines=[outcome(DONE)], proc=proc)
        self.assertEqual((code, len(calls), os.listdir(home)), (0, 1, []))

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
        self.assertEqual((argv[0], kw["cwd"]), ("claude", os.getcwd()))
        self.assertEqual(kw["env"]["PATH"], os.environ["PATH"])

    def test_a_run_by_hand_leaves_its_record(self):
        code, _, _, _ = self.run_main("--sid", SID, lines=[outcome(DONE)])
        entry = drive.session(self.work, SID)
        self.assertEqual((code, entry["cwd"], entry["project"]), (0, os.getcwd(), False))
        self.assertEqual(entry["resume"], f"cd {os.getcwd()} && claude --resume {SID} --add-dir {self.work}")
        self.assertEqual(entry["transcript"], clients.claude.transcript(os.getcwd(), SID))

    def test_input_is_text_from_the_argument_or_stdin_never_a_files_content(self):
        here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, here)
        with open("input.md", "w") as f:
            f.write("SECRET")
        for argv, stdin, want in ((["--input", "input.md"], "", "input.md"),
                                  (["--input", "-"], "Use B.\n", "Use B.\n")):
            with self.subTest(argv=argv), unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
                code, _, _, ((cmd, _),) = self.run_main(*argv, lines=[outcome(DONE)])
            with open(os.path.join(self.work, "run.jsonl")) as f:
                inputs = [e["text"] for e in map(json.loads, f) if e["kind"] == "input"]
            self.assertEqual((code, inputs[-1]), (0, want))
            self.assertTrue(cmd[2].endswith(f"\n# Input\n\n{want.strip()}\n"), cmd[2][-100:])
            self.assertNotIn("SECRET", cmd[2])

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

    def test_a_bad_workers_per_column_exits_2_before_anything_starts(self):
        with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
            f.write("workers_per_column = 0\n")
        for extra in ((), ("--dry-run",)):
            code, out, err, calls = self.run_main(*extra)
            self.assertEqual((code, out, calls), (2, "", []))
            self.assertIn("drive.py: workers_per_column: want an integer from 1 to 9999", err)

    def test_resume_end_to_end(self):
        code, _, _, ((argv, _),) = self.run_main("--sid", SID, "--resume", lines=[outcome(DONE)])
        self.assertEqual((code, argv[3:5]), (0, ["--resume", SID]))
        self.assertTrue(argv[2].startswith("Resumed agent run"))
        self.assertEqual(self.run_main("--resume")[0], 2)

    def test_a_run_client_needs_input_out_and_workdir(self):
        for argv in (["--out", "o.md"], ["--input", "x", "--workdir", self.work]):
            err = io.StringIO()
            with redirect_stderr(err):
                code = drive.main(["--role", "dummy-tester", *argv], root=CORE)
            self.assertEqual(code, 2, argv)
            self.assertIn("client 'claude' needs --input, --out and --workdir", err.getvalue())


if __name__ == "__main__":
    unittest.main()
