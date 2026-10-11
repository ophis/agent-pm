"""new/core/src/driver/registry.py: config merge, the [adapters.<name>] tables and Adapter loading. Adapter packages
are registered in sys.modules or the import system only for the test's span; the registry itself is never patched."""
import importlib.abc
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from new.tests import CORE, SRC, fake_adapter, hermetic
from adapters.base import Adapter, ConfigError
from driver import registry

FAKE = '[adapters.fake]\nimplementation = "adapters.fake:FakeAdapter"\n'
LOCAL_SHOWN = "~/.agent-pm/core-new.local.toml"


class OddAdapter:
    pass


class HalfAdapter(Adapter):
    def headless_run(self):
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

    def write(self, name, content) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode())
        return path

    def local(self, text):
        """`text` as this test's ~/.agent-pm/core-new.local.toml."""
        with open(os.path.join(self.home, "core-new.local.toml"), "w", encoding="utf-8") as f:
            f.write(text)

    def register(self, **modules):
        """Each name's module as sys.modules["adapters.<name>"] for the rest of the test."""
        patch = mock.patch.dict(sys.modules, {f"adapters.{name}": m for name, m in modules.items()})
        patch.start()
        self.addCleanup(patch.stop)

    def refused(self, message, call):
        with self.assertRaises(ConfigError) as caught:
            call()
        self.assertEqual(str(caught.exception), message)


class ConstantsTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(registry.ROOT, CORE)
        self.assertEqual(registry.CONFIG, os.path.join(CORE, "config.toml"))
        self.assertEqual(registry.LOCAL, LOCAL_SHOWN)


class MergeTest(unittest.TestCase):
    def test_tables_merge_key_by_key_and_the_other_value_wins(self):
        base = {"a": {"x": 1, "y": [1, 2], "t": {"p": 1, "q": 2}}, "b": 1, "c": {"k": 1}}
        over = {"a": {"y": [3], "z": 2, "t": {"q": 3}}, "b": {"n": 1}, "c": 5, "d": "new"}
        self.assertEqual(registry.merge(base, over),
                         {"a": {"x": 1, "y": [3], "z": 2, "t": {"p": 1, "q": 3}}, "b": {"n": 1}, "c": 5, "d": "new"})

    def test_leaves_its_arguments_unchanged(self):
        base, over = {"a": {"x": 1}}, {"a": {"x": 2}}
        registry.merge(base, over)
        self.assertEqual((base, over), ({"a": {"x": 1}}, {"a": {"x": 2}}))


class ReadConfigTest(Base):
    def test_merges_the_local_file_over_the_base_file(self):
        path = self.write("config.toml", 'top = 1\nlist = [1, 2]\n[t]\na = "base"\nb = "base"\n')
        local = self.write("local.toml", 'list = [3]\n[t]\nb = "local"\nc = "local"\n')
        self.assertEqual(registry.read_config(path, local),
                         {"top": 1, "list": [3], "t": {"a": "base", "b": "local", "c": "local"}})

    def test_expands_tilde_in_the_local_path(self):
        path = self.write("config.toml", "a = 1\n")
        self.local("a = 2\n")
        self.assertEqual(registry.read_config(path, LOCAL_SHOWN), {"a": 2})

    def test_reads_the_local_file_only_when_it_is_a_file(self):
        path = self.write("config.toml", "a = 1\n")
        self.assertEqual(registry.read_config(path, os.path.join(self.dir, "absent.toml")), {"a": 1})
        self.assertEqual(registry.read_config(path, self.dir), {"a": 1})

    def test_defaults_to_the_committed_config_and_the_home_local_file(self):
        self.local('extra = "x"\n')
        self.assertEqual(registry.read_config()["extra"], "x")


class TablesTest(Base):
    def test_committed_config_loads_the_available_adapter(self):
        self.assertEqual(registry.configured(), ["codex"])
        adapter = registry.adapter("codex")
        self.assertIsNotNone(adapter.headless_run())
        self.assertIsNone(adapter.tui_run())

    def test_the_merged_tables_and_their_sorted_names(self):
        path = self.write("config.toml", '[adapters.b]\nk = 1\n[adapters.a]\nk = 1\n')
        self.local('[adapters.b]\nk = 2\n[adapters.c]\nk = 3\n')
        self.assertEqual(registry.tables(path), {"a": {"k": 1}, "b": {"k": 2}, "c": {"k": 3}})
        self.assertEqual(registry.configured(path), ["a", "b", "c"])

    def test_no_tables_when_absent_or_not_a_table(self):
        for text in ("x = 1\n", "adapters = 1\n", 'adapters = ["fake"]\n'):
            with self.subTest(text=text):
                path = self.write("config.toml", text)
                self.assertEqual(registry.tables(path), {})
                self.assertEqual(registry.configured(path), [])

    def config_error(self, path, local):
        for call in (registry.tables, registry.configured, lambda p, loc: registry.adapter("fake", path=p, local=loc)):
            with self.assertRaises(ConfigError) as caught:
                call(path, local)
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


