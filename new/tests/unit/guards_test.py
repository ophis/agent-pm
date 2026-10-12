"""new/tests/guards.py: each guard on the real tree, and on violating samples written to temp trees; every test
is a Case, with no class or module fixture."""

from new.tests import guards
from new.tests.case import CORE, NEW, ROOT, Case

import ast
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

OLD = "/".join(("core", "src"))
PKG = "src/agent_pm"
FIXTURES = ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule")


def fixtures(root: str) -> list[str]:
    return sorted(
        f"{path.relative_to(root)}:{node.lineno}: {node.name}"
        for path in Path(root).rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in FIXTURES
    )


def flat(suite):
    for test in suite:
        yield from flat(test) if isinstance(test, unittest.TestSuite) else (test,)


class Sample(Case):
    def tree(self, files: dict[str, str]) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for rel, text in files.items():
            path = os.path.join(tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return tmp.name


class RealTree(Case):
    def test_each_guard_is_clean(self):
        for guard, root in (
            (guards.layer_imports, CORE),
            (guards.runtime_names, CORE),
            (guards.old_core, NEW),
            (guards.adapters_key, CORE),
            (guards.syntax_311, NEW),
            (guards.private_imports, CORE),
            (guards.modes_import_api, CORE),
            (guards.tmux_calls, CORE),
            (guards.syntax_39, CORE),
            (guards.import_hygiene, NEW),
        ):
            with self.subTest(guard.__name__):
                self.assertEqual(guard(root), [])

    def test_src_never_reaches_the_old_core(self):
        old = os.path.realpath(os.path.join(os.path.dirname(NEW), "core", "src"))
        src = os.path.realpath(ROOT)
        for top, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(f for f in files if f.endswith(".py")):
                parts = os.path.relpath(os.path.join(top, name), ROOT)[:-3].split(os.sep)
                parts = parts[:-1] if parts[-1] == "__init__" else parts
                module = importlib.import_module(".".join(parts))
                self.assertEqual(os.path.commonpath([src, os.path.realpath(module.__file__)]), src)
        for name, module in list(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if file:
                self.assertNotEqual(os.path.commonpath([old, os.path.realpath(file)]), old, name)
        self.assertNotIn(old, {os.path.realpath(p) for p in sys.path if p})


class EveryTest(Sample):
    def test_is_a_case(self):
        loader, found = unittest.TestLoader(), []
        for suite in ("unit", "integration"):
            tests = loader.discover(
                os.path.join(NEW, "tests", suite), "*_test.py", os.path.dirname(NEW)
            )
            found += [test.id() for test in flat(tests) if not isinstance(test, Case)]
        self.assertEqual((loader.errors, found), ([], []))

    def test_has_no_class_or_module_fixture(self):
        self.assertEqual(fixtures(os.path.join(NEW, "tests")), [])
        root = self.tree(
            {
                "a.py": "class A:\n    @classmethod\n    def setUpClass(cls): pass\n"
                "    @classmethod\n    async def tearDownClass(cls): pass\n",
                "b/c.py": "def setUpModule(): pass\ndef tearDownModule(): pass\ndef setUp(): pass\n",
            }
        )
        self.assertEqual(
            fixtures(root),
            [
                "a.py:3: setUpClass",
                "a.py:5: tearDownClass",
                "b/c.py:1: setUpModule",
                "b/c.py:2: tearDownModule",
            ],
        )


class LayerImports(Sample):
    def test_adapters_importing_driver_names_it(self):
        root = self.tree({f"{PKG}/adapters/x.py": "import agent_pm.driver\n"})
        self.assertEqual(
            guards.layer_imports(root), [f"{PKG}/adapters/x.py: adapters imports agent_pm.driver"]
        )

    def test_driver_modes_outside_driver_names_it(self):
        root = self.tree({f"{PKG}/delegate/x.py": "from agent_pm.driver.modes import headless\n"})
        self.assertEqual(
            guards.layer_imports(root),
            [f"{PKG}/delegate/x.py: delegate imports agent_pm.driver.modes"],
        )

    def test_a_relative_import_is_resolved(self):
        root = self.tree({f"{PKG}/composer/x.py": "from ..driver import api\n"})
        self.assertEqual(
            guards.layer_imports(root),
            [f"{PKG}/composer/x.py: composer imports agent_pm.driver.api"],
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                f"{PKG}/delegate/x.py": "from agent_pm.driver.api import start\n"
                "from agent_pm.composer import y\nfrom agent_pm.driver import api\n",
                f"{PKG}/driver/x.py": "import agent_pm.adapters.base\nfrom .modes import headless\n"
                "from agent_pm.driver.modes import headless\n",
                f"{PKG}/x.py": "from agent_pm.driver import api\nimport agent_pm.driver.api\n"
                "from agent_pm.driver.api import start\nfrom .driver import api\nimport agent_pm\n",
            }
        )
        self.assertEqual(guards.layer_imports(root), [])

    def test_outside_the_layers_reaches_only_driver_api(self):
        root = self.tree(
            {
                f"{PKG}/x.py": "import agent_pm.adapters\n",
                f"{PKG}/y.py": "from agent_pm.driver import registry\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/x.py: outside the layers imports agent_pm.adapters",
                f"{PKG}/y.py: outside the layers imports agent_pm.driver.registry",
            ],
        )

    def test_nested_and_literal_imports_count(self):
        root = self.tree(
            {
                f"{PKG}/adapters/x.py": "def f():\n    import importlib\n"
                "    importlib.import_module('agent_pm.driver.registry')\n"
                "    __import__('agent_pm.composer')\n"
                "    from agent_pm.delegate import y\n"
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/adapters/x.py: adapters imports agent_pm.composer",
                f"{PKG}/adapters/x.py: adapters imports agent_pm.delegate.y",
                f"{PKG}/adapters/x.py: adapters imports agent_pm.driver.registry",
            ],
        )

    def test_a_bare_layer_utils_or_repo_name_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/delegate/x.py": "from driver import api\nimport composer\n"
                "from utils.paths import under\n",
                f"{PKG}/x.py": "import repo\ndef f():\n    __import__('adapters.base')\n",
                f"{PKG}/driver/x.py": "import repository\nimport drivers\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/delegate/x.py: bare import composer",
                f"{PKG}/delegate/x.py: bare import driver.api",
                f"{PKG}/delegate/x.py: bare import utils.paths",
                f"{PKG}/x.py: bare import adapters.base",
                f"{PKG}/x.py: bare import repo",
            ],
        )

    def test_the_package_is_the_one_dir_under_src(self):
        root = self.tree(
            {
                "src/pkg/adapters/x.py": "import pkg.driver\n",
                "src/pkg/delegate/x.py": "from pkg.driver import api\nimport agent_pm.adapters\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root), ["src/pkg/adapters/x.py: adapters imports pkg.driver"]
        )


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({f"{PKG}/driver/x.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(
            guards.runtime_names(root),
            [
                f"{PKG}/driver/x.py:1: runtime name Claude",
                f"{PKG}/driver/x.py:2: runtime name codex",
            ],
        )

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({f"{PKG}/adapters/claude/x.py": 'NAME = "claude"\n'})
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_table_header_implementation_line_and_comments_are_exempt(self):
        root = self.tree(
            {
                "config.toml": "# codex in a comment\n[adapters.codex]\n"
                'implementation = "adapters.codex:CodexAdapter"  # codex\n'
                'key = "x # codex"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), ["config.toml:4: runtime name codex"])

    def test_toml_runtime_line_is_exempt_under_any_table(self):
        root = self.tree(
            {
                "config.toml": 'runtime = "codex"\n[roles.x]\nruntime = "codex"\n'
                '[roles.y]\nruntime = "codex"  # note\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_name_in_a_key_value_elsewhere(self):
        root = self.tree(
            {
                "config.toml": '[adapters.codex]\nimplementation = "adapters.codex:CodexAdapter"\n'
                '[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\n'
                'runtime = "codex" x\n'
            }
        )
        self.assertEqual(
            guards.runtime_names(root),
            [f"config.toml:{n}: runtime name codex" for n in (4, 5, 6, 7)],
        )


class OldCore(Sample):
    def test_imports_and_strings_naming_the_old_core(self):
        root = self.tree(
            {
                "a.py": "from core.src import drive\n",
                "b.py": "import core\n",
                "c.py": f'import sys\nsys.path.insert(0, "/repo/{OLD}")\n',
                "d.py": f'"""Reads {OLD}/drive.py."""\n',
            }
        )
        self.assertEqual(
            guards.old_core(root),
            [
                "a.py: imports core.src",
                "b.py: imports core",
                f"c.py:2: names the old {OLD}",
                f"d.py:1: names the old {OLD}",
            ],
        )

    def test_the_new_core_path_is_clean(self):
        root = self.tree({"a.py": f'P = "/repo/new/{OLD}"\nimport new.core\n'})
        self.assertEqual(guards.old_core(root), [])


class AdaptersKey(Sample):
    def test_the_literal_key_only_inside_driver(self):
        sample = 'cfg = {}\nvalue = cfg["adapters"]\n'
        root = self.tree({f"{PKG}/composer/x.py": sample, f"{PKG}/driver/x.py": sample})
        self.assertEqual(
            guards.adapters_key(root), [f'{PKG}/composer/x.py:2: literal key "adapters"']
        )


class ModesImportApi(Sample):
    def test_a_mode_importing_driver_api_any_way_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/driver/modes/a.py": "from agent_pm.driver import api\n",
                f"{PKG}/driver/modes/b.py": "import agent_pm.driver.api\n",
                f"{PKG}/driver/modes/c.py": "from ..api import DriverError\n",
                f"{PKG}/driver/modes/d.py": "def f():\n    from .. import api\n",
                f"{PKG}/driver/modes/__init__.py": "__import__('agent_pm.driver.api')\n",
            }
        )
        self.assertEqual(
            guards.modes_import_api(root),
            [
                f"{PKG}/driver/modes/__init__.py: imports agent_pm.driver.api",
                f"{PKG}/driver/modes/a.py: imports agent_pm.driver.api",
                f"{PKG}/driver/modes/b.py: imports agent_pm.driver.api",
                f"{PKG}/driver/modes/c.py: imports agent_pm.driver.api",
                f"{PKG}/driver/modes/d.py: imports agent_pm.driver.api",
            ],
        )

    def test_driver_errors_in_a_mode_and_driver_api_outside_modes_are_clean(self):
        root = self.tree(
            {
                f"{PKG}/driver/modes/a.py": "from agent_pm.driver.errors import DriverError\n"
                "from agent_pm.driver import records\nfrom ..errors import DriverError\n",
                f"{PKG}/driver/x.py": "from agent_pm.driver import api\n",
                f"{PKG}/y.py": "import agent_pm.driver.api\n",
            }
        )
        self.assertEqual(guards.modes_import_api(root), [])


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                f"{PKG}/driver/tmux_host.py": sample,
                f"{PKG}/driver/modes/x.py": sample,
                f"{PKG}/composer/y.py": 'TOOL = "tmux"\n',
                "src/driver/tmux_host.py": sample,
                "tests/z.py": sample,
            }
        )
        self.assertEqual(
            guards.tmux_calls(root),
            [
                f'{PKG}/composer/y.py:1: str constant "tmux"',
                f'{PKG}/driver/modes/x.py:2: str constant "tmux"',
                'src/driver/tmux_host.py:2: str constant "tmux"',
                'tests/z.py:2: str constant "tmux"',
            ],
        )

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree(
            {f"{PKG}/driver/x.py": 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'}
        )
        self.assertEqual(guards.tmux_calls(root), [])


class Syntax39(Sample):
    def test_newer_syntax_in_a_standalone_file_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/driver/tmux_host.py": "def f(x: int | None): pass\nmatch x:\n    case _: pass\n",
                f"{PKG}/driver/api.py": "match x:\n    case _: pass\n",
            }
        )
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"{PKG}/driver/tmux_host.py: "), found)

    def test_the_tui_hook_is_standalone_too(self):
        root = self.tree({f"{PKG}/driver/modes/tui_hook.py": "match x:\n    case _: pass\n"})
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"{PKG}/driver/modes/tui_hook.py: "), found)

    def test_3_9_syntax_is_clean(self):
        root = self.tree(
            {
                f"{PKG}/driver/tmux_host.py": "from __future__ import annotations\n"
                "def f(x: int | None) -> list[str]: return []\n"
            }
        )
        self.assertEqual(guards.syntax_39(root), [])


