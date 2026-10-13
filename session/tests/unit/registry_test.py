"""adapters/registry.py: what Session.configure stores (the [adapters.<name>] tables, the hosts' tables and the default
host), settings() (Session.settings) from a read session config, the tui settings from [hosts.tmux] and Adapter
loading. Adapter packages are registered in sys.modules or the
import system only for the test's span; the registry itself is never patched. Case resets it around each test."""

import importlib
import importlib.abc
import importlib.util
import os
import sys
import tomllib
import types
import unittest
from unittest import mock

import agent_pm.session
import agent_pm.session.hosts.tmux
from agent_pm.session import Session
from agent_pm.session.adapters import registry
from agent_pm.session.adapters.base import Adapter
from agent_pm.session.engine.types import ConfigError
from agent_pm.utils import config_file
from agent_pm.utils import registry as class_registry

from session.tests import fake_adapter
from session.tests.case import HOSTS, Case, settings

FAKE = '[adapters.fake]\nimplementation = "adapters.fake:FakeAdapter"\n'
UNCONFIGURED = "the session is not configured: call Session.configure first"


class OddAdapter:
    pass


class HalfAdapter(Adapter):
    def headless(self):
        return None


def odd_adapter():
    return None


def package(name, **names) -> types.ModuleType:
    """An in-memory module agent_pm.session.adapters.<name> holding `names`."""
    module = types.ModuleType(f"agent_pm.session.adapters.{name}")
    vars(module).update(names)
    return module


class SourceFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds module `name` and runs `source` as its body, as if it were a file on sys.path."""

    def __init__(self, name, source):
        self.name, self.source = name, source

    def find_spec(self, fullname, path, target=None):
        return importlib.util.spec_from_loader(fullname, self) if fullname == self.name else None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(self.source, vars(module))


class Base(Case):
    def configure(self, text, tmux=None):
        """TOML `text`'s [adapters] tables and HOSTS (`tmux`'s keys in [hosts.tmux]) through Session.configure."""
        Session.configure(**settings(tomllib.loads(text).get("adapters", {}), tmux))

    def register(self, **modules):
        """Each name's module as sys.modules["agent_pm.session.adapters.<name>"] for the rest of the test."""
        patch = mock.patch.dict(
            sys.modules, {f"agent_pm.session.adapters.{name}": m for name, m in modules.items()}
        )
        patch.start()
        self.addCleanup(patch.stop)

    def refused(self, message, call):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)


class ConfigureTest(Base):
    def test_unconfigured_every_read_says_so(self):
        for call in (lambda: registry.adapter("claude"), registry.configured, registry.tui):
            with self.subTest(call=call):
                self.refused(UNCONFIGURED, call)

    def test_configured_the_adapters_and_tui_settings_are_read_without_a_file(self):
        self.register(fake=fake_adapter)
        with mock.patch("builtins.open") as opened:
            self.configure(FAKE + 'scenario = "/s"\n[adapters.a]\nk = 1\n', {"status_line": True})
            self.assertEqual(registry.configured(), ["a", "fake"])
            self.assertEqual(registry.adapter("fake").config, {"scenario": "/s"})
            self.assertEqual(registry.tui(), registry.TuiSettings(True, None, None))
        opened.assert_not_called()

    def test_configuring_again_replaces_and_reset_drops(self):
        self.register(fake=fake_adapter)
        self.configure(FAKE + 'scenario = "/a"\n', {"workers_per_column": 2})
        self.configure(FAKE + 'scenario = "/b"\n')
        self.assertEqual(registry.adapter("fake").config, {"scenario": "/b"})
        self.assertEqual(registry.tui(), registry.TuiSettings())
        registry.reset()
        self.refused(UNCONFIGURED, registry.configured)

    def test_the_adapter_tables_are_deep_copies(self):
        self.register(fake=fake_adapter)
        given = settings(
            {"fake": {"implementation": "adapters.fake:FakeAdapter", "log": {"a": [1]}}}
        )
        Session.configure(**given)
        given["adapters"]["fake"]["log"]["a"].append(2)
        self.assertEqual(registry.adapter("fake").config, {"log": {"a": [1]}})

    def test_refuses_bad_input_and_keeps_what_was_configured(self):
        self.configure(FAKE)
        tables = "adapters: want a mapping of [adapters.<name>] tables"
        hosts = "hosts: want a mapping of [hosts.<name>] tables"
        for kw, message in (
            ({"adapters": 1}, tables),
            ({"adapters": ["fake"]}, tables),
            ({"adapters": {"fake": 1}}, tables),
            ({"adapters": {1: {}}}, tables),
            ({"hosts": {**HOSTS, 2: {}}}, "[hosts] 2: want a string key"),
            ({"hosts": 1}, hosts),
            ({"hosts": {**HOSTS, "default": {}}}, hosts),
            ({"hosts": {"tmux": 1}}, "[hosts] tmux: want default or a [hosts.<name>] table"),
            (
                {"hosts": {"tmux": {"implementation": "agent_pm.TmuxHost"}}},
                "[hosts.tmux].implementation: want module.path:Class",
            ),
            ({"default_host": "pty"}, "[hosts] default: want one of tmux"),
            ({"default_host": None}, "[hosts] default: want one of tmux"),
        ):
            with self.subTest(kw=kw):
                given = {**settings({}), **kw}
                self.refused(message, lambda: Session.configure(**given))
                self.assertEqual(registry.configured(), ["fake"])

    def test_the_forward_tmux_host_name_is_never_loaded(self):
        with (
            mock.patch.object(importlib, "import_module", wraps=importlib.import_module) as imp,
            mock.patch.object(class_registry, "load") as load,
        ):
            Session.configure(**settings({}, {"workers_per_column": 4}))
            self.assertEqual(registry.tui(), registry.TuiSettings(False, 4, None))
        imp.assert_not_called()
        load.assert_not_called()
        self.assertFalse(hasattr(agent_pm.session.hosts.tmux, "TmuxHost"))


