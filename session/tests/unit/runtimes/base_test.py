"""runtimes/base.py: Runtime, the ABC each Mode's running session implements (a class start
returning it started, then its session calls), and the start helpers the Runtimes share: child_env
and identity_drop.
"""

import inspect
import itertools
import os
import tempfile
from unittest import mock

from agent_pm.session.adapters.base import Adapter, Headless
from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import Access, ConfigError, Launch, Mode, Spec
from agent_pm.session.hosts.base import Host
from agent_pm.session.runtimes import headless, in_process, tui
from agent_pm.session.runtimes.base import Runtime, child_env, identity_drop

from session.tests.case import HOSTS, Case
from session.tests.fakes.terminal import MemoryHost

MEMBERS = ["poll", "returncode", "screen", "send", "start", "state", "stop"]
KINDS = {
    headless.HeadlessRuntime: Mode.HEADLESS,
    in_process.InProcessRuntime: Mode.IN_PROCESS,
    tui.TuiRuntime: Mode.TUI,
}
LEASE = {"AGENT_PM_MANAGER", "AGENT_PM_HOLDER_ID", "AGENT_PM_HOLDER_GENERATION"}
IDENTITIES = {
    "AGENT_PM_SID",
    "AGENT_PM_EVENTS",
    *LEASE,
    "INNER_SESSION",
    "INNER_ID",
    "OTHER_SESSION",
    "OTHER_ID",
}


class Launches(Headless):
    """A headless mode recording each launch it is asked for."""

    def __init__(self):
        self.asked = []

    def launch(self, spec):
        self.asked.append(spec)
        return Launch(("x",))

    def events(self, lines):
        return iter(())


class HeadlessAdapter(Adapter):
    def __init__(self, config):
        super().__init__(config)
        self.mode = Launches()

    def headless(self):
        return self.mode


class NoModeAdapter(Adapter):
    def headless(self):
        return None

    def tui(self):
        return None


class InnerAdapter(NoModeAdapter):
    """A client whose sessions set INNER_SESSION in their children."""

    session_env = "INNER_SESSION"
    identity_env = ("INNER_SESSION", "INNER_ID")
    child_defaults = {"PAGER": "cat", "NO_COLOR": "1"}


class OtherAdapter(NoModeAdapter):
    session_env = "OTHER_SESSION"
    identity_env = ("OTHER_SESSION", "OTHER_ID")
    child_defaults = {"OTHER_MODE": "on"}


def registry(**adapters) -> Registry:
    """A Registry of `adapters` (client → Adapter class, None: one failing to load)."""

    def load(path, _base):
        found = classes[path]
        if found is None:
            raise ValueError(f"{path}: cannot import")
        return found

    classes = {f"tests.{name}:{name.capitalize()}Adapter": cls for name, cls in adapters.items()}
    return Registry(
        adapters={
            name: {"implementation": f"tests.{name}:{name.capitalize()}Adapter"}
            for name in adapters
        },
        hosts=Host.parse_hosts({**HOSTS, "default": "tmux"}),
        class_loader=load,
    )


class RuntimeTest(Case):
    def test_its_abstract_members_are_one_start_and_the_six_session_calls(self):
        self.assertTrue(inspect.isabstract(Runtime))
        self.assertEqual(sorted(Runtime.__abstractmethods__), MEMBERS)

    def test_each_mode_subclasses_it_with_its_parameters_and_its_kind(self):
        for mode, name in itertools.product(KINDS, MEMBERS):
            with self.subTest(mode=mode.__name__, name=name):
                self.assertTrue(issubclass(mode, Runtime))
                self.assertFalse(inspect.isabstract(mode))
                self.assertEqual(params(getattr(mode, name)), params(getattr(Runtime, name)))
        self.assertEqual({mode: mode.kind for mode in KINDS}, KINDS)

    def test_start_is_the_class_entry_and_no_session_is_built_without_its_prepared(self):
        for mode in (Runtime, *KINDS):
            with self.subTest(mode=mode.__name__):
                self.assertIsInstance(inspect.getattr_static(mode, "start"), classmethod)
                self.assertIsInstance(inspect.getattr_static(mode, "prepared"), property)
                self.assertIsNone(inspect.getattr_static(mode, "prepared").fset)
        for mode in KINDS:
            with self.subTest(mode=mode.__name__), self.assertRaises(TypeError):
                mode()

    def test_a_failed_start_returns_no_session_and_records_nothing(self):
        spec = Spec(workdir="/w", access=Access(cwd="/w"), client="none", sid="s1")
        for mode, kind in KINDS.items():
            recorded = []
            with self.subTest(mode=kind.value), self.assertRaises(ConfigError) as caught:
                mode.start(
                    spec,
                    NoModeAdapter({}),
                    registry=registry(),
                    record=recorded.append,
                    host=MemoryHost(),
                )
            self.assertEqual(
                str(caught.exception), f"client 'none' does not support {kind.value} mode"
            )
            self.assertEqual(recorded, [])

    def test_a_start_inside_an_agent_session_is_refused_before_its_launch(self):
        adapter, recorded = HeadlessAdapter({}), []
        spec = Spec(workdir="/w", access=Access(cwd="/w"), client="headless", sid="s1")
        with (
            mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}),
            self.assertRaises(ConfigError) as caught,
        ):
            headless.HeadlessRuntime.start(
                spec, adapter, registry=registry(), record=recorded.append, host=MemoryHost()
            )
        self.assertTrue(str(caught.exception).startswith("refused inside an agent session"))
        self.assertEqual((adapter.mode.asked, recorded), ([], []))


