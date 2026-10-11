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
                            (guards.adapters_key, CORE), (guards.syntax_311, NEW), (guards.utils_imports, CORE),
                            (guards.repo_library, CORE), (guards.modes_import_api, CORE)):
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


class UtilsImports(Sample):
    def test_a_layer_another_src_module_or_a_third_party_module_is_a_violation(self):
        root = self.tree({"src/utils/x.py": "import driver\nfrom adapters.base import ConfigError\nimport repo\n"
                                            "def f():\n    import yaml\n",
                          "src/utils/y.py": "from .. import composer\n"})
        self.assertEqual(guards.utils_imports(root), ["src/utils/x.py: imports adapters.base",
                                                      "src/utils/x.py: imports driver",
                                                      "src/utils/x.py: imports repo",
                                                      "src/utils/x.py: imports yaml",
                                                      "src/utils/y.py: imports composer"])

    def test_the_stdlib_and_utils_are_clean_and_only_utils_is_checked(self):
        root = self.tree({"src/utils/x.py": "from __future__ import annotations\nimport os, tomllib\n"
                                            "from utils import paths\nfrom utils.files import read_regular\n"
                                            "from . import files\n",
                          "src/driver/x.py": "import yaml\n"})
        self.assertEqual(guards.utils_imports(root), [])


class RepoLibrary(Sample):
    def test_a_src_module_importing_repo_is_a_violation(self):
        root = self.tree({"src/composer/x.py": "import repo\n", "src/driver/y.py": "from repo import merge\n",
                          "src/utils/z.py": "def f():\n    __import__('repo')\n",
                          "src/composer/w.py": "from .. import repo\n"})
        self.assertEqual(guards.repo_library(root), ["src/composer/w.py: imports repo",
                                                     "src/composer/x.py: imports repo",
                                                     "src/driver/y.py: imports repo.merge",
                                                     "src/utils/z.py: imports repo"])

    def test_repo_py_itself_and_modules_outside_src_are_clean(self):
        root = self.tree({"src/repo.py": "import os\n", "tests/repo_test.py": "import repo\n",
                          "src/composer/x.py": "import repository\nfrom utils import config_file\n"})
        self.assertEqual(guards.repo_library(root), [])


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


class Syntax311(Sample):
    def test_newer_syntax_is_a_violation(self):
        root = self.tree({"x.py": "type X = int\n", "y.py": "X = int\n"})
        found = guards.syntax_311(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("x.py: "), found)


if __name__ == "__main__":
    unittest.main()
