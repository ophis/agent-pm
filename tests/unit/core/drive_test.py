from tests import ROOT, hermetic

import dataclasses
import errno
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

import clients
import compose
import drive
import report
import roster
import tmux_host
from clients import claude as claude_client
from clients import codex
from clients.claude import ClaudeClient
from clients.skill import SkillClient

CORE = os.path.join(ROOT, "core")
SID = "11111111-2222-3333-4444-555555555555"
NO_ACCESS = drive.Access(dirs=[], commands=[])
PARAMS = compose.RunParams(input="x", out="o", workdir="w", sid=SID)
TASKS = os.path.join(CORE, "team", "tasks")
METHODS = os.path.join(CORE, "team", "methods")
INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3"}


def run(**kw):
    return compose.RunConfig(
        **{"role": "r", "tier": 2, "effort": "high", "output": {"type": "local"}, **kw}
    )


class Recorder(clients.Client):
    needs_config = False
    seen = []

    def launch(self, prompt, run, *, params, access):
        Recorder.seen.append(dict(prompt=prompt, run=run, params=params, access=access))
        return clients.Launch(["fake", params.sid], {"FAKE": "1"}, cwd=access.cwd)


def claude(**overrides):
    return ClaudeClient({**clients.get("claude", CORE).config, **overrides})


def own(session="mgr"):
    def proc(argv, **kw):
        proc.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, f"{session}\n", "")

    proc.calls = []
    return proc


class Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(unittest.mock.patch.dict(os.environ))
        for key in INSIDE:
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = os.path.join(self.tmp.name, "work")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        Recorder.seen = []
        module = types.ModuleType("clients.fake")
        module.FakeClient = Recorder
        module.RUNS = Recorder.runs
        self.enterContext(unittest.mock.patch.dict(sys.modules, {"clients.fake": module}))
        original = clients.load_config
        self.enterContext(
            unittest.mock.patch.object(
                clients,
                "load_config",
                side_effect=lambda name, root: (
                    {"implementation": "FakeClient"}
                    if name == "fake" and root == CORE
                    else original(name, root)
                ),
            )
        )

    def params(self, **kw):
        return compose.RunParams(
            **{
                "input": "Research X.",
                "out": os.path.join(self.work, "out.md"),
                "workdir": self.work,
                "sid": SID,
                **kw,
            }
        )

    def plan(
        self,
        role="researcher",
        task="light-research",
        client="fake",
        repo=None,
        layers=(),
        cwd=None,
        root=CORE,
        **params,
    ):
        return drive.plan(
            root,
            clients.get(client, root),
            role,
            task,
            params=self.params(**params),
            repo=repo,
            layers=layers,
            cwd=cwd or self.work,
        )[0]

    def report(self):
        return compose.report_command(f"{CORE}/src", self.params())


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
        self.assertEqual(
            clients.load_config("claude", tmp.name),
            {"flags": ["-b"], "tiers": {"1": "x", "2": "z"}},
        )

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


class ClientDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        hermetic.home(self)
        self.path = os.path.join(self.tmp.name, "config.toml")

    def config(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_package_import_does_not_load_unselected_concrete_clients(self):
        code = """import sys
import clients
assert "clients.claude" not in sys.modules
assert "clients.skill" not in sys.modules
assert clients.load_module("codex").RUNS
assert "clients.claude" not in sys.modules
assert "clients.skill" not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.join(CORE, "src"),
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_clients_use_explicit_implementations(self):
        self.config(
            '[clients.skill]\nimplementation = "SkillClient"\n'
            '[clients.claude]\nimplementation = "ClaudeClient"\n'
        )
        self.assertIsInstance(clients.get("skill", self.tmp.name), SkillClient)
        self.assertIsInstance(clients.get("claude", self.tmp.name), ClaudeClient)
        self.assertFalse(hasattr(clients, "names"))

    def test_configured_class_receives_only_runtime_config(self):
        self.config('[clients.third_party]\nimplementation = "CustomAdapter"\ntier = 3\n')
        module = types.ModuleType("clients.third_party")
        module.CustomAdapter = type("CustomAdapter", (clients.Client,), {})
        module.RUNS = module.CustomAdapter.runs
        with unittest.mock.patch.dict(sys.modules, {"clients.third_party": module}):
            client = clients.get("third_party", self.tmp.name)
            self.assertIsInstance(client, module.CustomAdapter)
            self.assertEqual(client.config, {"tier": 3})
            module.CustomAdapter.needs_config = False
            self.assertEqual(clients.get("third_party", self.tmp.name).config, {})

    def test_missing_or_malformed_implementation_fails_after_module_capability(self):
        for field in (
            "",
            'implementation = ""',
            "implementation = 1",
            "implementation = []",
            "implementation = {}",
            'implementation = "other.Adapter"',
            'implementation = "bad name"',
        ):
            module = types.ModuleType("clients.third")
            module.RUNS = True
            with (
                self.subTest(field=field),
                unittest.mock.patch("importlib.import_module", return_value=module) as imported,
            ):
                self.config("[clients.third]\n" + field + "\n")
                with self.assertRaisesRegex(compose.ConfigError, "implementation"):
                    clients.get("third", self.tmp.name)
                imported.assert_called_once_with(".third", "clients")

    def test_missing_and_malformed_client_configuration_fail(self):
        for config in (None, "tier = 1\n", "clients = 1\n", "[clients]\nclaude = 1\n"):
            with self.subTest(config=config):
                if config is None:
                    if os.path.exists(self.path):
                        os.unlink(self.path)
                else:
                    self.config(config)
                with self.assertRaises(compose.ConfigError):
                    clients.get("claude", self.tmp.name)
        self.config("[clients.skill]\n")
        with self.assertRaisesRegex(compose.ConfigError, r"clients.claude"):
            clients.get("claude", self.tmp.name)

    def test_unsafe_names_never_reach_import(self):
        self.config("[clients.claude]\n")
        for name in ("Claude", "../claude", "claude.skill", "a-b", "_private", ""):
            with (
                self.subTest(name=name),
                unittest.mock.patch("importlib.import_module") as imported,
            ):
                with self.assertRaises(compose.ConfigError):
                    clients.get(name, self.tmp.name)
                imported.assert_not_called()

    def test_missing_module_class_and_wrong_class_fail_clearly(self):
        self.config('[clients.third]\nimplementation = "ThirdClient"\n')
        with self.assertRaisesRegex(compose.ConfigError, "module"):
            clients.get("third", self.tmp.name)
        module = types.ModuleType("clients.third")
        module.RUNS = True
        with unittest.mock.patch.dict(sys.modules, {"clients.third": module}):
            with self.assertRaisesRegex(compose.ConfigError, "ThirdClient"):
                clients.get("third", self.tmp.name)
            module.ThirdClient = object
            with self.assertRaisesRegex(compose.ConfigError, "subclass"):
                clients.get("third", self.tmp.name)
            module.ThirdClient = clients.Client
            with self.assertRaisesRegex(compose.ConfigError, "subclass"):
                clients.get("third", self.tmp.name)

    def test_failure_inside_an_existing_module_is_not_disguised_as_missing_module(self):
        self.config('[clients.third]\nimplementation = "ThirdClient"\n')
        failure = ModuleNotFoundError("No module named 'dependency'", name="dependency")
        with unittest.mock.patch("importlib.import_module", side_effect=failure):
            with self.assertRaisesRegex(compose.ConfigError, "dependency") as cm:
                clients.get("third", self.tmp.name)
        self.assertIs(cm.exception.__cause__, failure)


class ClientWorkers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent_pm = hermetic.home(self)

    def config(self, text):
        with open(os.path.join(self.tmp.name, "config.toml"), "w") as f:
            f.write(text)

    def test_a_client_without_the_worker_capability_refuses(self):
        spec = clients.WorkerSpec(
            name="w1",
            sid=SID,
            cwd=self.tmp.name,
            flags=(),
            prompt=None,
            events=os.path.join(self.tmp.name, "events"),
            resume=False,
            env={},
        )
        for client in (clients.Client({}), SkillClient({})):
            name = type(client).__name__
            with self.subTest(name):
                with self.assertRaisesRegex(compose.ConfigError, f"^{name} has no worker command$"):
                    client.worker(spec)
                with self.assertRaisesRegex(compose.ConfigError, f"^{name} has no worker reply$"):
                    client.worker_reply(SID)
        self.assertEqual(
            (
                clients.Client.session_env,
                dict(clients.Client.command_env),
                clients.Client.worker_dialogs,
            ),
            ("", {}, ()),
        )
        self.assertEqual(clients.WorkerCommand(["/x"]).env, {})

    def test_configured_names_the_client_tables_sorted_the_local_file_on_top(self):
        self.config(
            '[clients.skill]\nimplementation = "SkillClient"\n'
            '[clients.claude]\nimplementation = "ClaudeClient"\n'
        )
        self.assertEqual(clients.configured(self.tmp.name), ["claude", "skill"])
        self.assertEqual(clients.configured(CORE), ["claude", "codex", "skill"])
        with open(os.path.join(self.agent_pm, "core.local.toml"), "w") as f:
            f.write('[clients.extra]\nimplementation = "ExtraClient"\n')
        self.assertEqual(clients.configured(self.tmp.name), ["claude", "extra", "skill"])

    def test_configured_without_a_config_or_a_clients_table(self):
        with self.assertRaisesRegex(compose.ConfigError, "^no core config "):
            clients.configured(self.tmp.name)
        for text in ("tier = 1\n", "clients = 1\n"):
            with self.subTest(text):
                self.config(text)
                self.assertEqual(clients.configured(self.tmp.name), [])
        self.config("[clients\n")
        with self.assertRaisesRegex(compose.ConfigError, "^core config "):
            clients.configured(self.tmp.name)

    def test_loaded_leaves_out_a_client_whose_definition_fails_with_one_stderr_line(self):
        self.config(
            '[clients.skill]\nimplementation = "SkillClient"\n[clients.broken]\nimplementation = "X"\n'
            '[clients.claude]\nimplementation = "ClaudeClient"\n[clients.codex]\n'
        )
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(
                clients.loaded(self.tmp.name), {"claude": ClaudeClient, "skill": SkillClient}
            )
        self.assertEqual(
            err.getvalue(),
            "clients: broken skipped: client 'broken' has no module clients.broken\n"
            "clients: codex skipped: [clients.codex].implementation must name a class "
            "in clients.codex\n",
        )

    def test_caller_is_the_client_whose_session_marker_the_env_holds(self):
        a = type("AClient", (clients.Client,), {"session_env": "A_SESSION"})
        b = type("BClient", (clients.Client,), {"session_env": "B_SESSION"})
        classes = {"a": a, "b": b, "plain": clients.Client}
        for env, want in (
            ({"PATH": "/bin", "": "1"}, None),
            ({"A_SESSION": "1"}, "a"),
            ({"B_SESSION": ""}, "b"),
        ):
            with self.subTest(env=env):
                self.assertEqual(clients.caller(env, classes), want)
        with self.assertRaises(compose.ConfigError) as cm:
            clients.caller({"A_SESSION": "1", "B_SESSION": "1"}, classes)
        self.assertEqual(
            str(cm.exception),
            "the caller runs inside more than one client's session "
            "(a: A_SESSION, b: B_SESSION): pass --client",
        )
        self.assertEqual(clients.caller({"CLAUDECODE": "1"}, clients.loaded(CORE)), "claude")


class Claude(Base):
    def test_the_prompt_names_the_file_the_agent_writes_the_deliverable_to(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere", "out.md")
        for layers in ([{"output": {"type": "local"}}], [{"output": {"type": "orchestrator"}}]):
            for params, want in (
                ({}, "<Workdir>/out.md"),
                ({"out": elsewhere}, "<Workdir>/tmp/deliverable.md"),
            ):
                with self.subTest(layers=layers, params=params):
                    launch = self.plan(client="claude", repo=self.repo, layers=layers, **params)
                    self.assertIn(
                        f"Write the deliverable to `{want}` and return that file as the outcome's "
                        "`deliverable`",
                        launch.argv[2],
                    )

    def test_light_research_argv(self):
        launch = self.plan(client="claude", repo=self.repo)
        self.assertEqual(launch.argv[:2], ["claude", "-p"])
        self.assertTrue(launch.argv[2].startswith("# Guide"))
        self.assertEqual(
            launch.argv[3:],
            [
                "--session-id",
                SID,
                "--model",
                "opus",
                "--effort",
                "high",
                "--permission-mode",
                "auto",
                "--strict-mcp-config",
                "--setting-sources",
                "user",
                "--output-format",
                "stream-json",
                "--verbose",
                "--settings",
                '{"enabledPlugins": {"agent-pm@agent-pm": false}}',
                "--add-dir",
                TASKS,
                "--add-dir",
                METHODS,
                "--allowedTools",
                f"Bash(python3 {CORE}/src/repo.py worktree --dir {self.work}/src *)",
                f"Bash({self.report()} *)",
            ],
        )
        self.assertIn(
            "\n- `report`: `python3 <scripts>/report.py --to <Workdir>/run.jsonl`\n", launch.argv[2]
        )
        self.assertIn("## Return\n\nReport through `report`, never in a reply:", launch.argv[2])
        self.assertEqual(launch.env, {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
        self.assertEqual(launch.cwd, self.work)

    def test_a_roles_effort_and_repo_commands(self):
        for role, task, effort, cmds in (
            ("engineer", None, "xhigh", ("worktree", "status")),
            ("pm", "product-design", "high", ("worktree",)),
        ):
            with self.subTest(role=role):
                argv = self.plan(role, task, client="claude").argv
                self.assertEqual(argv[argv.index("--effort") + 1], effort)
                self.assertEqual(
                    argv[argv.index("--allowedTools") :],
                    ["--allowedTools"]
                    + [
                        f"Bash(python3 {CORE}/src/repo.py {cmd} --dir {self.work}/src *)"
                        for cmd in cmds
                    ]
                    + [f"Bash({self.report()} *)"],
                )
                self.assertIn(
                    "`python3 <scripts>/repo.py worktree --dir <Workdir>/src --branch <branch> "
                    "[--name <checkout>] <repo>`",
                    argv[2],
                )

    def test_the_gate_is_pre_approved_verbatim(self):
        gate = "python3 /u/usage.py --below 80"
        argv = self.plan(
            "researcher", "deep-research", client="claude", repo=self.repo, layers=[{"gate": gate}]
        ).argv
        self.assertEqual(argv[argv.index("--allowedTools") :][-1], f"Bash({gate})")
        self.assertIn(f"\n- `<gate>`: `{gate}`\n", argv[2])

    def test_every_run_reads_the_tasks_dir_a_researchers_the_methods_dir_too(self):
        for role, task, dirs in (
            ("researcher", None, [TASKS, METHODS]),
            ("researcher", "deep-research", [TASKS, METHODS]),
            ("researcher", "light-research", [TASKS, METHODS]),
            ("engineer", None, [TASKS]),
            ("dummy-tester", "echo", [TASKS]),
        ):
            argv = self.plan(role, task, client="claude", repo=self.repo).argv
            self.assertEqual(
                [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"], dirs, (role, task)
            )

    def test_a_researcher_names_core_method_files_for_claude_by_default_and_as_a_skill(self):
        prompts = {"claude": self.plan("researcher", "deep-research", client="claude").argv[2]}
        self.plan("researcher", "deep-research")
        prompts["default"] = Recorder.seen[-1]["prompt"]
        prompts["skill"] = drive.inline(CORE, clients.get("skill", CORE), "researcher", None)
        for client, prompt in prompts.items():
            with self.subTest(client=client):
                self.assertIn(f"\n- `<methods>`: `{METHODS}`\n", prompt)
                for name in ("deep-research", "ultracode"):
                    self.assertTrue(os.path.isfile(os.path.join(METHODS, f"{name}.md")), name)
                    self.assertIn(f"`<methods>/{name}.md`", prompt)

    def test_interactive_is_argv_without_the_headless_flags_and_neither_has_deny_rules(self):
        for role, task in (("researcher", "light-research"), ("engineer", "build")):
            launch = self.plan(role, task, client="claude", repo=self.repo)
            argv = launch.argv
            self.assertNotIn("--disallowedTools", argv)
            j = argv.index("--settings")
            rest = [a for i, a in enumerate(argv) if i not in (1, 2, j, j + 1)]
            for flag in ("--output-format", "stream-json", "--verbose"):
                rest.remove(flag)
            interactive = list(launch.interactive)
            for flag in ("--settings", "--name"):
                i = interactive.index(flag)
                del interactive[i : i + 2]
            self.assertEqual(interactive, ["claude", argv[2], *rest[1:]])

    def test_interactive_names_the_claude_session_after_the_tui_session(self):
        for resume in (False, True):
            for prefix, name in ((None, f"researcher-{SID[:8]}"), ("p-1", f"p-1-{SID[:8]}")):
                with self.subTest(resume=resume, prefix=prefix):
                    launch = self.plan(client="claude", resume=resume, prefix=prefix)
                    i = launch.interactive.index("--name")
                    self.assertEqual(
                        (launch.interactive.count("--name"), launch.interactive[i + 1]), (1, name)
                    )
                    self.assertNotIn("--name", launch.argv)

    def test_interactive_adds_the_stop_hook_to_the_settings(self):
        launch = self.plan(client="claude")
        i = launch.interactive.index("--settings")
        cmd = f"{self.report()} stop --pending background_tasks"
        self.assertEqual(
            json.loads(launch.interactive[i + 1]),
            {
                "enabledPlugins": {"agent-pm@agent-pm": False},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]},
            },
        )

    def test_agent_runs_keep_this_plugin_out(self):
        with (
            open(os.path.join(CORE, ".claude-plugin", "plugin.json")) as f,
            open(os.path.join(CORE, "..", ".claude-plugin", "marketplace.json")) as g,
        ):
            plugin = f"{json.load(f)['name']}@{json.load(g)['name']}"
        launch = self.plan(client="claude")
        tui = drive.tmux_host.with_hooks(launch.interactive, "/e")
        for argv in (launch.argv, launch.interactive, tui):
            self.assertEqual(argv.count("--settings"), 1)
            self.assertEqual(
                json.loads(argv[argv.index("--settings") + 1])["enabledPlugins"], {plugin: False}
            )

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
        self.assertEqual(
            argv[argv.index("--allowedTools") :], ["--allowedTools", "Bash(make test)", "WebFetch"]
        )

    def test_no_rule_groups_when_empty(self):
        argv = claude().launch("p", run(), params=PARAMS, access=NO_ACCESS).argv
        for flag in ("--add-dir", "--disallowedTools", "--allowedTools"):
            self.assertNotIn(flag, argv)

    def test_braces_in_the_input_survive(self):
        argv = self.plan(client="claude", input="Explain {{x}} templates.").argv
        self.assertTrue(argv[2].rstrip().endswith("Explain {{x}} templates."))

    def test_a_bad_config_is_a_config_error(self):
        for name, make, want in (
            (
                "unmapped tier",
                lambda: claude(tiers={}).launch("p", run(), params=PARAMS, access=NO_ACCESS),
                "no model for tier 2",
            ),
            (
                "--setting-sources",
                lambda: claude(flags=["--verbose", "--setting-sources", "user"]),
                "--setting-sources is the driver's",
            ),
            (
                "--setting-sources=",
                lambda: claude(flags=["--verbose", "--setting-sources=user,project", "user"]),
                "--setting-sources is the driver's",
            ),
            ("unknown key", lambda: claude(argv=[]), "unknown key 'argv'"),
        ):
            with self.subTest(name), self.assertRaises(compose.ConfigError) as cm:
                make()
            self.assertIn(want, str(cm.exception))

    def test_role_value_beats_the_global(self):
        c = claude(allow=["Read"], roles={"r": {"allow": ["WebFetch"]}})
        self.assertEqual(c.value(run(), "allow"), ["WebFetch"])
        self.assertEqual(c.value(run(task="u"), "allow"), ["WebFetch"])
        self.assertEqual(c.value(run(role="x"), "allow"), ["Read"])
        self.assertIsNone(c.value(run(role="x"), "nothing"))

    def test_claude_tier_override_changes_the_model(self):
        with unittest.mock.patch.object(
            clients,
            "load_config",
            lambda name, root: {**clients.base.load_config(name, root), "tier": 3},
        ):
            argv = self.plan(client="claude").argv
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_transcript_and_resume_command_follow_the_cwd(self):
        self.assertEqual(
            claude_client.transcript("/a/b.c d", SID, "/p"), f"/p/-a-b-c-d/{SID}.jsonl"
        )
        launch = self.plan(client="claude")
        self.assertEqual(
            (launch.resume, launch.transcript),
            (f"cd {self.work} && claude --resume {SID}", claude_client.transcript(self.work, SID)),
        )
        with redirect_stderr(io.StringIO()):
            launch = self.plan(client="claude", cwd=self.repo)
        self.assertEqual(
            launch.resume, f"cd {self.repo} && claude --resume {SID} --add-dir {self.work}"
        )
        self.assertEqual(launch.transcript, claude_client.transcript(self.repo, SID))


REFUSED = (
    "--resume",
    "-r",
    "--session-id",
    "--continue",
    "-c",
    "--fork-session",
    "--from-pr",
    "--teleport",
)
TOOL = {"type": "tool_use", "id": "x", "name": "Bash", "input": {}}


def record(kind, *blocks, mid=None):
    message = {"content": list(blocks)}
    if mid:
        message["id"] = mid
    return json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n"


def text_block(t):
    return {"type": "text", "text": t}


class ClaudeWorker(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        self.exe = os.path.join(self.bin, "claude")
        with open(self.exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.exe, 0o755)
        self.events = os.path.join(self.root, "events")
        self.cfg = os.path.join(self.root, "cfg")
        self.client = claude()

    def spec(self, **kw):
        return clients.WorkerSpec(
            **{
                "name": "w1",
                "sid": SID,
                "cwd": self.root,
                "flags": (),
                "prompt": None,
                "events": self.events,
                "resume": False,
                "env": {"PATH": self.bin},
                **kw,
            }
        )

    def test_session_marker_and_context_variables(self):
        self.assertEqual(ClaudeClient.session_env, "CLAUDECODE")
        self.assertEqual(
            dict(ClaudeClient.command_env),
            dict.fromkeys(
                (
                    *tmux_host.PARENT_KEYS,
                    "CLAUDECODE",
                    "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ATTENDED",
                    "CLAUDE_CODE_EXECPATH",
                    "CLAUDE_CODE_MESSAGING_SOCKET",
                    "CLAUDE_CODE_MESSAGING_TOKEN",
                    "CLAUDE_PID",
                    "CLAUDE_EFFORT",
                )
            ),
        )
        with self.assertRaises(TypeError):
            ClaudeClient.command_env["X"] = "1"

    def test_the_start_and_restart_argv(self):
        flags = ("--model", "m c")
        head = [self.exe, "--session-id", SID, "--name", "w1", *flags]
        cases = (
            ({}, head),
            ({"prompt": ""}, head),
            ({"prompt": "do it"}, [*head, "--", "do it"]),
            ({"prompt": "-x --model"}, [*head, "--", "-x --model"]),
            (
                {"resume": True, "prompt": "p"},
                [self.exe, "--resume", SID, "--name", "w1", *flags, "--", "p"],
            ),
            (
                {"resume": True, "exe": "/stored/claude", "env": {}},
                ["/stored/claude", "--resume", SID, "--name", "w1", *flags],
            ),
        )
        for kw, argv in cases:
            with self.subTest(**kw):
                self.assertEqual(
                    self.client.worker(self.spec(flags=flags, **kw)),
                    clients.WorkerCommand(tmux_host.with_hooks(argv, self.events)),
                )

    def test_own_settings_merged_with_the_hooks(self):
        own = {"type": "command", "command": "mine"}
        flags = ("--settings", json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [own]}]}}))
        argv = self.client.worker(self.spec(flags=flags)).argv
        self.assertEqual(argv.count("--settings"), 1)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(
            settings["hooks"]["Stop"],
            [{"hooks": [own]}, *json.loads(tmux_host.hooks(self.events))["hooks"]["Stop"]],
        )
        with self.assertRaisesRegex(compose.ConfigError, "^--settings"):
            self.client.worker(self.spec(flags=("--settings", "[]")))

    def test_claude_resolved_on_the_specs_path(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        self.assertEqual(self.client.worker(self.spec(env={"PATH": "bin"})).argv[0], self.exe)
        for env in ({"PATH": os.path.join(self.root, "empty")}, {"PATH": ""}):
            with (
                self.subTest(env=env),
                self.assertRaisesRegex(compose.ConfigError, "^claude not found on PATH$"),
            ):
                self.client.worker(self.spec(env=env))

    def test_flags_that_pick_the_session_are_refused_before_a_bare_dashdash(self):
        empty = {"PATH": os.path.join(self.root, "empty")}
        for flag in REFUSED:
            for given in [(flag, "x")] + ([(f"{flag}=x",)] if flag.startswith("--") else []):
                for resume in (False, True):
                    with self.subTest(given=given, resume=resume):
                        with self.assertRaises(compose.ConfigError) as cm:
                            self.client.worker(
                                self.spec(flags=("--model", "m", *given), resume=resume, env=empty)
                            )
                        self.assertEqual(
                            str(cm.exception),
                            f"{flag}: crew.py picks the session; use start --resume <sid>",
                        )
                        argv = self.client.worker(
                            self.spec(flags=("--model", "m", "--", *given), resume=resume)
                        ).argv
                        self.assertEqual(argv[-len(given) - 1 :], ["--", *given])

    def transcript(self, *lines, sid=SID, project="-p"):
        d = os.path.join(self.cfg, "projects", project)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write("".join(lines))

    def reply(self, sid=SID):
        with unittest.mock.patch.object(claude_client, "config_dir", return_value=self.cfg):
            return self.client.worker_reply(sid)

    def test_reply_is_the_last_text_bearing_assistant_message(self):
        cases = {
            "the last assistant text wins": (
                (
                    record("assistant", text_block("first")),
                    record("user", text_block("q")),
                    record("assistant", text_block("a"), TOOL, text_block("b")),
                    record("assistant", TOOL),
                    "not json\n",
                    record("user", text_block("later")),
                ),
                "a\nb",
            ),
            "blocks of one message joined": (
                (
                    record("assistant", text_block("old"), mid="m0"),
                    record("assistant", text_block("a"), mid="m1"),
                    record("assistant", TOOL, mid="m1"),
                    record("assistant", text_block("b"), mid="m1"),
                    record("assistant", TOOL, mid="m2"),
                ),
                "a\nb",
            ),
            "lines without id are own messages": (
                (record("assistant", text_block("a")), record("assistant", text_block("b"))),
                "b",
            ),
            "other records inside a message skipped": (
                (
                    record("assistant", text_block("old"), mid="m0"),
                    record("assistant", text_block("a"), mid="m1"),
                    record("user", text_block("result")),
                    "not json\n",
                    record("assistant", text_block("b"), mid="m1"),
                ),
                "a\nb",
            ),
            "a cut last line": (
                (
                    record("assistant", text_block("a"), mid="m1"),
                    record("assistant", text_block("cut"), mid="m2")[:-10],
                ),
                "a",
            ),
            "a whole last line without its newline": (
                (
                    record("assistant", text_block("a"), mid="m1"),
                    record("assistant", text_block("whole"), mid="m2").rstrip("\n"),
                ),
                "whole",
            ),
            "none yet": ((record("user", text_block("q")), record("assistant", TOOL)), ""),
            "empty": ((), ""),
            "blank lines": (("\n", "\n"), ""),
            "no text, a fragment last": (
                (record("user", text_block("q")), record("assistant", TOOL, mid="m1"), "x"),
                "",
            ),
        }
        for why, (lines, want) in cases.items():
            with self.subTest(why):
                self.transcript(*lines)
                self.assertEqual(self.reply(), want)

    def test_a_long_transcript_read_from_its_end(self):
        big = "é" * 40000
        earlier = [
            record("assistant", text_block(f"old {i} " + "x" * 1000), mid=f"m{i}")
            for i in range(300)
        ]
        last = [
            record("assistant", text_block("a " + big), mid="mL"),
            record("assistant", TOOL, mid="mL"),
            record("assistant", text_block("b " + big), mid="mL"),
            record("assistant", text_block("c " + big), mid="mL"),
        ]
        tail = [record("user", text_block("q")), record("assistant", TOOL, mid="mN")]
        data = "".join([*earlier, *last, *tail]).encode()
        if data[len(data) - clients.base.BLOCK] & 0xC0 != 0x80:
            tail[0] = record("user", text_block("qq"))
            data = "".join([*earlier, *last, *tail]).encode()
        self.assertEqual(data[len(data) - clients.base.BLOCK] & 0xC0, 0x80)
        self.transcript(*earlier, *last, *tail)
        reads = []

        def spy(*args, **kw):
            f = open(*args, **kw)
            read = f.read
            f.read = lambda n=-1: reads.append(read(n)) or reads[-1]
            return f

        with unittest.mock.patch.object(claude_client, "open", side_effect=spy, create=True):
            self.assertEqual(self.reply(), "\n".join(("a " + big, "b " + big, "c " + big)))
        self.assertGreater(len(reads), 3)
        self.assertLessEqual(
            sum(map(len, reads)), len("".join([*last, *tail]).encode()) + clients.base.BLOCK
        )

    def test_no_transcript_of_its_session(self):
        for sids in ((), ("2" + SID[1:],)):
            with self.subTest(sids=sids):
                for sid in sids:
                    self.transcript(record("assistant", text_block("x")), sid=sid)
                with self.assertRaises(compose.ConfigError) as cm:
                    self.reply()
                self.assertEqual(
                    str(cm.exception),
                    f"no transcript for {SID}: started with a leaked Claude Code variable?",
                )

    def test_a_bad_session_id_before_any_glob(self):
        self.transcript(record("assistant", text_block("x")), sid="evil")
        with unittest.mock.patch.object(claude_client.glob, "glob") as g:
            for sid in ("*", "evil", "A" + SID[1:], f"../{SID}", SID + "\n"):
                with (
                    self.subTest(sid=sid),
                    self.assertRaisesRegex(compose.ConfigError, "^bad session id "),
                ):
                    self.reply(sid)
            g.assert_not_called()

    def test_an_unreadable_transcript(self):
        self.transcript(record("assistant", text_block("x")))
        path = os.path.join(self.cfg, "projects", "-p", SID + ".jsonl")
        with (
            unittest.mock.patch.object(
                claude_client,
                "open",
                side_effect=PermissionError(13, "Permission denied"),
                create=True,
            ),
            self.assertRaisesRegex(
                compose.ConfigError, f"^transcript {re.escape(path)}: Permission denied$"
            ),
        ):
            self.reply()

    def test_the_config_dir_from_the_env_else_home(self):
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg}):
            self.assertEqual(claude_client.config_dir(), self.cfg)
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "", "HOME": self.root}):
            self.assertEqual(claude_client.config_dir(), os.path.join(self.root, ".claude"))


class Codex(Base):
    def setUp(self):
        super().setUp()
        hermetic.home(self)
        os.environ.pop("CODEX_HOME", None)
        self.enterContext(
            unittest.mock.patch.object(codex, "_implicit_writable_roots", return_value=[])
        )

    def test_report_interpreter_matches_prompt_and_access_command(self):
        interpreter = "/owned Python/bin/python3"
        with unittest.mock.patch.object(sys, "executable", interpreter):
            launch = self.plan("dummy-tester", "echo", client="codex", cwd=self.work)
        self.assertIn(f"`{shlex.quote(interpreter)} <scripts>/report.py", launch.argv[-1])
        self.assertIn(
            compose.report_command(f"{CORE}/src", self.params(), interpreter) + " *",
            launch.session_state["commands"],
        )

    def saved_session(self, *, client_id=None):
        codex_home = os.path.join(self.tmp.name, "user-codex")
        os.makedirs(codex_home)
        extra = os.path.join(self.tmp.name, "extra")
        os.makedirs(extra)
        snapshot = {
            "cwd": os.path.realpath(self.work),
            "project": False,
            "dirs": [os.path.realpath(extra)],
            "commands": ["make test"],
            "model": "saved-model",
            "effort": "low",
            "network": True,
            "codex_home": os.path.realpath(codex_home),
        }
        state = codex._create_session(
            self.work, SID, snapshot, [snapshot["cwd"], snapshot["codex_home"], *snapshot["dirs"]]
        )
        if client_id:
            codex._bind_session(self.work, SID, client_id)
        return state.snapshot

    def metadata(self, client_id, snapshot, *, filename=None, payload_id=None, cwd=None):
        folder = os.path.join(snapshot["codex_home"], "sessions", "2026", "10", "11")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, filename or f"rollout-2026-10-11T01-02-03-{client_id}.jsonl")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "type": "session_meta",
                    "payload": {"id": payload_id or client_id, "cwd": cwd or snapshot["cwd"]},
                },
                stream,
            )
            stream.write("\n")
        return path

    def test_new_launch_captures_effective_options(self):
        config_home = os.path.join(self.tmp.name, "user-codex")
        with (
            unittest.mock.patch.dict(os.environ, {"CODEX_HOME": config_home}),
            redirect_stderr(io.StringIO()),
        ):
            launch = clients.get("codex", CORE).launch(
                "prompt",
                run(),
                params=self.params(),
                access=drive.Access(dirs=[self.repo], commands=["make test"], cwd=self.work),
            )
        self.assertEqual(launch.env["CODEX_HOME"], os.path.realpath(config_home))
        self.assertEqual(
            set(launch.writable_roots),
            set(map(os.path.realpath, [self.work, config_home, self.repo])),
        )
        self.assertEqual(
            launch.session_state,
            {
                "cwd": os.path.realpath(self.work),
                "project": False,
                "dirs": [os.path.realpath(self.repo)],
                "commands": ["make test"],
                "model": "gpt-6.1-sol",
                "effort": "high",
                "network": False,
                "codex_home": os.path.realpath(config_home),
            },
        )

    def test_external_tilde_codex_home_matches_launch_and_recovery_settings(self):
        config_home = os.path.join(os.path.realpath(self.tmp.name), "user-codex")
        with (
            unittest.mock.patch.dict(
                os.environ, {"HOME": self.tmp.name, "CODEX_HOME": "~/user-codex"}
            ),
            redirect_stderr(io.StringIO()),
        ):
            launch = clients.get("codex", CORE).launch(
                "prompt",
                run(),
                params=self.params(),
                access=drive.Access(dirs=[], commands=[], cwd=self.work),
            )
        self.assertEqual(launch.env["CODEX_HOME"], config_home)
        self.assertEqual(launch.session_state["codex_home"], config_home)
        self.assertIn(config_home, launch.writable_roots)

    def test_codex_validates_its_own_saved_payload_before_use(self):
        snapshot = self.saved_session(client_id=SID)
        root = os.path.join(os.path.expanduser("~"), ".agent-pm", "core-sessions")
        path = codex._path(root, os.path.realpath(self.work), SID)
        with open(path) as f:
            original = json.load(f)
        client = clients.get("codex", CORE)
        for change in (
            {"network": "yes"},
            {"cwd": "/work/../other"},
            {"dirs": [2]},
            {"extra": True},
        ):
            data = dict(original, snapshot={**snapshot, **change})
            with open(path, "w") as f:
                json.dump(data, f)
            for use in (
                lambda: client.launch(
                    "next", run(), params=self.params(resume=True), access=NO_ACCESS
                ),
                lambda: client.resume_location(self.work, SID),
                lambda: client.session_info(self.work, SID),
            ):
                with (
                    self.subTest(change=change),
                    self.assertRaisesRegex(compose.ConfigError, "Codex recovery"),
                ):
                    use()
        with open(path, "w") as f:
            json.dump(dict(original, client_id="not-a-uuid"), f)
        with self.assertRaisesRegex(compose.ConfigError, "Codex.*ID"):
            client.session_info(self.work, SID)

    def test_saved_effective_paths_require_recorded_grants_and_store_isolation(self):
        snapshot = self.saved_session(client_id=SID)
        self.metadata(SID, snapshot)
        root = os.path.join(os.path.expanduser("~"), ".agent-pm", "core-sessions")
        path = codex._path(root, os.path.realpath(self.work), SID)
        with open(path) as stream:
            original = json.load(stream)
        client = clients.get("codex", CORE)
        changes = [
            dict(
                original,
                writable_roots=[grant for grant in original["writable_roots"] if grant != omitted],
            )
            for omitted in original["writable_roots"]
        ]
        changes.append(
            dict(
                original, snapshot={**snapshot, "dirs": [os.path.realpath(os.path.expanduser("~"))]}
            )
        )
        for data in changes:
            with open(path, "w") as stream:
                json.dump(data, stream)
            launch = drive.Launch(
                ["fake"], session_state=data["snapshot"], writable_roots=data["writable_roots"]
            )
            for use in (
                lambda: client.launch(
                    "next", run(), params=self.params(resume=True), access=NO_ACCESS
                ),
                lambda: client.resume_location(self.work, SID),
                lambda: client.session_info(self.work, SID),
                lambda: client.prepare_session(launch, self.params(resume=True)),
            ):
                with (
                    self.subTest(data=data),
                    self.assertRaisesRegex(compose.ConfigError, "writable|grant|changed"),
                ):
                    use()

    def test_saved_paths_may_be_covered_by_parent_grants(self):
        snapshot = self.saved_session(client_id=SID)
        self.metadata(SID, snapshot)
        root = os.path.join(os.path.expanduser("~"), ".agent-pm", "core-sessions")
        path = codex._path(root, os.path.realpath(self.work), SID)
        with open(path) as stream:
            data = json.load(stream)
        data["writable_roots"] = [os.path.realpath(self.work), os.path.realpath(self.tmp.name)]
        with open(path, "w") as stream:
            json.dump(data, stream)
        client = clients.get("codex", CORE)
        self.assertEqual(client.resume_location(self.work, SID), (snapshot["cwd"], False))
        self.assertTrue(client.session_info(self.work, SID).exists)
        launch = client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)
        self.assertEqual(launch.session_state, snapshot)
        client.prepare_session(launch, self.params(resume=True))

    def test_deep_native_metadata_is_a_config_error(self):
        snapshot = self.saved_session(client_id=SID)
        path = self.metadata(SID, snapshot)
        with open(path, "w") as stream:
            stream.write("[" * 2000 + "0" + "]" * 2000)
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        with (
            unittest.mock.patch.object(json, "_default_decoder", decoder),
            self.assertRaisesRegex(compose.ConfigError, "malformed Codex session metadata"),
        ):
            clients.get("codex", CORE).session_info(self.work, SID)

    def test_resume_uses_saved_options_and_parent_flags(self):
        client_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        snapshot = self.saved_session(client_id=client_id)
        self.metadata(client_id, snapshot)
        changed = type(clients.get("codex", CORE))(
            {
                **clients.get("codex", CORE).config,
                "tiers": {"2": "changed-model"},
                "shell_network": False,
            }
        )
        with (
            unittest.mock.patch.dict(
                os.environ, {"CODEX_HOME": os.path.join(self.work, "changed-home")}
            ),
            redirect_stderr(io.StringIO()),
        ):
            launch = changed.launch(
                "next",
                run(),
                params=self.params(resume=True),
                access=drive.Access(dirs=[self.repo], commands=[], cwd=self.work),
            )
        self.assertEqual(launch.env["CODEX_HOME"], snapshot["codex_home"])
        self.assertEqual(launch.cwd, snapshot["cwd"])
        self.assertEqual(launch.session_state, snapshot)
        self.assertEqual(launch.argv[-3:], ["resume", client_id, "next"])
        self.assertLess(launch.argv.index("--add-dir"), launch.argv.index("resume"))
        self.assertEqual(launch.argv[launch.argv.index("--add-dir") + 1], snapshot["dirs"][0])
        self.assertEqual(launch.argv[launch.argv.index("-m") + 1], "saved-model")
        self.assertIn("sandbox_workspace_write.network_access=true", launch.argv)
        self.assertNotIn("changed-model", launch.argv)
        self.assertNotIn(self.repo, launch.argv)

    def test_resume_rejects_missing_state_id_file_and_mismatched_metadata(self):
        client = clients.get("codex", CORE)
        with self.assertRaisesRegex(compose.ConfigError, "missing Codex recovery state"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)
        snapshot = self.saved_session()
        with self.assertRaisesRegex(compose.ConfigError, "missing Codex client ID"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)
        client_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        codex._bind_session(self.work, SID, client_id)
        with self.assertRaisesRegex(compose.ConfigError, "missing Codex session file"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)
        path = self.metadata(client_id, snapshot, payload_id="ffffffff-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        with self.assertRaisesRegex(compose.ConfigError, "mismatched Codex session metadata"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)
        os.unlink(path)
        self.metadata(client_id, snapshot)
        self.metadata(client_id, snapshot, filename=f"rollout-duplicate-{client_id}.jsonl")
        with self.assertRaisesRegex(compose.ConfigError, "duplicate Codex session files"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)

    def test_resume_rejects_session_file_symlink_and_wrong_cwd(self):
        client = clients.get("codex", CORE)
        client_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        snapshot = self.saved_session(client_id=client_id)
        path = self.metadata(client_id, snapshot, cwd=self.repo)
        with self.assertRaisesRegex(compose.ConfigError, "mismatched Codex session metadata"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)
        os.unlink(path)
        target = os.path.join(self.tmp.name, "target.jsonl")
        with open(target, "w", encoding="utf-8") as stream:
            json.dump(
                {"type": "session_meta", "payload": {"id": client_id, "cwd": snapshot["cwd"]}},
                stream,
            )
            stream.write("\n")
        os.symlink(target, path)
        with self.assertRaisesRegex(compose.ConfigError, "unsafe Codex session file symlink"):
            client.launch("next", run(), params=self.params(resume=True), access=NO_ACCESS)

    def test_fifo_transcript_is_rejected_without_blocking_resume_or_inspection(self):
        client = clients.get("codex", CORE)
        client_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        snapshot = self.saved_session(client_id=client_id)
        path = self.metadata(client_id, snapshot)
        os.unlink(path)
        os.mkfifo(path)

        def expire(signum, frame):
            self.fail("Codex session access blocked on a FIFO transcript")

        previous = signal.signal(signal.SIGALRM, expire)
        try:
            for name, use in (
                (
                    "resume",
                    lambda: client.launch(
                        "next", run(), params=self.params(resume=True), access=NO_ACCESS
                    ),
                ),
                ("inspection", lambda: drive.session_info(client, self.work, SID)),
            ):
                with self.subTest(operation=name):
                    signal.alarm(2)
                    try:
                        with self.assertRaisesRegex(
                            compose.ConfigError, "unsafe Codex session file"
                        ):
                            use()
                    finally:
                        signal.alarm(0)
        finally:
            signal.signal(signal.SIGALRM, previous)

    def test_session_info_reports_native_metadata_and_unsupported_usage(self):
        client = clients.get("codex", CORE)
        snapshot = self.saved_session()
        self.assertEqual(client.session_info(self.work, SID).client_id, None)
        client_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        codex._bind_session(self.work, SID, client_id)
        missing = client.session_info(self.work, SID)
        self.assertEqual(
            (
                missing.resume_supported,
                missing.inspection_supported,
                missing.client_id,
                missing.exists,
                missing.usage_supported,
                missing.usage,
            ),
            (True, True, client_id, False, False, None),
        )
        path = self.metadata(client_id, snapshot)
        found = client.session_info(self.work, SID)
        self.assertEqual((found.client_id, found.exists, found.transcript), (client_id, True, path))
        self.assertAlmostEqual(found.last_activity, os.stat(path).st_mtime, delta=1)
        self.assertIn("codex resume -s workspace-write", found.resume)
        self.assertTrue(found.resume.endswith(client_id))
        self.assertIn("CODEX_HOME=", found.resume)
        self.assertIn("--add-dir", found.resume)
        self.assertFalse(found.usage_supported)

    def test_four_tiers_select_astra_sol_luna_terra_and_show_the_model(self):
        expected = {"1": "gpt-6-astra", "2": "gpt-6.1-sol", "3": "gpt-6-luna", "4": "gpt-5.6-terra"}
        client = clients.get("codex", CORE)
        self.assertEqual(client.config["tiers"], expected)
        for tier, model in expected.items():
            with self.subTest(tier=tier):
                notice = io.StringIO()
                with redirect_stderr(notice):
                    launch = client.launch(
                        "prompt",
                        run(tier=int(tier)),
                        params=self.params(),
                        access=drive.Access(dirs=[], commands=[], cwd=self.work),
                    )
                self.assertEqual(launch.argv[launch.argv.index("-m") + 1], model)
                self.assertIn(f"Codex tier {tier} uses {model};", notice.getvalue())

    def test_plan_shows_selected_model_and_local_update_hint(self):
        config = clients.load_config("codex", CORE)
        notice = io.StringIO()
        with redirect_stderr(notice):
            launch, resolved = drive.plan(
                CORE,
                clients.get("codex", CORE),
                "researcher",
                "light-research",
                params=self.params(),
                cwd=self.work,
            )
        self.assertIn(
            f"drive.py: Codex tier {resolved.tier} uses {config['tiers'][str(resolved.tier)]}; "
            "update [clients.codex.tiers] in ~/.agent-pm/core.local.toml when models change\n",
            notice.getvalue(),
        )
        self.assertIn("configured commands are subject to Codex approval", notice.getvalue())
        self.assertEqual(
            launch.argv[launch.argv.index("-m") + 1], config["tiers"][str(resolved.tier)]
        )

    def test_missing_model_emits_no_selection_hint(self):
        config = clients.get("codex", CORE).config
        client = type(clients.get("codex", CORE))({**config, "tiers": {}})
        notice = io.StringIO()
        with (
            redirect_stderr(notice),
            self.assertRaisesRegex(compose.ConfigError, "no model for tier"),
        ):
            drive.plan(
                CORE, client, "researcher", "light-research", params=self.params(), cwd=self.work
            )
        self.assertNotIn("Codex tier", notice.getvalue())

    def test_every_configured_role_and_task_has_a_headless_command(self):
        config = clients.load_config("codex", CORE)
        self.assertEqual(config["implementation"], "CodexClient")
        self.assertIsInstance(clients.get("codex", CORE), clients.Client)
        for role, tasks in clients.catalog("codex", CORE).items():
            for task in (None, *tasks):
                with self.subTest(role=role, task=task):
                    launch, resolved = drive.plan(
                        CORE,
                        clients.get("codex", CORE),
                        role,
                        task,
                        params=self.params(),
                        cwd=self.work,
                    )
                    argv = launch.argv
                    self.assertEqual(argv[:3], ["codex", "exec", "--json"])
                    self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
                    self.assertIn("--skip-git-repo-check", argv)
                    self.assertIn('approvals_reviewer="auto_review"', argv)
                    self.assertIn('approval_policy="on-request"', argv)
                    self.assertIn("sandbox_workspace_write.network_access=false", argv)
                    self.assertIn("sandbox_workspace_write.writable_roots=[]", argv)
                    self.assertEqual(
                        argv[argv.index("-m") + 1], config["tiers"][str(resolved.tier)]
                    )
                    self.assertIn(
                        f"model_reasoning_effort={config['efforts'][resolved.effort]}", argv
                    )
                    self.assertIn("project_doc_max_bytes=0", argv)
                    self.assertTrue(argv[-1].startswith("# Guide"))
                    self.assertNotIn("--ignore-rules", argv)
                    self.assertNotIn("--ignore-user-config", argv)
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
                    self.assertEqual(
                        (launch.cwd, launch.env, launch.interactive),
                        (
                            self.work,
                            {"CODEX_HOME": os.path.realpath(os.path.expanduser("~/.codex"))},
                            [],
                        ),
                    )
                    self.assertIn("--add-dir", argv)
                    self.assertEqual(
                        drive.command(launch, "headless", clients.get("codex", CORE)), argv
                    )
                    with self.assertRaisesRegex(compose.ConfigError, "has no tui command"):
                        drive.command(launch, "tui", clients.get("codex", CORE))

    def test_missing_or_malformed_mappings_fail_before_launch(self):
        config = clients.get("codex", CORE).config
        for changed, message in (
            ({"tiers": {}}, "no model for tier 2"),
            ({"efforts": {}}, "no effort for 'high'"),
            ({"tiers": []}, "tiers must be a table"),
            ({"tiers": {"2": 7}}, "tier '2'"),
            ({"efforts": {"high": 7}}, "effort 'high'"),
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(compose.ConfigError, message),
            ):
                client = type(clients.get("codex", CORE))({**config, **changed})
                drive.plan(
                    CORE,
                    client,
                    "researcher",
                    "light-research",
                    params=self.params(),
                    cwd=self.work,
                )

    def test_network_requires_a_boolean_and_is_explicit_for_each_choice(self):
        config = clients.get("codex", CORE).config
        client_type = type(clients.get("codex", CORE))
        self.assertIs(config["shell_network"], False)
        for enabled in (False, True):
            with self.subTest(enabled=enabled), redirect_stderr(io.StringIO()):
                client = client_type({**config, "shell_network": enabled})
                launch = client.launch(
                    "prompt",
                    run(),
                    params=self.params(),
                    access=drive.Access(dirs=[], commands=[], cwd=self.work),
                )
                self.assertIn(
                    f"sandbox_workspace_write.network_access={str(enabled).lower()}", launch.argv
                )
        for bad in (0, 1, "false", None, []):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(
                    compose.ConfigError, r"\[clients.codex\].shell_network must be a boolean"
                ),
            ):
                client_type({**config, "shell_network": bad})

    def test_add_dir_keeps_read_and_write_intent_and_commands_require_approval(self):
        read_dir, write_dir = (
            os.path.join(self.tmp.name, "read"),
            os.path.join(self.tmp.name, "write"),
        )
        access = drive.Access(dirs=[read_dir, write_dir], commands=["make test"], cwd=self.work)
        notice = io.StringIO()
        with redirect_stderr(notice):
            launch = clients.get("codex", CORE).launch(
                "prompt", run(), params=self.params(), access=access
            )
        self.assertEqual(
            [launch.argv[i + 1] for i, value in enumerate(launch.argv) if value == "--add-dir"],
            [read_dir, write_dir],
        )
        self.assertIn('approvals_reviewer="auto_review"', launch.argv)
        self.assertIn('approval_policy="on-request"', launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("-s") + 1], "workspace-write")
        self.assertIn("configured commands are subject to Codex approval", notice.getvalue())
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", launch.argv)
        self.assertFalse(any(".rules" in value for value in launch.argv))

    def test_untrusted_project_codex_settings_are_rejected_from_cwd_or_ancestor(self):
        client = clients.get("codex", CORE)
        for location, link in (("cwd", False), ("ancestor", False), ("ancestor", True)):
            with self.subTest(location=location, link=link):
                root = tempfile.mkdtemp(dir=self.tmp.name)
                work = os.path.join(root, "work")
                os.makedirs(work)
                path = os.path.join(work if location == "cwd" else root, ".codex")
                if link:
                    os.symlink(self.repo, path)
                else:
                    os.mkdir(path)
                with unittest.mock.patch.dict(
                    os.environ, {"CODEX_HOME": os.path.join(self.tmp.name, "user-config")}
                ):
                    with self.assertRaisesRegex(
                        compose.ConfigError, r"project Codex settings .*\.codex.*clean workdir"
                    ):
                        client.launch(
                            "p",
                            run(),
                            params=self.params(workdir=work),
                            access=drive.Access(dirs=[], commands=[], cwd=work),
                        )
                with redirect_stderr(io.StringIO()):
                    trusted = client.launch(
                        "p",
                        run(),
                        params=self.params(workdir=work),
                        access=drive.Access(dirs=[], commands=[], cwd=work, project=True),
                    )
                self.assertEqual(trusted.cwd, work)

    def test_untrusted_project_codex_settings_are_rejected_on_resolved_ancestor(self):
        client = clients.get("codex", CORE)
        real_parent = os.path.join(self.tmp.name, "real")
        os.makedirs(real_parent)
        os.mkdir(os.path.join(real_parent, ".codex"))
        real_work = os.path.join(real_parent, "work")
        os.mkdir(real_work)
        linked_work = os.path.join(self.tmp.name, "linked-work")
        os.symlink(real_work, linked_work)
        with unittest.mock.patch.dict(
            os.environ, {"CODEX_HOME": os.path.join(self.tmp.name, "user-config")}
        ):
            with self.assertRaisesRegex(
                compose.ConfigError, r"project Codex settings .*\.codex.*clean workdir"
            ):
                client.launch(
                    "p",
                    run(),
                    params=self.params(workdir=linked_work),
                    access=drive.Access(dirs=[], commands=[], cwd=linked_work),
                )

    def test_untrusted_codex_home_cannot_be_project_controlled(self):
        client = clients.get("codex", CORE)
        os.makedirs(self.work)
        cases = [os.path.join(self.work, ".codex")]
        lexical_link = os.path.join(self.work, "config-link")
        os.symlink(self.repo, lexical_link)
        cases.append(lexical_link)
        real_inside_link = os.path.join(self.tmp.name, "external-link")
        os.symlink(self.work, real_inside_link)
        cases.append(real_inside_link)
        for config_home in cases:
            with self.subTest(config_home=config_home):
                with unittest.mock.patch.dict(os.environ, {"CODEX_HOME": config_home}):
                    with self.assertRaisesRegex(
                        compose.ConfigError, r"CODEX_HOME .*inside.*workdir"
                    ):
                        client.launch(
                            "p",
                            run(),
                            params=self.params(),
                            access=drive.Access(dirs=[], commands=[], cwd=self.work),
                        )

    def test_tilde_codex_home_keeps_literal_and_resolved_workdir_guards(self):
        client = clients.get("codex", CORE)
        os.makedirs(self.work)
        os.symlink(self.repo, os.path.join(self.work, "config-link"))
        os.symlink(self.work, os.path.join(self.tmp.name, "external-link"))
        for config_home in ("~/work/config", "~/work/config-link", "~/external-link"):
            with (
                self.subTest(config_home=config_home),
                unittest.mock.patch.dict(
                    os.environ, {"HOME": self.tmp.name, "CODEX_HOME": config_home}
                ),
            ):
                with self.assertRaisesRegex(compose.ConfigError, r"CODEX_HOME .*inside.*workdir"):
                    client.launch(
                        "p",
                        run(),
                        params=self.params(),
                        access=drive.Access(dirs=[], commands=[], cwd=self.work),
                    )

    def test_genuine_codex_home_outside_workdir_is_not_a_project_setting(self):
        client = clients.get("codex", CORE)
        home = os.path.join(self.tmp.name, ".codex")
        work = os.path.join(home, "runs", "work")
        os.makedirs(work)
        with (
            unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name, "CODEX_HOME": home}),
            redirect_stderr(io.StringIO()),
        ):
            launch = client.launch(
                "p",
                run(),
                params=self.params(workdir=work),
                access=drive.Access(dirs=[], commands=[], cwd=work),
            )
        self.assertEqual(launch.cwd, work)

    def test_default_user_settings_with_custom_codex_home_are_not_project_settings(self):
        default = os.path.join(self.tmp.name, ".codex")
        os.mkdir(default)
        work = os.path.join(self.tmp.name, "runs", "work")
        os.makedirs(work)
        custom = os.path.join(self.tmp.name, "custom-config")
        with (
            unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name, "CODEX_HOME": custom}),
            redirect_stderr(io.StringIO()),
        ):
            launch = clients.get("codex", CORE).launch(
                "p",
                run(),
                params=self.params(workdir=work),
                access=drive.Access(dirs=[], commands=[], cwd=work),
            )
        self.assertEqual(launch.env["CODEX_HOME"], os.path.realpath(custom))
        os.rmdir(default)
        os.symlink(custom, default)
        with (
            unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name, "CODEX_HOME": custom}),
            self.assertRaisesRegex(compose.ConfigError, "project Codex settings"),
        ):
            clients.get("codex", CORE).launch(
                "p",
                run(),
                params=self.params(workdir=work),
                access=drive.Access(dirs=[], commands=[], cwd=work),
            )

    def test_deep_event_does_not_drop_later_valid_output(self):
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        with unittest.mock.patch.object(json, "_default_decoder", decoder):
            seen = list(
                clients.get("codex", CORE).events(
                    [
                        "[" * 2000 + "0" + "]" * 2000,
                        '{"type":"item.completed","item":{"type":"agent_message","text":"later"}}',
                    ]
                )
            )
        self.assertEqual(
            seen,
            [clients.Event("stderr", "codex: invalid JSON event"), clients.Event("text", "later")],
        )

    def test_custom_codex_home_at_untrusted_project_ancestor_is_rejected(self):
        client = clients.get("codex", CORE)
        home = os.path.join(self.repo, ".codex")
        work = os.path.join(self.repo, "run")
        os.makedirs(home)
        os.makedirs(work)
        with unittest.mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            with self.assertRaisesRegex(
                compose.ConfigError, r"project Codex settings .*\.codex.*clean workdir"
            ):
                client.launch(
                    "p",
                    run(),
                    params=self.params(workdir=work),
                    access=drive.Access(dirs=[], commands=[], cwd=work),
                )

    def test_untrusted_cwd_outside_workdir_fails_before_launch(self):
        client = clients.get("codex", CORE)
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(compose.ConfigError, "untrusted cwd"),
        ):
            drive.plan(
                CORE, client, "researcher", "light-research", params=self.params(), cwd=self.repo
            )
        trusted = client.launch(
            "p",
            run(),
            params=self.params(),
            access=drive.Access(dirs=[TASKS], commands=[], cwd=self.repo, project=True),
        )
        self.assertEqual(trusted.cwd, self.repo)
        self.assertNotIn("project_doc_max_bytes=0", trusted.argv)
        self.assertEqual(trusted.argv[trusted.argv.index("--add-dir") + 1], TASKS)

    def test_handover_requires_progress_child_results_and_terminal_outcome_in_order(self):
        text = clients.get("codex", CORE).handover()
        for fragment in (
            f"[{clients.PROGRESS}:<name>]",
            "immediately",
            "`report progress <name> <your report>`",
            "before calling the next tool",
            "every spawned child",
            "available wait mechanism",
            "collect each child result",
            "background work",
            "last tool action",
            "`report outcome --status <done|needs_input|failed>",
            "final reply is not an outcome",
        ):
            self.assertIn(fragment, text)
        self.assertLess(text.index("`report progress"), text.index("wait for every spawned child"))
        self.assertLess(text.index("collect each child result"), text.index("`report outcome"))
        self.assertNotIn("wait_agent", text)

    def test_json_events_show_only_user_readable_text(self):
        client = clients.get("codex", CORE)
        lines = [
            '{"type":"thread.started","thread_id":"11111111-2222-3333-4444-555555555555"}\n',
            '{"type":"thread.started","thread_id":"123"}\n',
            '{"type":"turn.started"}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"Hello\\nworld"}}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"  "}}\n',
            '{"type":"item.completed","item":{"type":"command_execution","command":"secret"}}\n',
            '{"type":"error","message":"rate limit"}\n',
            '{"type":"error","message":{"message":"nested problem"}}\n',
            '{"type":"turn.failed","error":{"message":"turn failed"}}\n',
            "not JSON\n",
            "[]\n",
        ]
        seen = list(client.events(lines))
        self.assertEqual(
            seen,
            [
                clients.Event("session", session_id=SID),
                clients.Event("stderr", "codex: invalid thread.started ID"),
                clients.Event("text", "Hello"),
                clients.Event("text", "world"),
                clients.Event("stderr", "rate limit"),
                clients.Event("stderr", "nested problem"),
                clients.Event("stderr", "turn failed"),
                clients.Event("stderr", "codex: invalid JSON event"),
            ],
        )
        self.assertNotIn("secret", str(seen))


class Generic(Base):
    def test_driver_hands_the_client_neutral_access(self):
        launch = self.plan(repo=self.repo)
        (seen,) = Recorder.seen
        self.assertEqual(
            seen["access"],
            drive.Access(
                dirs=[TASKS, METHODS],
                commands=[
                    f"python3 {CORE}/src/repo.py worktree --dir {self.work}/src *",
                    f"{self.report()} *",
                ],
                cwd=self.work,
            ),
        )
        self.assertEqual(
            (seen["params"].sid, seen["params"].resume, seen["run"].task),
            (SID, False, "light-research"),
        )
        self.assertTrue(seen["prompt"].startswith("# Guide"))
        self.assertEqual(
            (launch.argv, launch.env, launch.cwd), (["fake", SID], {"FAKE": "1"}, self.work)
        )

    def test_layers_apply_after_the_clients_config(self):
        client = Recorder({"tier": 3, "effort": "low"})
        drive.plan(
            CORE, client, "researcher", "light-research", params=self.params(), cwd=self.work
        )
        drive.plan(
            CORE,
            client,
            "researcher",
            "light-research",
            params=self.params(),
            layers=[{"tier": 4}, {"tier": 1}],
            cwd=self.work,
        )
        plain, layered = (s["run"] for s in Recorder.seen)
        self.assertEqual((plain.tier, plain.effort), (3, "low"))
        self.assertEqual((layered.tier, layered.effort), (1, "low"))

    def test_plan_uses_public_resolution_and_checks_trusted_overlays(self):
        client = Recorder({"tier": 3, "effort": "low"})
        overlays = ({"effort": "high"}, {"roles": {"researcher": {"tier": 1}}})
        _, planned = drive.plan(
            CORE,
            client,
            "researcher",
            "light-research",
            params=self.params(),
            layers=overlays,
            cwd=self.work,
        )
        resolved = compose.resolve(
            CORE, client, "researcher", "light-research", overlays=overlays, trusted=True
        )
        self.assertEqual(planned, resolved)
        with self.assertRaisesRegex(compose.ConfigError, "unknown key"):
            drive.plan(
                CORE,
                client,
                "researcher",
                params=self.params(),
                layers=({"surprise": 1},),
                cwd=self.work,
            )

    def test_access_fills_the_dirs_and_commands_report_then_gate_last(self):
        gate = "python3 /u/usage.py {{workdir}} *"
        report = f"{compose.report_command('/s', self.params())} *"
        for name, cfg, repo, dirs, commands in (
            (
                "gate",
                {"commands": ["{{scripts}}/x *"], "gate": gate},
                None,
                ["/t"],
                ["/s/x *", report, gate],
            ),
            ("no gate", {"commands": ["x"]}, None, ["/t"], ["x", report]),
            (
                "repo entry binds to the repo arg",
                {"write": ["repo"], "commands": ["{{scripts}}/x --dir {{workdir}}/src *"]},
                self.repo,
                ["/t", self.repo],
                [f"/s/x --dir {self.work}/src *", report],
            ),
            (
                "methods fills read and write",
                {"read": ["{{methods}}", "/t"], "write": ["{{methods}}/out"]},
                None,
                ["/t", "/m", "/m/out"],
                [report],
            ),
        ):
            with self.subTest(name):
                acc = drive.access(
                    run(**cfg), self.params(), repo=repo, scripts="/s", methods="/m", tasks="/t"
                )
                self.assertEqual(acc, drive.Access(dirs=dirs, commands=commands, cwd=self.work))
        for key in ("read", "write"):
            with self.subTest(key), self.assertRaises(compose.ConfigError) as cm:
                drive.access(
                    run(**{key: ["{{nope}}"]}),
                    self.params(),
                    repo=None,
                    scripts="/s",
                    methods="/m",
                    tasks="/t",
                )
            self.assertIn("{{nope}}", str(cm.exception))

    def test_the_run_never_needs_the_out_dir_nor_a_repo_not_given(self):
        for role, task, params, dirs in (
            (
                "researcher",
                "light-research",
                {"out": os.path.join(self.tmp.name, "elsewhere", "out.md")},
                [TASKS, METHODS],
            ),
            ("engineer", None, {}, [TASKS]),
        ):
            with self.subTest(role=role):
                self.plan(role, task, **params)
                self.assertEqual(Recorder.seen[-1]["access"].dirs, dirs)

    def test_a_client_unknown_or_of_the_wrong_kind(self):
        with self.assertRaises(compose.ConfigError) as cm:
            self.plan(client="nope")
        self.assertEqual("client 'nope' has no module clients.nope", str(cm.exception))
        with self.assertRaises(compose.ConfigError):
            self.plan(client="skill")
        with self.assertRaises(compose.ConfigError):
            drive.inline(CORE, claude(), "dummy-tester")

    def test_plan_sets_the_configs_status_line_and_workers_per_column(self):
        path = os.path.join(hermetic.home(self), "core.local.toml")
        for attr, default, good, bad, msg in (
            (
                "status_line",
                False,
                (("true", True), ("false", False)),
                '"on"',
                "status_line: want true or false",
            ),
            (
                "per_column",
                None,
                (("2", 2),),
                "0",
                "workers_per_column: want an integer from 1 to 9999",
            ),
        ):
            key = "workers_per_column" if attr == "per_column" else attr
            with self.subTest(key):
                if os.path.exists(path):
                    os.remove(path)
                self.assertIs(getattr(self.plan(), attr), default)
                for text, want in good:
                    with open(path, "w") as f:
                        f.write(f"{key} = {text}\n")
                    self.assertIs(getattr(self.plan(), attr), want, text)
                with open(path, "w") as f:
                    f.write(f"{key} = {bad}\n")
                with self.assertRaisesRegex(compose.ConfigError, msg):
                    self.plan()

    def test_plan_sets_the_configs_grid_retile(self):
        path = os.path.join(hermetic.home(self), "core.local.toml")
        self.assertIsNone(self.plan().retile)
        for mode in ("all", "open-close", "off"):
            with open(path, "w") as f:
                f.write(f'grid_retile = "{mode}"\n')
            self.assertEqual(self.plan().retile, mode)
        with open(path, "w") as f:
            f.write('grid_retile = "x"\n')
        with self.assertRaisesRegex(
            compose.ConfigError, "^grid_retile: want one of all, open-close, off$"
        ):
            self.plan()


class Cwd(Base):
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
        root = os.path.join(self.tmp.name, "core")
        os.makedirs(root, exist_ok=True)
        if not os.path.lexists(os.path.join(root, "team")):
            os.symlink(os.path.join(CORE, "team"), os.path.join(root, "team"))
        shutil.copy(os.path.join(CORE, compose.CONFIG), root)
        with open(self.local, "w") as f:
            f.write(f"trusted_dirs = {json.dumps(list(trusted))}\n")
        return root

    def launch(self, *trusted, cwd=None, layers=(), **params):
        err = io.StringIO()
        with redirect_stderr(err):
            launch = self.plan(
                client="claude", root=self.core(*trusted), cwd=cwd, layers=layers, **params
            )
        return launch, err.getvalue()

    @staticmethod
    def flag(argv, name):
        return [argv[i + 1] for i, a in enumerate(argv) if a == name]

    def test_unset_is_the_callers_current_directory(self):
        err = io.StringIO()
        with redirect_stderr(err):
            launch, _ = drive.plan(
                CORE, claude(), "researcher", "light-research", params=self.params()
            )
        self.assertEqual((launch.cwd, launch.project), (os.getcwd(), False))
        self.assertEqual(self.flag(launch.argv, "--add-dir"), [self.work, TASKS, METHODS])
        self.assertEqual(
            err.getvalue(),
            f"drive.py: cwd {os.getcwd()} is not in trusted_dirs: its project settings are off\n",
        )

    def test_the_run_key_wins_over_the_default_and_expands_tilde(self):
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            launch, _ = self.launch(
                cwd=self.trusted, layers=[{"roles": {"researcher": {"cwd": "~/other"}}}]
            )
        self.assertEqual(launch.cwd, os.path.join(self.tmp.name, "other"))

    def test_a_trusted_cwd_loads_project_settings_and_its_mcp_config(self):
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.trusted, link)
        for entry, cwd in (
            (self.trusted, self.trusted),
            (link, self.trusted),
            (self.tmp.name, self.trusted),
        ):
            with self.subTest(entry=entry):
                launch, err = self.launch(entry, cwd=cwd)
                for argv in (launch.argv, launch.interactive):
                    self.assertEqual(self.flag(argv, "--setting-sources"), ["user,project,local"])
                    self.assertEqual(
                        self.flag(argv, "--mcp-config"), [os.path.join(cwd, ".mcp.json")]
                    )
                    self.assertEqual(self.flag(argv, "--add-dir"), [self.work, *self.read])
                    self.assertIn("--strict-mcp-config", argv)
                self.assertEqual((launch.cwd, launch.project, err), (cwd, True, ""))

    def test_a_trusted_cwd_without_mcp_json_gets_no_mcp_config(self):
        os.remove(os.path.join(self.trusted, ".mcp.json"))
        launch, _ = self.launch(self.trusted, cwd=self.trusted)
        self.assertEqual(self.flag(launch.argv, "--mcp-config"), [])

    def test_an_untrusted_cwd_gets_user_settings_and_a_notice(self):
        launch, err = self.launch(self.trusted, cwd=self.other)
        self.assertEqual(
            (self.flag(launch.argv, "--setting-sources"), self.flag(launch.argv, "--mcp-config")),
            (["user"], []),
        )
        self.assertEqual(
            (launch.cwd, launch.project, self.flag(launch.argv, "--add-dir")),
            (self.other, False, [self.work, *self.read]),
        )
        self.assertEqual(
            err,
            f"drive.py: cwd {self.other} is not in trusted_dirs: its project settings are off\n",
        )

    def test_the_workdir_as_cwd_has_no_notice_and_adds_no_workdir(self):
        launch, err = self.launch()
        self.assertEqual(
            (launch.cwd, err, self.flag(launch.argv, "--add-dir")), (self.work, "", self.read)
        )
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

    def test_a_cwd_that_is_no_directory_or_no_path_or_a_bad_trusted_dirs_is_a_config_error(self):
        for trusted, cwd, want in (
            ((), os.path.join(self.tmp.name, "nope"), "is not a directory"),
            ((), "rel/dir", "absolute or ~ path"),
            (("rel/dir",), None, "trusted_dirs"),
        ):
            with (
                self.subTest(trusted=trusted, cwd=cwd),
                self.assertRaises(compose.ConfigError) as cm,
            ):
                self.launch(*trusted, cwd=self.other, layers=[{"cwd": cwd}] if cwd else [])
            self.assertIn(want, str(cm.exception))

    def write_record(self, *entries):
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "run.jsonl"), "w") as f:
            f.writelines(json.dumps({"ts": "t", "kind": "session", **e}) + "\n" for e in entries)

    def test_a_resume_runs_in_the_cwd_its_start_recorded(self):
        new, _ = self.launch(self.trusted, cwd=self.trusted)
        p = self.params()
        with redirect_stderr(io.StringIO()):
            drive.start(
                new,
                run(),
                p,
                client=claude(),
                sinks=[],
                popen=lambda argv, **kw: FakeProc(feed(p.channel, [outcome(DONE)])),
            )
        launch, err = self.launch(
            self.trusted, cwd=self.other, layers=[{"cwd": self.work}], resume=True
        )
        self.assertEqual((launch.cwd, launch.project, err), (self.trusted, True, ""))

    def test_a_resume_reuses_the_recorded_cwd_and_project(self):
        self.write_record({"sid": SID, "cwd": self.trusted, "project": True})
        launch, err = self.launch(
            self.trusted, cwd=self.other, layers=[{"cwd": self.work}], resume=True
        )
        self.assertEqual((launch.cwd, launch.project, err), (self.trusted, True, ""))
        self.assertEqual(self.flag(launch.argv, "--resume"), [SID])
        self.write_record({"sid": SID, "cwd": self.trusted, "project": False})
        launch, _ = self.launch(self.trusted, resume=True)
        self.assertEqual(
            (launch.cwd, self.flag(launch.argv, "--setting-sources")), (self.trusted, ["user"])
        )

    def test_a_resume_whose_cwd_is_no_longer_trusted_is_refused(self):
        self.write_record({"sid": SID, "cwd": self.trusted, "project": True})
        with self.assertRaises(compose.ConfigError) as cm:
            self.launch(resume=True)
        self.assertIn(f"cwd {self.trusted} is no longer in trusted_dirs", str(cm.exception))

    def test_a_resume_without_its_session_in_the_record_runs_in_the_workdir(self):
        for entries in (
            (),
            ({"sid": "other", "cwd": self.trusted, "project": True},),
            ({"sid": SID, "cwd": "rel", "project": True},),
            ({"sid": SID, "cwd": self.trusted, "project": 1},),
        ):
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
                launch, _ = self.launch(
                    self.trusted, cwd=cwd, input="work/input.md", workdir="work"
                )
                work = os.path.join(os.getcwd(), "work")
                self.assertIn(f"\n- `<Workdir>`: `{work}`\n", launch.argv[2])
                self.assertTrue(launch.argv[2].endswith("\n# Input\n\nwork/input.md\n"))


class Skill(Base):
    def text(self, role, task=None):
        return drive.inline(CORE, clients.get("skill", CORE), role, task)

    def test_prints_the_prompt_with_this_cores_paths_parameters_and_input_replacing_the_tail(self):
        text = self.text("pm")
        self.assertTrue(text.startswith("# Guide"))
        self.assertIn(f"\n- `<scripts>`: `{CORE}/src`\n", text)
        self.assertIn(
            "`python3 <scripts>/repo.py worktree --dir <Workdir>/src --branch <branch> [--name <checkout>] "
            "<repo>`",
            text,
        )
        for placeholder in ("${CLAUDE_SKILL_DIR}", "$ARGUMENTS", "{{"):
            self.assertNotIn(placeholder, text)
        self.assertIn(
            f"\n# Parameters\n\n{compose.RULE}{SkillClient({}).approval()}\n\n- `<Workdir>`: the dir `mktemp -d` prints, run once at the "
            "start and reused for this invocation\n- `<scripts>`: ",
            text,
        )
        self.assertNotIn("- `report`:", text)
        self.assertTrue(
            text.endswith(
                "\n# Input\n\nThe text after `<role>[:<task>]` in your "
                "`/agent-pm:act-as` arguments.\n"
            )
        )
        for tail in ("\n---\n\nInput:", "Workdir: "):
            self.assertNotIn(tail, text)

    def test_document_roles_return_to_the_orchestrator_with_no_deliverable_file_line(self):
        for role, task in (
            ("researcher", None),
            ("researcher", "light-research"),
            ("pm", None),
            ("dummy-tester", None),
        ):
            text = self.text(role, task)
            self.assertNotIn("Write the deliverable to", text, role)
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
        self.assertIn(
            "\n- **Resume**: run [Engineer › Repo](#repo) steps 1–2 again, then "
            "[Engineer › Repo › Merge](#repo)",
            self.text("engineer"),
        )

    def test_every_run_renders(self):
        for role, task in (
            ("researcher", None),
            ("researcher", "deep-research"),
            ("pm", None),
            ("engineer", None),
            ("engineer", "light-build"),
            ("dummy-tester", None),
            ("dummy-tester", "echo"),
        ):
            self.assertNotIn("{{", self.text(role, task))

    def test_main_prints_the_prompt_and_runs_nothing(self):
        calls = []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = drive.main(
                ["--client", "skill", "--role", "dummy-tester"],
                root=CORE,
                popen=lambda *a, **k: calls.append(a),
            )
        self.assertEqual((code, calls, err.getvalue()), (0, [], ""))
        self.assertEqual(out.getvalue(), self.text("dummy-tester"))

    def test_main_names_a_given_task_else_the_run_picks_one(self):
        for extra, want in (
            ((), "**Your task**: pick it from your charter's Tasks section"),
            (("--task", "light-build"), "**Your task**: `light-build` (`<tasks>/light-build.md`)."),
        ):
            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                self.assertEqual(
                    drive.main(["--client", "skill", "--role", "engineer", *extra], root=CORE), 0
                )
            self.assertIn(want, out.getvalue().split("\n# Principles\n", 1)[0])

    def test_an_old_skill_output_table_exits_2(self):
        with open(os.path.join(hermetic.home(self), "core.local.toml"), "w") as f:
            f.write(
                '[clients.skill.roles.researcher.tasks.deep-research.output]\ntype = "orchestrator"\n'
            )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            self.assertEqual(
                drive.main(["--client", "skill", "--role", "researcher"], root=CORE), 2
            )
        self.assertEqual(out.getvalue(), "")
        self.assertIn("[clients.skill.roles.researcher]", err.getvalue())

    def test_main_refuses_a_runner_other_than_headless(self):
        for extra in ((), ("--dry-run",)):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                code = drive.main(
                    ["--client", "skill", "--role", "dummy-tester", "--runner", "tui", *extra],
                    root=CORE,
                )
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertIn(
                "drive.py: SkillClient prints a prompt; it takes no --runner tui\n", err.getvalue()
            )

    def test_main_takes_the_client_once(self):
        calls = []
        with (
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as cm,
        ):
            drive.main(
                [
                    "--client",
                    "skill",
                    "--role",
                    "dummy-tester",
                    "--client",
                    "claude",
                    "--input",
                    "x",
                    "--out",
                    "o.md",
                    "--workdir",
                    self.work,
                ],
                root=CORE,
                popen=lambda *a, **k: calls.append(a),
            )
        self.assertEqual((cm.exception.code, calls), (2, []))


class RoleRuns(Base):
    def main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--input",
            "Build X.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
            "--sid",
            SID,
            "--dry-run",
            *extra,
        ]
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            unittest.mock.patch.object(drive, "start") as start,
        ):
            code = drive.main(argv, root=CORE)
        start.assert_not_called()
        return code, json.loads(out.getvalue()) if code == 0 else out.getvalue(), err.getvalue()

    def test_a_run_needs_no_task_and_a_given_one_is_named_in_the_prompt(self):
        for extra, want in (
            (("--role", "engineer"), "**Your task**: pick it from your charter's Tasks section"),
            (
                ("--role", "researcher", "--task", "light-research"),
                "**Your task**: `light-research` (`<tasks>/light-research.md`).",
            ),
        ):
            with self.subTest(extra=extra):
                code, shown, _ = self.main(*extra)
                self.assertEqual(code, 0)
                self.assertIn(want, shown["argv"][2].split("\n# Principles\n")[0])

    def test_the_tui_session_is_named_after_the_role_or_the_prefix(self):
        for extra, name in (
            ((), f"engineer-{SID[:8]}"),
            (("--prefix", "p", "--events", "/tmp/ev.log"), f"p-{SID[:8]}"),
        ):
            with self.subTest(extra=extra):
                code, shown, _ = self.main("--role", "engineer", "--runner", "tui", *extra)
                argv = shown["argv"]
                self.assertEqual(
                    (code, argv[0], argv[argv.index("--name") + 1]), (0, "claude", name)
                )
                self.assertFalse(os.path.exists(self.work))

    def test_a_task_off_the_roles_index_exits_2(self):
        code, out, err = self.main("--role", "engineer", "--task", "deep-research")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(
            err,
            "drive.py: task 'deep-research' is not one of engineer's tasks (build, light-build)\n",
        )

    def test_old_keys_in_the_core_local_file_exit_2(self):
        path = os.path.join(hermetic.home(self), "core.local.toml")
        for local in (
            '[roles.engineer.tasks.build]\neffort = "max"\n',
            '[roles.engineer]\ndefault_task = "build"\n',
        ):
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
        self.assertIn(
            "Continue the task this session already picked or was given, following your task file's "
            "`## Resume` section; never pick it again.",
            prompt,
        )
        self.assertIn("**Your task**: pick it from", prompt.split("\n# Principles\n")[0])


STEPS = (
    "\n## Steps\n\n1. **Report progress:**\n   [agent-pm-progress:start] the topic\n2. **Write.** Write it.\n\n"
    "## Resume\n\nRedo step 2.\n"
)
NOTE = "# Note\n" + STEPS
WRITER = (
    "# Writer\n\nYou write notes.\n\n## Tasks\n\n"
    "Pick the task below that fits the input; unsure → `note` (`<tasks>/note.md`).\n\n"
    "- `note` (`<tasks>/note.md`): a short note; always; light.\n"
)
PM_EXT = (
    "## Tasks\n\n- `one-pager` (`<tasks>/one-pager.md`): a one-page brief; when asked; light.\n"
)
TEAM = {"roles/writer.md": WRITER, "tasks/note.md": NOTE}
TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")


class TeamRuns(Base):
    def setUp(self):
        super().setUp()
        self.home = hermetic.home(self)
        self.top = os.path.dirname(self.home)
        self.team = self.team_dir(TEAM)
        self.local()

    def team_dir(self, files, name="team"):
        top = os.path.realpath(os.path.join(self.home, name))
        for rel, text in files.items():
            path = os.path.join(top, rel)
            for d in (top, os.path.dirname(path)):
                os.makedirs(d, exist_ok=True)
                os.chmod(d, 0o755)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o644)
        return top

    def path(self, rel):
        return os.path.join(self.team, rel)

    def local(self, text="", dirs=None):
        dirs = [self.team] if dirs is None else dirs
        with open(os.path.join(self.home, "core.local.toml"), "w") as f:
            f.write(f"team_dirs = {json.dumps(dirs)}\n" + text)

    def main(self, *extra, dry=True, temp=(), root=CORE, result=None):
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--input",
            "Write X.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
            "--sid",
            SID,
            *(["--dry-run"] if dry else []),
            *extra,
        ]
        result = result or drive.Result(0, drive.Outcome("done", "t", "s", deliverable="x"))
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            unittest.mock.patch.object(drive, "start", return_value=result) as start,
        ):
            code = drive.main(argv, root=root, temp=temp)
        shown = json.loads(out.getvalue()) if code == 0 and dry else out.getvalue()
        return code, shown, err.getvalue(), start.call_args_list

    def refused(self, *extra, want, **kw):
        code, out, err, calls = self.main(*extra, **kw)
        self.assertEqual((code, out, calls), (2, "", []), err)
        self.assertIn(want, err)
        return err

    def listed(self, *argv, temp=()):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = drive.main(["--list", *argv], root=CORE, temp=temp)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def dirs(argv):
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]

    def test_a_custom_role_reads_its_tasks_from_its_team_dirs_tasks_dir(self):
        code, shown, err, _ = self.main("--role", "writer", "--task", "note")
        self.assertEqual(code, 0, err)
        prompt = shown["argv"][2]
        self.assertIn("\n# Writer\n\nYou write notes.\n", prompt)
        self.assertIn("\n- `note` (`<tasks>/note.md`): a short note; always; light.\n", prompt)
        self.assertEqual(TASKS_PARAM.search(prompt)[1], self.path("tasks"))
        self.assertIn(self.path("tasks"), self.dirs(shown["argv"]))
        self.assertNotIn(TASKS, self.dirs(shown["argv"]))
        self.assertFalse(os.path.exists(self.work))
        os.makedirs(self.work)
        for extra in ((), ("--resume",)):
            code, _, err, calls = self.main("--role", "writer", *extra, dry=False)
            self.assertEqual((code, len(calls)), (0, 1), err)
            self.assertIn(self.path("tasks"), calls[0].args[0].argv)
        self.assertEqual(os.listdir(self.work), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, "views")))

    def test_an_untouched_role_runs_as_without_team_dirs(self):
        for role in ("researcher", "engineer", "dummy-tester"):
            with self.subTest(role=role):
                self.local()
                code, shown, err, _ = self.main("--role", role)
                self.assertEqual(code, 0, err)
                self.assertEqual(TASKS_PARAM.search(shown["argv"][2])[1], TASKS)
                self.assertIn(TASKS, self.dirs(shown["argv"]))
                self.local(dirs=[])
                self.assertEqual(self.main("--role", role)[1], shown)

    def test_a_role_table_without_a_charter_stops_only_that_role(self):
        self.team = self.team_dir(
            {"roles/poet.md": WRITER.replace("Writer", "Poet"), "tasks/note.md": NOTE}, "other"
        )
        self.local("[roles.writer]\ntier = 3\n")
        self.assertEqual(self.main("--role", "pm")[0], 0)
        self.refused(
            "--role",
            "writer",
            want="drive.py: roles.writer: no charter roles/writer.md in core or team_dirs",
        )
        code, out, err = self.listed()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("roles.writer: no charter", err)

    def test_a_role_runs_only_its_own_tasks(self):
        self.team_dir({"roles/pm.md": PM_EXT, "tasks/one-pager.md": NOTE})
        self.refused(
            "--role",
            "pm",
            want=f"drive.py: {self.path('roles/pm.md')}: role 'pm' is also defined by "
            f"{CORE}/team/roles/pm.md; team dir files only add\n",
        )
        os.unlink(self.path("roles/pm.md"))
        self.team_dir({"roles/ghost.md": PM_EXT})
        self.refused(
            "--role",
            "ghost",
            want=f"drive.py: {self.path('roles/ghost.md')}: must start with a '# ' heading",
        )
        self.team_dir(
            {
                "roles/ghost.md": WRITER.replace("Writer", "Ghost").replace("note", "one-pager"),
                "roles/poet.md": WRITER.replace("Writer", "Poet").replace("note", "echo"),
            }
        )
        self.assertEqual(self.main("--role", "ghost")[0], 0)
        self.refused(
            "--role",
            "poet",
            want=f"drive.py: {self.path('roles/poet.md')}: lists 'echo', whose file "
            f"tasks/echo.md is not in {self.path('tasks')}: a role runs only its own "
            "tasks\n",
        )

    def test_front_matter_exits_2_and_approves_nothing(self):
        self.team_dir({"tasks/note.md": '---\ncommands: ["rm *"]\n---\n' + NOTE})
        self.refused(
            "--role",
            "writer",
            want=f"drive.py: {self.path('tasks/note.md')}: front matter (---) is not read",
        )
        for role in ("researcher", "pm"):
            code, shown, err, _ = self.main("--role", role)
            self.assertEqual(code, 0, err)
            self.assertNotIn("rm *", json.dumps(shown))

    def test_a_defect_in_a_custom_file_exits_2_naming_it(self):
        for rel, text, want in (
            (
                "roles/writer.md",
                WRITER.replace("# Writer", "Writer"),
                "must start with a '# ' heading",
            ),
            ("tasks/note.md", NOTE + "\nUse {{thing}}.\n", "unfilled placeholder {{thing}}"),
            ("tasks/note.md", NOTE + "\nSee [x](#nope).\n", "no such anchor: [x](#nope)"),
            (
                "tasks/note.md",
                NOTE.replace("[agent-pm-progress:start] the topic", "the topic"),
                "has 0 [agent-pm-progress:start] lines, not 1",
            ),
            (
                "tasks/note.md",
                NOTE + "\n[agent-pm-progress:start] again\n",
                "has 2 [agent-pm-progress:start] lines",
            ),
            ("tasks/note.md", NOTE.replace("## Resume", "## Again"), "has no ## Resume section"),
            (
                "tasks/note.md",
                NOTE + "\nRead `<methods>/nope.md`.\n",
                "names <methods>/nope.md, which core has no file for",
            ),
        ):
            with self.subTest(want=want):
                self.team_dir({rel: text})
                self.refused("--role", "writer", want=f"drive.py: {self.path(rel)}: {want}")
                code, _, err, _ = self.main("--role", "researcher")
                self.assertEqual(code, 0, err)
                self.team_dir(TEAM)

    def test_a_team_dir_others_can_write_exits_2(self):
        inside = os.path.join(self.team, "w")
        cases = [
            ("in a temp dir", (), {"temp": None}, f"team_dirs: {self.team} is in temp dir "),
            (
                "holding the workdir",
                ("--workdir", inside),
                {},
                f"team_dirs: {self.team} overlaps the workdir {inside}: "
                "the agent run can write there",
            ),
            (
                "in the workdir",
                ("--workdir", self.home),
                {},
                f"team_dirs: {self.team} overlaps the workdir {self.home}",
            ),
            (
                "in the cwd",
                (),
                {"local": f'cwd = "{self.top}"\n'},
                f"team_dirs: {self.team} overlaps the cwd {self.top}",
            ),
            (
                "holding a write dir",
                (),
                {"local": f'[roles.pm]\nwrite = ["{inside}"]\n'},
                f"team_dirs: {self.team} overlaps write dir {inside} {inside}",
            ),
            (
                "in a write dir",
                (),
                {"local": f'[roles.pm]\nwrite = ["{self.top}"]\n'},
                f"team_dirs: {self.team} overlaps write dir {self.top} {self.top}",
            ),
            (
                "a group-writable file",
                (),
                {"chmod": ("tasks/note.md", 0o664)},
                f"team_dirs: {self.path('tasks/note.md')} is writable by group or others: chmod go-w it",
            ),
            (
                "in a read dir",
                (),
                {"local": f'[roles.pm]\nread = ["{self.top}"]\n'},
                f"team_dirs: {self.team} overlaps a dir ClaudeClient adds {self.top}: the agent run can write there",
            ),
            (
                "holding a read dir",
                (),
                {"local": f'[roles.writer]\nread = ["{inside}"]\n'},
                f"team_dirs: {self.team} overlaps a dir ClaudeClient adds {inside}",
            ),
            (
                "an others-writable tasks/",
                (),
                {"chmod": ("tasks", 0o757)},
                f"team_dirs: {self.path('tasks')} is writable by group or others",
            ),
        ]
        for name, extra, kw, want in cases:
            with self.subTest(name):
                self.local(kw.get("local", ""))
                if "chmod" in kw:
                    os.chmod(self.path(kw["chmod"][0]), kw["chmod"][1])
                role = "writer" if "roles.writer" in kw.get("local", "") else "pm"
                self.refused(
                    "--role", role, *extra, want=f"drive.py: {want}", temp=kw.get("temp", ())
                )
                if "chmod" in kw:
                    os.chmod(
                        self.path(kw["chmod"][0]), 0o755 if kw["chmod"][0] == "tasks" else 0o644
                    )
        self.local()
        self.assertEqual(self.main("--role", "pm")[0], 0)

    def test_a_team_dirs_parent_others_could_replace_it_from_exits_2(self):
        parent = os.path.dirname(self.team)
        os.chmod(parent, 0o775)
        self.refused(
            "--role",
            "pm",
            want=f"drive.py: team_dirs: {self.team}'s parent dir {parent} is writable by group "
            "or others: they could replace the team dir\n",
        )
        os.chmod(parent, 0o1777)
        self.assertEqual(self.main("--role", "pm")[0], 0)
        os.chmod(parent, 0o755)
        real = os.stat

        def stat_(path, *a, **kw):
            st = real(path, *a, **kw)
            return os.stat_result((*st[:4], 4242, *st[5:])) if os.fspath(path) == parent else st

        with unittest.mock.patch.object(os, "stat", stat_):
            self.refused(
                "--role",
                "pm",
                want=f"drive.py: team_dirs: {self.team}'s parent dir {parent} is owned by uid "
                "4242, not you or root: they could replace the team dir\n",
            )
        self.assertEqual(self.main("--role", "pm")[0], 0)

    def test_a_symlink_in_a_team_dir_is_checked_by_its_real_path(self):
        elsewhere = os.path.realpath(os.path.join(self.tmp.name, "elsewhere"))
        os.makedirs(elsewhere)
        link = self.path("tasks/note.md")
        os.rename(link, os.path.join(elsewhere, "note.md"))
        os.symlink(os.path.join(elsewhere, "note.md"), link)
        self.assertEqual(self.main("--role", "pm")[0], 0)
        self.refused(
            "--role",
            "pm",
            temp=[elsewhere],
            want=f"drive.py: team_dirs: {link} ({elsewhere}/note.md) is in temp dir {elsewhere}: any "
            "process can write there\n",
        )
        tasks, moved = self.path("tasks"), os.path.join(os.path.realpath(self.work), "team-tasks")
        os.makedirs(self.work)
        os.rename(tasks, moved)
        os.symlink(moved, tasks)
        self.refused(
            "--role",
            "pm",
            want=f"drive.py: team_dirs: {tasks} ({moved}) overlaps the workdir "
            f"{os.path.realpath(self.work)}: the agent run can write there\n",
        )
        os.chmod(os.path.join(elsewhere, "note.md"), 0o666)
        self.refused(
            "--role",
            "pm",
            "--workdir",
            os.path.join(self.tmp.name, "w2"),
            want=f"drive.py: team_dirs: {link} ({elsewhere}/note.md) is writable by group or others",
        )

    def test_a_team_dir_owned_by_another_user_exits_2(self):
        me = os.getuid()
        with unittest.mock.patch.object(os, "getuid", return_value=me + 1):
            self.refused(
                "--role",
                "pm",
                want=f"drive.py: team_dirs: {self.team} is owned by uid {me}, not you "
                f"(uid {me + 1}): chown it\n",
            )
            code, out, err = self.listed()
            self.assertEqual((code, out), (2, ""))
            self.assertIn(f"{self.team} is owned by uid {me}", err)
        self.assertEqual(self.main("--role", "pm")[0], 0)

    def test_an_orchestrator_run_is_refused_only_when_it_could_write_a_team_dir(self):
        inside = os.path.join(self.team, "w")
        os.makedirs(self.work)
        base = drive.plan(
            CORE, clients.get("fake", CORE), "pm", None, params=self.params(), cwd=self.work
        )
        self.local(dirs=[])
        self.assertEqual(
            drive.plan(
                CORE, clients.get("fake", CORE), "pm", None, params=self.params(), cwd=self.work
            ),
            base,
        )
        self.local()
        with self.assertRaises(compose.ConfigError) as cm:
            drive.plan(
                CORE,
                clients.get("fake", CORE),
                "pm",
                None,
                params=self.params(workdir=inside),
                cwd=self.work,
            )
        self.assertEqual(
            str(cm.exception),
            f"team_dirs: {self.team} overlaps the workdir {inside}: the agent run can write there",
        )
        tasks, moved = self.path("tasks"), os.path.join(os.path.realpath(self.work), "team-tasks")
        os.rename(tasks, moved)
        os.symlink(moved, tasks)
        with self.assertRaises(compose.ConfigError) as cm:
            drive.plan(
                CORE, clients.get("fake", CORE), "pm", None, params=self.params(), cwd=self.work
            )
        self.assertEqual(
            str(cm.exception),
            f"team_dirs: {tasks} ({moved}) overlaps the workdir "
            f"{os.path.realpath(self.work)}: the agent run can write there",
        )
        os.unlink(tasks)
        os.rename(moved, tasks)
        with open(os.path.join(self.home, "core.local.toml"), "w") as f:
            f.write('team_dirs = "x"\n')
        self.assertEqual(
            drive.plan(
                CORE, clients.get("fake", CORE), "pm", None, params=self.params(), cwd=self.work
            ),
            base,
        )

    def test_list_abbreviated_is_list(self):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            self.assertEqual(drive.main(["--li"], root=CORE, temp=()), 0)
        self.assertEqual(out.getvalue(), self.listed()[1])
        for argv, extra in (
            (["--li", "--role", "pm"], "--role"),
            (["--list", "--rol", "pm"], "--rol"),
            (["--li", "--cl", "skill", "--dry-run"], "--dry-run"),
        ):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()) as err,
                self.assertRaises(SystemExit) as cm,
            ):
                drive.main(argv, root=CORE, temp=())
            self.assertEqual(cm.exception.code, 2)
            self.assertIn(f"--list takes only --client, not {extra}", err.getvalue())

    def test_a_misnamed_file_a_run_names_exits_2_naming_it(self):
        self.team_dir({"roles/Poet.md": WRITER.replace("Writer", "Poet")})
        self.refused(
            "--role",
            "Poet",
            want=f"drive.py: {self.path('roles/Poet.md')}: name 'Poet': want [a-z0-9-]+\n",
        )
        self.assertEqual(self.main("--role", "writer")[0], 0)

    def test_a_bad_team_dirs_exits_2(self):
        for value, want in (
            ('"x"', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
            ('["rel"]', "team_dirs: 'rel' is not an absolute or ~ path"),
            ('["~/missing"]', "team_dirs: ~/missing is not a directory"),
            (
                json.dumps([self.team, "~/.agent-pm/team"]),
                "team_dirs: ~/.agent-pm/team is listed twice",
            ),
        ):
            with self.subTest(value=value):
                with open(os.path.join(self.home, "core.local.toml"), "w") as f:
                    f.write(f"team_dirs = {value}\n")
                self.refused("--role", "pm", want=f"drive.py: {want}\n")
        self.local(f"[clients.claude]\nteam_dirs = {json.dumps([self.team])}\n", dirs=[])
        self.refused("--role", "pm", want="unknown key 'team_dirs'")
        root = os.path.join(self.tmp.name, "core")
        shutil.copytree(CORE, root, ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(root, "config.toml")) as f:
            text = f.read()
        with open(os.path.join(root, "config.toml"), "w") as f:
            f.write(f"team_dirs = {json.dumps([self.team])}\n" + text)
        self.local(dirs=[])
        self.refused(
            "--role",
            "pm",
            root=root,
            want="drive.py: team_dirs: set it in ~/.agent-pm/core.local.toml, not config.toml\n",
        )

    def test_list_prints_each_role_and_task_with_its_source(self):
        code, out, err = self.listed()
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        heads = [line for line in lines if not line.startswith("  ")]
        self.assertEqual(
            heads,
            [
                "researcher  github  built-in",
                "pm  github  built-in",
                "engineer  pull-request  built-in",
                "dummy-tester  local  built-in",
                f"writer  local  {self.path('roles/writer.md')}",
            ],
        )
        self.assertIn("\npm  github  built-in\n  product-design: ", out)
        self.assertTrue(
            out.endswith(
                f"\nwriter  local  {self.path('roles/writer.md')}\n"
                f"  note: a short note; always; light.  [{self.path('tasks/note.md')}]\n"
            )
        )
        self.assertTrue(
            all(
                line.endswith("  [built-in]") or self.team in line
                for line in lines
                if line[:2] == "  "
            )
        )
        self.assertNotIn("<tasks>", out)

    def test_list_stops_at_any_defect_and_takes_no_other_option(self):
        self.team_dir({"tasks/echo.md": NOTE})
        code, out, err = self.listed()
        self.assertEqual((code, out), (2, ""))
        self.assertIn(
            f"drive.py: {self.path('tasks/echo.md')}: task 'echo' is also defined by ", err
        )
        self.assertEqual(self.listed(temp=None)[0], 2)
        for argv in (["--list", "--role", "pm"], []):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()) as err,
                self.assertRaises(SystemExit) as cm,
            ):
                drive.main(argv, root=CORE, temp=())
            self.assertEqual(cm.exception.code, 2)
            self.assertIn(
                "--list takes only --client, not --role"
                if argv
                else "the following arguments are required: --role",
                err.getvalue(),
            )

    def test_list_shows_the_selected_clients_destinations(self):
        claude = self.listed()[1]
        for argv in (("--client", "skill"), ("--client=skill",), ("--cl", "skill")):
            with self.subTest(argv=argv):
                code, out, err = self.listed(*argv)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(
                    [line for line in out.splitlines() if not line.startswith("  ")],
                    [
                        "researcher  orchestrator  built-in",
                        "pm  orchestrator  built-in",
                        "engineer  pull-request  built-in",
                        "dummy-tester  orchestrator  built-in",
                        f"writer  local  {self.path('roles/writer.md')}",
                    ],
                )
                self.assertEqual(
                    [line for line in out.splitlines() if line.startswith("  ")],
                    [line for line in claude.splitlines() if line.startswith("  ")],
                )
        self.assertEqual(self.listed("--client", "codex"), (0, claude, ""))
        code, out, err = self.listed("--client", "nope")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("client 'nope' has no module clients.nope", err)

    def test_a_codex_run_adds_its_tasks_dir_and_can_write_no_other_team_dir(self):
        os.makedirs(self.work)
        self.local(f'cwd = "{self.work}"\n')
        code, shown, err, _ = self.main("--client", "codex", "--role", "writer", "--task", "note")
        self.assertEqual(code, 0, err)
        self.assertEqual(TASKS_PARAM.search(shown["argv"][-1])[1], self.path("tasks"))
        self.assertIn(self.path("tasks"), self.dirs(shown["argv"]))
        self.assertNotIn(TASKS, self.dirs(shown["argv"]))
        self.local(f'cwd = "{self.work}"\n[roles.writer]\nread = ["{self.team}"]\n')
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                name = {"claude": "ClaudeClient", "codex": "CodexClient"}[client]
                self.refused(
                    "--client",
                    client,
                    "--role",
                    "writer",
                    want=f"drive.py: team_dirs: {self.team} overlaps a dir {name} adds {self.team}: the "
                    "agent run can write there\n",
                )

    def test_the_skill_client_names_the_roles_own_tasks_dir(self):
        for role, tasks in (("writer", self.path("tasks")), ("researcher", TASKS)):
            for extra in ((), ("--dry-run",)):
                with self.subTest(role=role, extra=extra):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = drive.main(
                            ["--client", "skill", "--role", role, *extra], root=CORE, temp=()
                        )
                    self.assertEqual(code, 0, err.getvalue())
                    self.assertEqual(TASKS_PARAM.search(out.getvalue())[1], tasks)
        self.assertFalse(os.path.exists(os.path.join(self.home, "views")))


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
    return (
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        + "\n"
    )


def progress(name, text):
    return {"kind": "progress", "name": name, "text": text}


def outcome(data):
    return {"kind": "outcome", "outcome": data}


def feed(channel, items):
    for item in items:
        if isinstance(item, dict):
            drive.append_line(channel, json.dumps(item, ensure_ascii=False) + "\n")
        else:
            yield item


DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
STOP = {"kind": "stop"}
PENDING = {"kind": "stop", "pending": 2}


class FakeTui:
    def __init__(self, channel, steps):
        self.channel, self.steps, self.calls, self.now = channel, list(steps), [], 0

    def patch(self, **api):
        stack = ExitStack()
        stack.enter_context(
            unittest.mock.patch.multiple(
                drive.tmux_host,
                **{
                    "start": self.start,
                    "status": self.status,
                    "kill": self.kill,
                    "send": self.send,
                    **api,
                },
            )
        )
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
        return drive.tmux_host.RUNNING

    def kill(self, name):
        self.calls.append(("kill", name))

    def send(self, name, text):
        self.calls.append(("send", name, text))


class ClaudeEvents(unittest.TestCase):
    def kinds(self, *lines):
        return [(e.kind, e.text) for e in claude().events(lines)]

    def test_assistant_text_lines_are_text(self):
        lines = [said("Starting.\n[agent-pm-progress:start] 2 rounds\n\n  \nDone.")]
        self.assertEqual(
            self.kinds("not json\n", *lines),
            [
                ("text", "not json"),
                ("text", "Starting."),
                ("text", "[agent-pm-progress:start] 2 rounds"),
                ("text", "Done."),
            ],
        )

    def test_results_and_odd_json_are_skipped(self):
        lines = [
            "null\n",
            "3\n",
            "[]\n",
            '"text"\n',
            *stream(
                {"type": "assistant"},
                {"type": "assistant", "message": {"content": "x"}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash"}, {"type": "thinking"}, 7]
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "bye",
                    "structured_output": DONE,
                },
            ),
        ]
        self.assertEqual(self.kinds(*lines), [])


class Handover(unittest.TestCase):
    def test_each_client_names_the_progress_mark(self):
        for c in (claude(), SkillClient({})):
            self.assertIn(f"[{clients.PROGRESS}:<name>]", c.handover(), type(c).__name__)
        self.assertIn(
            "before calling the next tool, send a text message containing only that line",
            SkillClient({}).handover(),
        )
        text = claude().handover()
        self.assertNotIn("ultracode", text)
        self.assertNotIn("progress:budget", text)
        self.assertIn("`report progress <name>", text)
        self.assertIn("`report outcome --status", text)
        self.assertIn("last action", text)
        self.assertNotIn("structured output", text)

    def test_the_base_client_returns_no_outcome(self):
        self.assertEqual(clients.Client({}).handover(), "")
        self.assertEqual(
            [e.kind for e in clients.Client({}).events(['{"type": "result"}\n'])], ["text"]
        )


class Schema(unittest.TestCase):
    def test_schema_and_validate_agree(self):
        schema = compose.outcome_schema(CORE)
        self.assertEqual(tuple(schema["properties"]["status"]["enum"]), drive.STATUSES)
        self.assertEqual(
            set(schema["properties"]), {f.name for f in dataclasses.fields(drive.Outcome)}
        )
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
        return [{k: v for k, v in line.items() if k != "ts"} for line in self.raw()]

    def stop_line(self, stdin, *argv):
        before = len(self.lines())
        with unittest.mock.patch("sys.stdin", stdin):
            self.assertEqual(self.report("stop", *argv), (0, "report.py: stop reported\n"))
        lines = self.lines()
        self.assertEqual(len(lines), before + 1)
        return lines[-1]

    def test_stop_pending_counts_the_list_at_key_on_stdin_else_is_a_plain_stop(self):
        undecodable = io.TextIOWrapper(
            io.BytesIO(b'{"background_tasks": [\xff\xfe]}'), encoding="utf-8"
        )
        for name, stdin, want in (
            (
                "2",
                io.StringIO(json.dumps({"background_tasks": [{}, {}]})),
                {"kind": "stop", "pending": 2},
            ),
            (
                "0",
                io.StringIO(json.dumps({"background_tasks": []})),
                {"kind": "stop", "pending": 0},
            ),
            ("empty", io.StringIO(""), STOP),
            ("not json", io.StringIO("not json"), STOP),
            ("a list", io.StringIO("[1]"), STOP),
            ("no key", io.StringIO("{}"), STOP),
            ("no list", io.StringIO('{"background_tasks": 3}'), STOP),
            ("undecodable", undecodable, STOP),
        ):
            with self.subTest(name):
                self.assertEqual(self.stop_line(stdin, "--pending", "background_tasks"), want)

    def test_stop_without_pending_reads_no_stdin(self):
        class Unreadable:
            def read(self, *args):
                raise AssertionError("stdin was read")

        self.assertEqual(self.stop_line(Unreadable()), STOP)

    def test_report_event_maps_a_stops_pending_count(self):
        self.assertEqual(drive.report_event('{"kind": "stop"}'), clients.Event("stop"))
        self.assertEqual(
            drive.report_event('{"kind": "stop", "pending": 2}'), clients.Event("stop", pending=2)
        )
        self.assertEqual(
            drive.report_event('{"kind": "stop", "pending": 0}'), clients.Event("stop")
        )
        for pending in ("true", "-1", '"2"', "1.5"):
            with self.subTest(pending=pending):
                self.assertEqual(
                    drive.report_event(f'{{"kind": "stop", "pending": {pending}}}'),
                    clients.Event("stop"),
                )

    def test_every_line_starts_with_an_iso_ts_with_offset_then_the_kind(self):
        doc = os.path.join(self.work, "doc.md")
        with open(doc, "w") as f:
            f.write("# Doc\n")
        self.assertEqual(self.report("progress", "start", "go")[0], 0)
        self.assertEqual(
            self.report(
                "outcome",
                "--status",
                "done",
                "--title",
                "T",
                "--summary",
                "S",
                "--deliverable",
                doc,
            )[0],
            0,
        )
        with unittest.mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(self.report("stop")[0], 0)
        lines = self.raw()
        self.assertEqual([list(line)[:2] for line in lines], [["ts", "kind"]] * 3)
        self.assertEqual([line["kind"] for line in lines], ["progress", "outcome", "stop"])
        for line in lines:
            self.assertIsNotNone(datetime.fromisoformat(line["ts"]).utcoffset(), line["ts"])
        self.assertEqual(lines[1]["outcome"]["deliverable"], "# Doc\n")

    def test_progress_appends_one_line(self):
        self.assertEqual(
            self.report("progress", "round-1", "2", "rounds,", "cap 80"),
            (0, "report.py: progress reported\n"),
        )
        self.assertEqual(self.report("progress", "start", "-x go")[0], 0)
        self.assertEqual(
            self.lines(), [progress("round-1", "2 rounds, cap 80"), progress("start", "-x go")]
        )

    def test_outcome_from_arguments_the_deliverable_from_its_file_questions_and_files_repeatable(
        self,
    ):
        doc = os.path.join(self.work, "doc.md")
        with open(doc, "w") as f:
            f.write("# Doc\n")
        for argv, want in (
            (("--status", "done", "--title", "T", "--summary", "S", "--deliverable", doc), DONE),
            (
                (
                    "--status",
                    "needs_input",
                    "--title",
                    "T",
                    "--summary",
                    "- a\n- b",
                    "--question",
                    "Which repo?",
                    "--question",
                    "- Why?",
                    "--file",
                    "a.md",
                    "--file",
                    "b.md",
                    "--url",
                    "https://x",
                ),
                {
                    "status": "needs_input",
                    "title": "T",
                    "summary": "- a\n- b",
                    "questions": ["Which repo?", "- Why?"],
                    "url": "https://x",
                    "files": ["a.md", "b.md"],
                },
            ),
        ):
            with self.subTest(status=want["status"]):
                before = self.lines()
                code, out = self.report("outcome", *argv)
                self.assertEqual((code, self.lines()), (0, [*before, outcome(want)]))
                self.assertIn("outcome reported", out)

    def test_bad_arguments_append_nothing(self):
        base = ["outcome", "--title", "T", "--summary", "S"]
        for argv in (
            base + ["--status", "maybe"],
            ["outcome", "--status", "done"],
            ["note", "x"],
            [],
            base + ["--status", "done", "--deliverable", os.path.join(self.work, "no.md")],
        ):
            self.assertNotEqual(self.report(*argv)[0], 0, argv)
        self.assertEqual(self.lines(), [])

    def test_a_deliverable_that_is_not_utf8_is_an_error_line(self):
        doc = os.path.join(self.work, "doc.md")
        with open(doc, "wb") as f:
            f.write(b"\xff\xfe")
        code, _ = self.report(
            "outcome", "--status", "done", "--title", "T", "--summary", "S", "--deliverable", doc
        )
        self.assertEqual((code, self.lines()), (1, []))

    def test_a_fifo_or_symlinked_channel_is_refused_not_waited_on_or_followed(self):
        target = os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")
        for name, plant in (
            ("fifo", os.mkfifo),
            ("symlink", lambda path: os.symlink(target, path)),
        ):
            with self.subTest(name):
                plant(self.channel)
                self.assertEqual(self.report("progress", "x", "y")[0], 1)
                os.remove(self.channel)
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
        self.assertEqual(
            self.check({**DONE, "title": "  T  "}),
            drive.Outcome("done", "T", "S", deliverable="# Doc\n"),
        )

    def test_the_shape_schema_and_title_hold(self):
        for data, msg in (
            ([DONE], "not an object"),
            ({**DONE, "status": "maybe"}, "status 'maybe'"),
            ({**DONE, "summary": 3}, "must be text"),
            ({**DONE, "deliverable": ["x"]}, "must be text"),
            ({**DONE, "files": "x.md"}, "must be lists"),
            ({**DONE, "extra": 1}, "unknown field 'extra'"),
            ({**DONE, "title": "x" * 201}, "title is longer than 200"),
            ({**DONE, "summary": "x" * 4001}, "summary is longer than 4000"),
            ({**DONE, "status": "needs_input", "questions": [3]}, "questions[0] must be text"),
            ({**DONE, "files": None}, "files must be a list"),
            ({**DONE, "url": None}, "url must be text"),
            *(
                ({**DONE, "title": title}, "printable")
                for title in ("", "  ", "a\nb", "a\x1b[2Jb", 5)
            ),
        ):
            with self.subTest(msg=msg, data=data):
                self.fails(data, msg)

    def test_questions(self):
        ask = {**DONE, "status": "needs_input"}
        self.fails(ask, "1–4 questions")
        self.fails({**ask, "questions": ["q"] * 5}, "1–4 questions")
        self.assertEqual(self.check({**ask, "questions": ["q?"]}).questions, ["q?"])
        self.assertEqual(self.check({**DONE, "questions": ["q"]}).questions, [])

    def test_done_needs_the_deliverable_when_it_comes_back(self):
        for output in ({"type": "local"}, {"type": "orchestrator"}):
            self.fails({**DONE, "deliverable": " "}, "must carry the deliverable", output)
        self.assertEqual(
            self.check({**DONE, "deliverable": ""}, {"type": "pull-request"}).deliverable, ""
        )
        self.assertEqual(
            self.check({**DONE, "status": "failed", "deliverable": ""}).status, "failed"
        )

    def test_url_must_fit_the_destination(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        ok = "https://github.com/o/docs/blob/main/R/x.md"
        self.assertEqual(self.check({**DONE, "url": ok}, gh).url, ok)
        for bad in (
            "https://evil.example/o/docs/blob/main/x.md",
            "https://github.com/o/docs/blob/dev/x.md",
        ):
            self.fails({**DONE, "url": bad}, "url must start with", gh)
        for bad in (
            ok + "/../../../../x/y",
            "https://github.com/o/docs/blob/main/%2e%2e/%2e%2e/x",
            ok + "\n)[x](y",
            ok + " x",
            "http://github.com/o/docs/blob/main/x.md",
            5,
        ):
            self.fails({**DONE, "url": bad}, "plain https link", gh)
        pr = {"type": "pull-request"}
        for good in (
            "https://github.com/o/n/pull/7",
            "https://ghe.io/o/n/compare/main...b?expand=1",
            "https://github.com/o/n/tree/b",
        ):
            self.assertEqual(self.check({**DONE, "url": good}, pr).url, good)
        self.fails(
            {**DONE, "url": "https://github.com/o/n/issues/1"}, "pull request, compare or tree", pr
        )
        self.assertEqual(self.check({**DONE, "url": "https://x"}).url, "")

    def test_files_are_single_link_md_files_under_the_workdir(self):
        plan = self.touch("docs", "plan.md")
        self.assertEqual(
            self.check({**DONE, "files": [plan, "docs/plan.md"]}).files,
            [os.path.realpath(plan)] * 2,
        )
        os.makedirs(self.work + "2")
        sibling = os.path.join(self.work + "2", "x.md")
        with open(sibling, "w") as f:
            f.write("x")
        outside = os.path.join(self.tmp.name, "secret.md")
        with open(outside, "w") as f:
            f.write("key")
        os.symlink(outside, os.path.join(self.work, "link.md"))
        os.link(outside, os.path.join(self.work, "hard.md"))
        for bad in (
            self.touch("notes.txt"),
            os.path.join(self.work, "missing.md"),
            sibling,
            "link.md",
            "hard.md",
            os.path.join(self.work, "..", "secret.md"),
            os.path.expanduser("~/.ssh/id_rsa"),
            7,
        ):
            self.fails({**DONE, "files": [bad]}, "single-link .md file under the workdir")


class Start(Base):
    def setUp(self):
        super().setUp()
        quiet = redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def start(self, items, rc=0, output=None, sinks=None, stderr=(), **params):
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work)
        calls, log = [], io.StringIO()
        p = self.params(**params)

        def popen(argv, **kw):
            calls.append((argv, kw))
            self.proc = FakeProc(feed(p.channel, items), rc, stderr)
            return self.proc

        r = drive.start(
            launch,
            run(output=output or {"type": "local"}),
            p,
            client=claude(),
            popen=popen,
            sinks=sinks if sinks is not None else [drive.terminal(log)],
        )
        return r, calls, log.getvalue()

    def read(self, name):
        with open(os.path.join(self.work, name)) as f:
            return f.read()

    def events(self):
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def last(self, kind):
        return [e for e in self.events() if e["kind"] == kind][-1]

    def exists(self, name):
        return os.path.lexists(os.path.join(self.work, name))

    def test_done_run_saves_outcome_deliverable_and_progress(self):
        r, ((argv, kw),), log = self.start(
            [progress("round", "half way"), said("hi"), outcome(DONE)]
        )
        out = os.path.join(self.work, "out.md")
        self.assertEqual((r.returncode, r.outcome.status, r.outcome.url), (0, "done", out))
        self.assertEqual((argv, kw["cwd"], kw["env"]["FAKE"]), (["fake"], self.work, "1"))
        self.assertEqual(self.read("out.md"), "# Doc\n")
        self.assertEqual(self.last("result")["outcome"]["url"], out)
        self.assertEqual(
            [(e["name"], e["text"]) for e in self.events() if e["kind"] == "progress"],
            [("round", "half way")],
        )
        self.assertEqual(log, "Progress (round): half way\nhi\n")

    def test_the_popen_env_drops_the_parent_session_keys_its_pwd_is_the_cwd_and_its_pipes_are_text(
        self,
    ):
        parent = {
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_JOB_DIR": "/j",
            "CLAUDECODE": "1",
            "PWD": "/stale",
        }
        with unittest.mock.patch.dict(os.environ, parent):
            _, ((_, kw),), _ = self.start([outcome(DONE)])
        self.assertNotIn("CLAUDE_CODE_CHILD_SESSION", kw["env"])
        self.assertNotIn("CLAUDE_JOB_DIR", kw["env"])
        self.assertEqual((kw["env"]["CLAUDECODE"], kw["env"]["FAKE"]), ("1", "1"))
        self.assertEqual((kw["cwd"], kw["env"]["PWD"]), (self.work, self.work))
        self.assertEqual(
            (kw["stdin"], kw["stdout"], kw["stderr"], kw["text"], kw["errors"]),
            (subprocess.DEVNULL, subprocess.PIPE, subprocess.PIPE, True, "replace"),
        )

    def test_headless_ignores_stops_and_they_reach_no_sink(self):
        seen = []
        with unittest.mock.patch.object(drive.tmux_host, "send") as send:
            r, _, _ = self.start(
                [
                    *[STOP] * (drive.STOP_LIMIT + 1),
                    progress("start", "x"),
                    STOP,
                    said("still going"),
                    outcome(DONE),
                ],
                sinks=[lambda e: seen.append(e.kind)],
            )
        self.assertEqual((r.returncode, r.outcome.status, send.called), (0, "done", False))
        self.assertEqual(seen, ["progress", "text", "outcome"])

    def test_progress_arrives_while_stdout_is_quiet(self):
        got = threading.Event()

        def quiet():
            drive.append_line(
                os.path.join(self.work, "run.jsonl"), json.dumps(progress("start", "go")) + "\n"
            )
            self.assertTrue(got.wait(5), "no progress event while stdout was quiet")
            yield said("after")

        seen = []

        def sink(event):
            seen.append(event.kind)
            if event.kind == "progress":
                got.set()

        launch = drive.Launch(["fake"], cwd=self.work)
        with unittest.mock.patch.object(drive, "POLL", 0.01):
            drive.start(
                launch,
                run(),
                self.params(),
                client=claude(),
                sinks=[sink],
                popen=lambda argv, **kw: FakeProc(quiet()),
            )
        self.assertEqual(seen, ["progress", "text"])

    def test_stderr_lines_are_events_and_a_nonzero_exit_is_the_error(self):
        seen = []

        def popen(argv, **kw):
            return FakeProc([], 1, stderr=["boom\n", "\n", "bang\n"])

        launch = drive.Launch(["fake"], cwd=self.work)
        r = drive.start(
            launch, run(), self.params(), client=claude(), popen=popen, sinks=[seen.append]
        )
        self.assertEqual((r.returncode, r.error), (1, "the client exited 1"))
        self.assertEqual(
            [(e.kind, e.text) for e in seen],
            [("stderr", "boom"), ("stderr", ""), ("stderr", "bang")],
        )
        errs = [e for e in self.events() if e["kind"] == "stderr"]
        self.assertEqual([list(e) for e in errs], [["ts", "kind", "text"]] * 3)
        self.assertEqual([e["text"] for e in errs], ["boom", "", "bang"])
        self.assertEqual(self.last("result")["error"], "the client exited 1")

    def test_valid_report_survives_nonzero_exit_with_diagnostic_and_original_rc(self):
        seen = []
        r, _, _ = self.start([progress("start", "go"), outcome(DONE)], rc=7, sinks=[seen.append])
        saved = self.last("result")
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (7, "done", "the client exited 7")
        )
        self.assertEqual(self.read("out.md"), DONE["deliverable"])
        self.assertEqual([e.kind for e in seen], ["progress", "outcome"])
        self.assertEqual(saved["outcome"]["url"], os.path.join(self.work, "out.md"))
        self.assertEqual((saved["error"], self.last("end")["rc"]), (r.error, 7))

    def test_stderr_is_recorded_between_the_reports_it_arrived_with(self):
        r, _, log = self.start(
            [progress("start", "go"), said("hi"), outcome(DONE)], stderr=["warn\n"]
        )
        self.assertEqual(r.returncode, 0)
        kinds = [e["kind"] for e in self.events()]
        self.assertEqual(
            sorted(kinds),
            sorted(["input", "session", "progress", "stderr", "outcome", "result", "end"]),
        )
        self.assertEqual(sorted(log.splitlines()), ["Progress (start): go", "hi", "warn"])

    def test_the_run_ends_only_after_both_streams_do(self):
        out_done = threading.Event()

        def stdout():
            yield said("hi")
            out_done.set()

        def stderr():
            self.assertTrue(out_done.wait(5), "stdout never ended")
            time.sleep(0.1)
            yield "late\n"

        launch = drive.Launch(["fake"], cwd=self.work)
        seen = []
        drive.start(
            launch,
            run(),
            self.params(),
            client=claude(),
            sinks=[seen.append],
            popen=lambda argv, **kw: FakeProc(stdout(), 1, stderr()),
        )
        self.assertEqual([(e.kind, e.text) for e in seen], [("text", "hi"), ("stderr", "late")])
        self.assertEqual([e["text"] for e in self.events() if e["kind"] == "stderr"], ["late"])

    def test_an_error_reading_stderr_stops_the_run(self):
        def stderr():
            yield "x\n"
            raise KeyboardInterrupt

        launch = drive.Launch(["fake"], cwd=self.work)
        with self.assertRaises(KeyboardInterrupt):
            drive.start(
                launch,
                run(),
                self.params(),
                client=claude(),
                sinks=[],
                popen=lambda argv, **kw: FakeProc(iter(()), 0, stderr()),
            )

    def test_the_stderr_record_failing_never_stops_the_run(self):
        real = drive.append_line

        def append_line(path, line):
            if '"kind": "stderr"' in line:
                raise OSError(errno.ENOSPC, "full")
            real(path, line)

        seen = []
        with unittest.mock.patch.object(drive, "append_line", append_line):
            r, _, _ = self.start(
                [progress("start", "go"), outcome(DONE)], sinks=[seen.append], stderr=["warn\n"]
            )
        self.assertEqual(
            (r.returncode, r.outcome.status, sorted(e.kind for e in seen)),
            (0, "done", ["outcome", "progress", "stderr"]),
        )
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
        self.assertEqual(
            (r.error, [e.kind for e in seen]), ("the agent run returned no outcome", ["text"])
        )
        r, _, _ = self.start([outcome({**DONE, "title": "new"})], resume=True)
        self.assertEqual(r.outcome.title, "new")

    def test_the_last_outcome_counts(self):
        r, _, _ = self.start(
            [
                outcome({**DONE, "title": "first"}),
                said("fixing"),
                outcome({**DONE, "title": "real"}),
            ]
        )
        self.assertEqual(r.outcome.title, "real")

    def test_malformed_channel_lines_are_skipped(self):
        seen = []
        bad = [
            {"kind": "progress", "name": "a b", "text": "x"},
            {"kind": "progress", "name": "x", "text": " \n "},
            {"kind": "outcome", "outcome": "done"},
            {"kind": "x"},
        ]
        r, _, _ = self.start([*bad, progress("ok", "yes")], sinks=[seen.append])
        self.assertEqual([(e.kind, e.name) for e in seen], [("progress", "ok")])
        self.assertIsNone(r.outcome)

    def test_a_multi_line_progress_report_becomes_one_line(self):
        seen = []
        self.start([progress("start", "first build\n  of the PRD")], sinks=[seen.append])
        self.assertEqual(
            [(e.name, e.text) for e in seen if e.kind == "progress"],
            [("start", "first build of the PRD")],
        )

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

    def test_a_destination_decides_the_url_and_whether_the_deliverable_is_saved(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        url = "https://github.com/o/docs/blob/main/x.md"
        needs = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}
        for output, data, want_url, saved in (
            ({"type": "orchestrator"}, DONE, "", "# Doc\n"),
            (gh, {**DONE, "url": url}, url, None),
            (None, needs, "", None),
        ):
            with self.subTest(output=output, status=data["status"]):
                if self.exists("out.md"):
                    os.remove(os.path.join(self.work, "out.md"))
                r, _, _ = self.start([outcome(data)], output=output)
                self.assertEqual(r.outcome.url, want_url)
                self.assertEqual(
                    self.read("out.md") if saved else self.exists("out.md"), saved or False
                )
                self.assertEqual(
                    self.last("result")["outcome"]["questions"], data.get("questions", [])
                )

    def test_failed_client_does_not_reuse_a_previous_runs_outcome(self):
        self.start([outcome(DONE)])
        seen = []
        r, _, _ = self.start([], rc=143, sinks=[seen.append])
        self.assertEqual((r.returncode, r.outcome, seen), (143, None, []))
        self.assertFalse(self.exists("out.md"))
        self.assertEqual(self.last("result")["error"], "the client exited 143")

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
                self.assertEqual(
                    (st.st_ino, st.st_mtime_ns), (written[-1].st_ino, written[-1].st_mtime_ns)
                )
                self.assertEqual((save.called, self.read("out.md")), (False, "# Doc\n"))
                self.assertEqual(
                    (r.outcome.url, r.outcome.deliverable),
                    (out if dest == "local" else "", "# Doc\n"),
                )

    def test_a_deliverable_from_another_file_is_saved_to_out(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        other = os.path.join(self.work, "tmp", "deliverable.md")

        def items():
            os.makedirs(os.path.dirname(other))
            with open(other, "w") as f:
                f.write("# Doc\n")
            yield from feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)])

        r, _, _ = self.start(items())
        self.assertEqual(
            [(str(c.args[0]), c.args[1]) for c in save.call_args_list], [(out, "# Doc\n")]
        )
        self.assertEqual(
            (r.outcome.url, self.read("out.md"), self.read("tmp/deliverable.md")),
            (out, "# Doc\n", "# Doc\n"),
        )

    def test_an_out_not_a_regular_file_holding_the_deliverable_is_replaced_never_followed_or_waited_on(
        self,
    ):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        target = os.path.join(self.tmp.name, "zshrc")

        def write(text):
            def plant():
                with open(out, "w") as f:
                    f.write(text)

            return plant

        def link(text):
            def plant():
                with open(target, "w") as f:
                    f.write(text)
                os.symlink(target, out)

            return plant

        cases = [
            (f"differs: {w!r}", write(w), None)
            for w in ("", "# Do", "# Doc", "# Doc\n\n", "# Doc\nmore", "# Dac\n")
        ]
        cases += [
            ("fifo", lambda: os.mkfifo(out), None),
            ("symlink", link("mine"), "mine"),
            ("symlink to the deliverable", link("# Doc\n"), "# Doc\n"),
        ]
        for name, plant, kept in cases:

            def items():
                plant()
                yield from feed(
                    os.path.join(self.work, "run.jsonl"), [progress("round", "x"), outcome(DONE)]
                )

            with self.subTest(name):
                save.reset_mock()
                if os.path.lexists(out):
                    os.remove(out)
                self.start(items())
                self.assertEqual(
                    (save.call_count, stat.S_ISREG(os.lstat(out).st_mode), self.read("out.md")),
                    (1, True, "# Doc\n"),
                )
                self.assertEqual(self.last("result")["outcome"]["status"], "done")
                if kept is not None:
                    with open(target) as f:
                        self.assertEqual(f.read(), kept)

    def test_a_stale_out_never_stands_for_the_deliverable(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        os.makedirs(self.work)
        with open(out, "w") as f:
            f.write("# Doc\n")
        seen = []
        launch = drive.Launch(["fake"], cwd=self.work)
        p = self.params()
        drive.start(
            launch,
            run(),
            p,
            client=claude(),
            sinks=[],
            begun=lambda: seen.append(os.path.lexists(out)),
            popen=lambda argv, **kw: FakeProc(feed(p.channel, [outcome(DONE)])),
        )
        self.assertEqual((seen, save.call_count, self.read("out.md")), ([False], 1, "# Doc\n"))

    def test_a_resume_keeps_the_draft_its_session_wrote_to_out(self):
        save, out = self.spy_save(), os.path.join(self.work, "out.md")
        os.makedirs(self.work)
        with open(out, "w") as f:
            f.write("# Draft\n")
        seen = []
        launch = drive.Launch(["fake"], cwd=self.work)
        p = self.params(resume=True)
        drive.start(
            launch,
            run(),
            p,
            client=claude(),
            sinks=[],
            begun=lambda: seen.append(self.read("out.md")),
            popen=lambda argv, **kw: FakeProc(
                feed(p.channel, [outcome({**DONE, "deliverable": "# Draft\n"})])
            ),
        )
        self.assertEqual(
            (seen, save.call_count, self.read("out.md")), (["# Draft\n"], 0, "# Draft\n")
        )

    def test_a_record_replaced_by_a_symlink_is_not_read_or_followed_and_the_run_still_ends(self):
        target = os.path.join(self.tmp.name, "zshrc")
        planted = json.dumps(outcome(DONE)) + "\n"
        with open(target, "w") as f:
            f.write(planted)

        def items():
            os.remove(os.path.join(self.work, "run.jsonl"))
            os.symlink(target, os.path.join(self.work, "run.jsonl"))
            with open(target, "a") as f:
                f.write(planted)
            yield said("x")

        err = io.StringIO()
        with redirect_stderr(err):
            r, _, _ = self.start(items())
        with open(target) as f:
            self.assertEqual(f.read(), planted * 2)
        self.assertEqual(r, drive.Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(
            len(re.findall(r"^drive\.py: .*run\.jsonl", err.getvalue(), re.M)), 2, err.getvalue()
        )

    def test_a_fifo_or_symlink_at_the_record_stops_the_run_before_it_starts(self):
        os.makedirs(self.work)
        path, target = os.path.join(self.work, "run.jsonl"), os.path.join(self.tmp.name, "zshrc")
        with open(target, "w") as f:
            f.write("mine")
        for name, plant in (("fifo", os.mkfifo), ("symlink", lambda p: os.symlink(target, p))):
            with self.subTest(name):
                plant(path)
                with self.assertRaises(OSError):
                    self.start([outcome(DONE)])
                self.assertFalse(hasattr(self, "proc"))
                os.remove(path)
        with open(target) as f:
            self.assertEqual(f.read(), "mine")

    def test_an_error_reading_stdout_stops_the_run_and_ends_the_record(self):
        def items():
            yield said("x")
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        self.assertTrue(self.proc.killed)
        result, end = self.events()[-2:]
        self.assertEqual(
            self.bare(result), {"kind": "result", "error": "stopped: KeyboardInterrupt"}
        )
        self.assertEqual(self.bare(end), {"kind": "end", "sid": SID, "rc": 1})

    def test_sinks_replace_the_defaults_and_every_sink_gets_every_event(self):
        a, b = [], []
        r, _, log = self.start(
            [progress("start", "one"), said("hi"), outcome(DONE)], sinks=[a.append, b.append]
        )
        self.assertEqual(
            [(e.kind, e.name, e.text) for e in a[:2]],
            [("progress", "start", "one"), ("text", "", "hi")],
        )
        self.assertEqual(
            (a[2].kind, a[2].outcome["url"]), ("outcome", os.path.join(self.work, "out.md"))
        )
        self.assertEqual((len(a), a, log), (3, b, ""))
        self.assertEqual(self.read("out.md"), "# Doc\n")

    def recorded(self, launch, items=(), **params):
        p, seen = self.params(**params), []
        r = drive.start(
            launch,
            run(),
            p,
            client=claude(),
            sinks=[],
            begun=lambda: seen.append(self.events()),
            popen=lambda argv, **kw: FakeProc(feed(p.channel, items)),
        )
        return r, seen[0], self.events()

    @staticmethod
    def bare(event):
        return {k: v for k, v in event.items() if k != "ts"}

    def test_the_record_is_input_session_the_reports_result_and_end(self):
        launch = drive.Launch(
            ["fake"], cwd=self.repo, transcript="/p/x.jsonl", resume="cd x && resume", project=True
        )
        r, before, after = self.recorded(launch, [progress("start", "go"), outcome(DONE)])
        self.assertEqual([e["kind"] for e in before], ["input", "session"])
        self.assertEqual(
            [e["kind"] for e in after], ["input", "session", "progress", "outcome", "result", "end"]
        )
        mine = after[:2] + after[4:]
        self.assertEqual(
            [list(e) for e in mine],
            [
                ["ts", "kind", "text"],
                ["ts", "kind", "sid", "cwd", "project", "transcript", "resume"],
                ["ts", "kind", "outcome"],
                ["ts", "kind", "sid", "rc"],
            ],
        )
        for e in mine:
            self.assertIsNotNone(datetime.fromisoformat(e["ts"]).utcoffset(), e["ts"])
        kept = {k: v for k, v in dataclasses.asdict(r.outcome).items() if k != "deliverable"}
        self.assertEqual(
            [self.bare(e) for e in mine],
            [
                {"kind": "input", "text": "Research X."},
                {
                    "kind": "session",
                    "sid": SID,
                    "cwd": self.repo,
                    "project": True,
                    "transcript": "/p/x.jsonl",
                    "resume": "cd x && resume",
                },
                {"kind": "result", "outcome": kept},
                {"kind": "end", "sid": SID, "rc": 0},
            ],
        )
        self.assertEqual(
            (kept["url"], r.outcome.deliverable), (os.path.join(self.work, "out.md"), "# Doc\n")
        )
        self.assertEqual(sorted(os.listdir(self.work)), ["out.md", "run.jsonl"])
        self.assertEqual(drive.session(self.work, SID), after[1])

    def test_the_input_is_recorded_as_given_and_the_cwd_defaults_to_the_workdir(self):
        _, before, _ = self.recorded(drive.Launch(["fake"]), input="  Answers:\n- B\n")
        self.assertEqual((before[0]["text"], before[1]["cwd"]), ("  Answers:\n- B\n", self.work))

    def test_a_run_without_a_valid_outcome_records_its_error_and_sinks_get_no_outcome(self):
        for items, rc, error, kinds in (
            ([said("bye")], 0, "the agent run returned no outcome", ["text"]),
            ([said("bye")], 143, "the client exited 143", ["text"]),
            ([outcome({**DONE, "status": "maybe"})], 143, "the client exited 143", []),
            ([outcome({**DONE, "status": "maybe"})], 0, "invalid outcome: status 'maybe'", []),
        ):
            with self.subTest(items=items, rc=rc):
                seen = []
                r, _, _ = self.start(items, rc=rc, sinks=[seen.append])
                result, end = self.events()[-2:]
                self.assertEqual(
                    (r.returncode, r.outcome, [e.kind for e in seen]), (rc, None, kinds)
                )
                self.assertIn(error, r.error)
                self.assertEqual(
                    (self.bare(result), self.bare(end)),
                    ({"kind": "result", "error": r.error}, {"kind": "end", "sid": SID, "rc": rc}),
                )

    def test_a_resume_appends_its_input_and_session(self):
        self.recorded(
            drive.Launch(["fake"], cwd=self.repo), [progress("start", "go"), outcome(self.NEEDS)]
        )
        self.recorded(
            drive.Launch(["fake"], cwd=self.repo), [outcome(DONE)], resume=True, input="Use B."
        )
        events = self.events()
        self.assertEqual(
            [e["kind"] for e in events],
            [
                "input",
                "session",
                "progress",
                "outcome",
                "result",
                "end",
                "input",
                "session",
                "outcome",
                "result",
                "end",
            ],
        )
        self.assertEqual(
            [e["text"] for e in events if e["kind"] == "input"], ["Research X.", "Use B."]
        )
        self.assertEqual(
            [e["outcome"]["status"] for e in events if e["kind"] == "result"],
            ["needs_input", "done"],
        )

    def test_the_driver_events_are_no_reports_even_when_the_agent_appends_them(self):
        os.makedirs(self.work)
        tail = drive.Tail(os.path.join(self.work, "run.jsonl"))
        lines = [
            {"ts": "t", "kind": "input", "text": "x"},
            {"ts": "t", "kind": "session", "sid": SID, "cwd": "/", "project": False},
            {"ts": "t", "kind": "result", "outcome": DONE},
            {"ts": "t", "kind": "end", "sid": SID, "rc": 0},
        ]
        for line in lines:
            drive.append_line(os.path.join(self.work, "run.jsonl"), json.dumps(line) + "\n")
        self.assertEqual(list(tail()), [])
        r, _, _ = self.start([{"kind": "result", "outcome": DONE}])
        self.assertEqual(r.error, "the agent run returned no outcome")

    def test_the_closing_writes_never_mask_the_runs_exception(self):
        def items():
            os.remove(os.path.join(self.work, "run.jsonl"))
            os.mkdir(os.path.join(self.work, "run.jsonl"))
            yield said("x")
            raise KeyboardInterrupt

        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(KeyboardInterrupt):
            self.start(items(), sinks=[])
        self.assertEqual(
            len(re.findall(r"^drive\.py: .*run\.jsonl", err.getvalue(), re.M)), 2, err.getvalue()
        )

    FAILED = {**DONE, "status": "failed", "deliverable": ""}
    NEEDS = {**DONE, "status": "needs_input", "questions": ["Which repo?"], "deliverable": ""}

    def missing(self, *items, **params):
        seen, err = [], io.StringIO()
        with redirect_stderr(err):
            r, _, _ = self.start(list(items), sinks=[seen.append], **params)
        return (
            r,
            [e.name for e in seen if e.kind == "missing"],
            err.getvalue(),
            [e.kind for e in seen],
        )

    def test_done_or_failed_without_start_reports_it_missing(self):
        for o in (DONE, self.FAILED):
            r, missing, err, kinds = self.missing(progress("round", "one"), outcome(o))
            self.assertEqual((r.outcome.status, missing), (o["status"], ["start"]))
            self.assertEqual(err, "drive.py: missing progress mark: start\n")
            self.assertEqual(kinds, ["progress", "missing", "outcome"])

    def test_no_report_when_start_was_seen_needs_input_resumed_or_no_outcome(self):
        for items, params in (
            ((progress("start", "go"), outcome(DONE)), {}),
            ((outcome(self.NEEDS),), {}),
            ((outcome(DONE),), {"resume": True}),
            ((said("bye"),), {}),
        ):
            _, missing, err, _ = self.missing(*items, **params)
            self.assertEqual((missing, err), ([], ""), (items, params))


class Session(Base):
    def write(self, *lines):
        os.makedirs(self.work, exist_ok=True)
        with open(os.path.join(self.work, "run.jsonl"), "wb") as f:
            f.writelines(
                (line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n"
                for line in lines
            )

    def test_the_latest_valid_session_event_of_the_sid_wins(self):
        first = {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": False}
        latest = {**first, "cwd": "/b", "project": True}
        self.write(
            first,
            latest,
            b"not json",
            b"[]",
            b"\xff\xfe",
            b'{"kind": "session"',
            {**first, "kind": "input"},
            {**first, "sid": "other"},
            {**first, "cwd": "rel"},
            {**first, "cwd": "/x\x1b[2J"},
            {**first, "cwd": 3},
            {**first, "project": 1},
            {k: v for k, v in first.items() if k != "project"},
        )
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
    def test_each_runner_starts_its_command_an_unknown_runner_or_no_command_is_a_config_error(self):
        launch = drive.Launch(["h"], interactive=["i"])
        self.assertEqual(
            [drive.command(launch, r, Recorder({})) for r in ("headless", "tui")], [["h"], ["i"]]
        )
        for launch, runner, msg in (
            (drive.Launch(["h"], interactive=["i"]), "gui", "unknown runner 'gui'"),
            (drive.Launch(["h"]), "tui", "Recorder has no tui command"),
            (drive.Launch([], interactive=["i"]), "headless", "has no headless command"),
        ):
            with self.assertRaises(compose.ConfigError) as e:
                drive.command(launch, runner, Recorder({}))
            self.assertIn(msg, str(e.exception))


class HostFailure(Base):
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
            return drive.start(
                drive.Launch(["x"]),
                run(),
                self.params(),
                client=Recorder({}),
                runner="failing",
                sinks=[],
            )

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
        for error, rc, text in (
            (SystemExit(129), 129, "stopped: SystemExit: 129"),
            (SystemExit("bye"), 1, "stopped: SystemExit: bye"),
            (KeyboardInterrupt(), 1, "stopped: KeyboardInterrupt"),
            (RuntimeError("one\ntwo"), 1, "stopped: RuntimeError: one two"),
        ):
            with self.subTest(error=error):
                with self.assertRaises(type(error)) as cm:
                    self.start(poll=error)
                self.assertIs(cm.exception, error)
                with open(os.path.join(self.work, "run.jsonl")) as f:
                    events = [json.loads(line) for line in f]
                self.assertEqual(
                    [e["kind"] for e in events[-4:]], ["input", "session", "result", "end"]
                )
                self.assertEqual(
                    {k: events[-2][k] for k in ("kind", "error")}, {"kind": "result", "error": text}
                )
                self.assertEqual(
                    {k: events[-1][k] for k in ("kind", "sid", "rc")},
                    {"kind": "end", "sid": SID, "rc": rc},
                )

    def test_headless_raises_a_failed_popen(self):
        popen = unittest.mock.Mock(
            side_effect=FileNotFoundError(2, "No such file or directory", "fake")
        )
        with self.assertRaises(FileNotFoundError):
            drive.start(
                drive.Launch(["fake"]),
                run(),
                self.params(),
                client=Recorder({}),
                sinks=[],
                popen=popen,
            )


class TuiRunner(Base):
    NAME = f"r-{SID[:8]}"

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)

    def start(self, *steps, sinks=None, show=None, api=None, **params):
        p = self.params(**params)
        self.fake, self.err, seen = FakeTui(p.channel, steps), io.StringIO(), []
        launch = drive.Launch(["fake"], {"FAKE": "1"}, cwd=self.work, interactive=["claude", "hi"])
        with (
            self.fake.patch(**(api or {})),
            unittest.mock.patch.object(drive, "POLL", 0),
            redirect_stderr(self.err),
        ):
            r = drive.start(
                launch,
                run(show=show),
                p,
                client=claude(),
                runner="tui",
                sinks=[seen.append] if sinks is None else sinks,
            )
        return r, self.fake.calls, [e.kind for e in seen], self.err.getvalue()

    def main(self, *steps, api=None, extra=(), **kw):
        fake, err = FakeTui(os.path.join(self.work, "run.jsonl"), steps), io.StringIO()
        argv = [
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "Hello.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
            "--runner",
            "tui",
            *extra,
        ]
        with (
            fake.patch(**(api or {})),
            unittest.mock.patch.object(drive, "POLL", 0),
            redirect_stderr(err),
        ):
            return drive.main(argv, root=CORE, **kw), fake.calls, err.getvalue()

    def test_session_name_and_start_arguments_pwd_the_runs_cwd(self):
        with unittest.mock.patch.dict(os.environ, {"PWD": "/stale"}):
            _, calls, _, _ = self.start([outcome(DONE)], show="echo {{session}}")
        ((_, name, argv, kw),) = [c for c in calls if c[0] == "start"]
        self.assertEqual((name, argv, kw["cwd"]), (self.NAME, ["claude", "hi"], self.work))
        self.assertEqual(kw["template"], "echo {{session}}")
        self.assertEqual(
            (kw["env"]["FAKE"], kw["env"]["PATH"], kw["env"]["PWD"]),
            ("1", os.environ["PATH"], self.work),
        )

    def test_tui_session_name(self):
        self.assertIs(drive.tui_session, compose.tui_session)
        self.assertEqual(drive.tui_session("r", SID), self.NAME)
        self.assertEqual(
            drive.tui_session("r", SID, prefix="engineer-TASK-1"), "engineer-TASK-1-11111111"
        )
        self.assertEqual(drive.tui_session("r", SID, prefix=None), self.NAME)

    def test_tui_session_matches_the_prefix_and_sid_shape_only(self):
        for name, prefix in (
            (self.NAME, "r"),
            ("engineer-TASK-1-11111111", "engineer-TASK-1"),
            ("A_b-0-deadbeef", "A_b-0"),
        ):
            self.assertEqual(drive.TUI_SESSION.fullmatch(name)["prefix"], prefix)
        for name in (
            "r-t-xyz",
            "r-t-1111111",
            "r-t-111111111",
            "-11111111",
            "11111111",
            "r t-11111111",
            "r-t-11111111\n",
        ):
            self.assertIsNone(drive.TUI_SESSION.fullmatch(name), name)

    def launched(
        self, layout=None, status_line=False, prefix=None, per_column=None, retile=None, **kw
    ):
        p = self.params(prefix=prefix)
        fake = FakeTui(p.channel, [[outcome(DONE)]])
        launch = drive.Launch(
            ["fake"],
            {},
            cwd=self.work,
            interactive=["claude"],
            status_line=status_line,
            per_column=per_column,
            retile=retile,
        )
        with (
            fake.patch(),
            unittest.mock.patch.object(drive, "POLL", 0),
            redirect_stderr(io.StringIO()),
        ):
            drive.start(
                launch, run(), p, client=claude(), runner="tui", layout=layout, sinks=[], **kw
            )
        ((_, name, _, kw),) = [c for c in fake.calls if c[0] == "start"]
        return name, kw

    def test_the_layout_launch_prefix_and_events_reach_tui_start(self):
        unset = {
            "split": None,
            "split_from": None,
            "opener": None,
            "status_line": False,
            "per_column": None,
            "retile": None,
            "events": None,
        }
        for kw, name, want in (
            ({}, self.NAME, {}),
            ({"layout": drive.Layout()}, self.NAME, {}),
            (
                {"layout": drive.Layout("below", "s")},
                self.NAME,
                {"split": "below", "split_from": "s"},
            ),
            (
                {"layout": drive.Layout(opener="w0t0p0:AB-12")},
                self.NAME,
                {"opener": "w0t0p0:AB-12"},
            ),
            (
                {"layout": drive.Layout("right", None, "mine")},
                self.NAME,
                {"split": "right", "opener": "mine"},
            ),
            ({"status_line": True}, self.NAME, {"status_line": True}),
            ({"per_column": 2}, self.NAME, {"per_column": 2}),
            ({"retile": "off"}, self.NAME, {"retile": "off"}),
            (
                {"prefix": "engineer-TASK-1", "events": "/tmp/ev.log"},
                "engineer-TASK-1-11111111",
                {"events": "/tmp/ev.log"},
            ),
        ):
            with self.subTest(**kw):
                got_name, got = self.launched(**kw)
                self.assertEqual((got_name, {k: got[k] for k in unset}), (name, {**unset, **want}))
                self.assertIs(got["status_line"], want.get("status_line", False))

    def test_the_tui_session_has_the_name_its_command_gives_claude(self):
        for prefix in (None, "engineer-TASK-1"):
            with self.subTest(prefix=prefix):
                p, c = self.params(prefix=prefix), claude()
                launch, r = drive.plan(
                    CORE, c, "researcher", "light-research", params=p, cwd=self.work
                )
                fake = FakeTui(p.channel, [[outcome(DONE)]])
                with (
                    fake.patch(),
                    unittest.mock.patch.object(drive, "POLL", 0),
                    redirect_stderr(io.StringIO()),
                ):
                    drive.start(launch, r, p, client=c, runner="tui", sinks=[])
                ((_, name, argv, _),) = [call for call in fake.calls if call[0] == "start"]
                self.assertEqual(argv[argv.index("--name") + 1], name)

    def test_a_bad_layout_is_a_config_error_before_the_workdir_or_channel(self):
        cases = (
            ("headless", {"layout": drive.Layout()}, "no layout"),
            ("tui", {"layout": drive.Layout("left")}, "split"),
            ("tui", {"layout": drive.Layout(split_from="a b")}, "split_from"),
            ("tui", {"layout": drive.Layout(split_from="")}, "split_from"),
            ("tui", {"layout": drive.Layout(opener="a b")}, "opener"),
            ("tui", {"layout": drive.Layout(opener="")}, "opener"),
            ("tui", {"layout": drive.Layout(opener="a:b:c")}, "opener"),
            ("headless", {"prefix": "p"}, "prefix"),
            ("headless", {"events": "/tmp/ev.log"}, "events"),
        )
        for runner, kw, word in cases:
            with self.subTest(runner=runner, **kw):
                p = self.params(prefix=kw.pop("prefix", None))
                launch = drive.Launch(["fake"], {}, cwd=self.work, interactive=["claude"])
                with self.assertRaisesRegex(drive.ConfigError, word):
                    drive.start(launch, run(), p, client=claude(), runner=runner, sinks=[], **kw)
                self.assertFalse(os.path.lexists(self.work))
                self.assertFalse(os.path.lexists(p.channel))

    def test_main_split_split_from_prefix_and_events_reach_start_and_the_tui_session(self):
        seen = []
        real = drive.start

        def start(*a, **kw):
            seen.append((kw["layout"], a[2].prefix, kw["events"]))
            return real(*a, **kw)

        for flags, want in (
            (["--split", "below", "--split-from", "s"], (drive.Layout("below", "s"), None, None)),
            (["--split-from", "s"], (drive.Layout(None, "s"), None, None)),
            (["--split", "right"], (drive.Layout("right"), None, None)),
            ([], (None, None, None)),
            (
                ["--prefix", "engineer-TASK-1", "--events", "/tmp/ev.log"],
                (None, "engineer-TASK-1", "/tmp/ev.log"),
            ),
        ):
            with self.subTest(flags=flags), unittest.mock.patch.object(drive, "start", start):
                code, calls, _ = self.main([outcome(DONE)], extra=flags)
                self.assertEqual((code, seen.pop()), (0, want))
        ((_, name, _, kw),) = [c for c in calls if c[0] == "start"]
        self.assertRegex(name, r"engineer-TASK-1-[0-9a-f]{8}")
        self.assertEqual(kw["events"], "/tmp/ev.log")

    def test_main_without_events_the_tui_run_uses_the_manager_directorys(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = own("mgr")
        code, calls, _ = self.main([outcome(DONE)], proc=proc)
        ((_, _, _, kw),) = [c for c in calls if c[0] == "start"]
        want = os.path.join(home, "managers", "mgr", "events")
        self.assertEqual((code, kw["events"], len(proc.calls)), (0, want, 1))
        self.assertEqual(stat.S_IMODE(os.stat(want).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(want)).st_mode), 0o700)

    def test_main_manager_beats_tmux_and_events_beats_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = own("mgr")
        _, calls, _ = self.main([outcome(DONE)], extra=["--manager", "other"], proc=proc)
        ((_, _, _, kw),) = [c for c in calls if c[0] == "start"]
        self.assertEqual(
            (kw["events"], proc.calls), (os.path.join(home, "managers", "other", "events"), [])
        )
        shutil.rmtree(os.path.join(home, "managers"))
        _, calls, _ = self.main(
            [outcome(DONE)], extra=["--manager", "other", "--events", "/tmp/ev.log"], proc=proc
        )
        ((_, _, _, kw),) = [c for c in calls if c[0] == "start"]
        self.assertEqual(
            (kw["events"], os.path.exists(os.path.join(home, "managers"))), ("/tmp/ev.log", False)
        )

    def test_main_outside_tmux_without_manager_the_tui_run_has_no_events(self):
        home = hermetic.home(self)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, calls, _ = self.main([outcome(DONE)], proc=proc)
        ((_, _, _, kw),) = [c for c in calls if c[0] == "start"]
        self.assertEqual((code, kw["events"], os.listdir(home)), (0, None, []))

    def test_main_a_bad_manager_exits_2_before_anything_starts(self):
        home = hermetic.home(self)
        os.symlink(self.tmp.name, os.path.join(home, "managers"))
        os.environ.update(INSIDE)
        bad = f"drive.py: manager directory {home}/managers: not a directory owned by you\n"
        cases = (
            (
                ["--manager", "a/b"],
                own(),
                "drive.py: manager 'a/b': want [A-Za-z0-9_-]+\n",
                ((), ("--dry-run",)),
            ),
            (
                [],
                own("a b"),
                "drive.py: own tmux session 'a b': want [A-Za-z0-9_-]+; give --manager <name>\n",
                ((), ("--dry-run",)),
            ),
            (["--manager", "m1"], own(), bad, ((),)),
        )
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
        base = [
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "Hello.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
        ]
        cases = ([], ["--dry-run"], ["--runner", "headless"], ["--client", "skill"])
        for extra in cases:
            with self.subTest(extra=extra):
                err, out = io.StringIO(), io.StringIO()
                with (
                    redirect_stderr(err),
                    redirect_stdout(out),
                    unittest.mock.patch.object(drive, "start") as start,
                ):
                    self.assertEqual(
                        drive.main([*base, *extra, "--manager", "m1"], root=CORE, proc=proc), 2
                    )
                self.assertEqual(
                    (err.getvalue(), out.getvalue()),
                    ("drive.py: --manager needs --runner tui or --detach\n", ""),
                )
                start.assert_not_called()
                proc.assert_not_called()
                self.assertFalse(os.path.exists(self.work))

    def test_main_a_bad_prefix_or_headless_prefix_events_or_layout_exits_2_before_anything_starts(
        self,
    ):
        base = [
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "Hello.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
        ]
        cases = (
            (["--runner", "tui", "--prefix", "a b"], "prefix"),
            (["--runner", "tui", "--prefix", ""], "prefix"),
            (["--prefix", "p"], "prefix"),
            (["--events", "/tmp/ev.log"], "events"),
            (["--runner", "headless", "--prefix", "p", "--dry-run"], "prefix"),
            (["--events", "/tmp/ev.log", "--dry-run"], "events"),
            (["--split", "below"], "no layout"),
        )
        for extra, word in cases:
            with self.subTest(extra=extra):
                err = io.StringIO()
                with redirect_stderr(err), unittest.mock.patch.object(drive, "start") as start:
                    self.assertEqual(drive.main([*base, *extra], root=CORE), 2)
                self.assertIn(word, err.getvalue())
                start.assert_not_called()
                self.assertFalse(os.path.exists(self.work))

    def test_done_on_the_first_outcome_without_waiting_for_a_stop_or_a_kill(self):
        r, calls, kinds, _ = self.start([progress("start", "x")], [outcome(DONE)])
        self.assertEqual(
            (r.returncode, r.outcome.status, kinds), (0, "done", ["progress", "outcome"])
        )
        self.assertEqual([c[0] for c in calls], ["start"])
        self.assertEqual(self.fake.steps, [])

    def test_reported_outcome_survives_a_tui_session_exiting_nonzero(self):
        def status(name):
            for event in (progress("start", "go"), outcome(DONE)):
                drive.append_line(os.path.join(self.work, "run.jsonl"), json.dumps(event) + "\n")
            return 7

        r, calls, kinds, _ = self.start(api={"status": status})
        with open(os.path.join(self.work, "run.jsonl")) as f:
            saved = [json.loads(line) for line in f]
        result = next(e for e in saved if e["kind"] == "result")
        end = next(e for e in saved if e["kind"] == "end")
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (7, "done", "the client exited 7")
        )
        self.assertEqual(kinds, ["progress", "outcome"])
        self.assertEqual(result["outcome"]["url"], os.path.join(self.work, "out.md"))
        self.assertEqual((result["error"], end["rc"]), ("the client exited 7", 7))
        self.assertEqual([c[0] for c in calls], ["start"])

    def test_a_dead_pane_or_a_gone_session_ends_the_run_without_an_outcome(self):
        for state, result in (
            (3, (3, None, "the client exited 3")),
            (0, (0, None, "the agent run returned no outcome")),
            (None, (0, None, "the agent run returned no outcome")),
        ):
            r, calls, _, _ = self.start([progress("round", "x")], state)
            self.assertEqual((r.returncode, r.outcome, r.error), result)
            self.assertEqual([c[0] for c in calls], ["start"])

    def test_main_exits_3_on_a_dead_panes_status_1_on_0_and_0_on_an_outcome(self):
        self.assertEqual([self.main(state)[0] for state in (3, 0)], [3, 1])
        code, (start, *rest), _ = self.main([outcome(DONE)])
        self.assertEqual((code, start[2][0], rest), (0, "claude", []))

    def test_a_sink_raising_kills_the_session_prefixed_or_not(self):
        def sink(event):
            raise RuntimeError("sink")

        for prefix, name in ((None, self.NAME), ("p-1", "p-1-11111111")):
            with self.subTest(prefix=prefix):
                with self.assertRaises(RuntimeError):
                    self.start([progress("round", "x")], sinks=[sink], prefix=prefix)
                self.assertEqual(self.fake.calls[-1], ("kill", name))

    def test_a_failed_kill_is_printed_and_the_drivers_exception_raised(self):
        def sink(event):
            raise RuntimeError("sink")

        kill = unittest.mock.Mock(side_effect=drive.tmux_host.TuiError("gone"))
        with self.assertRaises(RuntimeError):
            self.start([progress("round", "x")], sinks=[sink], api={"kill": kill})
        self.assertEqual(kill.call_args, unittest.mock.call(self.NAME))
        self.assertIn("drive.py: tui: gone\n", self.err.getvalue())

    def test_a_tui_error_on_start_is_the_result(self):
        r, calls, _, _ = self.start(
            api={"start": unittest.mock.Mock(side_effect=drive.tmux_host.TuiError("no tmux"))}
        )
        self.assertEqual((r, calls), (drive.Result(1, None, "tui: no tmux"), []))

    def test_an_interrupted_start_kills_no_session_it_did_not_start(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(api={"start": unittest.mock.Mock(side_effect=KeyboardInterrupt)})
        self.assertEqual(self.fake.calls, [])

    def test_a_tui_error_reading_the_session_kills_it_and_is_the_result(self):
        status = unittest.mock.Mock(side_effect=drive.tmux_host.TuiError("no server"))
        r, calls, _, _ = self.start(api={"status": status})
        self.assertEqual(
            (r, [c[0] for c in calls]), (drive.Result(1, None, "tui: no server"), ["start", "kill"])
        )
        code, calls, err = self.main(api={"status": status})
        self.assertEqual((code, [c[0] for c in calls]), (3, ["start", "kill"]))
        self.assertIn("drive.py: tui: no server\n", err)

    def test_the_first_stop_without_an_outcome_nudges_once(self):
        r, calls, kinds, _ = self.start([STOP], [STOP], [outcome(DONE), STOP])
        self.assertEqual([c for c in calls if c[0] == "send"], [("send", self.NAME, drive.NUDGE)])
        self.assertEqual((r.outcome.status, kinds), ("done", ["missing", "outcome"]))

    def test_it_gives_up_after_stop_limit_counted_stops_and_leaves_the_session(self):
        r, calls, kinds, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT))
        self.assertEqual(r, drive.Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(
            ([c[0] for c in calls], kinds, self.fake.steps), (["start", "send"], [], [])
        )
        self.assertIn(
            f"drive.py: no outcome after {drive.STOP_LIMIT} stops; session {self.NAME} left open: "
            f"tmux attach -t '={self.NAME}'\n",
            err,
        )
        code, calls, _ = self.main(*[[STOP]] * (1 + drive.STOP_LIMIT))
        self.assertEqual((code, [c[0] for c in calls]), (1, ["start", "send"]))

    def test_the_left_open_line_takes_the_attach_prefix(self):
        for env, prefix in (
            ({}, ""),
            ({"TUI_ATTACH_PREFIX": " "}, ""),
            ({"TUI_ATTACH_PREFIX": "docker exec -it box"}, "docker exec -it box "),
        ):
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                *_, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT))
                self.assertIn(f"left open: {prefix}tmux attach -t '={self.NAME}'\n", err)

    def test_progress_resets_the_count(self):
        again = [[STOP]] * (drive.STOP_LIMIT - 1)
        r, calls, _, _ = self.start(
            [STOP], *again, [progress("round", "x")], *again, [outcome(DONE)]
        )
        self.assertEqual((r.outcome.status, [c[0] for c in calls]), ("done", ["start", "send"]))

    def test_pending_stops_are_ignored_however_many(self):
        r, calls, _, _ = self.start(*[[PENDING]] * (2 + drive.STOP_LIMIT), [outcome(DONE)])
        self.assertEqual(
            (r.returncode, r.outcome.status, [c[0] for c in calls]), (0, "done", ["start"])
        )
        self.assertEqual(self.fake.steps, [])

    def test_pending_stops_between_others_do_not_count_or_nudge(self):
        steps = [[PENDING], [PENDING], [STOP], *[[PENDING], [STOP]] * drive.STOP_LIMIT]
        r, calls, _, err = self.start(*steps)
        self.assertEqual(
            (r.error, [c[0] for c in calls], self.fake.steps),
            ("the agent run returned no outcome", ["start", "send"], []),
        )
        self.assertIn(
            f"drive.py: no outcome after {drive.STOP_LIMIT} stops; session {self.NAME} left open: "
            f"tmux attach -t '={self.NAME}'\n",
            err,
        )

    def test_it_gives_up_when_no_progress_comes_for_wait_limit_and_leaves_the_session(self):
        steps = [(drive.WAIT_LIMIT, []), (drive.WAIT_LIMIT + 1, [])]
        r, calls, kinds, err = self.start(*steps)
        self.assertEqual(r, drive.Result(0, None, "the agent run returned no outcome"))
        self.assertEqual(([c[0] for c in calls], kinds, self.fake.steps), (["start"], [], []))
        self.assertIn(
            f"drive.py: no outcome 2 h after the last progress report; session {self.NAME} left open: "
            f"tmux attach -t '={self.NAME}'\n",
            err,
        )
        code, calls, _ = self.main(*steps)
        self.assertEqual((code, [c[0] for c in calls]), (1, ["start"]))

    def test_exactly_wait_limit_of_quiet_is_not_over_it_and_an_outcome_in_the_poll_that_passes_it_wins(
        self,
    ):
        for steps in (
            ((drive.WAIT_LIMIT, []), [outcome(DONE)]),
            ((drive.WAIT_LIMIT + 1, [outcome(DONE)]),),
        ):
            with self.subTest(steps=steps):
                r, calls, _, err = self.start(*steps)
                self.assertEqual(
                    (r.returncode, r.outcome.status, [c[0] for c in calls], err),
                    (0, "done", ["start"], "drive.py: missing progress mark: start\n"),
                )

    def test_progress_restarts_the_quiet_clock_pending_stops_do_not(self):
        p = drive.WAIT_LIMIT - 10
        for name, steps in (
            (
                "progress",
                (
                    (p, [progress("round", "x")]),
                    (drive.WAIT_LIMIT + 10, []),
                    (p + drive.WAIT_LIMIT, []),
                    (p + drive.WAIT_LIMIT + 1, []),
                ),
            ),
            ("pending", ((p, [PENDING]), (drive.WAIT_LIMIT + 1, []))),
        ):
            with self.subTest(name):
                r, _, _, _ = self.start(*steps)
                self.assertEqual(
                    (r.error, self.fake.steps), ("the agent run returned no outcome", [])
                )

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        send = unittest.mock.Mock(side_effect=drive.tmux_host.TuiError("no pane"))
        r, _, _, err = self.start(*[[STOP]] * (1 + drive.STOP_LIMIT), api={"send": send})
        self.assertEqual((r.error, send.call_count), ("the agent run returned no outcome", 1))
        self.assertIn("drive.py: tui: no pane\n", err)

    def test_start_refuses_a_client_without_the_command_before_anything_starts(self):
        popen = unittest.mock.Mock()
        with (
            unittest.mock.patch.object(drive.tmux_host, "start") as start,
            self.assertRaises(compose.ConfigError),
        ):
            drive.start(
                drive.Launch(["fake"]),
                run(),
                self.params(),
                client=Recorder({}),
                runner="tui",
                popen=popen,
            )
        self.assertEqual(
            (popen.called, start.called, os.path.exists(self.work)), (False, False, False)
        )


class Detach(Base):
    DRIVER = f"dummy-tester-{SID[:8]}-drive"

    def setUp(self):
        super().setUp()
        p = unittest.mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for k in ("TMUX", "TMUX_PANE", "TUI_ATTACH_PREFIX"):
            os.environ.pop(k, None)
        os.environ.update(ITERM_SESSION_ID="w0t0p0:AB-12", TERM_PROGRAM="iTerm.app")
        self.events = os.path.join(self.tmp.name, "w.events")
        for s in drive.SIGNALS:  # a driver's main sets them and leaves them blocked
            self.addCleanup(signal.signal, s, signal.getsignal(s))
        self.addCleanup(
            signal.pthread_sigmask, signal.SIG_SETMASK, signal.pthread_sigmask(signal.SIG_BLOCK, [])
        )
        for s in drive.SIGNALS:  # first: one left pending is dropped as the mask is restored
            self.addCleanup(signal.signal, s, signal.SIG_IGN)

    def argv(self, *extra):
        return [
            "--role",
            "dummy-tester",
            "--input",
            "Hello.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
            "--sid",
            SID,
            *extra,
        ]

    def outer(self, *extra, rc=0, proc=None, events=True, own="mgr"):
        calls, handover = [], {}

        def fake(argv, **kw):
            calls.append(argv)
            if argv[1] == "display-message":
                return subprocess.CompletedProcess(argv, 0, f"{own}\n", "")
            if argv[1] == "list-clients":
                return subprocess.CompletedProcess(
                    argv, 0, "100 /dev/ttys001 %1 1 /tmp/tmux-501/default\n", ""
                )
            if argv[1] == "new-session" and rc == 0:
                with open(argv[-1]) as f:
                    handover.update(json.load(f))
                os.unlink(argv[-1])
            return subprocess.CompletedProcess(argv, rc, "", "duplicate session: x\n" if rc else "")

        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = drive.main(
                self.argv("--detach", *(("--events", self.events) if events else ()), *extra),
                root=CORE,
                proc=proc or fake,
            )
        return code, err.getvalue(), calls, handover

    def inner(self, *steps, argv=None, api=None, **kw):
        fake = FakeTui(os.path.join(self.work, "run.jsonl"), steps)
        argv = argv or self.argv("--runner", "tui", "--events", self.events, "--driver", "d-drive")
        with (
            fake.patch(**(api or {})),
            unittest.mock.patch.object(drive, "POLL", 0),
            redirect_stderr(io.StringIO()),
        ):
            return drive.main(argv, root=CORE, **kw), fake.calls

    def lines(self):
        with open(self.events) as f:
            lines = f.readlines()
        for line in lines:
            self.assertRegex(line, r"^\d\d:\d\d:\d\d ")
        return [line[9:] for line in lines]

    @staticmethod
    def entries(name):
        with roster.locked(roster.directory(name), write=False) as r:
            return r["entries"]

    def test_the_driver_session_never_matches_a_tui_session_or_the_reserved_name(self):
        reserved = re.compile(r"[a-z][a-z0-9-]*-[A-Z][A-Z0-9]*-\d+-[0-9a-f]{8}")
        self.assertTrue(reserved.fullmatch(drive.tui_session("r", SID, "engineer-TASK-1")))
        for prefix, want in (
            (None, f"r-{SID[:8]}-drive"),
            ("engineer-TASK-1", f"engineer-TASK-1-{SID[:8]}-drive"),
        ):
            name = drive.driver_session("r", SID, prefix)
            self.assertEqual(name, want)
            self.assertTrue(drive.tmux_host.NAME.fullmatch(name))
            self.assertIsNone(drive.TUI_SESSION.fullmatch(name))
            self.assertIsNone(reserved.fullmatch(name))

    def test_detach_needs_an_events_file_and_a_client_that_runs_before_anything_starts(self):
        home = hermetic.home(self)
        skill, no = (
            ["--client", "skill", "--role", "dummy-tester"],
            "SkillClient prints a prompt; it takes no --detach",
        )
        need = "--detach needs --events or --manager"
        cases = (
            (self.argv("--detach"), need),
            (self.argv("--detach", "--dry-run"), need),
            (self.argv("--detach", "--runner", "tui"), need),
            (self.argv("--detach", "--runner", "tui", "--dry-run"), need),
            ([*skill, "--detach", "--events", self.events], no),
            ([*skill, "--driver", "d", "--events", self.events], no),
        )
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

    def test_discovered_client_driver_writes_its_cleanup_outcome(self):
        argv = self.argv(
            "--client", "fake", "--events", self.events, "--driver", "d-drive", "--dry-run"
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(drive.main(argv, root=CORE), 0)
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])

    def test_dry_run_prints_the_plan_and_the_driver_and_starts_nothing(self):
        for runner in ("tui", "headless"):
            with self.subTest(runner=runner):
                out, proc = io.StringIO(), unittest.mock.Mock()
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    code = drive.main(
                        self.argv(
                            "--runner", runner, "--detach", "--events", self.events, "--dry-run"
                        ),
                        root=CORE,
                        proc=proc,
                    )
                data = json.loads(out.getvalue())
                self.assertEqual(
                    (code, data["argv"][0], data["driver"]), (0, "claude", self.DRIVER)
                )
                proc.assert_not_called()
                self.assertFalse(os.path.exists(self.events))
                self.assertFalse(os.path.exists(self.work))

    def test_detach_starts_this_command_as_the_driver_in_its_session_and_exits(self):
        driver, tui = f"p-{SID[:8]}-drive", f"p-{SID[:8]}"
        code, err, (argv, opener), handover = self.outer("--runner", "tui", "--prefix", "p")
        self.assertEqual(code, 0)
        self.assertEqual(
            argv,
            [
                "tmux",
                "new-session",
                "-d",
                "-e",
                "ITERM_SESSION_ID=w0t0p0:AB-12",
                "-s",
                driver,
                sys.executable,
                "-I",
                "-c",
                drive.tmux_host.EXEC,
                argv[-1],
            ],
        )
        self.assertEqual(
            opener, ["tmux", "set-option", "-t", f"={driver}:", "@opener", "w0t0p0:AB-12"]
        )
        self.assertFalse(os.path.exists(os.path.dirname(argv[-1])))
        self.assertEqual(
            handover["argv"],
            [
                sys.executable,
                os.path.abspath(drive.__file__),
                "--role=dummy-tester",
                "--input=Hello.",
                f"--out={self.work}/out.md",
                f"--workdir={self.work}",
                f"--sid={SID}",
                "--runner=tui",
                "--prefix=p",
                f"--events={self.events}",
                "--opener=w0t0p0:AB-12",
                f"--driver={driver}",
            ],
        )
        self.assertEqual(handover["cwd"], os.getcwd())
        self.assertEqual(handover["block"], [signal.SIGHUP, signal.SIGTERM, signal.SIGINT])
        self.assertEqual(handover["env"]["PATH"], os.environ["PATH"])
        self.assertNotIn("ITERM_SESSION_ID", handover["env"])
        self.assertEqual(
            err,
            f"drive.py: cwd {os.getcwd()} is not in trusted_dirs: its project settings are off\n"
            f"drive.py: session {SID}\n"
            f"drive.py: driver session {driver}: tmux attach -t '={driver}'\n"
            f"drive.py: tui session {tui}: tmux attach -t '={tui}'\n",
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.work))
        code, calls = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        ((_, name, _, kw),) = [c for c in calls if c[0] == "start"]
        self.assertEqual(
            (code, name, kw["opener"], kw["events"]), (0, tui, "w0t0p0:AB-12", self.events)
        )
        self.assertEqual(self.lines(), [f"{driver} outcome done\n"])

    def test_without_events_the_driver_gets_the_manager_directorys_not_a_manager(self):
        home = hermetic.home(self)
        want = os.path.join(home, "managers", "mgr", "events")
        os.environ.update(INSIDE)
        for runner in ("tui", "headless"):
            with self.subTest(runner=runner):
                code, _, _, handover = self.outer(
                    "--runner", runner, "--manager", "mgr", events=False
                )
                self.assertEqual(code, 0)
                self.assertEqual(
                    [a for a in handover["argv"] if a.startswith(("--events", "--manager"))],
                    [f"--events={want}"],
                )
                self.assertEqual(stat.S_IMODE(os.stat(want).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(want)).st_mode), 0o700)
                os.unlink(want)
        code, _, _, handover = self.outer("--runner", "tui", events=False)
        self.assertEqual((code, f"--events={want}" in handover["argv"]), (0, True))
        code, _ = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        self.assertEqual(code, 0)
        with open(want) as f:
            self.assertEqual([line[9:] for line in f], [f"{self.DRIVER} outcome done\n"])

    def test_manager_beats_the_callers_tmux_session_and_events_beats_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        code, _, calls, handover = self.outer(
            "--runner", "headless", "--manager", "other", events=False
        )
        self.assertEqual((code, [c[1] for c in calls]), (0, ["new-session"]))
        self.assertIn(f"--events={home}/managers/other/events", handover["argv"])
        self.assertTrue(os.path.isfile(os.path.join(home, "managers", "other", "events")))
        self.assertFalse(os.path.exists(os.path.join(home, "managers", "mgr")))
        shutil.rmtree(os.path.join(home, "managers"))
        code, _, _, handover = self.outer("--runner", "headless", "--manager", "other")
        self.assertEqual(code, 0)
        self.assertEqual(
            [a for a in handover["argv"] if a.startswith("--events")], [f"--events={self.events}"]
        )
        self.assertFalse(os.path.exists(os.path.join(home, "managers")))

    def test_a_driver_never_resolves_a_manager(self):
        home = hermetic.home(self)
        os.environ.update(INSIDE)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, calls = self.inner(
            [outcome(DONE)], argv=self.argv("--runner", "tui", "--driver", "d-drive"), proc=proc
        )
        ((_, _, _, kw),) = [c for c in calls if c[0] == "start"]
        self.assertEqual((code, kw["events"], os.listdir(home)), (0, None, []))

    def test_stdin_input_reaches_the_driver_as_text_a_dash_too_not_its_stdin(self):
        for runner, text in (("headless", "# Echo\nthis"), ("tui", "-")):
            with (
                self.subTest(text=text),
                unittest.mock.patch.object(sys, "stdin", io.StringIO(text)),
            ):
                code, _, _, handover = self.outer("--runner", runner, "--input", "-")
                self.assertEqual((code, handover["argv"][3]), (0, f"--input={text}"))
        stdin = unittest.mock.Mock()
        with unittest.mock.patch.object(sys, "stdin", stdin):
            code, calls = self.inner([outcome(DONE)], argv=handover["argv"][2:])
        stdin.read.assert_not_called()
        ((_, _, argv, _),) = [c for c in calls if c[0] == "start"]
        self.assertTrue(argv[1].endswith("\n# Input\n\n-\n"), argv[1][-40:])
        self.assertEqual((code, self.lines()), (0, [f"{self.DRIVER} outcome done\n"]))

    def test_without_an_opener_the_driver_gets_no_iterm_pane_and_headless_no_tui_line(self):
        os.environ["TERM_PROGRAM"] = "vscode"
        for runner in ("tui", "headless"):
            with self.subTest(runner=runner):
                code, err, (argv,), handover = self.outer("--runner", runner)
                self.assertEqual((code, argv[4]), (0, "ITERM_SESSION_ID="))
                self.assertEqual(
                    [a for a in handover["argv"] if a.startswith(("--opener", "--events"))],
                    [f"--events={self.events}"],
                )
                self.assertEqual("tui session" in err, runner == "tui")

    def test_a_split_from_no_terminal_shows_exits_2_before_anything_starts(self):
        for rc, out, want in (
            (0, "", "no terminal shows tmux session s"),
            (1, "", "tmux: can't find session: s"),
        ):
            with self.subTest(want=want):
                calls = []

                def proc(argv, **kw):
                    calls.append(argv[1])
                    return subprocess.CompletedProcess(
                        argv, rc, out, "can't find session: s\n" if rc else ""
                    )

                code, err, _, _ = self.outer("--runner", "tui", "--split-from", "s", proc=proc)
                self.assertEqual((code, calls), (2, ["list-clients"]))
                self.assertTrue(err.endswith(f"drive.py: {want}\n"), err)
                self.assertFalse(os.path.exists(self.events))

    def test_the_driver_carries_its_runs_opener_and_none_without_one(self):
        os.environ.update(INSIDE)
        for runner, want in (
            ("tui", [["set-option", "-t", f"={self.DRIVER}:", "@opener", "mgr"]]),
            ("headless", []),
        ):
            with self.subTest(runner=runner):
                code, err, calls, _ = self.outer("--runner", runner)
                self.assertEqual(code, 0, err)
                self.assertEqual([c[1:] for c in calls if c[1] == "set-option"], want)

    def test_a_failed_opener_only_prints_its_line(self):
        def proc(argv, **kw):
            if argv[1] == "new-session":
                os.unlink(argv[-1])
            fail = argv[1] == "set-option"
            return subprocess.CompletedProcess(
                argv, int(fail), "", "no such session\n" if fail else ""
            )

        err = io.StringIO()
        with redirect_stderr(err):
            drive.detach(
                "d", [sys.executable], cwd=self.tmp.name, env={}, iterm="", proc=proc, opener="mgr"
            )
        self.assertEqual(err.getvalue(), "drive.py: driver session d: @opener: no such session\n")

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
            drive.detach(
                "d",
                [sys.executable],
                cwd=self.tmp.name,
                env={},
                iterm="",
                proc=proc,
                sleep=lambda s: None,
            )
        self.assertEqual([c[1:] for c in calls[1:]], [["kill-session", "-t", "=d"]])
        self.assertFalse(os.path.exists(os.path.dirname(calls[0][-1])))

    def test_an_argv_item_holding_nul_or_a_lone_surrogate_raises_before_the_handover_or_tmux(self):
        for item, want in (
            ("--input=a\0b", "an argv item holds a NUL character"),
            ("--input=a\ud800b", "an argv item cannot be encoded"),
        ):
            with (
                self.subTest(want),
                unittest.mock.patch.object(drive.tempfile, "mkdtemp") as mkdtemp,
            ):
                proc = unittest.mock.Mock()
                with self.assertRaisesRegex(drive.RunnerError, want):
                    drive.detach(
                        "d", [sys.executable, item], cwd=self.tmp.name, env={}, iterm="", proc=proc
                    )
                proc.assert_not_called()
                mkdtemp.assert_not_called()

    def test_a_tui_run_detached_from_an_attached_session_gets_its_role_entry_under_the_driver_name(
        self,
    ):
        hermetic.home(self)
        roster.events(roster.directory("mgr"))
        os.environ.update(INSIDE)
        code, err, _, handover = self.outer(
            "--runner",
            "tui",
            "--task",
            "echo",
            "--repo",
            self.repo,
            "--client",
            "claude",
            "--split",
            "below",
            "--split-from",
            "s",
            "--prefix",
            "p",
            events=False,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("--input=Hello.", handover["argv"])
        ((name, e),) = self.entries("mgr").items()
        del e["started"]
        self.assertEqual(
            (name, e),
            (
                f"p-{SID[:8]}-drive",
                {
                    "kind": "role",
                    "sid": SID,
                    "cwd": os.getcwd(),
                    "note": None,
                    "opener": "mgr",
                    "pane": None,
                    "split": "below",
                    "split_from": "s",
                    "tui": f"p-{SID[:8]}",
                    "state": "working",
                    "resume": [
                        sys.executable,
                        os.path.abspath(drive.__file__),
                        "--role=dummy-tester",
                        "--task=echo",
                        f"--out={self.work}/out.md",
                        f"--workdir={self.work}",
                        f"--repo={self.repo}",
                        "--client=claude",
                        "--runner=tui",
                        "--split=below",
                        "--split-from=s",
                        "--prefix=p",
                        "--detach",
                        "--manager=mgr",
                    ],
                },
            ),
        )

    def test_a_headless_role_entry_has_no_tui_or_opener_and_its_default_runner(self):
        hermetic.home(self)
        code, err, _, _ = self.outer("--manager", "m1", events=False)
        self.assertEqual(code, 0, err)
        ((name, e),) = self.entries("m1").items()
        self.assertEqual(
            (name, e["tui"], e["opener"], e["split"], e["split_from"]),
            (self.DRIVER, None, None, None, None),
        )
        self.assertEqual(
            e["resume"][2:],
            [
                "--role=dummy-tester",
                f"--out={self.work}/out.md",
                f"--workdir={self.work}",
                "--runner=headless",
                "--detach",
                "--manager=m1",
            ],
        )

    def test_a_role_entrys_resume_keeps_a_dash_leading_value_one_item_and_never_the_input(self):
        hermetic.home(self)
        code, err, _, handover = self.outer(
            "--manager", "m1", "--out=--input=x", "--inp", "Other.", events=False
        )
        self.assertEqual(code, 0, err)
        self.assertIn("--input=Other.", handover["argv"])
        (e,) = self.entries("m1").values()
        self.assertEqual(
            e["resume"][2:],
            [
                "--role=dummy-tester",
                "--out=--input=x",
                f"--workdir={self.work}",
                "--runner=headless",
                "--detach",
                "--manager=m1",
            ],
        )

    def test_no_role_entry_for_another_events_file_a_dry_run_or_a_failed_start(self):
        hermetic.home(self)
        d = roster.directory("m1")
        roster.events(d)
        for extra, kw, want in (
            (("--manager", "m1"), {}, 0),
            (("--manager", "m1", "--dry-run"), {"events": False}, 0),
            (("--manager", "m1"), {"events": False, "rc": 1}, 3),
        ):
            with self.subTest(extra=extra, kw=kw):
                self.assertEqual(self.outer(*extra, **kw)[0], want)
                self.assertFalse(os.path.exists(os.path.join(d, "roster.json")))

    def test_detach_writes_the_entry_it_is_given_once_the_session_runs_never_when_it_fails_to_start(
        self,
    ):
        hermetic.home(self)
        d = roster.directory("m1")
        roster.ensure(d)
        path = os.path.join(d, "roster.json")
        fields = {
            "kind": "role",
            "sid": SID,
            "cwd": self.tmp.name,
            "resume": [sys.executable, os.path.abspath(drive.__file__), "--detach"],
        }

        def taken(argv, **kw):
            self.assertFalse(os.path.exists(path))
            os.unlink(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        def failed(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "duplicate session: d\n")

        def untaken(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "", "")

        for proc, want in (
            (failed, "tmux: duplicate session: d"),
            (untaken, "session d did not start"),
        ):
            with self.subTest(want=want):
                with self.assertRaisesRegex(drive.RunnerError, want):
                    drive.detach(
                        "d",
                        [sys.executable],
                        cwd=self.tmp.name,
                        env={},
                        iterm="",
                        proc=proc,
                        sleep=lambda s: None,
                        roster_entry=(d, fields),
                    )
                self.assertFalse(os.path.exists(path))
        drive.detach(
            "d",
            [sys.executable],
            cwd=self.tmp.name,
            env={},
            iterm="",
            proc=taken,
            roster_entry=(d, fields),
        )
        ((name, e),) = self.entries("m1").items()
        self.assertEqual((name, e), ("d", roster.entry(**fields, started=e["started"])))
        os.unlink(path)
        err = io.StringIO()
        with redirect_stderr(err):
            drive.detach(
                "d",
                [sys.executable],
                cwd=self.tmp.name,
                env={},
                iterm="",
                proc=taken,
                roster_entry=(d, {**fields, "cwd": "rel"}),
            )
        self.assertEqual(
            err.getvalue(),
            "manager: entry d not written: entry: cwd: 'rel': want an absolute printable path\n",
        )
        self.assertFalse(os.path.exists(path))

    def test_an_entry_write_failure_prints_one_line_and_the_run_goes_on(self):
        hermetic.home(self)
        d = roster.directory("m1")
        roster.ensure(d)
        with open(os.path.join(d, "roster.json"), "w") as f:
            f.write("{")
        code, err, _, _ = self.outer("--manager", "m1", events=False)
        failures = [line for line in err.splitlines() if line.startswith("manager: ")]
        self.assertEqual(code, 0, err)
        self.assertEqual(len(failures), 1, err)
        self.assertTrue(
            failures[0].startswith(
                f"manager: entry {self.DRIVER} not written: {d}/roster.json: invalid JSON: "
            ),
            failures,
        )
        self.assertIn(f"drive.py: driver session {self.DRIVER}: ", err)
        with open(os.path.join(d, "roster.json")) as f:
            self.assertEqual(f.read(), "{")
        os.environ.update(INSIDE)
        code, err, _, _ = self.outer(own="a b")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [line for line in err.splitlines() if line.startswith("manager: ")],
            [
                f"manager: entry {self.DRIVER} not written: own tmux session 'a b': want "
                f"{drive.tmux_host.NAME.pattern}; give --manager <name>"
            ],
        )

    def test_the_driver_appends_one_outcome_line_when_it_ends(self):
        needs = {"status": "needs_input", "title": "T", "summary": "S", "questions": ["Which?"]}
        cases = (
            ([outcome(DONE)], 0, "done"),
            ([outcome(needs)], 0, "needs_input"),
            ([outcome({"status": "failed", "title": "T", "summary": "S"})], 0, "failed"),
            ([outcome({"status": "done"})], 1, "error"),
            (0, 1, "error"),
            (3, 3, "error"),
        )
        for step, code, word in cases:
            with self.subTest(word=word, step=step):
                self.assertEqual(self.inner(step)[0], code)
                self.assertEqual(self.lines(), [f"d-drive outcome {word}\n"])
                os.unlink(self.events)

    def test_malformed_client_config_keeps_detached_outcome_and_inline_authority(self):
        original = clients.load_config
        for name, expected in (("claude", True), ("skill", False), ("missing", False)):
            with self.subTest(client=name):

                def invalid(client_name, root):
                    return {**original(client_name, root), "invalid_key": True}

                with unittest.mock.patch.object(clients, "load_config", side_effect=invalid):
                    code, calls = self.inner(
                        argv=self.argv(
                            "--client", name, "--events", self.events, "--driver", "d-drive"
                        )
                    )
                self.assertEqual((code, calls), (2, []))
                self.assertEqual(os.path.exists(self.events), expected)
                if expected:
                    self.assertEqual(self.lines(), ["d-drive outcome error\n"])
                    os.unlink(self.events)

    def test_startup_config_failures_preserve_module_authority(self):
        operator = hermetic.home(self)
        local = os.path.join(operator, "core.local.toml")
        root = os.path.join(self.tmp.name, "startup-core")
        os.mkdir(root)
        config = os.path.join(root, "config.toml")
        modules = {}
        for name, runs in (("startup_running", True), ("startup_inline", False)):
            module = types.ModuleType(f"clients.{name}")
            module.Adapter = type("Adapter", (clients.Client,), {"runs": runs})
            module.Other = type("Other", (clients.Client,), {"runs": not runs})
            module.RUNS = module.Adapter.runs
            modules[module.__name__] = module
        self.enterContext(unittest.mock.patch.dict(sys.modules, modules))
        cases = (
            ("claude", "ClaudeClient", True),
            ("skill", "SkillClient", False),
            ("startup_running", "Adapter", True),
            ("startup_inline", "Adapter", False),
        )
        for name, implementation, runs in cases:
            for failure in (
                "missing-table",
                "missing-implementation",
                "bad-implementation",
                "base-toml",
                "local-toml",
                "read-error",
                "constructor",
                "mismatch",
            ):
                if failure == "mismatch" and not name.startswith("startup_"):
                    continue
                for existing in (False, True):
                    for pending in (False, True):
                        with self.subTest(
                            client=name, failure=failure, existing=existing, pending=pending
                        ):
                            if os.path.exists(local):
                                os.unlink(local)
                            if os.path.exists(self.events):
                                os.unlink(self.events)
                            before = "existing events\n" if existing else ""
                            if existing:
                                Path(self.events).write_text(before)
                            header = f"[clients.{name}]\n"
                            text = header + f'implementation = "{implementation}"\n'
                            if failure == "missing-table":
                                text = "tier = 2\n"
                            elif failure == "missing-implementation":
                                text = header
                            elif failure == "bad-implementation":
                                text = header + 'implementation = "Missing"\n'
                            elif failure == "base-toml":
                                text = "[unfinished"
                            elif failure == "local-toml":
                                Path(local).write_text("[unfinished")
                            elif failure == "constructor":
                                text += "invalid_key = true\n"
                            elif failure == "mismatch":
                                text = header + 'implementation = "Other"\n'
                            Path(config).write_text(text)
                            with ExitStack() as stack:
                                if failure == "read-error":
                                    stack.enter_context(
                                        unittest.mock.patch.object(
                                            clients,
                                            "load_config",
                                            side_effect=PermissionError("fixture read denied"),
                                        )
                                    )
                                err = stack.enter_context(redirect_stderr(io.StringIO()))
                                if pending:
                                    signal.pthread_sigmask(signal.SIG_BLOCK, drive.SIGNALS)
                                    os.kill(os.getpid(), signal.SIGHUP)
                                    with self.assertRaises(SystemExit) as stopped:
                                        drive.main(
                                            self.argv(
                                                "--client",
                                                name,
                                                "--events",
                                                self.events,
                                                "--driver",
                                                "d-drive",
                                            ),
                                            root=root,
                                        )
                                    self.assertEqual(stopped.exception.code, 129)
                                else:
                                    code = drive.main(
                                        self.argv(
                                            "--client",
                                            name,
                                            "--events",
                                            self.events,
                                            "--driver",
                                            "d-drive",
                                        ),
                                        root=root,
                                    )
                                    self.assertEqual(code, 2, err.getvalue())
                                    self.assertIn("drive.py:", err.getvalue())
                            after = (
                                Path(self.events).read_text() if os.path.exists(self.events) else ""
                            )
                            if runs:
                                self.assertRegex(
                                    after[len(before) :],
                                    r"^\d\d:\d\d:\d\d d-drive outcome error\n$",
                                )
                            else:
                                self.assertEqual(after, before)
                                self.assertEqual(os.path.exists(self.events), existing)

    def test_unknown_or_invalid_module_has_no_event_authority(self):
        module = types.ModuleType("clients.unresolved")
        for failure in ("unknown", "import-error", "missing-capability", "invalid-capability"):
            for existing in (False, True):
                with self.subTest(failure=failure, existing=existing), ExitStack() as stack:
                    if os.path.exists(self.events):
                        os.unlink(self.events)
                    if existing:
                        Path(self.events).write_text("keep\n")
                    if failure == "import-error":
                        stack.enter_context(
                            unittest.mock.patch(
                                "importlib.import_module",
                                side_effect=ImportError("dependency unavailable"),
                            )
                        )
                    elif failure != "unknown":
                        if failure == "invalid-capability":
                            module.RUNS = "yes"
                        stack.enter_context(
                            unittest.mock.patch.dict(sys.modules, {module.__name__: module})
                        )
                    code, calls = self.inner(
                        argv=self.argv(
                            "--client", "unresolved", "--events", self.events, "--driver", "d-drive"
                        )
                    )
                    self.assertEqual((code, calls), (2, []))
                    self.assertEqual(os.path.exists(self.events), existing)
                    if existing:
                        self.assertEqual(Path(self.events).read_text(), "keep\n")

    def test_a_driver_that_cannot_start_its_run_appends_error(self):
        code, calls = self.inner(
            argv=self.argv("--task", "essay", "--events", self.events, "--driver", "d-drive")
        )
        self.assertEqual((code, calls), (2, []))
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])

    def test_a_headless_driver_appends_only_its_outcome(self):
        def popen(argv, **kw):
            return FakeProc(feed(os.path.join(self.work, "run.jsonl"), [outcome(DONE)]))

        with redirect_stderr(io.StringIO()):
            code = drive.main(
                self.argv("--events", self.events, "--driver", "d-drive"), root=CORE, popen=popen
            )
        self.assertEqual((code, self.lines()), (0, ["d-drive outcome done\n"]))

    def test_driver_cleanup_does_not_reload_the_selected_client(self):
        with (
            unittest.mock.patch.object(clients, "load_module", wraps=clients.load_module) as module,
            unittest.mock.patch.object(clients, "load_config", wraps=clients.load_config) as config,
        ):
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = drive.main(
                    self.argv("--events", self.events, "--driver", "d-drive", "--dry-run"),
                    root=CORE,
                )
        self.assertEqual(code, 0)
        module.assert_called_once_with("claude")
        config.assert_called_once_with("claude", CORE)
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])

    def test_a_signal_ends_the_driver_with_one_error_line_and_kills_its_tui_session(self):
        killed = []

        def status(name):
            self.assertTrue(
                callable(signal.getsignal(signal.SIGHUP)), "no handler: SIGHUP would end the tests"
            )
            os.kill(os.getpid(), signal.SIGHUP)
            return drive.tmux_host.RUNNING

        def kill(name):
            killed.append(name)
            os.kill(os.getpid(), signal.SIGTERM)

        with self.assertRaises(SystemExit) as cm:
            self.inner(api={"status": status, "kill": kill})
        self.assertEqual((cm.exception.code, killed), (129, [f"dummy-tester-{SID[:8]}"]))
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])

    def test_a_signal_sent_before_the_driver_handles_them_waits_and_ends_it_with_one_error_line(
        self,
    ):
        signal.pthread_sigmask(signal.SIG_BLOCK, drive.SIGNALS)
        os.kill(os.getpid(), signal.SIGHUP)
        with self.assertRaises(SystemExit) as cm:
            self.inner([outcome(DONE)])
        self.assertEqual(cm.exception.code, 129)
        self.assertEqual(self.lines(), ["d-drive outcome error\n"])
        self.assertFalse(os.path.exists(self.work))

    def test_a_signal_as_the_driver_ends_still_leaves_its_outcome_line(self):
        sigmask, sent, blocks = signal.pthread_sigmask, [], []

        def block_after_a_signal(how, signals):
            if how == signal.SIG_BLOCK:
                blocks.append(how)
            if how == signal.SIG_BLOCK and len(blocks) > 1 and not sent:
                sent.append(how)
                os.kill(os.getpid(), signal.SIGTERM)
            return sigmask(how, signals)

        with (
            unittest.mock.patch.object(signal, "pthread_sigmask", block_after_a_signal),
            self.assertRaises(SystemExit) as cm,
        ):
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
        with unittest.mock.patch.object(
            os, "write", side_effect=lambda fd, data: real(fd, bytes(data[:3]))
        ) as write:
            drive.append_line(self.path, "héllo wörld\n")
        self.assertEqual((write.call_count, self.content()), (5, "héllo wörld\n".encode()))

    def test_appends_after_what_is_there_and_an_empty_text_creates_the_file_owner_only(self):
        drive.append_line(self.path, "")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        drive.append_line(self.path, "one\n")
        drive.append_line(self.path, "two\n")
        self.assertEqual(self.content(), b"one\ntwo\n")

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
            self.assertEqual(
                (os.path.islink(self.path), f.read(), g.read()), (False, "new\n", "keep\n")
            )

    def test_no_temp_is_left_when_the_replace_fails(self):
        drive.save(self.path, "old\n")
        with unittest.mock.patch.object(drive.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                drive.save(self.path, "new\n")
        with open(self.path) as f:
            self.assertEqual(
                (f.read(), os.listdir(os.path.dirname(self.path))), ("old\n", ["f.txt"])
            )


class Main(Base):
    def run_main(self, *extra, lines=(), rc=0, **kw):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            return FakeProc(feed(os.path.join(self.work, "run.jsonl"), lines), rc)

        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "Hello.",
            "--out",
            os.path.join(self.work, "out.md"),
            "--workdir",
            self.work,
            *extra,
        ]
        with redirect_stdout(out), redirect_stderr(err):
            code = drive.main(argv, root=CORE, popen=popen, **kw)
        return code, out.getvalue(), err.getvalue(), calls

    def test_dry_run_prints_plan_and_runs_nothing(self):
        code, out, err, calls = self.run_main("--dry-run")
        data = json.loads(out)
        self.assertEqual((code, calls), (0, []))
        self.assertEqual((data["argv"][0], data["cwd"]), ("claude", os.getcwd()))
        self.assertIn(
            f"drive.py: cwd {os.getcwd()} is not in trusted_dirs: its project settings are off\n",
            err,
        )
        self.assertFalse(os.path.exists(self.work))

    def test_dry_run_prints_the_runners_command(self):
        with redirect_stderr(io.StringIO()):
            launch = self.plan(
                "dummy-tester", "echo", client="claude", input="Hello.", cwd=os.getcwd()
            )
        code, out, _, _ = self.run_main("--dry-run", "--sid", SID, "--runner", "tui")
        self.assertEqual((code, json.loads(out)["argv"]), (0, launch.interactive))

    def test_dry_run_shows_the_events_file_given_or_the_manager_directorys_and_creates_nothing(
        self,
    ):
        home = hermetic.home(self)
        want = os.path.join(home, "managers", "mgr", "events")
        os.environ.update(INSIDE)
        proc = own("mgr")
        cases = (
            ("--runner", "tui"),
            ("--detach",),
            ("--runner", "tui", "--detach"),
            ("--detach", "--manager", "mgr"),
        )
        for extra in cases:
            with self.subTest(extra=extra):
                code, out, _, _ = self.run_main("--dry-run", *extra, proc=proc)
                self.assertEqual((code, json.loads(out)["events"]), (0, want))
                self.assertEqual(os.listdir(home), [])
        code, out, _, _ = self.run_main(
            "--dry-run", "--runner", "tui", "--manager", "other", proc=proc
        )
        self.assertEqual(
            (code, json.loads(out)["events"]),
            (0, os.path.join(home, "managers", "other", "events")),
        )
        for extra in (("--runner", "tui"), ("--detach",)):
            code, out, _, _ = self.run_main(
                "--dry-run", *extra, "--events", "/tmp/ev.log", "--manager", "other", proc=proc
            )
            self.assertEqual((code, json.loads(out)["events"]), (0, "/tmp/ev.log"))
        self.assertEqual(os.listdir(home), [])

    def test_dry_run_shows_no_events_when_there_is_none(self):
        home = hermetic.home(self)
        proc = unittest.mock.Mock(side_effect=AssertionError("tmux"))
        code, out, _, _ = self.run_main("--dry-run", "--runner", "tui", proc=proc)
        self.assertEqual((code, "events" in json.loads(out)), (0, False))
        os.environ.update(INSIDE)
        code, out, _, _ = self.run_main("--dry-run", proc=proc)
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

    def test_done_run_exits_0_reports_its_session_and_leaves_its_record(self):
        code, _, err, calls = self.run_main("--sid", SID, lines=[outcome(DONE)])
        self.assertEqual(code, 0)
        self.assertIn(f"session {SID}", err)
        self.assertIn("status done", err)
        ((argv, kw),) = calls
        self.assertEqual((argv[0], kw["cwd"]), ("claude", os.getcwd()))
        self.assertEqual(kw["env"]["PATH"], os.environ["PATH"])
        entry = drive.session(self.work, SID)
        self.assertEqual((entry["cwd"], entry["project"]), (os.getcwd(), False))
        self.assertEqual(
            entry["resume"], f"cd {os.getcwd()} && claude --resume {SID} --add-dir {self.work}"
        )
        self.assertEqual(entry["transcript"], claude_client.transcript(os.getcwd(), SID))

    def test_input_is_text_from_the_argument_or_stdin_never_a_files_content(self):
        here = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, here)
        with open("input.md", "w") as f:
            f.write("SECRET")
        for argv, stdin, want in (
            (["--input", "input.md"], "", "input.md"),
            (["--input", "-"], "Use B.\n", "Use B.\n"),
        ):
            with self.subTest(argv=argv), unittest.mock.patch("sys.stdin", io.StringIO(stdin)):
                code, _, _, ((cmd, _),) = self.run_main(*argv, lines=[outcome(DONE)])
            with open(os.path.join(self.work, "run.jsonl")) as f:
                inputs = [e["text"] for e in map(json.loads, f) if e["kind"] == "input"]
            self.assertEqual((code, inputs[-1]), (0, want))
            self.assertTrue(cmd[2].endswith(f"\n# Input\n\n{want.strip()}\n"), cmd[2][-100:])
            self.assertNotIn("SECRET", cmd[2])

    def test_no_outcome_exits_1_and_a_client_failure_3(self):
        for lines, rc, code, want in (((), 0, 1, "no outcome"), ((), 1, 3, "the client exited 1")):
            with self.subTest(code=code):
                got, _, err, _ = self.run_main(lines=lines, rc=rc)
                self.assertEqual(got, code)
                self.assertIn(want, err)

    def test_a_config_error_exits_2_before_anything_starts(self):
        for name, extra, local, want in (
            (
                "a task off the index",
                ("--task", "essay"),
                None,
                "drive.py: task 'essay' is not one of",
            ),
            (
                "a bad workers_per_column",
                (),
                "workers_per_column = 0\n",
                "drive.py: workers_per_column: want an integer from 1 to 9999",
            ),
            (
                "a bad grid_retile",
                (),
                'grid_retile = "x"\n',
                "drive.py: grid_retile: want one of all, open-close, off",
            ),
        ):
            path = os.path.join(hermetic.home(self), "core.local.toml")
            if local:
                with open(path, "w") as f:
                    f.write(local)
            for dry in ((), ("--dry-run",)):
                with self.subTest(name, dry=dry):
                    code, out, err, calls = self.run_main(*extra, *dry)
                    self.assertEqual((code, out, calls), (2, "", []))
                    self.assertIn(want, err)

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


class CodexRecoveryDriver(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(
            unittest.mock.patch.object(codex, "_implicit_writable_roots", return_value=[])
        )
        self.private = hermetic.home(self)
        self.client = clients.get("codex", CORE)
        self.snapshot = {
            "cwd": os.path.realpath(self.work),
            "project": False,
            "dirs": [os.path.realpath(self.work)],
            "commands": ["report *"],
            "model": "gpt-6-luna",
            "effort": "low",
            "network": False,
            "codex_home": os.path.realpath(os.path.join(os.path.dirname(self.private), ".codex")),
        }
        self.native = "11111111-2222-4333-8444-555555555555"

    def events(self):
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def run_codex(self, native=None, *, resume=False, begun=None, sinks=(), popen=None):
        launch = drive.Launch(
            ["fake"],
            cwd=self.work,
            session_state=self.snapshot,
            writable_roots=[self.work, self.snapshot["codex_home"]],
        )
        lines = stream({"type": "thread.started", "thread_id": native or self.native})
        return drive.start(
            launch,
            run(),
            self.params(resume=resume),
            client=self.client,
            begun=begun,
            sinks=sinks,
            popen=popen or (lambda argv, **kw: FakeProc(lines)),
        )

    def test_snapshot_exists_before_spawn_and_native_mapping_before_later_events(self):
        observed = []

        def started():
            state = codex._load_session(self.work, SID)
            observed.append((state.snapshot, state.client_id, [e["kind"] for e in self.events()]))

        def popen(argv, **kw):
            self.assertEqual(codex._load_session(self.work, SID).client_id, None)

            def lines():
                yield from stream({"type": "thread.started", "thread_id": self.native})
                yield from stream(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "later"}}
                )

            return FakeProc(lines())

        seen = []

        def sink(event):
            if event.kind == "text":
                seen.append(
                    (
                        event.text,
                        codex._load_session(self.work, SID).client_id,
                        [e["kind"] for e in self.events()],
                    )
                )

        self.run_codex(begun=started, popen=popen, sinks=[sink])
        self.assertEqual(observed[0][:2], (self.snapshot, None))
        self.assertEqual(observed[0][2], ["input", "session"])
        self.assertEqual(codex._load_session(self.work, SID).client_id, self.native)
        self.assertEqual(
            [e["client_id"] for e in self.events() if e["kind"] == "client_session"], [self.native]
        )
        self.assertEqual(seen[0][:2], ("later", self.native))
        self.assertIn("client_session", seen[0][2])

    def test_resume_keeps_original_state_and_progress_and_rejects_changed_id(self):
        self.run_codex()
        drive.note(self.params().channel, "progress", name="review", text="first turn")
        self.run_codex(resume=True)
        self.assertIn("first turn", [e["text"] for e in self.events() if e["kind"] == "progress"])
        self.assertEqual(
            [e["client_id"] for e in self.events() if e["kind"] == "client_session"],
            [self.native, self.native],
        )
        with self.assertRaisesRegex(compose.ConfigError, "different client ID"):
            self.run_codex("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", resume=True)
        self.assertEqual(codex._load_session(self.work, SID).client_id, self.native)

    def test_resume_rejects_changed_snapshot_before_spawn(self):
        self.run_codex()
        launch = drive.Launch(
            ["fake"],
            cwd=self.work,
            session_state={**self.snapshot, "network": True},
            writable_roots=[self.work, self.snapshot["codex_home"]],
        )
        popen = unittest.mock.Mock()
        with self.assertRaisesRegex(compose.ConfigError, "changed Codex recovery state"):
            drive.start(
                launch, run(), self.params(resume=True), client=self.client, popen=popen, sinks=[]
            )
        popen.assert_not_called()
        self.assertEqual(codex._load_session(self.work, SID).snapshot, self.snapshot)

    def test_forged_run_record_cannot_change_trusted_mapping(self):
        self.run_codex()
        drive.note(
            self.params().channel,
            "client_session",
            sid=SID,
            client_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )
        drive.note(self.params().channel, "session", sid=SID, cwd="/", project=True)
        self.assertEqual(codex._load_session(self.work, SID).client_id, self.native)
        self.assertEqual(
            self.client.resume_location(self.work, SID), (os.path.realpath(self.work), False)
        )
        self.run_codex(resume=True)
        self.assertEqual(
            codex._load_session(self.work, SID).snapshot["dirs"], [os.path.realpath(self.work)]
        )

    def test_driver_delegates_recovery_to_another_client(self):
        recorded = []

        class FutureClient(clients.Client):
            def prepare_session(self, launch, params):
                recorded.append(("prepare", launch.session_state, params.sid))

            def record_session(self, params, session_id):
                recorded.append(("session", params.sid, session_id))

            def events(self, lines):
                for _ in lines:
                    yield clients.Event("session", session_id="future:session-1")
                    yield clients.Event("text", "future output")

        payload = {"cursor": {"offset": 3}}
        launch = drive.Launch(["future"], cwd=self.work, session_state=payload)
        seen = []

        def popen(argv, **kw):
            self.assertEqual(recorded, [("prepare", payload, SID)])
            return FakeProc(["ready"])

        def sink(event):
            self.assertEqual(recorded[-1], ("session", SID, "future:session-1"))
            seen.append(event)

        drive.start(
            launch, run(), self.params(), client=FutureClient({}), sinks=[sink], popen=popen
        )
        self.assertEqual(
            recorded, [("prepare", payload, SID), ("session", SID, "future:session-1")]
        )
        self.assertIsNone(codex._load_session(self.work, SID))
        self.assertIn(clients.Event("text", "future output"), seen)

    def test_driver_session_info_delegates_to_client(self):
        info = drive.session_info(self.client, self.work, SID)
        self.assertTrue(info.resume_supported)
        self.assertFalse(info.exists)

    def test_no_native_id_does_not_count_as_a_recoverable_run(self):
        launch = drive.Launch(
            ["fake"],
            cwd=self.work,
            session_state=self.snapshot,
            writable_roots=[self.work, self.snapshot["codex_home"]],
        )
        result = drive.start(
            launch,
            run(),
            self.params(),
            client=self.client,
            sinks=[],
            popen=lambda argv, **kw: FakeProc(stream({"type": "turn.completed"})),
        )
        self.assertIsNone(result.outcome)
        self.assertIn("did not report a native session ID", result.error)
        self.assertIsNone(codex._load_session(self.work, SID).client_id)

    def test_startup_json_diagnostics_survive_without_accepting_unbound_reports(self):
        cases = (
            ({"type": "error", "message": "authentication required"}, "authentication required"),
            ({"type": "turn.failed", "error": {"message": "startup failed"}}, "startup failed"),
            (
                {"type": "thread.started", "thread_id": "invalid"},
                "codex: invalid thread.started ID",
            ),
        )
        for index, (event, diagnostic) in enumerate(cases):
            with self.subTest(event=event):
                params = self.params(sid=f"{SID}-{index}")
                launch = drive.Launch(
                    ["fake"],
                    cwd=self.work,
                    session_state=self.snapshot,
                    writable_roots=[self.work, self.snapshot["codex_home"]],
                )
                seen = []

                def popen(argv, **kw):
                    items = [progress("start", "unbound progress"), outcome(DONE), *stream(event)]
                    return FakeProc(feed(params.channel, items), rc=1)

                result = drive.start(
                    launch, run(), params, client=self.client, sinks=[seen.append], popen=popen
                )
                self.assertEqual(seen, [clients.Event("stderr", diagnostic)])
                self.assertEqual(
                    (result.returncode, result.outcome, result.error),
                    (1, None, "the client did not report a native session ID"),
                )
                self.assertEqual(
                    [e["text"] for e in self.events() if e["kind"] == "stderr"][-1], diagnostic
                )
                self.assertIsNone(self.client.session_info(self.work, params.sid).client_id)
                self.assertFalse(os.path.exists(params.out))

    def test_startup_process_stderr_is_logged_without_accepting_unbound_output(self):
        launch = drive.Launch(
            ["fake"],
            cwd=self.work,
            session_state=self.snapshot,
            writable_roots=[self.work, self.snapshot["codex_home"]],
        )
        params, seen = self.params(), []
        diagnostics = ["authentication failed", "please sign in"]

        def popen(argv, **kw):
            items = [
                progress("start", "unbound progress"),
                outcome(DONE),
                *stream(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "unbound text"},
                    }
                ),
            ]
            return FakeProc(
                feed(params.channel, items), rc=7, stderr=[line + "\n" for line in diagnostics]
            )

        result = drive.start(
            launch, run(), params, client=self.client, sinks=[seen.append], popen=popen
        )
        self.assertEqual(seen, [clients.Event("stderr", line) for line in diagnostics])
        self.assertEqual([e["text"] for e in self.events() if e["kind"] == "stderr"], diagnostics)
        self.assertEqual(
            (result.returncode, result.outcome, result.error),
            (7, None, "the client did not report a native session ID"),
        )
        self.assertIsNone(self.client.session_info(self.work, SID).client_id)
        self.assertFalse(os.path.exists(params.out))

    def test_fsync_failure_stops_host_before_later_output_and_reports(self):
        real_fsync = os.fsync
        for target in ("state", "mirror"):
            with self.subTest(target=target):
                sid = SID + target
                params = self.params(sid=sid)
                process = FakeProc(
                    stream(
                        {"type": "thread.started", "thread_id": self.native},
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "later"},
                        },
                    )
                )
                failed = []

                def fsync(fd):
                    info = os.fstat(fd)
                    if stat.S_ISREG(info.st_mode):
                        if target == "state":
                            root = os.path.join(
                                os.path.dirname(self.private), ".agent-pm", "core-sessions"
                            )
                            paths = [
                                os.path.join(root, name)
                                for name in os.listdir(root)
                                if name.startswith(".state-")
                            ]
                        else:
                            paths = [params.channel]
                        for path in paths:
                            if os.path.exists(path) and os.stat(path).st_ino == info.st_ino:
                                with open(path) as f:
                                    data = f.read()
                                if (
                                    target == "state"
                                    and f'"client_id":"{self.native}"' in data
                                    or target == "mirror"
                                    and '"kind": "client_session"' in data
                                ):
                                    failed.append(data)
                                    raise OSError(errno.ENOSPC, "cannot fsync mapping")
                    return real_fsync(fd)

                def popen(argv, **kw):
                    drive.note(params.channel, "progress", name="start", text="queued report")
                    return process

                seen = []
                launch = drive.Launch(
                    ["fake"],
                    cwd=self.work,
                    session_state=self.snapshot,
                    writable_roots=[self.work, self.snapshot["codex_home"]],
                )
                with unittest.mock.patch.object(os, "fsync", fsync):
                    with self.assertRaisesRegex(OSError, "cannot fsync mapping"):
                        drive.start(
                            launch,
                            run(),
                            params,
                            client=self.client,
                            popen=popen,
                            sinks=[seen.append],
                        )
                self.assertTrue(failed, "must reach fsync after writing the native ID")
                self.assertTrue(process.killed)
                self.assertEqual(seen, [])
                self.assertEqual(
                    codex._load_session(self.work, sid).client_id,
                    None if target == "state" else self.native,
                )

    def test_session_query_exposes_native_file_and_manual_resume_without_usage_guess(self):
        self.run_codex()
        path = os.path.join(
            self.snapshot["codex_home"],
            "sessions",
            "2026",
            "10",
            "10",
            f"rollout-2026-10-10-{self.native}.jsonl",
        )
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": self.native, "cwd": self.snapshot["cwd"]},
                    }
                )
                + "\n"
            )
        info = drive.session_info(self.client, self.work, SID)
        self.assertEqual((info.client_id, info.exists, info.transcript), (self.native, True, path))
        self.assertIsInstance(info.last_activity, float)
        self.assertIn("codex resume -s workspace-write", info.resume)
        self.assertIn(self.native, info.resume)
        self.assertFalse(info.usage_supported)
        self.assertIsNone(info.usage)


if __name__ == "__main__":
    unittest.main()
