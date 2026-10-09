import dataclasses
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


def run(**kw):
    return compose.RunConfig(**{"role": "r", "task": "t", "tier": 2, "effort": "high", "output": {"type": "local"}, **kw})


class Recorder(clients.Client):
    """A fake client that needs no config file and records what the driver hands it."""
    needs_config = False
    seen = []

    def launch(self, prompt, run, *, params, access):
        Recorder.seen.append(dict(prompt=prompt, run=run, params=params, access=access))
        return clients.Launch(["fake", params.sid], {"FAKE": "1"}, cwd=access.cwd)


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

    def plan(self, role="researcher", task="light-research", client="fake", repo=None, layers=(), cwd=None, root=CORE,
             **params):
        return drive.plan(root, clients.get(client, root), role, task, params=self.params(**params), repo=repo,
                          layers=layers, cwd=cwd or self.work)[0]

    def report(self):
        return f"python3 {CORE}/src/report.py --to {self.work}/.report.jsonl"


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

    def test_light_research_argv(self):
        launch = self.plan(client="claude", repo=self.repo)
        self.assertEqual(launch.argv[:2], ["claude", "-p"])
        self.assertTrue(launch.argv[2].startswith("# Guide"))
        self.assertEqual(launch.argv[3:], [
            "--session-id", SID, "--model", "opus", "--effort", "high",
            "--permission-mode", "auto", "--strict-mcp-config", "--setting-sources", "user",
            "--output-format", "stream-json", "--verbose", "--settings", '{"enabledPlugins": {"agent-pm@agent-pm": false}}',
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py worktree --dir {self.work}/src *)",
            f"Bash({self.report()} *)"])
        self.assertIn(f"## Return\n\nReport through `{self.report()}`", launch.argv[2])
        self.assertIn(f"`{self.report()} progress start <what that line asks you to report>`", launch.argv[2])
        self.assertIn(f"`{self.report()} outcome --status <done|needs_input|failed> --title", launch.argv[2])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_build_xhigh_and_repo_commands(self):
        argv = self.plan("engineer", "build", client="claude").argv
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools"] + [
            f"Bash(python3 {CORE}/src/repo.py {cmd} --dir {self.work}/src *)" for cmd in ("worktree", "status")]
            + [f"Bash({self.report()} *)"])

    def test_product_design_argv_has_the_worktree_rule(self):
        argv = self.plan("pm", "product-design", client="claude").argv
        self.assertEqual(argv[argv.index("--allowedTools"):], [
            "--allowedTools", f"Bash(python3 {CORE}/src/repo.py worktree --dir {self.work}/src *)",
            f"Bash({self.report()} *)"])
        self.assertIn(f"`python3 {CORE}/src/repo.py worktree --dir <Workdir>/src --branch <branch> [--name <checkout>] <repo>`", argv[2])

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
            for prefix, name in ((None, f"researcher-light-research-{SID[:8]}"), ("p-1", f"p-1-{SID[:8]}")):
                with self.subTest(resume=resume, prefix=prefix):
                    launch = self.plan(client="claude", resume=resume, prefix=prefix)
                    i = launch.interactive.index("--name")
                    self.assertEqual((launch.interactive.count("--name"), launch.interactive[i + 1]), (1, name))
                    self.assertNotIn("--name", launch.argv)

    def test_interactive_adds_the_stop_hook_to_the_settings(self):
        launch = self.plan(client="claude")
        i = launch.interactive.index("--settings")
        cmd = f"python3 {CORE}/src/report.py --to {self.work}/.report.jsonl stop --pending background_tasks"
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

    def test_setting_sources_in_flags_is_refused(self):
        for flag in ("--setting-sources", "--setting-sources=user,project"):
            with self.subTest(flag), self.assertRaises(compose.ConfigError) as cm:
                claude(flags=["--verbose", flag, "user"])
            self.assertIn("--setting-sources is the driver's", str(cm.exception))

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
        self.assertEqual(seen["access"], drive.Access(dirs=[], commands=[
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
                           methods="/m")
        report = f"python3 /s/report.py --to {self.work}/.report.jsonl *"
        self.assertEqual(acc.commands, ["/s/x *", report, gate])
        acc = drive.access(run(commands=["x"]), self.params(), repo=None, scripts="/s", methods="/m")
        self.assertEqual(acc.commands, ["x", report])

    def test_repo_entry_binds_to_the_repo_arg(self):
        acc = drive.access(run(write=["repo"], commands=["{{scripts}}/x --dir {{workdir}}/src *"]), self.params(),
                           repo=self.repo, scripts="/s", methods="/m")
        self.assertEqual(acc, drive.Access(dirs=[self.repo], commands=[
            f"/s/x --dir {self.work}/src *", f"python3 /s/report.py --to {self.work}/.report.jsonl *"], cwd=self.work))

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
        self.plan("engineer", "build")
        self.assertEqual(Recorder.seen[0]["access"].dirs, [])

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
        self.assertEqual(self.flag(launch.argv, "--add-dir"), [self.work])
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
                    self.assertEqual(self.flag(argv, "--add-dir"), [self.work])
                    self.assertIn("--strict-mcp-config", argv)
                self.assertEqual((launch.cwd, launch.project, err), (cwd, True, ""))

    def test_a_trusted_cwd_without_mcp_json_gets_no_mcp_config(self):
        os.remove(os.path.join(self.trusted, ".mcp.json"))
        launch, _ = self.launch(self.trusted, cwd=self.trusted)
        self.assertEqual(self.flag(launch.argv, "--mcp-config"), [])

    def test_an_untrusted_cwd_gets_user_settings_and_a_notice(self):
        launch, err = self.launch(self.trusted, cwd=self.other)
        self.assertEqual((self.flag(launch.argv, "--setting-sources"), self.flag(launch.argv, "--mcp-config")), (["user"], []))
        self.assertEqual((launch.cwd, launch.project, self.flag(launch.argv, "--add-dir")), (self.other, False, [self.work]))
        self.assertEqual(err, f"drive.py: cwd {self.other} is not in trusted_dirs: its project settings are off\n")

    def test_the_workdir_as_cwd_has_no_notice_and_no_add_dir(self):
        launch, err = self.launch()
        self.assertEqual((launch.cwd, err, self.flag(launch.argv, "--add-dir")), (self.work, "", []))
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
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "run.json"), "w") as f:
            json.dump({"sessions": list(entries), "progress": [], "outcome": None}, f)

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

    def test_prompt_paths_stay_absolute_whatever_the_cwd(self):
        os.makedirs(self.work)
        with open(os.path.join(self.work, "input.md"), "w") as f:
            f.write("Research X.")
        here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, here)
        for cwd in (self.trusted, self.other):
            with self.subTest(cwd=cwd):
                launch, _ = self.launch(self.trusted, cwd=cwd, input="work/input.md", workdir="work")
                tail = launch.argv[2].rsplit("\n---\n", 1)[1]
                work = os.path.join(os.getcwd(), "work")
                self.assertEqual(tail, f"\nInput: {work}/input.md\nWorkdir: {work}\n")
                self.assertIn(f"--to {work}/.report.jsonl", launch.argv[2])