class PrivateImports(Sample):
    def test_an_underscore_name_from_another_module(self):
        root = self.tree(
            {
                f"{PKG}/a.py": "from b import _x\nfrom . import _y\n",
                f"{PKG}/c.py": "import b.d\nfrom e import f as g\n\ndef h():\n    return b.d._z, g._w\n",
            }
        )
        self.assertEqual(
            guards.private_imports(root),
            [
                f"{PKG}/a.py:1: private b._x",
                f"{PKG}/a.py:2: private ._y",
                f"{PKG}/c.py:5: private b.d._z",
                f"{PKG}/c.py:5: private g._w",
            ],
        )

    def test_an_alias_dunders_and_other_objects_are_clean(self):
        root = self.tree(
            {
                f"{PKG}/a.py": "import b as _b\nfrom c import d as _d\n_b.e, _d.f, _b.__file__\n\n"
                "class G:\n    def h(self, i):\n        return self._j, i._k\n"
            }
        )
        self.assertEqual(guards.private_imports(root), [])


class Syntax311(Sample):
    def test_newer_syntax_is_a_violation(self):
        root = self.tree({"x.py": "type X = int\n", "y.py": "X = int\n"})
        found = guards.syntax_311(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("x.py: "), found)


class ImportHygiene(Sample):
    def test_any_sys_path_mutation(self):
        root = self.tree(
            {
                "a.py": "import sys\nsys.path.insert(0, 'x')\ndef f():\n    sys.path.append('x')\n",
                "b.py": "import sys\nsys.path = []\nsys.path[0] = 'x'\nsys.path += ['x']\n"
                "del sys.path[0]\n",
                "c.py": "import sys as s\nfrom sys import path as p\nfrom unittest import mock\n"
                "s.path.extend([])\np.remove('x')\np[:0] = ['x']\np += ['x']\n"
                "mock.patch('sys.path', [])\n@mock.patch.object(s, 'path', [])\ndef g(): pass\n",
                "d.py": "import sys\nfrom sys import path\nfrom unittest import mock\n"
                "x = [sys.path[0], sys.path.index('x'), list(sys.path), sys.path.copy()]\n"
                "path = []\nmock.patch('os.path.join')\nmock.patch.object(sys, 'argv', [])\n",
            }
        )
        self.assertEqual(
            guards.import_hygiene(root),
            [f"{at}: sys.path mutation" for at in ("a.py:2", "a.py:4")]
            + [f"b.py:{n}: sys.path mutation" for n in range(2, 6)]
            + [f"c.py:{n}: sys.path mutation" for n in range(4, 10)],
        )

    def test_a_module_level_environment_write(self):
        root = self.tree(
            {
                "a.py": "import os\nos.environ['A'] = '1'\nos.environ.update(A='1')\n"
                "del os.environ['A']\nos.environ |= {}\nos.putenv('A', '1')\n",
                "b.py": "import os as o\nfrom os import environ, putenv\nfrom unittest import mock\n"
                "class C:\n    environ.setdefault('A', '1')\nif True:\n    putenv('A', '1')\n"
                "mock.patch.dict(environ, {'A': '1'}).start()\no.environ.pop('A')\n",
                "c.py": "import os\nfrom unittest import mock\nA = os.environ.get('A', os.environ['B'])\n"
                "def f():\n    os.environ['A'] = '1'\n    os.putenv('A', '1')\n"
                "g = lambda: os.environ.clear()\n@mock.patch.dict(os.environ, {'A': '1'})\n"
                "class D:\n    @mock.patch.dict(os.environ, {'A': '1'})\n    def h(self): pass\n",
            }
        )
        self.assertEqual(
            guards.import_hygiene(root),
            [f"a.py:{n}: module-level environment write" for n in range(2, 7)]
            + [f"b.py:{n}: module-level environment write" for n in (5, 7, 8, 9)],
        )

    def test_e402_i001_and_isort_comments(self):
        root = self.tree(
            {
                "a.py": "import x  # noqa: E402\nimport y  # noqa: F401,E402\nimport z  # NOQA:I001\n"
                "# isort: skip_file\n# ruff: noqa: E402\n",
                "b.py": "S = '# noqa: E402'\nimport w  # noqa: F401\n# ruff: noqa: E501\n# E402 isort\n",
            }
        )
        self.assertEqual(
            guards.import_hygiene(root),
            [
                "a.py:1: comment # noqa: E402",
                "a.py:2: comment # noqa: F401,E402",
                "a.py:3: comment # NOQA:I001",
                "a.py:4: comment # isort: skip_file",
                "a.py:5: comment # ruff: noqa: E402",
            ],
        )

    def test_the_import_root_holds_only_the_package(self):
        src = os.path.join("core", "src")
        names = (
            "agent_pm/__init__.py",
            "__pycache__/x.pyc",
            "repo.py",
            "other/__init__.py",
            "README",
        )
        root = self.tree({f"{src}/{name}": "" for name in names})
        self.assertEqual(
            guards.import_hygiene(root),
            [f"{src}/{entry}: not agent_pm" for entry in ("README", "other", "repo.py")],
        )


if __name__ == "__main__":
    unittest.main()