class AdapterTest(Base):
    def test_builds_the_configured_class_from_its_table_minus_implementation(self):
        self.register(fake=fake_adapter)
        self.local(FAKE + 'scenario = "/s.json"\nlog = "/log"\n')
        built = registry.adapter("fake")
        self.assertIs(type(built), fake_adapter.FakeAdapter)
        self.assertEqual(built.config, {"scenario": "/s.json", "log": "/log"})

    def test_reads_the_given_config_and_local_file(self):
        self.register(fake=fake_adapter)
        path = self.write("config.toml", FAKE + 'scenario = "/a"\nlog = "/log"\n')
        local = self.write("local.toml", '[adapters.fake]\nscenario = "/b"\ntui = true\n')
        built = registry.adapter("fake", path=path, local=local)
        self.assertIsInstance(built, fake_adapter.FakeAdapter)
        self.assertEqual(built.config, {"scenario": "/b", "log": "/log", "tui": True})

    def test_refuses_an_unsafe_name(self):
        for bad in ("Fake", "", "../fake", "a-b", "1a", "fake\n", "adapters.fake", 1, None):
            with self.subTest(bad=bad):
                self.refused(f"unsafe runtime name {bad!r}: want a lowercase identifier",
                             lambda: registry.adapter(bad))

    def test_refuses_a_missing_table(self):
        message = f"no [adapters.fake] table in {registry.CONFIG} or {LOCAL_SHOWN}"
        self.refused(message, lambda: registry.adapter("fake"))
        self.local("[adapters]\nfake = 1\n")
        self.refused(message, lambda: registry.adapter("fake"))

    def test_names_the_given_files_when_the_table_is_missing(self):
        path = self.write("config.toml", "")
        local = os.path.join(self.dir, "absent.toml")
        self.refused(f"no [adapters.fake] table in {path} or {local}",
                     lambda: registry.adapter("fake", path=path, local=local))

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
                self.local("[adapters.fake]\n" + line)
                self.refused(message, lambda: registry.adapter("fake"))

    def test_refuses_a_missing_package(self):
        self.local('[adapters.nope]\nimplementation = "adapters.nope:NopeAdapter"\n')
        self.refused("adapter 'nope' has no package adapters.nope", lambda: registry.adapter("nope"))

    def test_refuses_a_missing_parent_package(self):
        self.local(FAKE)
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", [p for p in sys.path if p != SRC]):
            del sys.modules[Adapter.__module__.partition(".")[0]]
            self.refused("adapter 'fake' has no package adapters.fake", lambda: registry.adapter("fake"))

    def test_refuses_a_package_that_fails_to_import(self):
        self.local('[adapters.broken]\nimplementation = "adapters.broken:BrokenAdapter"\n')
        for source, shown in (("import agent_pm_absent_dependency\n", "No module named 'agent_pm_absent_dependency'"),
                              ("from adapters.base import Absent\n", "cannot import name 'Absent'"),
                              ("raise OSError('disk gone')\n", "disk gone")):
            with self.subTest(source=source), mock.patch.dict(sys.modules), \
                    mock.patch.object(sys, "meta_path", [SourceFinder("adapters.broken", source), *sys.meta_path]):
                with self.assertRaises(ConfigError) as caught:
                    registry.adapter("broken")
                self.assertTrue(str(caught.exception).startswith(f"cannot import adapter 'broken': {shown}"),
                                caught.exception)

    def test_refuses_a_missing_class(self):
        self.register(empty=package("empty"))
        self.local('[adapters.empty]\nimplementation = "adapters.empty:EmptyAdapter"\n')
        self.refused("adapters.empty has no EmptyAdapter", lambda: registry.adapter("empty"))

    def test_refuses_what_is_not_a_concrete_adapter_subclass(self):
        for value in (OddAdapter, odd_adapter, Adapter, HalfAdapter, fake_adapter.FakeHeadlessRun):
            with self.subTest(value=value):
                self.register(odd=package("odd", OddAdapter=value))
                self.local('[adapters.odd]\nimplementation = "adapters.odd:OddAdapter"\n')
                self.refused("adapters.odd:OddAdapter must be a concrete Adapter subclass",
                             lambda: registry.adapter("odd"))

    def test_an_unknown_key_is_the_adapters_config_error(self):
        self.register(fake=fake_adapter)
        self.local(FAKE + 'model = "m"\nscenario = "/s"\nalpha = 1\n')
        self.refused("unknown key 'alpha' in FakeAdapter's config", lambda: registry.adapter("fake"))


if __name__ == "__main__":
    unittest.main()
