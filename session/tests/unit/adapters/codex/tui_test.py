"""Codex interactive launch policy, hook events and native limits."""

import contextlib
import dataclasses
import io
import json
import os
import shlex
import sys
import time
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.session.adapters.codex import CodexAdapter, common, tui
from agent_pm.session.adapters.codex.tui import CodexTui
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    HookCall,
    HookCallback,
    HookKind,
    Key,
    LimitHit,
    Paste,
    TurnEnd,
)
from agent_pm.utils import LOCAL_ENV_VARS

from session.tests.case import ROOTS, SRC
from session.tests.codex_tui import (
    INTERPRETER_PATHS,
    INTERPRETER_ROOTS,
    PROTECTED_ROOTS,
    PlanCase,
    interpreter,
)
from session.tests.source_roots import PACKAGE_ROOTS
from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base, committed_tui

HOOK = (
    sys.executable,
    "-I",
    "/protected/driver_hook.py",
    "context",
    json.dumps([SRC["session"], SRC["utils"]]),
)


class TuiTest(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(interpreter())

    def launch(self, *, resume=False, adapter=None):
        run = (adapter or self.adapter).tui()
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            mock.patch("subprocess.Popen") as proc,
        ):
            launch = run.launch(
                dataclasses.replace(self.spec, prompt="quoted ' prompt\nnext", resume=resume),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            )
        proc.assert_not_called()
        run.commit()
        return run, launch, json.loads(launch.argv[-1])

    def test_each_hook_label_maps_to_its_hook_kind(self):
        self.assertEqual(
            CodexTui.hooks,
            {
                "SessionStart": HookKind.ACTIVITY,
                "UserPromptSubmit": HookKind.ACTIVITY,
                "PostToolUse": HookKind.ACTIVITY,
                "PermissionRequest": HookKind.BLOCKED,
                "Stop": HookKind.TURN_END,
                "Interrupt": HookKind.TURN_END,
            },
        )
        for kind in CodexTui.hooks.values():
            self.assertIs(type(kind), HookKind)

    def test_the_launch_protects_packages_and_hook_namespaces(self):
        _, _, data = self.launch()
        self.assertEqual(data["protect"], list(PROTECTED_ROOTS))

    def test_writable_interpreter_libraries_and_site_files_are_rejected_before_commit(self):
        for index, root in enumerate(INTERPRETER_ROOTS):
            for path in (root, os.path.join(root, "startup.pth")):
                spec = dataclasses.replace(
                    self.spec,
                    sid=f"interpreter-{index}",
                    access=dataclasses.replace(self.spec.access, dirs=(path,)),
                )
                with (
                    self.subTest(path=path),
                    mock.patch("shutil.which", return_value="/protected/codex"),
                    self.assertRaisesRegex(ConfigError, "could rewrite hook dependency"),
                ):
                    self.adapter.tui().launch(spec, HOOK, source_roots=PACKAGE_ROOTS)
                self.assertIsNone(self.common.load(str(self.work), spec.sid))
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_a_virtual_environment_inside_the_cwd_is_rejected_before_commit(self):
        venv = self.cwd / ".venv" / "lib" / "python3.11" / "site-packages"
        venv.mkdir(parents=True)
        layout = {**INTERPRETER_PATHS, "purelib": str(venv), "platlib": str(venv)}
        spec = dataclasses.replace(self.spec, sid="venv")
        with (
            interpreter(layout, (str(venv),)),
            mock.patch("shutil.which", return_value="/protected/codex"),
            self.assertRaises(ConfigError) as raised,
        ):
            self.adapter.tui().launch(spec, HOOK, source_roots=PACKAGE_ROOTS)
        self.assertEqual(
            str(raised.exception),
            f"Codex tui writable root {self.cwd} could rewrite hook dependency {venv}",
        )
        self.assertIsNone(self.common.load(str(self.work), spec.sid))

    def test_resume_rejects_saved_library_grants_without_rewriting_recovery(self):
        library = INTERPRETER_ROOTS[0]
        spec = dataclasses.replace(
            self.spec,
            access=dataclasses.replace(self.spec.access, dirs=(library,)),
        )
        self.common.save(self.common.prepare(spec))
        self.common.bind(str(self.work), "caller-id", NATIVE)
        self.rollout()
        before = self.record().read_bytes()
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            self.assertRaisesRegex(ConfigError, "could rewrite hook dependency"),
        ):
            self.adapter.tui().launch(
                dataclasses.replace(spec, resume=True, access=self.spec.access),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            )
        self.assertEqual(self.record().read_bytes(), before)
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_other_interpreter_site_paths_are_protected_including_pth_files(self):
        site_path = "/external/interpreter/site-packages"
        with mock.patch("site.getsitepackages", return_value=[site_path]):
            _, _, data = self.launch()
            self.assertIn(site_path, data["protect"])
            spec = dataclasses.replace(
                self.spec,
                sid="external-site",
                access=dataclasses.replace(self.spec.access, dirs=(site_path + "/startup.pth",)),
            )
            with (
                mock.patch("shutil.which", return_value="/protected/codex"),
                self.assertRaisesRegex(ConfigError, "could rewrite hook dependency"),
            ):
                self.adapter.tui().launch(spec, HOOK, source_roots=PACKAGE_ROOTS)
        self.assertIsNone(self.common.load(str(self.work), spec.sid))

    def test_launch_preserves_opaque_hook_words_and_separate_roots(self):
        roots = tuple(reversed(PACKAGE_ROOTS))
        for index, hook in enumerate(
            (
                (*HOOK, "extra opaque word"),
                (*HOOK[:-1], json.dumps(list(roots), indent=2, ensure_ascii=False)),
                (*HOOK[:-1], "not JSON at all"),
            )
        ):
            spec = dataclasses.replace(self.spec, sid=f"opaque-{index}")
            with (
                self.subTest(hook=hook),
                mock.patch("shutil.which", return_value="/protected/codex"),
            ):
                launch = committed_tui(
                    self.adapter, dataclasses.replace(spec, prompt="hi"), hook, source_roots=roots
                )
            data = json.loads(launch.argv[-1])
            self.assertEqual(data["import_roots"], list(roots))
            self.assertEqual(data["hook"], list(hook))
            plan = tui.decode(data)
            for label, command in tui.commands(plan.hook).items():
                self.assertEqual(shlex.split(command), [*hook, label])

    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_factory_launch_flags_and_opaque_hook(self):
        self.assertIsNot(self.adapter.tui(), self.adapter.tui())
        run, launch, data = self.launch()
        self.assertEqual(launch.argv[1], "-I")
        self.assertTrue(launch.argv[2].endswith("/adapters/codex/tui/start.py"))
        plan = tui.decode(data)
        argv = tui.execution_argv(plan, run.state.snapshot, "hooks.state={trusted}")
        self.assertEqual(argv[:4], ["/protected/codex", "--no-daemon", "-C", str(self.cwd)])
        self.assertEqual(argv[-2:], ["--", "quoted ' prompt\nnext"])
        self.assertIn('tui.notification_method="bel"', argv)
        self.assertNotIn("sandbox_workspace_write.writable_roots=[]", argv)
        self.assertIn("model_reasoning_effort=low", argv)
        self.assertIn("gpt-test", argv)
        self.assertEqual(data["hook"], list(HOOK))
        self.assertEqual(plan.hook.argv, HOOK)
        commands = tui.commands(plan.hook)
        self.assertEqual(set(commands), set(run.hooks))
        for label, command in commands.items():
            self.assertEqual(shlex.split(command), [*HOOK, label])
        self.assertEqual(run.hook_callback(), HookCallback(tui.__name__, {"store": data["store"]}))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_resume_uses_shared_saved_policy_and_native_file(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=("-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=false"),
        )
        saved = CodexAdapter(self.adapter.config).common.save(
            CodexAdapter(self.adapter.config).common.prepare(self.spec)
        )
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        other = CodexAdapter({"env": {"CODEX_HOME": str(self.extra)}})
        run, launch, data = self.launch(resume=True, adapter=other)
        argv = tui.execution_argv(tui.decode(data), run.state.snapshot, "hooks.state={trusted}")
        self.assertEqual(argv[:3], ["/protected/codex", "resume", NATIVE])
        self.assertIn("gpt-test", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertEqual(launch.env["CODEX_HOME"], saved.snapshot.codex_home)
        self.assertIn(NATIVE, launch.resume)

    def test_the_session_env_is_last_in_the_launch_env_and_its_resume(self):
        access = dataclasses.replace(self.spec.access, env={"PYTHONPATH": "/a b;c"})
        self.spec = dataclasses.replace(self.spec, access=access)
        adapter = CodexAdapter({**self.adapter.config, "env": {"PYTHONPATH": "/config"}})
        _, launch, _ = self.launch(adapter=adapter)
        self.assertEqual(launch.env["PYTHONPATH"], "/a b;c")
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        self.spec = dataclasses.replace(self.spec, access=Access())
        _, resumed, _ = self.launch(resume=True, adapter=adapter)
        self.assertEqual(resumed.env["PYTHONPATH"], "/a b;c")
        self.assertEqual(shlex.split(resumed.resume)[3], "PYTHONPATH=/a b;c")

    def test_invalid_launch_plan_saves_no_recovery_state(self):
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            self.assertRaisesRegex(ConfigError, "invalid prompt"),
        ):
            committed_tui(
                self.adapter,
                dataclasses.replace(self.spec, prompt="invalid\0prompt"),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid))
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_invalid_launch_inputs_leave_no_state_directory(self):
        for prompt, hook, roots in (
            ("surrogate\ud800", HOOK, PACKAGE_ROOTS),
            ("hi", (), PACKAGE_ROOTS),
            ("hi", ("relative-hook",), PACKAGE_ROOTS),
            ("hi", HOOK, ()),
            ("hi", HOOK, (*PACKAGE_ROOTS, PACKAGE_ROOTS[0])),
            ("hi", HOOK, ("relative-root",)),
            ("hi", HOOK, (str(self.root / "missing"),)),
        ):
            with (
                self.subTest(prompt=prompt, hook=hook, roots=roots),
                mock.patch("shutil.which", return_value="/protected/codex"),
                self.assertRaises(ConfigError),
            ):
                committed_tui(
                    self.adapter,
                    dataclasses.replace(self.spec, prompt=prompt),
                    hook,
                    source_roots=roots,
                )
            self.assertIsNone(self.common.load(str(self.work), self.spec.sid))
            self.assertFalse((self.home / ".agent-pm").exists())

    def test_resume_failure_happens_before_command(self):
        with (
            mock.patch("shutil.which") as which,
            self.assertRaisesRegex(ConfigError, "missing.*state"),
        ):
            committed_tui(
                self.adapter,
                dataclasses.replace(self.spec, prompt="again", resume=True),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            )
        which.assert_not_called()

    def test_interactive_environment_preserves_external_default_values(self):
        parent = {key: value for key, value in os.environ.items() if key not in self.common.UNSET}
        for markers, color in (({"CLAUDECODE": "1"}, "1"), ({"CODEX_THREAD_ID": NATIVE}, "0")):
            with (
                self.subTest(markers=markers),
                mock.patch.dict(
                    os.environ,
                    {**parent, **markers, "NO_COLOR": color, "PAGER": "less"},
                    clear=True,
                ),
            ):
                self.spec = dataclasses.replace(self.spec, sid=f"color-{color}")
                _, launch, _ = self.launch()
                self.assertNotIn("NO_COLOR", launch.unset)
                self.assertNotIn("PAGER", launch.unset)

    def test_interactive_environment_injects_no_defaults_and_drops_parent_markers(self):
        with mock.patch.dict(
            os.environ, {"CODEX_CI": "1", "NO_COLOR": "1", "CODEX_THREAD_ID": NATIVE}
        ):
            _, launch, _ = self.launch()
        self.assertNotIn("CODEX_CI", launch.env)
        self.assertNotIn("NO_COLOR", launch.env)
        self.assertEqual(launch.unset, self.common.UNSET)
        self.assertIn("CODEX_THREAD_ID", launch.unset)
        second = dataclasses.replace(self.spec, sid="explicit-env", tier=None, effort=None)
        adapter = CodexAdapter({"env": {"CODEX_CI": "configured", "PAGER": "less"}})
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                adapter, dataclasses.replace(second, prompt="hi"), HOOK, source_roots=PACKAGE_ROOTS
            )
        self.assertEqual(launch.env["CODEX_CI"], "configured")
        self.assertNotIn("CODEX_CI", launch.unset)
        self.assertEqual(launch.env["PAGER"], "less")

    def test_interactive_launch_clears_git_local_environment_in_sorted_order(self):
        inherited = {key: "inherited" for key in LOCAL_ENV_VARS}
        with mock.patch.dict(os.environ, inherited):
            _, launch, _ = self.launch()
            snapshot = self.common.load(str(self.work), self.spec.sid).snapshot
            effective = self.common.environment(snapshot, interactive=True, ambient=True)
            child = {key: value for key, value in os.environ.items() if key not in launch.unset}
            child.update(launch.env)
        self.assertEqual(launch.unset[: len(LOCAL_ENV_VARS)], tuple(sorted(LOCAL_ENV_VARS)))
        self.assertFalse(LOCAL_ENV_VARS.intersection(launch.env))
        self.assertFalse(LOCAL_ENV_VARS.intersection(effective))
        self.assertFalse(LOCAL_ENV_VARS.intersection(child))

    def test_hook_mapping_null_reply_and_submit(self):
        run, _, _ = self.launch()
        self.assertEqual(run.submit("x"), (Paste("x"), Key("Enter")))
        self.assertEqual(
            run.hook_events("SessionStart", self.payload()), [ClientBound(NATIVE), Activity("")]
        )
        self.assertEqual(
            run.hook_events("PostToolUse", self.payload(tool_name="Bash")), [Activity("Bash")]
        )
        self.assertEqual(
            run.hook_events("PermissionRequest", self.payload(tool_name="Bash")), [Blocked("Bash")]
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message=None)), [TurnEnd()]
        )
        self.assertEqual(
            run.hook_events(
                "Stop", self.payload(last_assistant_message="raw", background_tasks=[1])
            ),
            [TurnEnd("raw", 0)],
        )
        for fields in (
            {"agent_id": CHILD},
            {"session_id": "invalid"},
            {"session_id": CHILD},
            {"cwd": str(self.extra)},
        ):
            self.assertEqual(run.hook_events("Stop", self.payload(**fields)), [])
        self.assertEqual(run.hook_events("unknown", self.payload()), [])

    def test_interrupt_retains_root_reply_on_live_and_attach_replay(self):
        run = self.adapter.tui()
        self.assertEqual(
            run.hook_events("Interrupt", self.payload()), [ClientBound(NATIVE), TurnEnd()]
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="kept")),
            [TurnEnd("kept")],
        )
        self.assertEqual(
            run.hook_events("Interrupt", self.payload(last_assistant_message="discard")),
            [TurnEnd("kept")],
        )
        attached = self.adapter.tui()
        self.assertEqual(
            attached.hook_events("Stop", self.payload(last_assistant_message="kept")),
            [ClientBound(NATIVE), TurnEnd("kept")],
        )
        self.assertEqual(attached.hook_events("Interrupt", self.payload()), [TurnEnd("kept")])
        attached.hook_events("Stop", self.payload(last_assistant_message=None))
        self.assertEqual(attached.hook_events("Interrupt", self.payload()), [TurnEnd()])

    def test_restore_readies_a_fresh_tui_as_the_one_that_launched(self):
        run, _, data = self.launch()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        foreign = self.payload(cwd=str(self.extra))
        self.assertEqual(
            self.adapter.tui().hook_events("Stop", foreign), [ClientBound(NATIVE), TurnEnd()]
        )
        restored = self.adapter.tui()
        restored.restore(self.spec, run.hook_callback().state)
        self.assertEqual(
            (restored.native, restored.store, restored.state),
            (NATIVE, data["store"], self.common.load(str(self.work), self.spec.sid)),
        )
        self.assertEqual(restored.hook_callback(), run.hook_callback())
        for payload in (foreign, self.payload(session_id=CHILD)):
            self.assertEqual(restored.hook_events("Stop", payload), [])
        self.assertEqual(
            restored.hook_events("Stop", self.payload(last_assistant_message="r")),
            [ClientBound(NATIVE), TurnEnd("r")],
        )

    def test_restore_without_a_recovery_state_keeps_none_and_a_bad_store_is_a_config_error(self):
        self.launch()
        fresh = self.adapter.tui()
        fresh.restore(dataclasses.replace(self.spec, sid="other-id"), {})
        self.assertEqual((fresh.state, fresh.native), (None, ""))
        for state in ({"store": "relative/store"}, {"store": str(self.extra)}):
            with self.subTest(state=state), self.assertRaises(ConfigError):
                self.adapter.tui().restore(self.spec, state)

    def test_identity_accepts_canonical_cwd_alias_and_keeps_native_binding(self):
        run, _, _ = self.launch()
        alias = self.root / "cwd-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.assertEqual(run.session_id(self.payload(cwd=str(alias))), NATIVE)
        self.assertEqual(run.session_id(self.payload(session_id=CHILD, cwd=str(alias))), "")
        self.assertEqual(run.native, NATIVE)

    def test_native_mismatch_is_rejected_before_canonical_cwd_check(self):
        run, _, _ = self.launch()
        self.assertEqual(run.session_id(self.payload()), NATIVE)
        with mock.patch("os.path.realpath", side_effect=RuntimeError("unexpected cwd check")):
            self.assertEqual(run.session_id(self.payload(session_id=CHILD)), "")
        self.assertEqual(run.native, NATIVE)

    def test_invalid_identity_does_not_bind_or_update_the_reply(self):
        run = self.adapter.tui()
        for fields in (
            {"agent_id": CHILD},
            {"session_id": None},
            {"session_id": "invalid"},
            {"cwd": None},
            {"cwd": 1},
            {"cwd": "relative"},
            {"cwd": "/invalid\0cwd"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(
                    run.session_id(
                        self.payload(
                            hook_event_name="Stop", last_assistant_message="foreign", **fields
                        )
                    ),
                    "",
                )
                self.assertEqual((run.native, run.last_reply), ("", ""))

    def test_limit_screen_official_variants_and_reset(self):
        run = self.adapter.tui()
        run.common.config["env"] = {"TZ": "UTC"}
        stamp = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        for prefix in (
            "You’ve hit your usage limit.",
            "You've hit your usage limit. Visit https://chatgpt.com/settings/usage "
            "to purchase more credits or",
            "Quota exceeded. Check your plan and billing details.",
            "Your workspace is out of credits.",
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    run.limit("■ " + prefix + " Try again at Jan 2nd, 2027 3:04 AM.\n› "),
                    LimitHit(stamp),
                )
        self.assertEqual(run.limit("■ You’ve hit your usage limit. Try again later."), LimitHit())
        self.assertEqual(
            run.limit("■ You've hit your usage limit. Try again at Jan 2th, 2027 3:04 AM."),
            LimitHit(),
        )
        self.assertIsNone(run.limit("assistant: You've hit your usage limit. is a quoted example"))
        self.assertIsNone(run.limit("ordinary network error"))

    def test_writable_executable_and_hook_dependency_are_rejected(self):
        for path in (self.cwd / "codex", self.extra / "driver.py"):
            spec = dataclasses.replace(self.spec, sid="unsafe-" + path.name)
            hook = HOOK if path.name == "codex" else (*HOOK[:2], str(path), *HOOK[3:])
            with (
                mock.patch(
                    "shutil.which",
                    return_value=str(path if path.name == "codex" else "/protected/codex"),
                ),
                self.assertRaisesRegex(ConfigError, "rewrite|writable"),
            ):
                committed_tui(
                    self.adapter,
                    dataclasses.replace(spec, prompt="hi"),
                    hook,
                    source_roots=PACKAGE_ROOTS,
                )
            self.assertIsNone(self.common.load(str(self.work), spec.sid))

    def test_narrow_screen_wrapping_keeps_limit_reset(self):
        adapter = CodexAdapter({"env": {"TZ": "UTC"}})
        stamp = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        screen = "■ You hit your spend cap set by the owner\n  of your workspace. Try again at Jan\n  2nd, 2027 3:04 AM.\n› "
        self.assertEqual(adapter.tui().limit(screen), LimitHit(stamp))

    def test_dialogs_preserve_main_worker_cases(self):
        self.assertIn("Trust this folder?", self.adapter.tui().dialogs)
        self.assertIn("Hooks need review", self.adapter.tui().dialogs)
        self.assertIn("Working directory · resume", self.adapter.tui().dialogs)
        self.assertIn("This conversation is open in another app", self.adapter.tui().dialogs)
        self.assertIn("Sign in with ChatGPT to use Codex", self.adapter.tui().dialogs)

    def test_driver_flattened_limit_screen_keeps_header_and_reset(self):
        adapter = CodexAdapter({"env": {"TZ": "UTC"}})
        screen = (
            "Codex > gpt-test ■ You've hit your usage limit. Try again at Jan 2nd, 2027 3:04 AM. › "
        )
        stamp = datetime(2027, 1, 2, 3, 4, tzinfo=ZoneInfo("UTC")).timestamp()
        self.assertEqual(adapter.tui().limit(screen), LimitHit(stamp))

    def test_assistant_bullets_and_quoted_limit_text_are_not_native_errors(self):
        run = self.adapter.tui()
        for screen in (
            (
                "Codex > gpt-test • You have asked me to quote this error. • You've hit your usage limit. is a quoted example › "
            ),
            "• You've hit your usage limit. is a quoted example\n› ",
            "You've hit your usage limit. is a quoted example",
            "! You've hit your usage limit. is a quoted example",
            "✖ You've hit your usage limit. is a quoted example",
        ):
            with self.subTest(screen=screen):
                self.assertIsNone(run.limit(screen))

    def test_configured_tmpdir_protects_dependencies_before_fresh_or_resume_launch(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        for resume in (False, True):
            for index, tmpdir in enumerate(("/protected", "/driver-only", *ROOTS, sys.prefix)):
                with self.subTest(resume=resume, tmpdir=tmpdir):
                    config = {**self.common.config, "env": {"TMPDIR": tmpdir}}
                    adapter = CodexAdapter(config)
                    spec = (
                        self.spec if resume else dataclasses.replace(self.spec, sid=f"env-{index}")
                    )
                    if resume:
                        with mock.patch("shutil.which", return_value="/protected/codex"):
                            launch = committed_tui(
                                adapter,
                                dataclasses.replace(spec, prompt="hi", resume=True),
                                HOOK,
                                source_roots=PACKAGE_ROOTS,
                            )
                            self.assertNotIn("TMPDIR", launch.env)
                        continue
                    with (
                        mock.patch("shutil.which", return_value="/protected/codex"),
                        self.assertRaisesRegex(ConfigError, "rewrite|writable"),
                    ):
                        committed_tui(
                            adapter,
                            dataclasses.replace(spec, prompt="hi", resume=resume),
                            (*HOOK[:2], "/driver-only/hook.py", *HOOK[3:]),
                            source_roots=PACKAGE_ROOTS,
                        )
                    if not resume:
                        self.assertIsNone(self.common.load(str(self.work), spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_tui_adapter_queries_fresh_saved_and_effective_temp_roots(self):
        home, temporary = self.root / "configured-home", self.root / "configured-tmp"
        home.mkdir()
        temporary.mkdir()
        alias = self.root / "temporary-link"
        alias.symlink_to(temporary)
        adapter = CodexAdapter(
            self.common.config | {"env": {"CODEX_HOME": str(home), "TMPDIR": str(alias)}},
        )
        run = adapter.tui()
        expected = [str(p) for p in (self.cwd, home, self.extra)]
        with mock.patch("subprocess.Popen") as proc:
            roots = run.common.writable_dirs(dataclasses.replace(self.spec, resume=False))
            self.assertEqual(list(roots[:3]), expected)
            self.assertIn(str(temporary), roots)
            self.assertEqual(len(roots), len(set(roots)))
            self.assertFalse((self.home / ".agent-pm").exists())
            with mock.patch("shutil.which", return_value="/protected/codex"):
                run.launch(
                    dataclasses.replace(self.spec, prompt="hello"), HOOK, source_roots=PACKAGE_ROOTS
                )
            run.commit()
            saved = self.common.load(str(self.work), self.spec.sid).writable_roots
            changed_home, changed_tmp = self.root / "changed-home", self.root / "changed-tmp"
            changed = CodexAdapter(
                self.common.config
                | {"env": {"CODEX_HOME": str(changed_home), "TMPDIR": str(changed_tmp)}},
            ).tui()
            spec = dataclasses.replace(
                self.spec,
                access=dataclasses.replace(self.spec.access, cwd=str(self.extra), dirs=()),
            )
            before = self.record().read_bytes()
            resumed = changed.common.writable_dirs(dataclasses.replace(spec, resume=True))
            self.assertEqual(tuple(resumed[: len(saved)]), saved)
            self.assertNotIn(str(changed_tmp), resumed)
            self.assertNotIn(str(changed_home), resumed)
            self.assertEqual(self.record().read_bytes(), before)
        proc.assert_not_called()


PROMPT = "quoted ' prompt\nnext"

POLICY = (
    "approval_policy",
    "approvals_reviewer",
    "sandbox_workspace_write",
)

REFUSED = (
    ("-C", "/x"),
    ("--cd=/x",),
    ("-C/x",),
    ("-p", "work"),
    ("--profile", "work"),
    ("resume",),
    ("fork",),
    ("--last",),
    ("--dangerously-bypass-hook-trust",),
    ("-c", "hooks.Stop=[]"),
    ("--config", '"hooks".state={}'),
    ("--config=sandbox_workspace_write.network_access=true",),
    ("-csandbox_workspace_write.writable_roots=[]",),
)


class SuppliedFlagsTest(Base):
    def launch(self, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                self.adapter,
                dataclasses.replace(spec, prompt=PROMPT, resume=resume),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            )
        return launch, json.loads(launch.argv[-1])

    def test_supplied_flags_env_and_hooks_share_one_path(self):
        flags = ("--add-dir", "../../added", '--config=model_verbosity="low"', "--enable", "web")
        spec = dataclasses.replace(self.spec, flags=flags, env={"S": "s"})
        launch, data = self.launch(spec)
        plan = tui.decode(data)
        snapshot = self.common.load(data["workdir"], data["sid"]).snapshot
        argv = tui.execution_argv(plan, snapshot, "hooks.state={trusted}")
        start = argv.index("--add-dir")
        self.assertEqual(argv[start : start + len(flags)], list(flags))
        self.assertEqual(launch.env["S"], "s")
        self.assertNotIn("add_dirs", data)
        self.assertEqual(
            common.added_dirs(snapshot.flags, str(self.cwd)),
            [os.path.join(self.cwd, "../../added")],
        )
        hook_flags = [
            word
            for event in self.adapter.tui().hooks
            for word in ("-c", next(word for word in argv if word.startswith(f"hooks.{event}=")))
        ]
        self.assertEqual(
            tui.trust_args(plan, snapshot),
            [*hook_flags, "-c", 'model_verbosity="low"', "--enable", "web"],
        )
        self.assert_no_policy(argv)

    def assert_no_policy(self, argv):
        self.assertNotIn("-s", argv)
        self.assertFalse([w for w in argv if w.startswith(POLICY)])

    def test_flags_after_separator_are_preserved_in_snapshot(self):
        flags = ("--", "resume", "-C", "/x")
        spec = dataclasses.replace(self.spec, flags=flags)
        self.launch(spec)
        self.assertEqual(self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(flags))

    def test_every_grant_source_is_checked_against_protected_paths(self):
        guarded = self.root / "guarded"
        cases = (
            (("--add-dir", "../../guarded"), str(guarded / "events")),
            (("--add-dir", str(guarded)), str(guarded / "events")),
            ((f"--add-dir={guarded / 'sub'}",), str(guarded)),
            ((), str(self.cwd / "events")),
            (("--add-dir", str(self.work)), str(self.work)),
            ((), str(self.extra / "events")),
        )
        for index, (flags, path) in enumerate(cases):
            spec = dataclasses.replace(
                self.spec, sid=f"guarded-{index}", flags=flags, protect=(path,)
            )
            with (
                self.subTest(flags=flags, path=path),
                self.assertRaisesRegex(ConfigError, "could rewrite hook dependency"),
            ):
                self.launch(spec)
            self.assertIsNone(self.common.load(str(self.work), spec.sid))

    def test_resume_checks_saved_grants_and_ignores_new_grants(self):
        guarded = self.root / "guarded"
        spec = dataclasses.replace(self.spec, flags=("--add-dir", str(guarded)))
        self.launch(spec)
        self.common.bind(str(self.work), spec.sid, NATIVE)
        self.rollout()
        changed = dataclasses.replace(spec, flags=(), protect=(str(guarded / "events"),))
        with self.assertRaisesRegex(ConfigError, "could rewrite hook dependency"):
            self.launch(changed, resume=True)
        changed = dataclasses.replace(spec, flags=("--add-dir", "/unrecorded"))
        _, data = self.launch(changed, resume=True)
        self.assertEqual(
            common.added_dirs(
                self.common.load(data["workdir"], data["sid"]).snapshot.flags, str(self.cwd)
            ),
            [str(guarded)],
        )


class LaunchPolicy(Base):
    def argv(self, adapter, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = committed_tui(
                adapter,
                dataclasses.replace(spec, prompt="hi", resume=resume),
                HOOK,
                source_roots=PACKAGE_ROOTS,
            )
        return tui.execution_argv(
            tui.decode(json.loads(launch.argv[-1])),
            adapter.common.load(spec.workdir, spec.sid).snapshot,
            "hooks.state={trusted}",
        )

    def test_supplied_policy_and_saved_replay_in_both_modes(self):
        mode = "tui"
        for saved, current in (
            ("workspace-write", "danger-full-access"),
            ("danger-full-access", "workspace-write"),
        ):
            with self.subTest(mode=mode, saved=saved):
                flags = (
                    "-s",
                    saved,
                    "-c",
                    'approval_policy="on-request"',
                    "-c",
                    'approvals_reviewer="auto_review"',
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                )
                spec = dataclasses.replace(
                    self.spec, sid=f"{mode}-{saved}", flags=flags, env={"K": "saved"}
                )
                first = self.argv(self.adapter, spec)
                self.assertEqual(first[first.index("-s") :][: len(flags)], list(flags))
                self.common.bind(str(self.work), spec.sid, NATIVE)
                self.rollout()
                changed = dataclasses.replace(spec, flags=("-s", current), env={"K": "changed"})
                second = self.argv(self.adapter, changed, resume=True)
                self.assertEqual(second[second.index("-s") :][: len(flags)], list(flags))
                self.assertEqual(
                    self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(flags)
                )

    def test_separator_tail_follows_generated_options_on_fresh_and_resume(self):
        mode = "tui"
        with self.subTest(mode=mode):
            supplied = ("--enable", "web", "--", "literal", "--last")
            spec = dataclasses.replace(self.spec, sid=f"separator-{mode}", flags=supplied)
            first = self.argv(self.adapter, spec)
            self.common.bind(str(self.work), spec.sid, NATIVE)
            self.rollout()
            changed = dataclasses.replace(spec, flags=("--", "changed"))
            second = self.argv(self.adapter, changed, resume=True)
            for argv in (first, second):
                boundary = argv.index("--")
                self.assertEqual(argv[boundary:], ["--", "literal", "--last", "hi"])
                self.assertIn("gpt-test", argv[:boundary])
                self.assertIn(str(self.extra), argv[:boundary])
                self.assertIn("project_doc_max_bytes=0", argv[:boundary])
                self.assertEqual(argv[:boundary].count("hooks.state={trusted}"), 1)
                self.assertEqual(
                    sum(
                        word.startswith("hooks.")
                        for word in argv[:boundary]
                        if word != "hooks.state={trusted}"
                    ),
                    6,
                )
            self.assertEqual(
                self.common.load(str(self.work), spec.sid).snapshot.flags, tuple(supplied)
            )


class PlanTest(PlanCase):
    def test_wire_roundtrip_decodes_an_object_without_reparsing_json(self):
        encoded = tui.encode(self.plan)
        data = json.loads(encoded)
        self.assertEqual(
            set(data),
            {
                "executable",
                "selection",
                "prompt",
                "hook",
                "workdir",
                "sid",
                "store",
                "protect",
                "import_roots",
            },
        )
        self.assertEqual(data["hook"], list(self.plan.hook.argv))
        self.assertEqual(data["import_roots"], list(self.plan.hook.import_roots))
        with mock.patch("json.loads", side_effect=AssertionError("launch JSON decoded twice")):
            decoded = tui.decode(data)
            with self.assertRaisesRegex(ValueError, "launch fields"):
                tui.decode(encoded)
        self.assertEqual(decoded, self.plan)

    def test_decoded_plan_is_detached_from_mutable_wire_input(self):
        encoded = tui.encode(self.plan)
        data = json.loads(encoded)
        decoded = tui.decode(data)
        for key in ("selection", "hook", "protect", "import_roots"):
            data[key].append("changed")
        data["prompt"] = "changed"
        self.assertEqual(decoded, self.plan)
        self.assertEqual(tui.encode(decoded), encoded)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decoded.prompt = "changed"

    def test_wire_rejects_competing_command_and_directory_authorities(self):
        data = json.loads(tui.encode(self.plan))
        for key, value in (
            ("argv", ["/foreign/codex"]),
            ("prefix", ["/foreign/codex", "--add-dir", "/foreign-write-root"]),
            ("commands", {"Stop": "foreign"}),
            ("hook_flags", ["-c", "hooks.Stop=[]"]),
            ("add_dirs", ["/foreign-write-root"]),
            ("flags", ["--add-dir", "/foreign-write-root"]),
            ("options", ["--add-dir", "/foreign-write-root"]),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "launch fields"):
                tui.decode(data | {key: value})

    def test_wire_validates_each_command_component_and_prompt(self):
        data = json.loads(tui.encode(self.plan))
        for key in ("selection", "hook"):
            for value in (None, "word", [1], ["a\0b"]):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ValueError, "invalid command"),
                ):
                    tui.decode(data | {key: value})
        with self.assertRaisesRegex(ValueError, "invalid command"):
            tui.decode(data | {"hook": []})
        for value in (None, 1, "relative", "/a\0b"):
            with (
                self.subTest(executable=value),
                self.assertRaisesRegex(ValueError, "absolute paths"),
            ):
                tui.decode(data | {"executable": value})
        for word in ("--add-dir", "-c", "--", "-s", "--sandbox=danger-full-access"):
            with self.subTest(selection=word), self.assertRaisesRegex(ValueError, "selection"):
                tui.decode(data | {"selection": ["resume", "saved-native", word]})
        for value in (None, 1, "a\0b"):
            with self.subTest(prompt=value), self.assertRaisesRegex(ValueError, "invalid prompt"):
                tui.decode(data | {"prompt": value})

    def test_execution_and_trust_use_the_same_pre_separator_flags(self):
        hooks = tui.hook_flags(self.plan.hook)
        trusted = "hooks.state={trusted}"
        command = tui.execution_argv(self.plan, self.snapshot, trusted)
        self.assertEqual(
            command,
            [
                *self.frontend,
                "-m",
                "requested",
                "-c",
                'model_verbosity="low"',
                "--enable",
                "web",
                "-m",
                "configured",
                "--add-dir",
                "/granted",
                *hooks,
                "-c",
                trusted,
                "--",
                "--config=literal",
                "--disable",
                "literal",
                self.plan.prompt,
            ],
        )
        self.assertEqual(
            tui.trust_args(self.plan, self.snapshot),
            [*hooks, "-c", 'model_verbosity="low"', "--enable", "web"],
        )
        changed = dataclasses.replace(self.snapshot, flags=("--disable", "web"))
        self.assertEqual(tui.trust_args(self.plan, changed), [*hooks, "--disable", "web"])
        changed_command = tui.execution_argv(self.plan, changed, trusted)
        self.assertEqual(changed_command[len(self.frontend) :][:2], ["--disable", "web"])
        self.assertNotIn("--enable", changed_command)

    def test_empty_flags_and_prompt_preserve_the_literal_tail_contract(self):
        for flags, prompt, tail in (
            ((), "", []),
            ((), "hello", ["--", "hello"]),
            (("--",), "", ["--"]),
            (("--",), "hello", ["--", "hello"]),
            (("--", "--config=literal"), "", ["--", "--config=literal"]),
            (("--", "--config=literal"), "hello", ["--", "--config=literal", "hello"]),
        ):
            plan = dataclasses.replace(self.plan, prompt=prompt)
            snapshot = dataclasses.replace(self.snapshot, flags=flags)
            with self.subTest(flags=flags, prompt=prompt):
                self.assertEqual(
                    tui.execution_argv(plan, snapshot, "trusted"),
                    [
                        *self.frontend,
                        "-m",
                        "configured",
                        "--add-dir",
                        "/granted",
                        *tui.hook_flags(plan.hook),
                        "-c",
                        "trusted",
                        *tail,
                    ],
                )
                self.assertEqual(tui.trust_args(plan, snapshot), list(tui.hook_flags(plan.hook)))

    def test_execution_write_grants_come_from_the_saved_snapshot(self):
        encoded = tui.encode(self.plan)
        command = tui.execution_argv(tui.decode(json.loads(encoded)), self.snapshot, "trusted")
        self.assertEqual(command[command.index("--add-dir") :][:2], ["--add-dir", "/granted"])
        self.assertNotIn("/foreign-write-root", command)
        changed = dataclasses.replace(self.snapshot, dirs=("/different-saved-grant",))
        changed_command = tui.execution_argv(self.plan, changed, "trusted")
        self.assertEqual(
            changed_command[changed_command.index("--add-dir") :][:2],
            ["--add-dir", "/different-saved-grant"],
        )
        self.assertNotIn("/granted", changed_command)
        self.assertEqual(tui.encode(self.plan), encoded)

    def test_standard_session_selectors_roundtrip_and_execute(self):
        for selection in ((), ("resume", "saved-native")):
            plan = dataclasses.replace(self.plan, selection=selection)
            decoded = tui.decode(json.loads(tui.encode(plan)))
            with self.subTest(selection=selection):
                self.assertEqual(decoded.selection, selection)
                command = tui.execution_argv(decoded, self.snapshot, "trusted")
                self.assertEqual(command[: 1 + len(selection)], [plan.executable, *selection])
                self.assertEqual(
                    command[1 + len(selection) :][:5],
                    ["--no-daemon", "-C", "/work", "-c", 'tui.notification_method="bel"'],
                )

    def test_fixed_frontend_uses_the_saved_snapshot_cwd(self):
        snapshot = dataclasses.replace(self.snapshot, cwd="/work/restored-project")
        command = tui.execution_argv(self.plan, snapshot, "trusted")
        self.assertEqual(command[command.index("-C") :][:2], ["-C", snapshot.cwd])
        self.assertNotIn("/work", command)


class HookTest(Base):
    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_bind_before_forward_and_ignore_children_foreign_or_rebound(self):
        self.prepare()
        self.assertTrue(tui.bind("SessionStart", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        for fields in (
            {"agent_id": CHILD},
            {"cwd": str(self.extra)},
            {"session_id": CHILD},
            {"session_id": "invalid"},
            {"hook_event_name": "PermissionRequest"},
        ):
            with self.subTest(fields=fields), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(
                    tui.bind("Stop", self.payload(**fields), str(self.work), self.spec.sid)
                )
        self.assertFalse(tui.bind("unknown", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_callback_binds_and_adds_label_without_another_payload_reader(self):
        self.prepare()
        payload = self.payload(last_assistant_message="reply")
        state = {"store": common.SessionStore().root}

        def call(given):
            return HookCall("Stop", given, self.spec.sid, str(self.work), None, state)

        self.assertEqual(tui.on_hook(call(payload)), {**payload, "hook_event_name": "Stop"})
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertIsNone(tui.on_hook(call(self.payload(agent_id=CHILD))))

    def test_invalid_identity_is_ignored_before_loading_a_record(self):
        for fields in (
            {"agent_id": CHILD},
            {"session_id": None},
            {"session_id": "invalid"},
            {"cwd": None},
            {"cwd": 1},
            {"cwd": "relative"},
            {"cwd": "/invalid\0cwd"},
            {"hook_event_name": "SessionStart"},
        ):
            with self.subTest(fields=fields), contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertFalse(
                    tui.bind("Stop", self.payload(**fields), "relative", self.spec.sid)
                )
                self.assertEqual(error.getvalue(), "")

    def test_valid_identity_reports_record_failure_before_foreign_cwd_filter(self):
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertFalse(
                tui.bind("Stop", self.payload(cwd=str(self.extra)), str(self.work), self.spec.sid)
            )
        self.assertEqual(error.getvalue(), "codex hook: missing Codex recovery state\n")

    def test_cwd_alias_binds_against_the_saved_canonical_path(self):
        self.prepare()
        alias = self.root / "cwd-alias"
        alias.symlink_to(self.cwd, target_is_directory=True)
        self.assertTrue(
            tui.bind("Stop", self.payload(cwd=str(alias)), str(self.work), self.spec.sid)
        )
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_missing_malformed_and_fifo_record_fail_without_forwarding(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        self.prepare()
        path = self.record()
        path.write_text("not-json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        self.assertLess(time.monotonic() - before, 0.5)

    def test_binding_lock_wait_is_bounded(self):
        self.prepare()
        with (
            mock.patch("fcntl.flock", side_effect=[BlockingIOError, None]),
            mock.patch("time.monotonic", side_effect=[0, 2]),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(tui.bind("Interrupt", self.payload(), str(self.work), self.spec.sid))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_explicit_caller_store_survives_configured_runtime_home(self):
        self.prepare()
        store = common.SessionStore().root
        runtime_home = self.root / "runtime-home"
        runtime_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(runtime_home)}):
            self.assertTrue(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse((runtime_home / ".agent-pm").exists())

    def test_explicit_store_keeps_symlink_mode_and_implicit_root_guards(self):
        self.prepare()
        store = self.record().parent
        alias = self.root / "alias"
        alias.symlink_to(store, target_is_directory=True)
        for path in (str(alias), "relative", str(store)):
            with self.subTest(path=path):
                if path == str(store):
                    store.chmod(0o755)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(
                        tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=path)
                    )
                store.chmod(0o700)
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(store.parent)}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=str(store))
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_authoritative_store_validates_every_recovery_path_with_writable_runtime_home(self):
        state = self.prepare()
        record = self.record()
        store = str(record.parent)
        runtime_home = self.cwd / "runtime-home"
        runtime_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(runtime_home)}):
            self.assertEqual(
                common.SessionStore(store).lookup(self.spec.sid, state.snapshot.cwd), state
            )
            self.assertEqual(common.SessionStore(store).load(str(self.work), self.spec.sid), state)
            bound = common.SessionStore(store).bind(str(self.work), self.spec.sid, NATIVE)
            self.assertEqual(bound.native_id, NATIVE)
            self.assertTrue(
                tui.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
            fresh = common.SessionStore(store).create(
                str(self.work),
                "explicit-store",
                state.snapshot,
                state.writable_roots,
            )
            self.assertEqual(common.SessionStore(store).save(fresh), fresh)
            self.assertEqual(common.SessionStore(store).load(str(self.work), fresh.sid), fresh)
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse((runtime_home / ".agent-pm").exists())

    def test_authoritative_store_still_rejects_actual_writable_store(self):
        state = self.prepare()
        record = self.record()
        unsafe = self.cwd / ".private-state" / "core-new-sessions" / "codex"
        for directory in (unsafe.parent.parent, unsafe.parent, unsafe):
            directory.mkdir(mode=0o700, exist_ok=True)
        copied = unsafe / record.name
        copied.write_bytes(record.read_bytes())
        copied.chmod(0o600)
        root = str(unsafe)
        calls = (
            lambda: common.SessionStore(root).lookup(self.spec.sid, state.snapshot.cwd),
            lambda: common.SessionStore(root).load(str(self.work), self.spec.sid),
            lambda: common.SessionStore(root).bind(str(self.work), self.spec.sid, NATIVE),
            lambda: common.SessionStore(root).create(
                str(self.work), "unsafe-store", state.snapshot, state.writable_roots
            ),
            lambda: common.SessionStore(root).save(state),
        )
        for call in calls:
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(common.RecoveryError, "inside writable root"),
            ):
                call()
        self.assertEqual(record.read_bytes(), copied.read_bytes())
        self.assertEqual(list(unsafe.iterdir()), [copied])
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)
