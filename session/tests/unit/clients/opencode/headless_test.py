"""OpenCode's headless launches: the launcher's argv, the run's flags and its OpenCode config."""

import json
import os
import re
import sys
from dataclasses import replace

from agent_pm.session.clients.opencode import OpenCodeClient, launcher
from agent_pm.session.engine.types import (
    LOCAL_CONTEXT,
    Access,
    Activity,
    ClientBound,
    ClientContext,
    ConfigError,
    Effort,
    Launch,
    Permission,
    Reply,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
)

from session.tests.case import Case
from session.tests.unit.clients.opencode.support import SES, packaged_table

MODEL = "ollama/qwen3.6:35b-mlx"


def decide(rules: dict, path: str) -> str | None:
    """OpenCode's verdict for an edit of `path` (relative to its worktree): its last matching rule,
    a rule's `*` any text (permission/index.ts evaluate, util/wildcard.ts match at v1.19.3).
    """
    found = None
    for pattern, action in rules.items():
        if re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), path, re.S):
            found = action
    return found


def holds(argv, words) -> bool:
    return any(tuple(argv[i : i + len(words)]) == tuple(words) for i in range(len(argv)))


class Base(Case):
    def setUp(self):
        self.cwd = os.path.realpath(os.path.join(self.home, "project"))
        os.mkdir(self.cwd)
        self.client = OpenCodeClient(packaged_table())

    def spec(self, **fields) -> Spec:
        access = fields.pop("access", Access(cwd=self.cwd))
        return Spec(workdir=self.home, access=access, sid="run-1", prompt="Hi there", **fields)

    def launch(self, spec: Spec, context: ClientContext = LOCAL_CONTEXT, client=None) -> Launch:
        run = (client or self.client).headless()
        assert run is not None
        run._context = context
        return run.launch(spec)

    def made(self, name: str) -> str:
        path = os.path.join(self.home, name)
        os.mkdir(path)
        return os.path.realpath(path)

    def command(self, launch: Launch) -> tuple[str, ...]:
        return launch.argv[launch.argv.index("--") + 1 :]

    def inline(self, launch: Launch) -> dict:
        return json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])