class ChildEnvTest(Case):
    def environ(self, **values):
        self.enterContext(
            mock.patch.dict(os.environ, {"HOME": os.environ["HOME"], **values}, clear=True)
        )

    def test_the_environment_less_drop_and_unset_then_the_launch_env_and_pwd(self):
        self.environ(KEEP="k", DROPPED="d", UNSET="u", SET="old")
        launch = Launch(("x",), env={"SET": "new", "ADDED": "1"}, unset=("UNSET",))
        self.assertEqual(
            child_env(launch, "/cwd", {"DROPPED"}),
            {
                "HOME": os.environ["HOME"],
                "KEEP": "k",
                "SET": "new",
                "ADDED": "1",
                "PWD": "/cwd",
                "AGENT_PM_AGENT_SESSION": "1",
            },
        )
        self.assertEqual(child_env(launch, "/c")["PWD"], "/c")

    def test_launch_env_keys_need_no_allowlist_and_may_bring_back_a_dropped_one(self):
        self.environ(KEEP="k", OTHER_MODE="on")
        launch = Launch(("x",), env={"EDITOR": "vi", "ZED": "1", "OTHER_MODE": "on"})
        env = child_env(launch, "/cwd", {"OTHER_MODE"})
        self.assertEqual(
            {k: env[k] for k in ("KEEP", "EDITOR", "ZED", "OTHER_MODE")},
            {"KEEP": "k", "EDITOR": "vi", "ZED": "1", "OTHER_MODE": "on"},
        )

    def test_every_client_carries_the_agent_session_marker_whatever_it_inherits_or_is_given(self):
        self.assertEqual(child_env(Launch(("x",)), "/cwd")["AGENT_PM_AGENT_SESSION"], "1")
        self.environ(AGENT_PM_AGENT_SESSION="0")
        for launch in (
            Launch(("x",)),
            Launch(("x",), env={"AGENT_PM_AGENT_SESSION": ""}),
            Launch(("x",), unset=("AGENT_PM_AGENT_SESSION",)),
        ):
            with self.subTest(launch=launch):
                env = child_env(launch, "/cwd", {"AGENT_PM_AGENT_SESSION"})
                self.assertEqual(env["AGENT_PM_AGENT_SESSION"], "1")


