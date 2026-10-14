"""Session preflight shares startup validation without creating state, events or runtime work."""

import contextlib
import itertools
import os
import tempfile
from dataclasses import replace
from unittest import mock

from agent_pm.session import Access, ConfigError, Effort, Host, Mode, Session, Spec, read_config
from agent_pm.session.engine.hosted import HostedSession
from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import HOSTS, REPO, SPAWNS, Case
from session.tests.fakes.preflight import PreflightAdapter, SandboxedPreflightAdapter
from session.tests.hosted_support import SID, fake_tmux


class Preflight(Case):
    def setUp(self):
        self.work = os.path.join(self.home, "absent-work")
        self.events = os.path.join(self.home, "absent-events")
        self.tmux = fake_tmux()
        self.tmux.validate_events.side_effect = tmux_host.validate_events
        self.tmux.validate_start.side_effect = tmux_host.validate_start
        self.configure()

    def configure(self, *, adapter=PreflightAdapter, tmux_settings=None, **settings):
        PreflightAdapter.prepared, PreflightAdapter.committed, PreflightAdapter.ran = [], [], []
        self.session = HostedSession.configured(
            adapters={
                "fake": {
                    "implementation": f"{adapter.__module__}:{adapter.__qualname__}",
                    **settings,
                }
            },
            hosts=Host.parse_hosts(
                {"tmux": {**HOSTS["tmux"], **(tmux_settings or {})}, "default": "tmux"}
            ),
            tmux=self.tmux,
        )

    def spec(self, mode=Mode.HEADLESS, **changes):
        return Spec(
            workdir=self.work, runtime="fake", mode=mode, prompt="input", sid=SID, **changes
        )

    def tree(self):
        return sorted(
            os.path.relpath(os.path.join(root, name), self.home)
            for root, dirs, files in os.walk(self.home)
            for name in (*dirs, *files)
        )

    @contextlib.contextmanager
    def no_runtime(self):
        with contextlib.ExitStack() as stack:
            calls = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in (*SPAWNS, "threading.Thread.start")
            ]
            yield
        for call in calls:
            call.assert_not_called()

    def unchanged(self, before):
        self.assertEqual(self.tree(), before)
        self.assertEqual((PreflightAdapter.committed, PreflightAdapter.ran), ([], []))
        self.tmux.start.assert_not_called()

    def refusal(self, spec, message, **placement):
        before = self.tree()
        diagnostics = []
        for method in (self.session.preflight, self.session.start):
            with self.subTest(method=method.__name__):
                with self.no_runtime(), self.assertRaises(ConfigError) as caught:
                    method(spec, **placement)
                self.assertIn(message, str(caught.exception))
                diagnostics.append(str(caught.exception))
                self.unchanged(before)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0], diagnostics[1])

    def test_all_modes_accept_fresh_and_resume_without_writing_or_committing(self):
        for mode, resume in itertools.product(Mode, (False, True)):
            with self.subTest(mode=mode, resume=resume):
                spec = self.spec(mode, resume=resume)
                before = self.tree()
                with self.no_runtime():
                    self.assertIsNone(self.session.preflight(spec))
                self.unchanged(before)
                self.assertEqual(PreflightAdapter.prepared[-1].sid, SID)
                self.assertEqual(PreflightAdapter.prepared[-1].resume, resume)

    def test_abc_forwards_preflight_to_the_configured_session_class(self):
        Session.configure(
            adapters={"fake": {"implementation": "session.tests.fakes.preflight:PreflightAdapter"}},
            hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
        )
        before = self.tree()
        with self.no_runtime():
            self.assertIsNone(Session.preflight(self.spec()))
        self.unchanged(before)

    def test_missing_sid_can_be_prepared_without_mutating_the_callers_spec(self):
        spec = replace(self.spec(), sid=None)
        before = self.tree()
        with self.no_runtime():
            self.session.preflight(spec)
        self.assertIsNone(spec.sid)
        self.assertTrue(PreflightAdapter.prepared[-1].sid)
        self.unchanged(before)

    def test_missing_runtime_mode_unknown_runtime_and_host_have_startup_parity(self):
        for spec, message, placement in (
            (replace(self.spec(), runtime=None), "spec.runtime", {}),
            (replace(self.spec(), mode=None), "spec.mode", {}),
            (replace(self.spec(), runtime="absent"), "adapters.absent", {}),
            (self.spec(), "unknown host absent", {"host": "absent"}),
        ):
            with self.subTest(spec=spec, placement=placement):
                self.refusal(spec, message, **placement)

    def test_unsupported_modes_and_selection_flags_are_refused_before_state(self):
        for mode in Mode:
            with self.subTest(mode=mode):
                self.configure(unsupported=mode.value)
                self.refusal(self.spec(mode), f"does not support {mode.value} mode")
                self.configure()
                self.refusal(self.spec(mode, flags=("--resume",)), "picks the session")

    def test_non_tui_placement_and_invalid_tui_name_are_refused_without_events_file(self):
        for mode in (Mode.HEADLESS, Mode.IN_PROCESS):
            for spec, placement, message in (
                (self.spec(mode, name="window"), {}, "name: tui mode only"),
                (self.spec(mode, events=self.events), {}, "events: tui mode only"),
                (self.spec(mode), {"display": "opaque"}, "display: tui mode only"),
            ):
                with self.subTest(mode=mode, message=message):
                    self.refusal(spec, message, **placement)
        self.refusal(self.spec(Mode.TUI, name="bad name", events=self.events), "name must match")

    def test_idle_display_adapter_launch_and_in_process_checks_have_startup_parity(self):
        for timeout in (0, -1, float("nan"), True, "1"):
            with self.subTest(timeout=timeout):
                self.refusal(self.spec(), "idle_timeout", idle_timeout=timeout)
        self.refusal(self.spec(Mode.TUI), "display", display="bad\nvalue")
        self.configure(empty_argv=True)
        for mode in (Mode.HEADLESS, Mode.TUI):
            with self.subTest(mode=mode):
                self.refusal(self.spec(mode), "gave no command")
        self.configure(bad_env=True)
        for mode in (Mode.HEADLESS, Mode.TUI):
            with self.subTest(mode=mode):
                self.refusal(self.spec(mode), "launch: env")
        self.configure()
        self.refusal(self.spec(Mode.IN_PROCESS, tier=4), "tier 4 is unmapped")

    def test_protection_checks_cwd_grants_and_runtime_dirs_for_all_modes(self):
        protected = os.path.join(self.home, "protected")
        for mode, resume, path, field in itertools.product(
            Mode,
            (False, True),
            (protected, os.path.join(protected, "child"), self.home),
            ("cwd", "dirs", "runtime"),
        ):
            with self.subTest(mode=mode, resume=resume, path=path, field=field):
                self.configure(writable=(path,) if field == "runtime" else ())
                access = (
                    Access(cwd=path)
                    if field == "cwd"
                    else Access(cwd=self.work, dirs=(path,) if field == "dirs" else ())
                )
                self.refusal(
                    self.spec(mode, resume=resume, access=access, protect=(protected,)),
                    "overlaps protected path",
                )

    def test_protection_uses_realpaths_and_effective_default_cwd(self):
        protected = os.path.join(self.home, "protected")
        os.mkdir(protected)
        alias = os.path.join(self.home, "alias")
        os.symlink(protected, alias)
        for access, protection in (
            (Access(), self.work),
            (Access(cwd=alias), protected),
            (Access(cwd=self.work, dirs=(alias,)), protected),
        ):
            with self.subTest(access=access):
                self.refusal(
                    self.spec(access=access, protect=(protection,)), "overlaps protected path"
                )

    def test_read_only_protected_grants_and_both_environment_sources_are_preserved(self):
        protected = os.path.join(self.home, "read-only")
        access = Access(
            cwd=self.work, reads=(protected,), env={"FROM_ACCESS": "kept", "SHARED": "access"}
        )
        for mode in Mode:
            with self.subTest(mode=mode):
                spec = self.spec(
                    mode,
                    access=access,
                    env={"FROM_SPEC": "kept", "SHARED": "spec"},
                    protect=(protected,),
                )
                before = self.tree()
                with self.no_runtime():
                    self.session.preflight(spec)
                prepared = PreflightAdapter.prepared[-1]
                self.assertEqual(prepared.access.reads, (protected,))
                self.assertEqual(
                    dict(prepared.access.env), {"FROM_ACCESS": "kept", "SHARED": "access"}
                )
                self.assertEqual(dict(prepared.env), {"FROM_SPEC": "kept", "SHARED": "spec"})
                self.unchanged(before)

    def test_tui_preflight_validates_events_without_creating_the_file(self):
        before = self.tree()
        with self.no_runtime():
            self.session.preflight(self.spec(Mode.TUI, events=self.events))
        self.unchanged(before)
        self.refusal(self.spec(Mode.TUI, events=self.events + "#bad"), "tmux would misread")
        self.refusal(
            self.spec(Mode.TUI, events=self.events, flags=("--resume",)), "picks the session"
        )

    def test_tui_settings_and_adapter_hook_protection_are_validated_before_state(self):
        self.configure(tmux_settings={"status_line": "invalid"})
        self.refusal(self.spec(Mode.TUI), "status_line: want true or false")
        self.configure(adapter=SandboxedPreflightAdapter)
        hook_root = os.path.join(self.agent_pm, "tui")
        self.refusal(
            self.spec(Mode.TUI, access=Access(cwd=self.work, dirs=(hook_root,))),
            "overlaps protected path",
        )

    def test_codex_oversized_recovery_state_is_refused_before_any_directory_is_created(self):
        root = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(root.cleanup)
        self.home = os.path.realpath(root.name)
        self.work = os.path.join(self.home, "absent-work")
        settings = read_config(local=os.devnull)
        self.session = HostedSession.configured(
            adapters=settings["adapters"], hosts=Host.parse_hosts(settings["hosts"])
        )
        spec = Spec(
            workdir=self.work,
            runtime="codex",
            mode=Mode.HEADLESS,
            sid=SID,
            tier=2,
            effort=Effort.HIGH,
            env={"LARGE": "x" * 70000},
        )
        lexists = os.path.lexists
        with (
            mock.patch.dict(
                os.environ, {"HOME": self.home, "CODEX_HOME": os.path.join(self.home, ".codex")}
            ),
            mock.patch(
                "os.path.lexists",
                side_effect=lambda path: (
                    os.path.commonpath((os.path.abspath(path), self.home)) == self.home
                    and lexists(path)
                ),
            ),
        ):
            self.refusal(spec, "recovery state is too large")
