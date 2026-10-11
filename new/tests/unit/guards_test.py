"""new/tests/guards.py: each guard on the real tree, and on violating samples written to temp trees."""
import importlib
import os
import sys
import tempfile
import unittest

from new.tests import CORE, NEW, SRC, guards

OLD = "/".join(("core", "src"))


class Sample(unittest.TestCase):
    def tree(self, files: dict[str, str]) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel, text in files.items():
            path = os.path.join(tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return tmp.name


class RealTree(unittest.TestCase):
    def test_each_guard_is_clean(self):
        for guard, root in ((guards.layer_imports, CORE), (guards.runtime_names, CORE), (guards.old_core, NEW),
                            (guards.adapters_key, CORE), (guards.syntax_311, NEW), (guards.modes_import_api, CORE),
                            (guards.tmux_calls, CORE), (guards.syntax_39, CORE)):
            with self.subTest(guard.__name__):
                self.assertEqual(guard(root), [])

    def test_src_never_reaches_the_old_core(self):
        old = os.path.realpath(os.path.join(os.path.dirname(NEW), "core", "src"))
        src = os.path.realpath(SRC)
        for top, dirs, files in os.walk(SRC):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(f for f in files if f.endswith(".py")):
                parts = os.path.relpath(os.path.join(top, name), SRC)[:-3].split(os.sep)
                parts = parts[:-1] if parts[-1] == "__init__" else parts
                module = importlib.import_module(".".join(parts))
                self.assertEqual(os.path.commonpath([src, os.path.realpath(module.__file__)]), src)
        for name, module in list(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if file:
                self.assertNotEqual(os.path.commonpath([old, os.path.realpath(file)]), old, name)
        self.assertNotIn(old, {os.path.realpath(p) for p in sys.path if p})


class LayerImports(Sample):
    def test_adapters_importing_driver_names_it(self):
        root = self.tree({"src/adapters/x.py": "import driver\n"})
        self.assertEqual(guards.layer_imports(root), ["src/adapters/x.py: adapters imports driver"])

    def test_driver_modes_outside_driver_names_it(self):
        root = self.tree({"src/delegate/x.py": "from driver.modes import headless_run\n"})
        self.assertEqual(guards.layer_imports(root), ["src/delegate/x.py: delegate imports driver.modes"])

    def test_a_relative_import_is_resolved(self):
        root = self.tree({"src/composer/x.py": "from ..driver import api\n"})
        self.assertEqual(guards.layer_imports(root), ["src/composer/x.py: composer imports driver.api"])

    def test_allowed_imports_are_clean(self):
        root = self.tree({"src/delegate/x.py": "from driver.api import start\nfrom composer import y\n",
                          "src/driver/x.py": "import adapters.base\nfrom .modes import headless_run\n"
                                             "from driver.modes import headless_run\n",
                          "src/x.py": "from driver import api\nimport driver.api\nfrom driver.api import start\n"})
        self.assertEqual(guards.layer_imports(root), [])

    def test_outside_the_layers_reaches_only_driver_api(self):
        root = self.tree({"src/x.py": "import adapters\n", "src/y.py": "from driver import registry\n"})
        self.assertEqual(guards.layer_imports(root), ["src/x.py: outside the layers imports adapters",
                                                      "src/y.py: outside the layers imports driver.registry"])

    def test_nested_and_literal_imports_count(self):
        root = self.tree({"src/adapters/x.py": "def f():\n    import importlib\n"
                                               "    importlib.import_module('driver.registry')\n"
                                               "    __import__('composer')\n"
                                               "    from delegate import y\n"})
        self.assertEqual(guards.layer_imports(root), ["src/adapters/x.py: adapters imports composer",
                                                      "src/adapters/x.py: adapters imports delegate.y",
                                                      "src/adapters/x.py: adapters imports driver.registry"])


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({"src/driver/x.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(guards.runtime_names(root), ["src/driver/x.py:1: runtime name Claude",
                                                      "src/driver/x.py:2: runtime name codex"])

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({"src/adapters/claude/x.py": 'NAME = "claude"\n'})
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_table_header_implementation_line_and_comments_are_exempt(self):
        root = self.tree({"config.toml": '# codex in a comment\n[adapters.codex]\n'
                                         'implementation = "adapters.codex:CodexAdapter"  # codex\n'
                                         'key = "x # codex"\n'})
        self.assertEqual(guards.runtime_names(root), ["config.toml:4: runtime name codex"])

    def test_toml_runtime_line_is_exempt_under_any_table(self):
        root = self.tree({"config.toml": 'runtime = "codex"\n[roles.x]\nruntime = "codex"\n'
                                         '[roles.y]\nruntime = "codex"  # note\n'})
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_name_in_a_key_value_elsewhere(self):
        root = self.tree({"config.toml": '[adapters.codex]\nimplementation = "adapters.codex:CodexAdapter"\n'
                                         '[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\n'
                                         'runtime = "codex" x\n'})
        self.assertEqual(guards.runtime_names(root), [f"config.toml:{n}: runtime name codex" for n in (4, 5, 6, 7)])


class OldCore(Sample):
    def test_imports_and_strings_naming_the_old_core(self):
        root = self.tree({"a.py": "from core.src import drive\n", "b.py": "import core\n",
                          "c.py": f'import sys\nsys.path.insert(0, "/repo/{OLD}")\n',
                          "d.py": f'"""Reads {OLD}/drive.py."""\n'})
        self.assertEqual(guards.old_core(root), ["a.py: imports core.src", "b.py: imports core",
                                                 f"c.py:2: names the old {OLD}", f"d.py:1: names the old {OLD}"])

    def test_the_new_core_path_is_clean(self):
        root = self.tree({"a.py": f'P = "/repo/new/{OLD}"\nimport new.core\n'})
        self.assertEqual(guards.old_core(root), [])


class AdaptersKey(Sample):
    def test_the_literal_key_only_inside_driver(self):
        sample = 'cfg = {}\nvalue = cfg["adapters"]\n'
        root = self.tree({"src/composer/x.py": sample, "src/driver/x.py": sample})
        self.assertEqual(guards.adapters_key(root), ['src/composer/x.py:2: literal key "adapters"'])


class ModesImportApi(Sample):
    def test_a_mode_importing_driver_api_any_way_is_a_violation(self):
        root = self.tree({"src/driver/modes/a.py": "from driver import api\n",
                          "src/driver/modes/b.py": "import driver.api\n",
                          "src/driver/modes/c.py": "from ..api import DriverError\n",
                          "src/driver/modes/d.py": "def f():\n    from .. import api\n",
                          "src/driver/modes/__init__.py": "__import__('driver.api')\n"})
        self.assertEqual(guards.modes_import_api(root), ["src/driver/modes/__init__.py: imports driver.api",
                                                         "src/driver/modes/a.py: imports driver.api",
                                                         "src/driver/modes/b.py: imports driver.api",
                                                         "src/driver/modes/c.py: imports driver.api",
                                                         "src/driver/modes/d.py: imports driver.api"])

    def test_driver_errors_in_a_mode_and_driver_api_outside_modes_are_clean(self):
        root = self.tree({"src/driver/modes/a.py": "from driver.errors import DriverError\nfrom driver import records\n"
                                                   "from ..errors import DriverError\n",
                          "src/driver/x.py": "from driver import api\n", "src/y.py": "import driver.api\n"})
        self.assertEqual(guards.modes_import_api(root), [])


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree({"src/driver/tmux_host.py": sample, "src/driver/modes/x.py": sample,
                          "src/composer/y.py": 'TOOL = "tmux"\n', "tests/z.py": sample})
        self.assertEqual(guards.tmux_calls(root), ['src/composer/y.py:1: str constant "tmux"',
                                                   'src/driver/modes/x.py:2: str constant "tmux"',
                                                   'tests/z.py:2: str constant "tmux"'])

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree({"src/driver/x.py": 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'})
        self.assertEqual(guards.tmux_calls(root), [])


class Syntax39(Sample):
    def test_newer_syntax_in_a_standalone_file_is_a_violation(self):
        root = self.tree({"src/driver/tmux_host.py": "def f(x: int | None): pass\nmatch x:\n    case _: pass\n",
                          "src/driver/api.py": "match x:\n    case _: pass\n"})
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("src/driver/tmux_host.py: "), found)

    def test_the_tui_hook_is_standalone_too(self):
        root = self.tree({"src/driver/modes/tui_hook.py": "match x:\n    case _: pass\n"})
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("src/driver/modes/tui_hook.py: "), found)

    def test_3_9_syntax_is_clean(self):
        root = self.tree({"src/driver/tmux_host.py": "from __future__ import annotations\n"
                                                     "def f(x: int | None) -> list[str]: return []\n"})
        self.assertEqual(guards.syntax_39(root), [])


class Syntax311(Sample):
    def test_newer_syntax_is_a_violation(self):
        root = self.tree({"x.py": "type X = int\n", "y.py": "X = int\n"})
        found = guards.syntax_311(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("x.py: "), found)


if __name__ == "__main__":
    unittest.main()