class PluginPathTest(ChildEnvTest):
    """FR-32: a session's PATH starts with the running plugin's bin/, the directory where PATH
    finds agent-pm-session (each plugin bin/ script puts its own first).
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plugin = os.path.join(os.path.realpath(tmp.name), "plugin root", "bin")
        os.makedirs(self.plugin)
        command = os.path.join(self.plugin, "agent-pm-session")
        with open(command, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(command, 0o755)

    def test_the_plugin_bin_goes_first_over_the_inherited_and_the_launch_path(self):
        self.environ(PATH=os.pathsep.join(("/usr/bin", self.plugin, "/bin")))
        self.assertEqual(
            child_env(Launch(("x",)), "/cwd")["PATH"],
            os.pathsep.join((self.plugin, "/usr/bin", self.plugin, "/bin")),
        )
        snapshot = Launch(("x",), env={"PATH": "/opt/old/bin:/usr/bin"})
        self.assertEqual(
            child_env(snapshot, "/cwd")["PATH"],
            os.pathsep.join((self.plugin, "/opt/old/bin", "/usr/bin")),
        )

    def test_a_path_already_led_by_the_plugin_bin_is_kept(self):
        path = os.pathsep.join((self.plugin, "/usr/bin"))
        self.environ(PATH=path)
        self.assertEqual(child_env(Launch(("x",)), "/cwd")["PATH"], path)

    def test_without_agent_pm_session_on_path_the_path_is_unchanged(self):
        self.environ(PATH="/usr/bin:/bin")
        self.assertEqual(child_env(Launch(("x",)), "/cwd")["PATH"], "/usr/bin:/bin")
        launch = Launch(("x",), env={"PATH": "/opt/bin"})
        self.assertEqual(child_env(launch, "/cwd")["PATH"], "/opt/bin")


class IdentityDropTest(ChildEnvTest):
    def test_every_configured_adapters_identity_env(self):
        self.environ(INNER_ID="i", OTHER_SESSION="", OTHER_ID="o", KEEP="k")
        self.assertEqual(
            identity_drop(registry(inner=InnerAdapter, other=OtherAdapter)), IDENTITIES
        )
        self.assertEqual(
            identity_drop(registry()), frozenset({"AGENT_PM_SID", "AGENT_PM_EVENTS", *LEASE})
        )

    def test_a_callers_start_token_never_reaches_a_client(self):
        self.environ(AGENT_PM_SID="0f8fad5b-d9cb-469f-a165-70867728950e", KEEP="k")
        env = child_env(Launch(("x",)), "/cwd", identity_drop(registry()))
        self.assertEqual((env.get("AGENT_PM_SID"), env["KEEP"]), (None, "k"))

    def test_a_managers_roster_lease_never_reaches_a_client(self):
        self.environ(**dict.fromkeys(LEASE, "1"), KEEP="k")
        env = child_env(Launch(("x",)), "/cwd", identity_drop(registry()))
        self.assertEqual((LEASE & env.keys(), env["KEEP"]), (set(), "k"))

    def test_a_callers_events_file_never_reaches_a_client(self):
        self.environ(AGENT_PM_EVENTS="/harness/events", KEEP="k")
        env = child_env(Launch(("x",)), "/cwd", identity_drop(registry()))
        self.assertEqual((env.get("AGENT_PM_EVENTS"), env["KEEP"]), (None, "k"))
        given = Launch(("x",), env={"AGENT_PM_EVENTS": "/given/events"})
        env = child_env(given, "/cwd", identity_drop(registry()))
        self.assertEqual(env["AGENT_PM_EVENTS"], "/given/events")

    def test_a_child_default_only_inside_its_client_and_at_its_value(self):
        both = registry(inner=InnerAdapter, other=OtherAdapter)
        for environ, defaults in (
            ({"PAGER": "cat", "NO_COLOR": "1", "OTHER_MODE": "on"}, set()),
            (
                {"INNER_SESSION": "1", "PAGER": "cat", "NO_COLOR": "0", "OTHER_MODE": "on"},
                {"PAGER"},
            ),
            (
                {"OTHER_SESSION": "1", "PAGER": "cat", "NO_COLOR": "1", "OTHER_MODE": "on"},
                {"OTHER_MODE"},
            ),
        ):
            with self.subTest(environ=environ):
                self.environ(**environ)
                self.assertEqual(identity_drop(both) - IDENTITIES, defaults)

    def test_codex_injected_defaults_only_inside_codex_at_their_injected_value(self):
        inside = {"CODEX_THREAD_ID": "t"}
        for environ, dropped in (
            ({"CLAUDECODE": "1", "NO_COLOR": "1", "PAGER": "cat"}, set()),
            ({**inside, "NO_COLOR": "0", "PAGER": "less", "CODEX_CI": "x"}, set(inside)),
            (
                {**inside, "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "CODEX_CI": "1"},
                {*inside, "NO_COLOR", "PAGER", "GIT_PAGER", "CODEX_CI"},
            ),
        ):
            with self.subTest(environ=environ):
                self.environ(**environ)
                self.assertEqual(
                    identity_drop(registry(codex=CodexAdapter)) & set(environ), dropped
                )

    def test_it_reads_class_data_and_builds_no_adapter(self):
        class Unbuilt(InnerAdapter):
            def __init__(self, config):
                raise AssertionError("built")

        self.environ(INNER_SESSION="1", PAGER="cat", OTHER_ID="o")
        self.assertEqual(
            identity_drop(registry(inner=Unbuilt, other=OtherAdapter)), {*IDENTITIES, "PAGER"}
        )

    def test_an_adapter_failing_to_load_is_a_config_error(self):
        with self.assertRaises(ConfigError) as caught:
            identity_drop(registry(inner=InnerAdapter, broken=None))
        self.assertEqual(
            str(caught.exception),
            "[adapters.broken].implementation: tests.broken:BrokenAdapter: cannot import",
        )


def params(function):
    return [
        (None if p.kind is inspect.Parameter.POSITIONAL_ONLY else p.name, p.kind, p.default)
        for p in inspect.signature(function).parameters.values()
    ]