class LaunchTest(Base):
    def test_the_launcher_runs_opencode_with_the_prompt_and_timeout(self):
        launch = self.launch(self.spec())
        self.assertEqual(
            launch.argv[: launch.argv.index("--") + 1],
            (
                os.path.realpath(sys.executable),
                "-I",
                launcher.__file__,
                "--timeout",
                "120",
                "--prompt",
                "Hi there",
                "--",
            ),
        )
        self.assertEqual(self.command(launch)[:4], ("opencode", "run", "--format", "json"))
        self.assertNotIn("Hi there", self.command(launch))
        self.assertEqual((launch.cwd, launch.project, launch.resume), (self.cwd, False, ""))
        self.assertIn("OPENCODE_PERMISSION", launch.unset)
        self.assertIn("OPENCODE_PID", launch.unset)

    def test_tier_model(self):
        self.assertTrue(holds(self.command(self.launch(self.spec(tier=3))), ("--model", MODEL)))

    def test_missing_tier(self):
        client = OpenCodeClient({**packaged_table(), "tiers": {"1": MODEL}})
        with self.assertRaisesRegex(ConfigError, "no model for tier 3"):
            self.launch(self.spec(tier=3), client=client)

    def test_no_tier(self):
        self.assertNotIn("--model", self.command(self.launch(self.spec())))

    def test_variant(self):
        command = self.command(self.launch(self.spec(effort=Effort.HIGH)))
        self.assertTrue(holds(command, ("--variant", "high")))

    def test_no_effort(self):
        self.assertNotIn("--variant", self.command(self.launch(self.spec())))

    def test_missing_effort(self):
        client = OpenCodeClient({**packaged_table(), "efforts": {"low": "low"}})
        with self.assertRaisesRegex(ConfigError, "no variant for effort 'high'"):
            self.launch(self.spec(effort=Effort.HIGH), client=client)

    def test_ollama_url_local(self):
        provider = self.inline(self.launch(self.spec()))["provider"]["ollama"]
        self.assertEqual(
            provider,
            {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama",
                "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                "models": {"qwen3.6:35b-mlx": {"name": "qwen3.6:35b-mlx"}},
            },
        )

    def test_ollama_url_docker_address(self):
        context = ClientContext("host.docker.internal", Permission.YOLO)
        provider = self.inline(self.launch(self.spec(), context))["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://host.docker.internal:11434/v1")

    def test_port(self):
        client = OpenCodeClient({**packaged_table(), "ollama_port": 11500})
        provider = self.inline(self.launch(self.spec(), client=client))["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:11500/v1")

    def test_only_ollama_tiers_are_listed(self):
        tiers = {"1": "ollama/a", "2": "ollama/a", "3": "other/b", "4": "ollama/c"}
        client = OpenCodeClient({**packaged_table(), "tiers": tiers})
        models = self.inline(self.launch(self.spec(), client=client))["provider"]["ollama"]
        self.assertEqual(models["models"], {"a": {"name": "a"}, "c": {"name": "c"}})

    def test_auto_has_no_auto_flag(self):
        self.assertNotIn("--auto", self.command(self.launch(self.spec())))

    def test_yolo_has_auto(self):
        context = replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        self.assertIn("--auto", self.command(self.launch(self.spec(), context)))

    def test_bash_ask(self):
        self.assertEqual(self.inline(self.launch(self.spec()))["permission"]["bash"], "ask")

    def test_a_refused_call_does_not_end_the_turn(self):
        inline = self.inline(self.launch(self.spec()))
        self.assertEqual(inline["experimental"], {"continue_loop_on_deny": True})

    def test_commands_allowed(self):
        access = Access(cwd=self.cwd, commands=("git status", "make test"))
        permission = self.inline(self.launch(self.spec(access=access)))["permission"]
        self.assertEqual(
            permission["bash"], {"*": "ask", "git status": "allow", "make test": "allow"}
        )

    def test_reads_and_dirs_are_external_directories(self):
        extra, read = self.made("extra"), self.made("read")
        access = Access(cwd=self.cwd, dirs=(extra,), reads=(read,))
        permission = self.inline(self.launch(self.spec(access=access)))["permission"]
        self.assertEqual(
            permission["external_directory"],
            {read: "allow", f"{read}/*": "allow", extra: "allow", f"{extra}/*": "allow"},
        )
        self.assertNotIn("external_directory", self.inline(self.launch(self.spec()))["permission"])

    def test_edits_stay_in_the_worktree_cwd_and_dirs(self):
        extra, read = self.made("extra"), self.made("read")
        access = Access(cwd=self.cwd, dirs=(extra,), reads=(read,))
        rules = self.inline(self.launch(self.spec(access=access)))["permission"]["edit"]
        checkout = os.path.join(self.home, "other", "checkout")
        for worktree in ("/", checkout, os.path.dirname(self.cwd)):
            with self.subTest(worktree=worktree):
                for path, action in (
                    (os.path.join(read, "task.md"), "deny"),
                    (os.path.join(read, "sub", "task.md"), "deny"),
                    (os.path.join(self.cwd, "a.py"), "allow"),
                    (os.path.join(extra, "sub", "b.md"), "allow"),
                    (os.path.join(self.cwd, ".opencode", "plugins", "x.ts"), "deny"),
                    (os.path.join(self.cwd, "opencode.json"), "deny"),
                    (os.path.join(self.cwd, "pkg", "opencode.jsonc"), "deny"),
                ):
                    found = decide(rules, os.path.relpath(path, worktree)) or "allow"
                    self.assertEqual(found, action, path)
        self.assertEqual(decide(rules, "../elsewhere/x.py"), "deny")
        self.assertIsNone(decide(rules, "src/inside_the_worktree.py"))

    def test_autoupdate_off(self):
        client = OpenCodeClient({**packaged_table(), "env": {"OPENCODE_DISABLE_AUTOUPDATE": "0"}})
        spec = self.spec(env={"OPENCODE_DISABLE_AUTOUPDATE": "0"})
        launch = self.launch(spec, client=client)
        self.assertEqual(launch.env["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        self.assertIn('"autoupdate": false', launch.env["OPENCODE_CONFIG_CONTENT"])

    def test_env_layers(self):
        client = OpenCodeClient({**packaged_table(), "env": {"A": "config", "B": "config"}})
        access = Access(cwd=self.cwd, env={"B": "access", "C": "access"})
        launch = self.launch(self.spec(access=access, env={"C": "spec"}), client=client)
        self.assertEqual(
            {k: launch.env[k] for k in "ABC"}, {"A": "config", "B": "access", "C": "spec"}
        )

    def test_project_config(self):
        self.assertEqual(self.launch(self.spec()).env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        project = self.launch(self.spec(access=Access(cwd=self.cwd, project=True)))
        self.assertNotIn("OPENCODE_DISABLE_PROJECT_CONFIG", project.env)

    def test_container_host_refused(self):
        context = ClientContext("host.docker.internal", Permission.YOLO, bin_dir="/opt/bin")
        with self.assertRaisesRegex(ConfigError, "runs on a local Host only"):
            self.launch(self.spec(), context)

    def test_spec_selector_refused(self):
        for flags in (("--session", SES), ("--format", "default"), ("--dir", "/"), ("--auto",)):
            with self.subTest(flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(flags=flags))

    def test_spec_env_overriding_the_config_is_refused(self):
        for spec in (
            self.spec(env={"OPENCODE_PERMISSION": "{}"}),
            self.spec(access=Access(cwd=self.cwd, env={"OPENCODE_CONFIG": "/x"})),
        ):
            with self.subTest(spec=spec), self.assertRaises(ConfigError):
                self.launch(spec)

    def test_flags_follow_the_config_and_mode(self):
        client = OpenCodeClient({**packaged_table(), "flags": ["--agent", "build"]})
        context = replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        command = self.command(self.launch(self.spec(flags=("--title", "t")), context, client))
        self.assertEqual(command[-5:], ("--agent", "build", "--auto", "--title", "t"))


class ResumeTest(Base):
    def test_bound_session_resumes(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        command = self.command(self.launch(self.spec(resume=True)))
        self.assertEqual(command[:6], ("opencode", "run", "--format", "json", "--session", SES))

    def test_a_new_run_selects_no_session(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertNotIn("--session", self.command(self.launch(self.spec())))

    def test_unbound_resume_fails(self):
        with self.assertRaisesRegex(ConfigError, "no OpenCode session bound to run ID 'run-1'"):
            self.launch(self.spec(resume=True))

    def test_tampered_binding_refused(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        (name,) = os.listdir(self.client.common.store)
        path = os.path.join(self.client.common.store, name)
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**record, "native": "--auto"}, f)
        with self.assertRaises(ConfigError):
            self.launch(self.spec(resume=True))


def line(kind: str, **fields) -> str:
    return json.dumps({"type": kind, "timestamp": 1, "sessionID": SES, **fields})


def text(value: str) -> str:
    return line("text", part={"type": "text", "text": value, "time": {"start": 1, "end": 2}})


class EventsTest(Base):
    def events(self, *lines: str, spec: Spec | None = None) -> list:
        run = self.client.headless()
        assert run is not None
        run.launch(spec or self.spec())
        return list(run.events(lines))

    def test_a_run_binds_replies_and_ends_with_its_last_text(self):
        found = self.events(
            line("step_start", part={"type": "step-start"}),
            text("one"),
            line("tool_use", part={"tool": "read", "state": {"status": "completed"}}),
            text("two"),
            line("step_finish", part={"reason": "stop"}),
        )
        self.assertEqual(
            found,
            [
                ClientBound(SES),
                Activity("step_start"),
                Reply("one"),
                Activity("read"),
                Reply("two"),
                Activity("step_finish"),
                TurnEnd("two"),
            ],
        )
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)

    def test_a_blank_text_is_no_reply(self):
        self.assertEqual(self.events(text("  ")), [ClientBound(SES), TurnEnd("")])

    def test_a_rejected_tool_is_stderr_and_the_turn_still_ends(self):
        rejected = "The user rejected permission to use this specific tool call."
        found = self.events(
            line(
                "tool_use", part={"tool": "bash", "state": {"status": "error", "error": rejected}}
            ),
            text("I cannot run it."),
        )
        self.assertEqual(
            found,
            [
                ClientBound(SES),
                Activity("bash"),
                Stderr(f"opencode: bash: {rejected}"),
                Reply("I cannot run it."),
                TurnEnd("I cannot run it."),
            ],
        )

    def test_an_error_fails_the_turn(self):
        error = {"name": "UnknownError", "data": {"message": "Model not found: p/m."}}
        found = self.events(line("error", error=error))
        self.assertEqual(
            found, [ClientBound(SES), TurnFailed("UnknownError", "Model not found: p/m.")]
        )

    def test_an_error_without_data_keeps_its_name(self):
        found = self.events(line("error", error={"name": "APIError"}), text("late"))
        self.assertEqual(found, [ClientBound(SES), TurnFailed("APIError", ""), Reply("late")])

    def test_no_session_fails_the_turn(self):
        found = self.events("not json", json.dumps({"type": "text"}))
        self.assertEqual(
            found,
            [
                Stderr("opencode: invalid JSON event"),
                Activity("text"),
                TurnFailed("no_session", "opencode ended before it started a session"),
            ],
        )
        self.assertIsNone(self.client.common.native(self.cwd, "run-1"))

    def test_an_invalid_session_id_binds_nothing(self):
        found = self.events(json.dumps({"type": "text", "sessionID": "--auto"}))
        self.assertEqual(found[0], Stderr("opencode: invalid sessionID '--auto'"))
        self.assertEqual(
            found[-1], TurnFailed("no_session", "opencode ended before it started a session")
        )

    def test_the_first_session_binds_once(self):
        other = "ses_zzzzzzzzzzzzzzzzzzzzzzzzzz"
        found = self.events(text("a"), json.dumps({"type": "text", "sessionID": other}))
        self.assertEqual(found.count(ClientBound(SES)), 1)
        self.assertNotIn(ClientBound(other), found)
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)

    def test_a_resume_rebinds_its_session(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        found = self.events(text("again"), spec=self.spec(resume=True))
        self.assertEqual(found, [ClientBound(SES), Reply("again"), TurnEnd("again")])
