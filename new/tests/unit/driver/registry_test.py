"""new/core/src/driver/registry.py: the [adapters.<name>] tables, their per-process cache and Adapter loading (the
config pair, merge and read: unit/utils/config_file_test.py). Adapter packages are registered in sys.modules or the
import system only for the test's span; the registry itself is never patched, except a wrap counting its file reads.
Each test drops the cache at its end."""
import importlib.abc
import importlib.util
import os
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock

from new.tests import SRC, fake_adapter, hermetic
from adapters.base import Adapter, ConfigError
from driver import registry

FAKE = '[adapters.fake]\nimplementation = "adapters.fake:FakeAdapter"\n'
LOCAL_SHOWN = "~/.agent-pm/core-new.local.toml"


class OddAdapter:
    pass


class HalfAdapter(Adapter):
    def headless(self):
        return None


def odd_adapter():
    return None


def package(name, **names) -> types.ModuleType:
    """An in-memory module adapters.<name> holding `names`."""
    module = types.ModuleType(f"adapters.{name}")
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


class Base(unittest.TestCase):
    def setUp(self):
        self.home = hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.addCleanup(registry.reset)

    def write(self, name, content) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode())
        return path

    def local(self, text):
        """`text` as this test's ~/.agent-pm/core-new.local.toml."""
        with open(os.path.join(self.home, "core-new.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def configure(self, text):
        """TOML `text` as the registry's config, no file read."""
        registry.configure(tomllib.loads(text))

    def register(self, **modules):
        """Each name's module as sys.modules["adapters.<name>"] for the rest of the test."""
        patch = mock.patch.dict(sys.modules, {f"adapters.{name}": m for name, m in modules.items()})
        patch.start()
        self.addCleanup(patch.stop)

    def refused(self, message, call):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)


class TablesTest(Base):
    def test_committed_config_has_the_claude_table(self):
        self.assertEqual(list(registry.tables()), ["claude", "codex"])
        self.assertEqual(registry.configured(), ["claude", "codex"])

    def test_the_merged_tables_and_their_sorted_names(self):
        path = self.write("config.toml", '[adapters.b]\nk = 1\n[adapters.a]\nk = 1\n')
        self.local('[adapters.b]\nk = 2\n[adapters.c]\nk = 3\n')
        self.assertEqual(registry.tables(path), {"a": {"k": 1}, "b": {"k": 2}, "c": {"k": 3}})

    def test_no_tables_when_absent_or_not_a_table(self):
        for text in ("x = 1\n", "adapters = 1\n", 'adapters = ["fake"]\n'):
            with self.subTest(text=text):
                path = self.write("config.toml", text)
                self.assertEqual(registry.tables(path), {})

    def config_error(self, path, local):
        with self.assertRaises(ConfigError) as caught:
            registry.tables(path, local)
        self.assertTrue(str(caught.exception).startswith(f"core config {path}: "), caught.exception)

    def test_an_unreadable_or_invalid_config_is_a_config_error(self):
        absent = os.path.join(self.dir, "absent.toml")
        self.config_error(absent, absent)
        self.config_error(self.dir, absent)
        for content in (b"[adapters\n", b'a = "\xff"\n'):
            with self.subTest(content=content):
                self.config_error(self.write("config.toml", content), absent)

    def test_an_invalid_local_file_is_a_config_error(self):
        path = self.write("config.toml", "a = 1\n")
        for content in (b"x = \n", b"\xfe = 1\n"):
            with self.subTest(content=content):
                self.config_error(path, self.write("local.toml", content))


class CacheTest(Base):
    def test_the_first_use_reads_the_files_once(self):
        self.register(fake=fake_adapter)
        self.local(FAKE + 'scenario = "/s"\n[adapters.a]\nk = 1\n')
        with mock.patch.object(registry, "tables", wraps=registry.tables) as read:
            first = registry.adapter("fake")
            second = registry.adapter("fake")
            self.assertEqual(registry.configured(), ["a", "claude", "codex", "fake"])
        read.assert_called_once_with(registry.CONFIG, registry.LOCAL)
        self.assertIsNot(first, second)
        self.assertEqual((first.config, second.config), ({"scenario": "/s"}, {"scenario": "/s"}))

    def test_reset_makes_the_next_use_read_the_files_again(self):
        self.register(fake=fake_adapter)
        self.local(FAKE + 'scenario = "/a"\n')
        self.assertEqual(registry.adapter("fake").config, {"scenario": "/a"})
        self.local(FAKE + 'scenario = "/b"\n')
        self.assertEqual(registry.adapter("fake").config, {"scenario": "/a"})
        registry.reset()
        self.assertEqual(registry.adapter("fake").config, {"scenario": "/b"})

    def test_a_failed_read_is_not_cached(self):
        self.register(fake=fake_adapter)
        self.local("x = \n")
        for call in (registry.configured, lambda: registry.adapter("fake")):
            with self.assertRaises(ConfigError) as caught:
                call()
            self.assertTrue(str(caught.exception).startswith(f"core config {registry.CONFIG}: "), caught.exception)
        self.local(FAKE + 'scenario = "/s"\n')
        self.assertEqual(registry.adapter("fake").config, {"scenario": "/s"})

    def test_configure_sets_the_cache_without_reading_a_file(self):
        self.register(fake=fake_adapter)
        self.local(FAKE + 'scenario = "/file"\n')
        self.assertEqual(registry.adapter("fake").config, {"scenario": "/file"})
        with mock.patch.object(registry, "tables", wraps=registry.tables) as read:
            self.configure(FAKE + 'scenario = "/given"\n[adapters.a]\nk = 1\n')
            self.assertEqual(registry.adapter("fake").config, {"scenario": "/given"})
            self.assertEqual(registry.configured(), ["a", "fake"])
            for config in ({}, {"adapters": 1}, {"adapters": ["fake"]}, {"other": {"fake": {}}}):
                with self.subTest(config=config):
                    registry.configure(config)
                    self.assertEqual(registry.configured(), [])
        read.assert_not_called()


class AdapterTest(Base):
    def test_builds_the_configured_class_from_its_table_minus_implementation(self):
        self.register(fake=fake_adapter)
        self.configure(FAKE + 'scenario = "/s.json"\nlog = "/log"\n')
        built = registry.adapter("fake")
        self.assertIs(type(built), fake_adapter.FakeAdapter)
        self.assertEqual(built.config, {"scenario": "/s.json", "log": "/log"})

    def test_refuses_an_unsafe_name(self):
        for bad in ("Fake", "", "../fake", "a-b", "1a", "fake\n", "adapters.fake", 1, None):
            with self.subTest(bad=bad):
                self.refused(f"unsafe runtime name {bad!r}: want a lowercase identifier",
                             lambda: registry.adapter(bad))

    def test_refuses_a_missing_table(self):
        message = f"no [adapters.fake] table in {registry.CONFIG} or {LOCAL_SHOWN}"
        for text in ("", "[adapters]\nfake = 1\n"):
            with self.subTest(text=text):
                self.configure(text)
                self.refused(message, lambda: registry.adapter("fake"))

    def test_refuses_an_implementation_of_another_shape_or_package(self):
        self.register(fake=fake_adapter)
        message = '[adapters.fake].implementation must be "adapters.fake:<Class>Adapter"'
        for line in ("", "implementation = 1\n", 'implementation = "adapters.fake:Fake"\n',
                     'implementation = "adapters.fake:Adapter"\n', 'implementation = "adapters.fake.FakeAdapter"\n',
                     'implementation = "fake:FakeAdapter"\n', 'implementation = "adapters.fake:FakeAdapter\\n"\n',
                     'implementation = "adapters.fake:fake.FakeAdapter"\n',
                     'implementation = "adapters.other:OtherAdapter"\n',
                     'implementation = "adapters.Fake:FakeAdapter"\n'):
            with self.subTest(line=line):
                self.configure("[adapters.fake]\n" + line)
                self.refused(message, lambda: registry.adapter("fake"))

    def test_refuses_a_missing_package(self):
        self.configure('[adapters.nope]\nimplementation = "adapters.nope:NopeAdapter"\n')
        self.refused("adapter 'nope' has no package adapters.nope", lambda: registry.adapter("nope"))

    def test_refuses_a_missing_parent_package(self):
        self.configure(FAKE)
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", [p for p in sys.path if p != SRC]):
            del sys.modules[Adapter.__module__.partition(".")[0]]
            self.refused("adapter 'fake' has no package adapters.fake", lambda: registry.adapter("fake"))

    def test_refuses_a_package_that_fails_to_import(self):
        self.configure('[adapters.broken]\nimplementation = "adapters.broken:BrokenAdapter"\n')
        for source, shown in (("import agent_pm_absent_dependency\n", "No module named 'agent_pm_absent_dependency'"),
                              ("from adapters.base import Absent\n", "cannot import name 'Absent'"),
                              ("raise OSError('disk gone')\n", "disk gone"),
                              ("def broken(:\n", "invalid syntax")):
            with self.subTest(source=source), mock.patch.dict(sys.modules), \
                    mock.patch.object(sys, "meta_path", [SourceFinder("adapters.broken", source), *sys.meta_path]):
                with self.assertRaises(ConfigError) as caught:
                    registry.adapter("broken")
                self.assertTrue(str(caught.exception).startswith(f"cannot import adapter 'broken': {shown}"),
                                caught.exception)

    def test_refuses_a_missing_class(self):
        self.register(empty=package("empty"))
        self.configure('[adapters.empty]\nimplementation = "adapters.empty:EmptyAdapter"\n')
        self.refused("adapters.empty has no EmptyAdapter", lambda: registry.adapter("empty"))

    def test_refuses_what_is_not_a_concrete_adapter_subclass(self):
        for value in (OddAdapter, odd_adapter, Adapter, HalfAdapter, fake_adapter.FakeHeadless):
            with self.subTest(value=value):
                self.register(odd=package("odd", OddAdapter=value))
                self.configure('[adapters.odd]\nimplementation = "adapters.odd:OddAdapter"\n')
                self.refused("adapters.odd:OddAdapter must be a concrete Adapter subclass",
                             lambda: registry.adapter("odd"))

    def test_an_unknown_key_is_the_adapters_config_error(self):
        self.register(fake=fake_adapter)
        self.configure(FAKE + 'model = "m"\nscenario = "/s"\nalpha = 1\n')
        self.refused("unknown key 'alpha' in FakeAdapter's config", lambda: registry.adapter("fake"))


if __name__ == "__main__":
    unittest.main()