class SettingsTest(Base):
    TWO = '[hosts]\ndefault = "a"\n[hosts.a]\nimplementation = "pkg.mod:AHost"\n[hosts.b]\nimplementation = "p.b:B"\n'

    def test_the_adapters_hosts_and_default_of_a_read_config(self):
        config = tomllib.loads(self.TWO + FAKE)
        found = registry.settings(config)
        self.assertEqual(
            found,
            {
                "adapters": {"fake": {"implementation": "adapters.fake:FakeAdapter"}},
                "hosts": {
                    "a": {"implementation": "pkg.mod:AHost"},
                    "b": {"implementation": "p.b:B"},
                },
                "default_host": "a",
            },
        )
        self.assertEqual(Session.settings(config), found)
        self.assertEqual(registry.settings(tomllib.loads(self.TWO))["adapters"], {})
        with self.assertRaisesRegex(ValueError, r"\A\[hosts\]: missing\Z"):
            registry.settings({})
        for config in (None, 1, [], "x"):
            with (
                self.subTest(config=config),
                self.assertRaisesRegex(ValueError, r"\Asession config: want a table\Z"),
            ):
                registry.settings(config)

    def test_the_session_config_with_the_local_file_on_top(self):
        with open(os.path.join(self.agent_pm, "local.toml"), "w") as f:
            f.write("[hosts.tmux]\nstatus_line = true\n")
        found = Session.settings(config_file.read_project("session"))
        self.assertEqual(sorted(found["adapters"]), ["claude", "codex", "print_prompt"])
        self.assertEqual(found["default_host"], "tmux")
        Session.configure(**found)
        self.assertEqual(registry.tui(), registry.TuiSettings(True, 3, "open-close"))


