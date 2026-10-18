"""OpenCode's TUI: launch argv and env, flag refusals, hook events and the hook callback."""

import json
import os
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_pm.session.clients.opencode import OpenCodeClient, tui
from agent_pm.session.clients.opencode.common import Bindings
from agent_pm.session.engine.types import (
    LOCAL_CONTEXT,
    Access,
    Activity,
    Blocked,
    ClientBound,
    ClientContext,
    ConfigError,
    HookCall,
    HookKind,
    Launch,
    Permission,
    Spec,
    TurnEnd,
    TurnFailed,
)

from session.tests.case import Case
from session.tests.unit.clients.opencode.support import SES, fake_git, packaged_table

HOOK = ("/bin/agent-pm-session", "hook", "/home/x/.agent-pm/tui/w1")
OTHER = "ses_otherOtherOtherOther0001"


class Base(Case):
    def setUp(self):
        self.cwd = os.path.realpath(os.path.join(self.home, "project"))
        os.mkdir(self.cwd)
        self.client = OpenCodeClient(packaged_table())
        self.enterContext(mock.patch("subprocess.run", side_effect=fake_git))

    def spec(self, **fields) -> Spec:
        fields.setdefault("prompt", "Hi there")
        fields.setdefault("access", Access(cwd=self.cwd))
        return Spec(workdir=self.home, sid="run-1", **fields)

    def run_tui(self, client=None, context: ClientContext = LOCAL_CONTEXT) -> tui.OpenCodeTui:
        run = (client or self.client).tui()
        assert isinstance(run, tui.OpenCodeTui)
        run._context = context
        return run

    def launch(self, spec: Spec, client=None, context=LOCAL_CONTEXT) -> Launch:
        return self.run_tui(client, context).launch(spec, HOOK)


