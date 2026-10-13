"""Codex interactive launch policy, hook events and native limits."""

import dataclasses
import json
import os
import shlex
import sys
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Key,
    LimitHit,
    Paste,
    SessionBound,
    TurnEnd,
)

from session.tests.case import ROOTS
from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")


class TuiTest(Base):
    def launch(self, *, resume=False, adapter=None):
        run = (adapter or self.adapter).tui()
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            mock.patch("subprocess.Popen") as proc,
        ):
            launch = run.launch("quoted ' prompt\nnext", self.spec, HOOK, resume=resume)
        proc.assert_not_called()
        return run, launch, json.loads(launch.argv[-1])

    def test_the_launch_protects_the_import_roots_first(self):
        _, _, data = self.launch()
        self.assertEqual(data["protect"][: len(ROOTS)], list(ROOTS))

    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_factory_launch_flags_and_opaque_hook(self):
        self.assertIsNot(self.adapter.tui(), self.adapter.tui())
        run, launch, data = self.launch()
        self.assertEqual(launch.argv[1], "-I")
        self.assertTrue(launch.argv[2].endswith("/adapters/codex/tui_start.py"))
        self.assertEqual(data["argv"][:4], ["/protected/codex", "--no-daemon", "-C", str(self.cwd)])
        self.assertEqual(data["argv"][-2:], ["--", "quoted ' prompt\nnext"])
        self.assertIn('tui.notification_method="bel"', data["argv"])
        self.assertNotIn("sandbox_workspace_write.writable_roots=[]", data["argv"])
        self.assertIn("model_reasoning_effort=low", data["argv"])
        self.assertIn("gpt-test", data["argv"])
        self.assertEqual(data["hook"], list(HOOK))
        self.assertEqual(set(data["commands"]), set(run.hooks))
        for label, command in data["commands"].items():
            self.assertEqual(shlex.split(command), [*HOOK, label])
        self.assertEqual(
            run.hook_state()["adapter_hook"],
            os.path.join(os.path.dirname(launch.argv[2]), "tui_hook.py"),
        )
        self.assertEqual(run.hook_state()["adapter_state"], {"store": data["store"]})
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_resume_uses_shared_saved_policy_and_native_file(self):
        self.spec = dataclasses.replace(
            self.spec,
            flags=("-s", "workspace-write", "-c", "sandbox_workspace_write.network_access=false"),
        )
        saved = CodexAdapter(self.adapter.config).common.prepare(self.spec, resume=False)
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        other = CodexAdapter({"env": {"CODEX_HOME": str(self.extra)}})
        run, launch, data = self.launch(resume=True, adapter=other)
        self.assertEqual(data["argv"][:3], ["/protected/codex", "resume", NATIVE])
        self.assertIn("gpt-test", data["argv"])
        self.assertIn("sandbox_workspace_write.network_access=false", data["argv"])
        self.assertEqual(launch.env["CODEX_HOME"], saved.snapshot["codex_home"])
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
        self.assertEqual(shlex.split(resumed.resume)[4], "PYTHONPATH=/a b;c")

    def test_resume_failure_happens_before_command(self):
        with (
            mock.patch("shutil.which") as which,
            self.assertRaisesRegex(ConfigError, "missing.*state"),
        ):
            self.adapter.tui().launch("again", self.spec, HOOK, resume=True)
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
            launch = adapter.tui().launch("hi", second, HOOK, resume=False)
        self.assertEqual(launch.env["CODEX_CI"], "configured")
        self.assertNotIn("CODEX_CI", launch.unset)
        self.assertEqual(launch.env["PAGER"], "less")

    def test_hook_mapping_null_reply_and_submit(self):
        run, _, _ = self.launch()
        self.assertEqual(run.submit("x"), (Paste("x"), Key("Enter")))
        self.assertEqual(
            run.hook_events("SessionStart", self.payload()), [SessionBound(NATIVE), Activity("")]
        )
        self.assertEqual(
            run.hook_events("PostToolUse", self.payload(tool_name="Bash"), NATIVE),
            [Activity("Bash")],
        )
        self.assertEqual(
            run.hook_events("PermissionRequest", self.payload(tool_name="Bash"), NATIVE),
            [Blocked("Bash")],
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message=None), NATIVE), [TurnEnd()]
        )
        self.assertEqual(
            run.hook_events(
                "Stop", self.payload(last_assistant_message="raw", background_tasks=[1]), NATIVE
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
            run.hook_events("Interrupt", self.payload()), [SessionBound(NATIVE), TurnEnd()]
        )
        self.assertEqual(
            run.hook_events("Stop", self.payload(last_assistant_message="kept"), NATIVE),
            [TurnEnd("kept")],
        )
        self.assertEqual(
            run.hook_events("Interrupt", self.payload(last_assistant_message="discard"), NATIVE),
            [TurnEnd("kept")],
        )
        attached = self.adapter.tui()
        attached.session_id(self.payload(hook_event_name="Stop", last_assistant_message="kept"))
        self.assertEqual(
            attached.hook_events("Interrupt", self.payload(), NATIVE), [TurnEnd("kept")]
        )
        attached.session_id(self.payload(hook_event_name="Stop", last_assistant_message=None))
        self.assertEqual(attached.hook_events("Interrupt", self.payload(), NATIVE), [TurnEnd()])

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
            hook = HOOK if path.name == "codex" else (sys.executable, "-I", str(path))
            with (
                mock.patch(
                    "shutil.which",
                    return_value=str(path if path.name == "codex" else "/protected/codex"),
                ),
                self.assertRaisesRegex(ConfigError, "rewrite|writable"),
            ):
                self.adapter.tui().launch("hi", spec, hook, resume=False)
            self.assertIsNone(self.common.load(str(self.work), spec.sid))

    def test_writable_symlink_to_protected_driver_hook_is_rejected(self):
        link = self.cwd / "driver-link.py"
        link.symlink_to("/protected/driver_hook.py")
        with (
            mock.patch("shutil.which", return_value="/protected/codex"),
            self.assertRaisesRegex(ConfigError, "rewrite|writable"),
        ):
            self.adapter.tui().launch(
                "hi", self.spec, (sys.executable, "-I", str(link)), resume=False
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid))

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
                            launch = adapter.tui().launch("hi", spec, HOOK, resume=True)
                            self.assertNotIn("TMPDIR", launch.env)
                        continue
                    with (
                        mock.patch("shutil.which", return_value="/protected/codex"),
                        self.assertRaisesRegex(ConfigError, "rewrite|writable"),
                    ):
                        adapter.tui().launch(
                            "hi",
                            spec,
                            (sys.executable, "-I", "/driver-only/hook.py"),
                            resume=resume,
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
            roots = run.common.writable_dirs(self.spec, resume=False)
            self.assertEqual(list(roots[:3]), expected)
            self.assertIn(str(temporary), roots)
            self.assertEqual(len(roots), len(set(roots)))
            self.assertFalse((self.home / ".agent-pm").exists())
            with mock.patch("shutil.which", return_value="/protected/codex"):
                run.launch("hello", self.spec, HOOK, resume=False)
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
            resumed = changed.common.writable_dirs(spec, resume=True)
            self.assertEqual(list(resumed[: len(saved)]), saved)
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
            launch = self.adapter.tui().launch(PROMPT, spec, HOOK, resume=resume)
        return launch, json.loads(launch.argv[-1])

    def test_supplied_flags_env_and_hooks_share_one_path(self):
        flags = ("--add-dir", "../../added", '--config=model_verbosity="low"', "--enable", "web")
        spec = dataclasses.replace(self.spec, flags=flags, env={"S": "s"})
        launch, data = self.launch(spec)
        argv = data["argv"]
        start = argv.index("--add-dir")
        self.assertEqual(argv[start : start + len(flags)], list(flags))
        self.assertEqual(launch.env["S"], "s")
        self.assertEqual(data["add_dirs"], [os.path.join(self.cwd, "../../added")])
        hook_flags = [
            word
            for event in self.adapter.tui().hooks
            for word in ("-c", next(word for word in argv if word.startswith(f"hooks.{event}=")))
        ]
        self.assertEqual(
            data["hook_flags"], [*hook_flags, "-c", 'model_verbosity="low"', "--enable", "web"]
        )
        self.assert_no_policy(argv)

    def assert_no_policy(self, argv):
        self.assertNotIn("-s", argv)
        self.assertFalse([w for w in argv if w.startswith(POLICY)])

    def test_flags_after_separator_are_preserved_in_snapshot(self):
        flags = ("--", "resume", "-C", "/x")
        spec = dataclasses.replace(self.spec, flags=flags)
        self.launch(spec)
        self.assertEqual(self.common.load(str(self.work), spec.sid).snapshot["flags"], list(flags))

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
        self.assertEqual(self.launch(changed, resume=True)[1]["add_dirs"], [str(guarded)])


class LaunchPolicy(Base):
    def argv(self, adapter, spec, *, resume=False):
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = adapter.tui().launch("hi", spec, HOOK, resume=resume)
        return json.loads(launch.argv[-1])["argv"]

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
                    self.common.load(str(self.work), spec.sid).snapshot["flags"], list(flags)
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
                self.assertEqual(sum(word.startswith("hooks.") for word in argv[:boundary]), 6)
            self.assertEqual(
                self.common.load(str(self.work), spec.sid).snapshot["flags"], list(supplied)
            )