class TuiTest(Base):
    def test_defaults_when_unset(self):
        self.configure(FAKE)
        self.assertEqual(registry.tui(), registry.TuiSettings(False, None, None))
        self.assertEqual(registry.TuiSettings(), registry.TuiSettings(False, None, None))
        Session.configure(adapters={}, hosts={"other": HOSTS["tmux"]}, default_host="other")
        self.assertEqual(registry.tui(), registry.TuiSettings())

    def test_the_hosts_tmux_keys_values(self):
        for tmux, want in (
            (
                {"status_line": True, "workers_per_column": 2, "grid_retile": "all"},
                (True, 2, "all"),
            ),
            (
                {"status_line": False, "workers_per_column": 9999, "grid_retile": "off"},
                (False, 9999, "off"),
            ),
            ({"workers_per_column": 1, "grid_retile": "open-close"}, (False, 1, "open-close")),
        ):
            with self.subTest(tmux=tmux):
                self.configure(FAKE, tmux)
                self.assertEqual(registry.tui(), registry.TuiSettings(*want))

    def test_an_adapters_key_is_no_setting(self):
        self.configure("[adapters.fake]\nstatus_line = true\nworkers_per_column = 2\n")
        self.assertEqual(registry.tui(), registry.TuiSettings())

    def test_a_value_of_the_wrong_type_or_range_is_a_config_error_naming_its_key(self):
        self.register(fake=fake_adapter)
        for key, values, message in (
            ("status_line", ("true", 1, 0, [True]), "status_line: want true or false"),
            (
                "workers_per_column",
                (0, 10000, -1, True, 2.0, "2"),
                "workers_per_column: want an integer from 1 to 9999",
            ),
            (
                "grid_retile",
                ("All", "", "never", 1, ["all"]),
                "grid_retile: want one of all, open-close, off",
            ),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.configure(FAKE, {key: value})
                    self.refused(message, registry.tui)
                    self.assertIs(type(registry.adapter("fake")), fake_adapter.FakeAdapter)


class AdapterTest(Base):
    def test_builds_the_configured_class_from_its_table_minus_implementation(self):
        self.register(fake=fake_adapter)
        self.configure(FAKE + 'scenario = "/s.json"\nlog = "/log"\n')
        built = registry.adapter("fake")
        self.assertEqual(registry.PACKAGE, agent_pm.session.__name__)
        self.assertIs(type(built), fake_adapter.FakeAdapter)
        self.assertEqual(built.config, {"scenario": "/s.json", "log": "/log"})

    def test_refuses_an_unsafe_name(self):
        for bad in ("Fake", "", "../fake", "a-b", "1a", "fake\n", "adapters.fake", 1, None):
            with self.subTest(bad=bad):
                self.refused(
                    f"unsafe runtime name {bad!r}: want a lowercase identifier",
                    lambda: registry.adapter(bad),
                )

    def test_refuses_a_missing_table(self):
        self.configure('[adapters.other]\nimplementation = "adapters.other:OtherAdapter"\n')
        self.refused(
            "no [adapters.fake] table in the configured adapters", lambda: registry.adapter("fake")
        )

    def test_refuses_an_implementation_of_another_shape_or_package(self):
        self.register(fake=fake_adapter)
        message = '[adapters.fake].implementation must be "adapters.fake:<Class>Adapter"'
        for line in (
            "",
            "implementation = 1\n",
            'implementation = "adapters.fake:Fake"\n',
            'implementation = "adapters.fake:Adapter"\n',
            'implementation = "adapters.fake.FakeAdapter"\n',
            'implementation = "fake:FakeAdapter"\n',
            'implementation = "adapters.fake:FakeAdapter\\n"\n',
            'implementation = "adapters.fake:fake.FakeAdapter"\n',
            'implementation = "adapters.other:OtherAdapter"\n',
            'implementation = "adapters.Fake:FakeAdapter"\n',
        ):
            with self.subTest(line=line):
                self.configure("[adapters.fake]\n" + line)
                self.refused(message, lambda: registry.adapter("fake"))

    def test_refuses_a_missing_package(self):
        self.configure('[adapters.nope]\nimplementation = "adapters.nope:NopeAdapter"\n')
        self.refused(
            "adapter 'nope' has no package adapters.nope", lambda: registry.adapter("nope")
        )

    def test_refuses_a_missing_parent_package(self):
        self.configure(FAKE)
        with mock.patch.dict(sys.modules), mock.patch.object(agent_pm, "__path__", []):
            del sys.modules[Adapter.__module__.rpartition(".")[0]]
            self.refused(
                "adapter 'fake' has no package adapters.fake", lambda: registry.adapter("fake")
            )

    def test_refuses_a_package_that_fails_to_import(self):
        self.configure('[adapters.broken]\nimplementation = "adapters.broken:BrokenAdapter"\n')
        for source, shown in (
            ("import agent_pm_absent_dependency\n", "No module named 'agent_pm_absent_dependency'"),
            ("from agent_pm.session.adapters.base import Absent\n", "cannot import name 'Absent'"),
            ("raise OSError('disk gone')\n", "disk gone"),
            ("def broken(:\n", "invalid syntax"),
        ):
            with (
                self.subTest(source=source),
                mock.patch.dict(sys.modules),
                mock.patch.object(
                    sys,
                    "meta_path",
                    [SourceFinder("agent_pm.session.adapters.broken", source), *sys.meta_path],
                ),
            ):
                with self.assertRaises(ConfigError) as caught:
                    registry.adapter("broken")
                self.assertTrue(
                    str(caught.exception).startswith(f"cannot import adapter 'broken': {shown}"),
                    caught.exception,
                )

    def test_refuses_a_missing_class(self):
        self.register(empty=package("empty"))
        self.configure('[adapters.empty]\nimplementation = "adapters.empty:EmptyAdapter"\n')
        self.refused("adapters.empty has no EmptyAdapter", lambda: registry.adapter("empty"))

    def test_refuses_what_is_not_a_concrete_adapter_subclass(self):
        for value in (OddAdapter, odd_adapter, Adapter, HalfAdapter, fake_adapter.FakeHeadless):
            with self.subTest(value=value):
                self.register(odd=package("odd", OddAdapter=value))
                self.configure('[adapters.odd]\nimplementation = "adapters.odd:OddAdapter"\n')
                self.refused(
                    "adapters.odd:OddAdapter must be a concrete Adapter subclass",
                    lambda: registry.adapter("odd"),
                )

    def test_an_unknown_key_is_the_adapters_config_error(self):
        self.register(fake=fake_adapter)
        self.configure(FAKE + 'model = "m"\nscenario = "/s"\nalpha = 1\n')
        self.refused(
            "unknown key 'alpha' in FakeAdapter's config", lambda: registry.adapter("fake")
        )


if __name__ == "__main__":
    unittest.main()