class LaunchTest(Base):
    def test_a_fresh_start_submits_its_prompt(self):
        launch = self.launch(self.spec(tier=1))
        self.assertEqual(
            launch.argv,
            ("opencode", "--model", "ollama/qwen3.6:35b-mlx", "--prompt=Hi there"),
        )
        self.assertEqual((launch.cwd, launch.project), (self.cwd, False))

    def test_no_prompt_no_flag_and_no_variant(self):
        launch = self.launch(self.spec(prompt="", effort="high"))
        self.assertEqual(launch.argv, ("opencode",))

    def test_a_resume_selects_its_session_and_opens_with_the_prompt(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        run = self.run_tui()
        launch = run.launch(self.spec(resume=True, prompt="Fix it."), HOOK)
        self.assertEqual(launch.argv, ("opencode", "--session", SES))
        self.assertEqual(run.opening, (tui.START, "Fix it."))
        self.assertEqual(launch.env["AGENT_PM_OPENCODE_SESSION"], SES)
        bare = self.run_tui()
        bare.launch(self.spec(resume=True, prompt=""), HOOK)
        self.assertIsNone(bare.opening)

    def test_resume_without_a_binding_fails(self):
        with self.assertRaisesRegex(ConfigError, "no OpenCode session bound to run ID 'run-1'"):
            self.launch(self.spec(resume=True))

    def test_the_env_carries_the_plugin_and_the_hook(self):
        launch = self.launch(self.spec())
        config = json.loads(launch.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["plugin"], [Path(tui.PLUGIN).resolve().as_uri()])
        self.assertTrue(os.path.isfile(tui.PLUGIN))
        self.assertEqual(config["permission"]["bash"], "ask")
        self.assertEqual(json.loads(launch.env["AGENT_PM_OPENCODE_HOOK"]), list(HOOK))
        self.assertEqual(launch.env["AGENT_PM_OPENCODE_SESSION"], "")
        self.assertEqual(launch.env["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        self.assertIn("OPENCODE_PURE", launch.unset)

    def test_flags_follow_the_config_and_mode(self):
        client = OpenCodeClient({**packaged_table(), "flags": ["--agent", "build"]})
        context = replace(LOCAL_CONTEXT, permission=Permission.YOLO)
        launch = self.launch(self.spec(flags=("--log-level", "INFO")), client, context)
        self.assertEqual(
            launch.argv,
            ("opencode", "--agent", "build", "--auto", "--log-level", "INFO", "--prompt=Hi there"),
        )

    def test_a_container_host_is_refused(self):
        context = ClientContext("host.docker.internal", Permission.YOLO, bin_dir="/opt/bin")
        with self.assertRaisesRegex(ConfigError, "runs on a local Host only"):
            self.launch(self.spec(), context=context)


class RefusalTest(Base):
    SELECTORS = (
        ("-s", SES),
        ("--session", SES),
        (f"--session={SES}",),
        ("-c",),
        ("--continue",),
        ("--fork",),
    )

    def test_selectors_in_flags_are_refused(self):
        for flags in self.SELECTORS:
            with self.subTest(source="spec", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(flags=flags))
            with self.subTest(source="config", flags=flags), self.assertRaises(ConfigError):
                OpenCodeClient({**packaged_table(), "flags": list(flags)})
            with self.subTest(source="mode", flags=flags), self.assertRaises(ConfigError):
                OpenCodeClient({**packaged_table(), "modes": {"auto": list(flags)}})

    def test_prompt_pure_and_pure_env_are_refused(self):
        for flags in (("--prompt", "x"), ("--prompt=x",), ("--pure",)):
            with self.subTest(source="spec", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(flags=flags))
            client = OpenCodeClient({**packaged_table(), "flags": list(flags)})
            with self.subTest(source="config", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(), client)
            client = OpenCodeClient({**packaged_table(), "modes": {"auto": list(flags)}})
            with self.subTest(source="mode", flags=flags), self.assertRaises(ConfigError):
                self.launch(self.spec(), client)
        for spec in (
            self.spec(env={"OPENCODE_PURE": "1"}),
            self.spec(access=Access(cwd=self.cwd, env={"OPENCODE_PURE": "1"})),
        ):
            with self.subTest(env=spec.env), self.assertRaisesRegex(ConfigError, "OPENCODE_PURE"):
                self.launch(spec)
        client = OpenCodeClient({**packaged_table(), "env": {"OPENCODE_PURE": "1"}})
        with self.assertRaisesRegex(ConfigError, "OPENCODE_PURE"):
            self.launch(self.spec(), client)

    def test_headless_keeps_pure(self):
        client = OpenCodeClient({**packaged_table(), "flags": ["--pure"]})
        run = client.headless()
        assert run is not None
        self.assertIn("--pure", run.launch(self.spec()).argv)


class CommitTest(Base):
    def test_a_fresh_start_unbinds_and_a_resume_keeps_its_binding(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        run = self.run_tui()
        run.launch(self.spec(resume=True), HOOK)
        run.commit()
        self.assertEqual(self.client.common.native(self.cwd, "run-1"), SES)
        run = self.run_tui()
        run.launch(self.spec(), HOOK)
        run.commit()
        self.assertIsNone(self.client.common.native(self.cwd, "run-1"))

    def test_the_callback_names_the_cwd_and_store(self):
        run = self.run_tui()
        run.launch(self.spec(), HOOK)
        callback = run.hook_callback()
        self.assertEqual(callback.module, tui.__name__)
        self.assertEqual(callback.state, {"cwd": self.cwd, "store": self.client.common.store})

    def test_restore_checks_its_state(self):
        run = self.run_tui()
        run.restore(self.spec(), {"cwd": self.cwd, "store": self.client.common.store})
        for state in ({}, {"cwd": "relative", "store": "/s"}, {"cwd": "/c", "store": 3}):
            with self.subTest(state=state), self.assertRaisesRegex(ConfigError, "bad state"):
                self.run_tui().restore(self.spec(), state)


class OnHookTest(Base):
    def call(self, payload, **state) -> dict | None:
        state = {"cwd": self.cwd, "store": self.client.common.store, **state}
        return tui.on_hook(HookCall("session.idle", payload, "run-1", self.home, None, state))

    def bound(self) -> str | None:
        return Bindings(self.client.common.store).native(self.cwd, "run-1")

    def test_an_unbound_run_binds_the_first_session(self):
        payload = {"session_id": SES, "last_assistant_message": "hi"}
        self.assertEqual(self.call(payload), payload)
        self.assertEqual(self.bound(), SES)

    def test_its_own_session_passes(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertEqual(self.call({"session_id": SES}), {"session_id": SES})

    def test_another_session_is_dropped(self):
        self.client.common.bind(self.cwd, "run-1", SES)
        self.assertIsNone(self.call({"session_id": OTHER}))
        self.assertEqual(self.bound(), SES)

    def test_a_bad_id_or_state_is_dropped(self):
        for payload in ({}, {"session_id": "--auto"}, {"session_id": 3}):
            with self.subTest(payload=payload):
                self.assertIsNone(self.call(payload))
        self.assertIsNone(self.call({"session_id": SES}, cwd="relative"))
        self.assertIsNone(self.call({"session_id": SES}, store=None))
        self.assertIsNone(self.bound())

    def test_a_corrupt_binding_is_rebound(self):
        self.client.common.bind(self.cwd, "run-1", OTHER)
        (name,) = os.listdir(self.client.common.store)
        Path(self.client.common.store, name).write_text("not json")
        self.assertEqual(self.call({"session_id": SES}), {"session_id": SES})
        self.assertEqual(self.bound(), SES)


class EventsTest(Base):
    def test_the_hook_labels(self):
        self.assertEqual(
            dict(tui.OpenCodeTui.hooks),
            {
                "session.idle": HookKind.TURN_END,
                "session.error": HookKind.TURN_FAILED,
                "permission.asked": HookKind.BLOCKED,
                "session.busy": HookKind.ACTIVITY,
                "permission.replied": HookKind.ACTIVITY,
                "tool": HookKind.ACTIVITY,
            },
        )

    def test_events(self):
        run = self.run_tui()
        self.assertEqual(run.hook_events(tui.START, {"session_id": SES}), [ClientBound(SES)])
        self.assertEqual(run.hook_events("session.busy", {"session_id": SES}), [Activity("")])
        self.assertEqual(
            run.hook_events("tool", {"session_id": SES, "tool_name": "bash"}), [Activity("bash")]
        )
        self.assertEqual(
            run.hook_events("permission.asked", {"session_id": SES, "permission": "bash"}),
            [Blocked("bash")],
        )
        self.assertEqual(
            run.hook_events("session.idle", {"session_id": SES, "last_assistant_message": "Done."}),
            [TurnEnd("Done.")],
        )
        self.assertEqual(run.hook_events("session.idle", {"session_id": SES}), [TurnEnd("")])
        self.assertEqual(
            run.hook_events(
                "session.error",
                {"session_id": SES, "error": "APIError", "message": "connection refused"},
            ),
            [TurnFailed("APIError", "connection refused")],
        )
        self.assertEqual(
            run.hook_events("session.error", {"session_id": SES}), [TurnFailed("unknown", "")]
        )

    def test_the_client_is_opencode_tui(self):
        self.assertIsInstance(self.client.tui(), tui.OpenCodeTui)