class Skill(Base):
    def text(self, role, task=None):
        return drive.inline(CORE, clients.get("skill", CORE), role, task)

    def test_prints_the_prompt_with_this_cores_paths(self):
        text = self.text("pm", "product-design")
        self.assertTrue(text.startswith("# Guide"))
        self.assertIn(f"`python3 {CORE}/src/repo.py worktree --dir <Workdir>/src --branch <branch> [--name <checkout>] <repo>`",
                      text)
        self.assertTrue(text.endswith("\n---\n\nInput: given with this prompt\n"
                                      "Workdir: the dir `mktemp -d` prints, run once at the start and reused for this "
                                      "invocation\n"))
        for placeholder in ("${CLAUDE_SKILL_DIR}", "$ARGUMENTS", "{{"):
            self.assertNotIn(placeholder, text)

    def test_deep_research_names_core_methods(self):
        text = self.text("researcher", "deep-research")
        for name in ("deep-research", "ultracode"):
            path = os.path.join(CORE, "team", "methods", f"{name}.md")
            self.assertTrue(os.path.isfile(path), path)
            self.assertIn(f"`{path}`", text)

    def test_document_tasks_return_to_the_orchestrator(self):
        for role, task in (("researcher", "light-research"), ("pm", "product-design")):
            text = self.text(role, task)
            self.assertIn("publish, post or save it nowhere. Leave `url` empty.", text)
            self.assertNotIn("ophis/private_docs", text)
            self.assertIn("## Return\n\nEnd with your final reply in this conversation", text)
            self.assertNotIn("Output: ", text)
            self.assertNotIn("## Resume", text)

    def test_pull_request_stays(self):
        for task in ("build", "light-build"):
            text = self.text("engineer", task)
            self.assertIn("gh pr create", text, task)
            self.assertNotIn("publish, post or save it nowhere", text, task)

    def test_every_task_renders(self):
        for role, task in (("researcher", "deep-research"), ("pm", "product-design"), ("engineer", "build"),
                           ("engineer", "light-build"), ("dummy-tester", "echo")):
            self.assertNotIn("{{", self.text(role, task))

    def test_main_prints_the_prompt_and_runs_nothing(self):
        calls = []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = drive.main(["--client", "skill", "--role", "dummy-tester"], root=CORE,
                              popen=lambda *a, **k: calls.append(a))
        self.assertEqual((code, calls, err.getvalue()), (0, [], ""))
        self.assertEqual(out.getvalue(), self.text("dummy-tester", "echo"))

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
                        sinks=sinks if sinks is not None else [drive.terminal(log)])
        return r, calls, log.getvalue()

    def read(self, name):
        with open(os.path.join(self.work, name)) as f:
            return f.read()

    def record(self):
        return json.loads(self.read("run.json"))

    def exists(self, name):
        return os.path.lexists(os.path.join(self.work, name))

    def test_done_run_saves_outcome_deliverable_and_progress(self):
        r, ((argv, kw),), log = self.start([progress("round", "half way"), said("hi"), outcome(DONE)])
        out = os.path.join(self.work, "out.md")
        self.assertEqual((r.returncode, r.outcome.status, r.outcome.url), (0, "done", out))
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake"], self.work, "1"))
        self.assertEqual(self.read("out.md"), "# Doc\n")
        self.assertEqual(self.record()["outcome"]["url"], out)
        self.assertEqual([p | {"ts": ""} for p in self.record()["progress"]], [{"ts": "", "name": "round", "text": "half way"}])
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
        r, _, _ = self.start([{"kind": "stop"}, progress("round", "x"), {"kind": "stop"}, outcome(DONE)],
                             sinks=[lambda e: seen.append(e.kind)])
        self.assertEqual((r.returncode, seen), (0, ["progress", "outcome"]))

    def test_headless_ignores_stops(self):
        with unittest.mock.patch.object(drive.tui_claude, "send") as send:
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
        self.assertEqual(self.record()["outcome"]["questions"], ["Which repo?"])

    def test_no_outcome(self):
        r, _, _ = self.start([said("bye")])
        self.assertEqual((r.returncode, r.outcome, r.error), (0, None, "the agent run returned no outcome"))

    def test_failed_client_ignores_the_outcome_and_leaves_no_stale_outcome(self):
        self.start([outcome(DONE)])
        seen = []
        r, _, _ = self.start([outcome(DONE)], rc=143, sinks=[seen.append])
        self.assertEqual((r.returncode, r.outcome, seen), (143, None, []))
        self.assertFalse(self.exists("out.md"))
        self.assertIsNone(self.record()["outcome"])

    def test_invalid_outcome_is_the_error_and_reaches_no_sink(self):
        seen = []
        r, _, _ = self.start([outcome({**DONE, "status": "maybe"})], sinks=[seen.append])
        self.assertIn("invalid outcome: status 'maybe'", r.error)
        self.assertEqual(seen, [])

    def test_a_symlink_planted_during_the_run_is_replaced_not_followed(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")

        def items():
            os.remove(os.path.join(self.work, "run.json"))
            for name in ("run.json", "out.md"):
                os.symlink(target, os.path.join(self.work, name))
            yield from feed(os.path.join(self.work, ".report.jsonl"), [progress("round", "x"), outcome(DONE)])

        self.start(items())
        with open(target) as f:
            self.assertEqual(f.read(), "mine")
        self.assertFalse(os.path.islink(os.path.join(self.work, "run.json")))
        self.assertEqual(self.record()["outcome"]["status"], "done")
        self.assertEqual(self.read("out.md"), "# Doc\n")

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
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def test_every_sink_gets_every_event(self):
        a, b = [], []
        self.start([said("hi"), outcome(DONE)], sinks=[a.append, b.append])
        self.assertEqual([e.kind for e in a], ["text", "outcome"])
        self.assertEqual(a, b)

    def test_resume_appends_progress_a_new_run_resets_it(self):
        self.start([progress("round", "one"), outcome(DONE)])
        self.start([progress("round", "二"), outcome(DONE)], resume=True)
        self.assertEqual([p["text"] for p in self.record()["progress"]], ["one", "二"])
        self.assertIn("二", self.read("run.json"))
        self.start([progress("round", "three"), outcome(DONE)])
        self.assertEqual([p["text"] for p in self.record()["progress"]], ["three"])

    def recorded(self, launch, items=(), **params):
        """drive.start of `launch` over `items`; (result, the record `begun` saw, the record after)."""
        p, seen = self.params(**params), []

        def begun():
            seen.append(drive.record(self.work))
        r = drive.start(launch, run(), p, client=claude(), sinks=[], begun=begun,
                        popen=lambda argv, **kw: FakeProc(feed(p.channel, items)))
        return r, seen[0], self.record()

    def test_the_record_holds_the_session_its_progress_and_outcome(self):
        launch = drive.Launch(["fake"], cwd=self.repo, transcript="/p/x.jsonl", resume="cd x && resume", project=True)
        r, before, after = self.recorded(launch, [progress("round", "one"), outcome(DONE)])
        entry = {"sid": SID, "cwd": self.repo, "project": True, "transcript": "/p/x.jsonl", "resume": "cd x && resume"}
        (b,), (a,) = before["sessions"], after["sessions"]
        self.assertEqual((b | {"started": ""}, before["progress"], before["outcome"]),
                         (entry | {"started": "", "ended": None}, [], None))
        self.assertEqual((a["started"], a["ended"] is not None), (b["started"], True))
        self.assertEqual([(x["name"], x["text"]) for x in after["progress"]], [("round", "one")])
        self.assertEqual(after["outcome"], dataclasses.asdict(r.outcome))
        self.assertEqual(drive.session(self.work, SID), a)

    def test_a_new_session_adds_an_entry_and_restarts_progress_a_resume_keeps_both(self):
        other = "99999999-2222-3333-4444-555555555555"
        self.recorded(drive.Launch(["fake"], cwd=self.work), [progress("a", "1")])
        self.recorded(drive.Launch(["fake"], cwd=self.work), [progress("b", "2")], sid=other)
        _, _, rec = self.recorded(drive.Launch(["fake"], cwd=self.repo), [progress("c", "3")], sid=other, resume=True)
        self.assertEqual([e["sid"] for e in rec["sessions"]], [SID, other])
        self.assertEqual([x["name"] for x in rec["progress"]], ["b", "c"])
        self.assertEqual(rec["sessions"][1]["cwd"], self.repo)

    def test_a_resume_keeps_its_sessions_start_time(self):
        self.recorded(drive.Launch(["fake"], cwd=self.work))
        started = self.record()["sessions"][0]["started"]
        with unittest.mock.patch.object(drive, "stamp", lambda: "later"):
            _, _, rec = self.recorded(drive.Launch(["fake"], cwd=self.work), resume=True)
        self.assertEqual((rec["sessions"][0]["started"], rec["sessions"][0]["ended"]), (started, "later"))

    def test_an_unreadable_record_is_started_anew(self):
        os.makedirs(self.work)
        for text in ("not json", "[]", '{"sessions": 1, "progress": 2}', '{"sessions": [1, {"sid": "x"}]}'):
            with self.subTest(text=text):
                with open(os.path.join(self.work, "run.json"), "w") as f:
                    f.write(text)
                _, _, rec = self.recorded(drive.Launch(["fake"], cwd=self.work), resume=True)
                self.assertEqual([e["sid"] for e in rec["sessions"] if isinstance(e, dict) and "cwd" in e], [SID])

    def test_a_symlink_or_fifo_record_is_not_read(self):
        os.makedirs(self.work)
        target = os.path.join(self.tmp.name, "planted.json")
        with open(target, "w") as f:
            json.dump({"sessions": [{"sid": SID, "cwd": "/", "project": True}]}, f)
        path = os.path.join(self.work, "run.json")
        os.symlink(target, path)
        self.assertEqual((drive.record(self.work), drive.session(self.work, SID)), ({}, None))
        os.remove(path)
        os.mkfifo(path)
        self.assertEqual(drive.record(self.work), {})

    def test_the_end_time_is_recorded_when_the_run_raises(self):
        def items():
            yield said("x")
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        self.assertIsNotNone(self.record()["sessions"][0]["ended"])

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

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)

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
        self.assertEqual(kw["template"], "echo {{session}}")
        self.assertEqual((kw["env"]["FAKE"], kw["env"]["PATH"]), ("1", os.environ["PATH"]))

    def test_pwd_is_the_runs_cwd(self):
        with unittest.mock.patch.dict(os.environ, {"PWD": "/stale"}):
            _, calls, _, _ = self.start([outcome(DONE)])
        (_, _, _, kw), = [c for c in calls if c[0] == "start"]
        self.assertEqual((kw["cwd"], kw["env"]["PWD"]), (self.work, self.work))

    def test_tui_session_name(self):
        self.assertIs(drive.tui_session, compose.tui_session)
        self.assertEqual(drive.tui_session("r", "t", SID), self.NAME)
        self.assertEqual(drive.tui_session("r", "t", SID, prefix="engineer-TASK-1"), "engineer-TASK-1-11111111")
        self.assertEqual(drive.tui_session("r", "t", SID, prefix=None), self.NAME)

    def test_tui_session_matches_the_prefix_and_sid_shape_only(self):
        for name, prefix in ((self.NAME, "r-t"), ("engineer-TASK-1-11111111", "engineer-TASK-1"), ("A_b-0-deadbeef", "A_b-0")):
            self.assertEqual(drive.TUI_SESSION.fullmatch(name)["prefix"], prefix)
        for name in ("r-t-xyz", "r-t-1111111", "r-t-111111111", "-11111111", "11111111", "r t-11111111", "r-t-11111111\n"):
            self.assertIsNone(drive.TUI_SESSION.fullmatch(name), name)

    def launched(self, layout=None, status_line=False, prefix=None, per_column=None, **kw):
        """The keyword arguments of the one tui_claude.start call of drive.start with the tui runner, and its name."""
        p = self.params(prefix=prefix)
        fake = FakeTui(p.channel, [[outcome(DONE)]])
        launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"], status_line=status_line,
                              per_column=per_column)
        with fake.patch(), unittest.mock.patch.object(drive, "POLL", 0):
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
        r, calls, kinds, _ = self.start([progress("round", "x")], [outcome(DONE)])
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
        r, _, kinds, err = self.start([progress("round", "x"), outcome(DONE)], progress=("start", "round"))
        self.assertEqual(kinds, ["progress", "missing", "outcome"])
        self.assertIn("drive.py: missing progress mark: start\n", err)

    def test_the_first_stop_without_an_outcome_nudges_once(self):
        r, calls, kinds, _ = self.start([STOP], [STOP], [outcome(DONE), STOP])
        self.assertEqual([c for c in calls if c[0] == "send"], [("send", self.NAME, drive.NUDGE)])
        self.assertEqual((r.outcome.status, kinds), ("done", ["outcome"]))

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
        self.assertEqual((r.outcome.status, err), ("done", ""))

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
        self.assertEqual((r.returncode, r.outcome.status, [c[0] for c in calls], err), (0, "done", ["start"], ""))

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
    DRIVER = f"dummy-tester-echo-{SID[:8]}-drive"

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
        return ["--role", "dummy-tester", "--task", "echo", "--input", "Hello.",
                "--out", os.path.join(self.work, "out.md"), "--workdir", self.work, "--sid", SID, *extra]

    def outer(self, *extra, rc=0, proc=None):
        """drive.main --detach, tmux through `proc`, else a fake that takes the handover file; (exit code, stderr, the
        tmux calls, the handover)."""
        calls, handover = [], {}

        def fake(argv, **kw):
            calls.append(argv)
            if argv[1] == "new-session" and rc == 0:
                with open(argv[-1]) as f:
                    handover.update(json.load(f))
                os.unlink(argv[-1])
            return subprocess.CompletedProcess(argv, rc, "", "duplicate session: x\n" if rc else "")

        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = drive.main(self.argv("--detach", "--events", self.events, *extra), root=CORE, proc=proc or fake)
        return code, err.getvalue(), calls, handover

    def inner(self, *steps, argv=None, api=None):
        """drive.main as the driver of a tui run over FakeTui(steps); (exit code, its calls)."""
        fake = FakeTui(os.path.join(self.work, ".report.jsonl"), steps)
        argv = argv or self.argv("--runner", "tui", "--events", self.events, "--driver", "d-drive")
        with fake.patch(**(api or {})), unittest.mock.patch.object(drive, "POLL", 0), redirect_stderr(io.StringIO()):
            return drive.main(argv, root=CORE), fake.calls

    def lines(self):
        with open(self.events) as f:
            lines = f.readlines()
        for line in lines:
            self.assertRegex(line, r"^\d\d:\d\d:\d\d ")
        return [line[9:] for line in lines]

    def test_the_driver_session_never_matches_a_tui_session_or_the_reserved_name(self):
        reserved = re.compile(r"[a-z][a-z0-9-]*-[A-Z][A-Z0-9]*-\d+-[0-9a-f]{8}")   # <role>-<ID>-<8 hex>
        self.assertTrue(reserved.fullmatch(drive.tui_session("r", "t", SID, "engineer-TASK-1")))
        for prefix, want in ((None, f"r-t-{SID[:8]}-drive"), ("engineer-TASK-1", f"engineer-TASK-1-{SID[:8]}-drive")):
            name = drive.driver_session("r", "t", SID, prefix)
            self.assertEqual(name, want)
            self.assertTrue(drive.tui_claude.NAME.fullmatch(name))
            self.assertIsNone(drive.TUI_SESSION.fullmatch(name))
            self.assertIsNone(reserved.fullmatch(name))

    def test_detach_needs_events_and_a_client_that_runs_before_anything_starts(self):
        skill, no = ["--client", "skill", "--role", "dummy-tester"], "SkillClient prints a prompt; it takes no --detach"
        cases = ((self.argv("--detach"), "--detach needs --events"),
                 (self.argv("--detach", "--runner", "tui"), "--detach needs --events"),
                 ([*skill, "--detach", "--events", self.events], no),
                 ([*skill, "--driver", "d", "--events", self.events], no))
        for argv, want in cases:
            with self.subTest(argv=argv):
                err, out, proc = io.StringIO(), io.StringIO(), unittest.mock.Mock()
                with redirect_stderr(err), redirect_stdout(out):
                    self.assertEqual(drive.main(argv, root=CORE, proc=proc), 2)
                self.assertEqual((err.getvalue(), out.getvalue()), (f"drive.py: {want}\n", ""))
                proc.assert_not_called()
                self.assertFalse(os.path.exists(self.events))
                self.assertFalse(os.path.exists(self.work))

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
            sys.executable, os.path.abspath(drive.__file__), "--role=dummy-tester", "--task=echo", "--input=Hello.",
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

    def test_stdin_input_reaches_the_driver_as_text(self):
        with unittest.mock.patch.object(sys, "stdin", io.StringIO("# Echo\nthis")):
            code, _, _, handover = self.outer("--input", "-")
        self.assertEqual((code, handover["argv"][4]), (0, "--input=# Echo\nthis"))

    def test_stdin_text_dash_reaches_the_driver_as_text_not_its_stdin(self):
        with unittest.mock.patch.object(sys, "stdin", io.StringIO("-")):
            code, _, _, handover = self.outer("--runner", "tui", "--input", "-")
        self.assertEqual((code, handover["argv"][4]), (0, "--input=-"))
        stdin = unittest.mock.Mock()
        with unittest.mock.patch.object(sys, "stdin", stdin):
            code, calls = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        stdin.read.assert_not_called()
        (_, _, argv, _), = [c for c in calls if c[0] == "start"]
        self.assertTrue(argv[1].endswith("Input:\n\n-\n"), argv[1][-40:])
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
            return FakeProc(feed(os.path.join(self.work, ".report.jsonl"), [outcome(DONE)]))

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
        self.assertEqual((cm.exception.code, killed), (129, [f"dummy-tester-echo-{SID[:8]}"]))
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
