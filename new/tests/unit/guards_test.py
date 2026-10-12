"""new/tests/guards.py: each guard on the real tree, and on violating samples written to temp trees; every test
is a Case, with no class or module fixture."""

import ast
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from new.tests import guards
from new.tests.case import CORE, NEW, ORCH_ROOT, PY, ROOT, Case, child_env

OLD = "/".join(("core", "src"))
PKG = "src/agent_pm"
ORCH = os.path.dirname(ORCH_ROOT)
OPKG = "src/agent_pm_orchestrator"
FIXTURES = ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule")
ORCH_GUARDS = (guards.orchestrator_imports, guards.orchestrator_text, guards.config_once)
PROBE = (
    "import importlib, json, sys\n"
    "modules = {n: importlib.import_module(n).__file__ for n in sys.argv[1:]}\n"
    "files = [getattr(m, '__file__', None) for m in list(sys.modules.values())]\n"
    "print(json.dumps({'modules': modules, 'files': files, 'path': sys.path}))\n"
)


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
            (guards.python_commands, NEW),
            *((guard, ORCH) for guard in ORCH_GUARDS),
            (guards.private_imports, ORCH),
            (guards.syntax_311, ORCH),
            (guards.old_core, ORCH),
        ):
            with self.subTest(guard.__name__, root=root):
                self.assertEqual(guard(root), [])

    def test_utils_holds_none_of_the_modules_that_left_it(self):
        moved = ("shell", "repos", "git", "protocol")
        utils = os.listdir(os.path.join(ROOT, "agent_pm", "utils"))
        self.assertEqual([n for n in utils if n.removesuffix(".py") in moved], [])

    def test_src_never_reaches_the_old_core(self):
        src = os.path.realpath(ROOT)
        for top, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(f for f in files if f.endswith(".py")):
                parts = os.path.relpath(os.path.join(top, name), ROOT)[:-3].split(os.sep)
                parts = parts[:-1] if parts[-1] == "__init__" else parts
                module = importlib.import_module(".".join(parts))
                self.assertEqual(os.path.commonpath([src, os.path.realpath(module.__file__)]), src)
        files = [getattr(m, "__file__", None) for m in list(sys.modules.values())]
        self.assert_no_old_core(files, sys.path)

    def test_orchestrator_loads_from_its_root_and_never_the_old_core(self):
        package = os.path.join(ORCH_ROOT, "agent_pm_orchestrator")
        names = sorted(
            "agent_pm_orchestrator" + ("" if f == "__init__.py" else f".{f[:-3]}")
            for f in os.listdir(package)
            if f.endswith(".py")
        )
        self.assertIn("agent_pm_orchestrator.run", names)
        out = subprocess.run(
            [*PY, "-c", PROBE, *names],
            env=child_env(None, ORCH_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        loaded = json.loads(out)
        for name in names:
            self.assertEqual(
                os.path.dirname(os.path.realpath(loaded["modules"][name])),
                os.path.realpath(package),
            )
        self.assert_no_old_core(loaded["files"], loaded["path"])

    def assert_no_old_core(self, files, path):
        old = os.path.realpath(os.path.join(os.path.dirname(NEW), "core", "src"))
        for file in filter(None, files):
            self.assertNotEqual(os.path.commonpath([old, os.path.realpath(file)]), old, file)
        self.assertNotIn(old, {os.path.realpath(p) for p in path if p})


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
        root = self.tree({f"{PKG}/session/adapters/x.py": "import agent_pm.session.driver\n"})
        self.assertEqual(
            guards.layer_imports(root),
            [f"{PKG}/session/adapters/x.py: session.adapters imports agent_pm.session.driver"],
        )

    def test_driver_modes_outside_driver_names_it(self):
        root = self.tree(
            {f"{PKG}/role/delegate/x.py": "from agent_pm.session.driver.modes import headless\n"}
        )
        self.assertEqual(
            guards.layer_imports(root),
            [f"{PKG}/role/delegate/x.py: role.delegate imports agent_pm.session.driver.modes"],
        )

    def test_a_relative_import_is_resolved(self):
        root = self.tree({f"{PKG}/role/composer/x.py": "from ...session.driver import api\n"})
        self.assertEqual(
            guards.layer_imports(root),
            [f"{PKG}/role/composer/x.py: role.composer imports agent_pm.session.driver.api"],
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                f"{PKG}/role/delegate/x.py": "from agent_pm.session.driver.api import start\n"
                "from agent_pm.role.composer import y\nfrom agent_pm.session.driver import api\n"
                "from agent_pm.utils.files import append_line\n",
                f"{PKG}/session/driver/x.py": "import agent_pm.session.adapters.base\nfrom .modes import headless\n"
                "from agent_pm.session.driver.modes import headless\n",
                f"{PKG}/x.py": "from agent_pm.session.driver import api\nimport agent_pm.session.driver.api\n"
                "from agent_pm.session.driver.api import start\nfrom .session.driver import api\nimport agent_pm\n",
            }
        )
        self.assertEqual(guards.layer_imports(root), [])

    def test_outside_the_layers_reaches_only_driver_api(self):
        root = self.tree(
            {
                f"{PKG}/x.py": "import agent_pm.session.adapters\n",
                f"{PKG}/y.py": "from agent_pm.session.driver import registry\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/x.py: outside the layers imports agent_pm.session.adapters",
                f"{PKG}/y.py: outside the layers imports agent_pm.session.driver.registry",
            ],
        )

    def test_a_layer_reaches_outside_the_layers_only_utils(self):
        root = self.tree(
            {
                f"{PKG}/role/delegate/x.py": "from agent_pm.manager import roster\n",
                f"{PKG}/session/driver/x.py": "import agent_pm.manager.crew\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/role/delegate/x.py: role.delegate imports agent_pm.manager.roster",
                f"{PKG}/session/driver/x.py: session.driver imports agent_pm.manager.crew",
            ],
        )

    def test_nested_and_literal_imports_count(self):
        root = self.tree(
            {
                f"{PKG}/session/adapters/x.py": "def f():\n    import importlib\n"
                "    importlib.import_module('agent_pm.session.driver.registry')\n"
                "    __import__('agent_pm.role.composer')\n"
                "    from agent_pm.role.delegate import y\n"
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/session/adapters/x.py: session.adapters imports agent_pm.role.composer",
                f"{PKG}/session/adapters/x.py: session.adapters imports agent_pm.role.delegate.y",
                f"{PKG}/session/adapters/x.py: session.adapters imports agent_pm.session.driver.registry",
            ],
        )

    def test_a_bare_name_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/role/delegate/x.py": "from driver import api\nimport composer\n"
                "from utils.paths import under\n",
                f"{PKG}/x.py": "import repo\ndef f():\n    __import__('adapters.base')\n",
                f"{PKG}/role/agent_tools/x.py": "import agent_tools\n",
                f"{PKG}/session/driver/x.py": "import repository\nimport drivers\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/role/agent_tools/x.py: bare import agent_tools",
                f"{PKG}/role/delegate/x.py: bare import composer",
                f"{PKG}/role/delegate/x.py: bare import driver.api",
                f"{PKG}/role/delegate/x.py: bare import utils.paths",
                f"{PKG}/x.py: bare import adapters.base",
                f"{PKG}/x.py: bare import repo",
            ],
        )

    def test_importing_the_orchestrator_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/role/delegate/x.py": "from agent_pm_orchestrator.config import load\n",
                f"{PKG}/session/driver/x.py": "from agent_pm_orchestrator import config\n",
                f"{PKG}/utils/x.py": "def f():\n    __import__('agent_pm_orchestrator')\n",
                f"{PKG}/x.py": "import agent_pm_orchestrator.run\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/role/delegate/x.py: role.delegate imports agent_pm_orchestrator.config",
                f"{PKG}/session/driver/x.py: session.driver imports agent_pm_orchestrator.config",
                f"{PKG}/utils/x.py: outside the layers imports agent_pm_orchestrator",
                f"{PKG}/x.py: outside the layers imports agent_pm_orchestrator.run",
            ],
        )

    def test_the_package_is_the_one_dir_under_src(self):
        root = self.tree(
            {
                "src/pkg/session/adapters/x.py": "import pkg.session.driver\n",
                "src/pkg/role/delegate/x.py": "from pkg.session.driver import api\nimport agent_pm.session.adapters\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            ["src/pkg/session/adapters/x.py: session.adapters imports pkg.session.driver"],
        )


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({f"{PKG}/session/driver/x.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(
            guards.runtime_names(root),
            [
                f"{PKG}/session/driver/x.py:1: runtime name Claude",
                f"{PKG}/session/driver/x.py:2: runtime name codex",
            ],
        )

    def test_print_prompt_is_a_runtime_name(self):
        root = self.tree({f"{PKG}/role/delegate/x.py": 'RUNTIME = "print_prompt"\n'})
        self.assertEqual(
            guards.runtime_names(root), [f"{PKG}/role/delegate/x.py:1: runtime name print_prompt"]
        )

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({f"{PKG}/session/adapters/claude/x.py": 'NAME = "claude"\n'})
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

    def test_toml_print_prompt_table_header_and_implementation_line_are_exempt(self):
        root = self.tree(
            {
                "config.toml": "# [adapters.print_prompt]  act-as's runtime\n[adapters.print_prompt]\n"
                'implementation = "adapters.print_prompt:PrintPromptAdapter"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

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
        root = self.tree(
            {f"{PKG}/role/composer/x.py": sample, f"{PKG}/session/driver/x.py": sample}
        )
        self.assertEqual(
            guards.adapters_key(root), [f'{PKG}/role/composer/x.py:2: literal key "adapters"']
        )


class ModesImportApi(Sample):
    def test_a_mode_importing_driver_api_any_way_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/session/driver/modes/a.py": "from agent_pm.session.driver import api\n",
                f"{PKG}/session/driver/modes/b.py": "import agent_pm.session.driver.api\n",
                f"{PKG}/session/driver/modes/c.py": "from ..api import DriverError\n",
                f"{PKG}/session/driver/modes/d.py": "def f():\n    from .. import api\n",
                f"{PKG}/session/driver/modes/__init__.py": "__import__('agent_pm.session.driver.api')\n",
            }
        )
        self.assertEqual(
            guards.modes_import_api(root),
            [
                f"{PKG}/session/driver/modes/__init__.py: imports agent_pm.session.driver.api",
                f"{PKG}/session/driver/modes/a.py: imports agent_pm.session.driver.api",
                f"{PKG}/session/driver/modes/b.py: imports agent_pm.session.driver.api",
                f"{PKG}/session/driver/modes/c.py: imports agent_pm.session.driver.api",
                f"{PKG}/session/driver/modes/d.py: imports agent_pm.session.driver.api",
            ],
        )

    def test_driver_errors_in_a_mode_and_driver_api_outside_modes_are_clean(self):
        root = self.tree(
            {
                f"{PKG}/session/driver/modes/a.py": "from agent_pm.session.driver.errors import DriverError\n"
                "from agent_pm.session.driver import records\nfrom ..errors import DriverError\n",
                f"{PKG}/session/driver/x.py": "from agent_pm.session.driver import api\n",
                f"{PKG}/y.py": "import agent_pm.session.driver.api\n",
            }
        )
        self.assertEqual(guards.modes_import_api(root), [])


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                f"{PKG}/session/driver/tmux_host.py": sample,
                f"{PKG}/session/driver/modes/x.py": sample,
                f"{PKG}/role/composer/y.py": 'TOOL = "tmux"\n',
                "src/driver/tmux_host.py": sample,
                "tests/z.py": sample,
            }
        )
        self.assertEqual(
            guards.tmux_calls(root),
            [
                f'{PKG}/role/composer/y.py:1: str constant "tmux"',
                f'{PKG}/session/driver/modes/x.py:2: str constant "tmux"',
                'src/driver/tmux_host.py:2: str constant "tmux"',
                'tests/z.py:2: str constant "tmux"',
            ],
        )

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree(
            {f"{PKG}/session/driver/x.py": 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'}
        )
        self.assertEqual(guards.tmux_calls(root), [])


class Syntax39(Sample):
    def test_newer_syntax_in_a_standalone_file_is_a_violation(self):
        root = self.tree(
            {
                f"{PKG}/session/driver/tmux_host.py": "def f(x: int | None): pass\nmatch x:\n    case _: pass\n",
                f"{PKG}/session/driver/api.py": "match x:\n    case _: pass\n",
            }
        )
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"{PKG}/session/driver/tmux_host.py: "), found)

    def test_the_tui_hook_is_standalone_too(self):
        root = self.tree(
            {f"{PKG}/session/driver/modes/tui_hook.py": "match x:\n    case _: pass\n"}
        )
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"{PKG}/session/driver/modes/tui_hook.py: "), found)

    def test_3_9_syntax_is_clean(self):
        root = self.tree(
            {
                f"{PKG}/session/driver/tmux_host.py": "from __future__ import annotations\n"
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

    def test_the_orchestrator_root_holds_only_its_package(self):
        src = "orchestrator/src"
        names = (
            "agent_pm_orchestrator/__init__.py",
            "__pycache__/x.pyc",
            "config.py",
            "agent_pm/__init__.py",
        )
        root = self.tree({f"{src}/{name}": "" for name in names})
        self.assertEqual(
            guards.import_hygiene(root),
            [f"{src}/{entry}: not agent_pm_orchestrator" for entry in ("agent_pm", "config.py")],
        )


class PythonCommands(Sample):
    def test_a_python_command_other_than_p_m_agent_pm(self):
        root = self.tree(
            {
                "core/config.toml": 'commands = ["python3 x.py", "python3 -m agent_pm.x"]\n'
                "# python3 -P -m agent_pm.role.agent_tools.repo\n",
                "orchestrator/config.toml": 'gate = "python3 -P -m agent_pmx.y"\n',
                "core/skills/a/SKILL.md": "Run `python3.11 x.py`.\n"
                "- Bash(python -P -m agent_pm.role.delegate.cli --list)\n",
                "core/team/roles/r.md": "1. `python3 -P agent_pm.x`\n2. /usr/bin/python3 -I -m agent_pm.x\n"
                "3. `env PYTHONPATH=x python3 -P -m agent_pm_orchestrator.run`\n",
            }
        )
        self.assertEqual(
            guards.python_commands(root),
            [
                'core/config.toml:1: python command "python3 -m agent_pm.x"',
                'core/config.toml:1: python command "python3 x.py"',
                'core/skills/a/SKILL.md:1: python command "python3.11 x.py"',
                'core/team/roles/r.md:1: python command "python3 -P agent_pm.x"',
                'core/team/roles/r.md:2: python command "python3 -I -m agent_pm.x"',
                'orchestrator/config.toml:1: python command "python3 -P -m agent_pmx.y"',
            ],
        )

    def test_other_words_and_files_are_clean(self):
        root = self.tree(
            {
                "core/team/a.md": "pythonic, my_python3 x.py, Python 3.11\n",
                "core/skills/a/notes.txt": "python3 x.py\n",
                "core/README.md": "python3 x.py\n",
                "orchestrator/other.toml": 'x = "python3 x.py"\n',
                "tests/a.md": "python3 x.py\n",
            }
        )
        self.assertEqual(guards.python_commands(root), [])


class OrchestratorImports(Sample):
    def test_forbidden_imports_name_the_file_and_line(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "import agent_pm.session.adapters\nfrom agent_pm.session.driver.modes import x\n"
                "from agent_pm.session.driver import tmux_host\nimport agent_pm.role.agent_tools.repo\n",
                f"{OPKG}/b.py": "from agent_pm.role.delegate import runner\n"
                "from agent_pm.role.delegate.outcome import Outcome\nimport core\nimport agent_pm\n",
                f"{OPKG}/run.py": "from agent_pm.role.composer import resolve\nfrom agent_pm.role import composer\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:1: imports agent_pm.session.adapters",
                f"{OPKG}/a.py:2: imports agent_pm.session.driver.modes.x",
                f"{OPKG}/a.py:3: imports agent_pm.session.driver.tmux_host",
                f"{OPKG}/a.py:4: imports agent_pm.role.agent_tools.repo",
                f"{OPKG}/b.py:1: imports agent_pm.role.delegate.runner",
                f"{OPKG}/b.py:2: imports agent_pm.role.delegate.outcome.Outcome",
                f"{OPKG}/b.py:3: imports core",
                f"{OPKG}/b.py:4: imports agent_pm",
                f"{OPKG}/run.py:1: imports agent_pm.role.composer.resolve",
                f"{OPKG}/run.py:2: imports agent_pm.role.composer",
            ],
        )

    def test_bare_and_relative_names(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "import config\nfrom linear import use_logs\nfrom driver import api\n"
                "import utils.shell\nimport delegate\nfrom . import b\nfrom .c import d\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:1: imports config",
                f"{OPKG}/a.py:2: imports linear.use_logs",
                f"{OPKG}/a.py:3: imports driver.api",
                f"{OPKG}/a.py:4: imports utils.shell",
                f"{OPKG}/a.py:5: imports delegate",
                f"{OPKG}/a.py:6: imports .b",
                f"{OPKG}/a.py:7: imports .c.d",
            ],
        )

    def test_submodules_by_attribute_and_other_forms(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "from agent_pm.role import delegate as d\nd.runner.run()\n"
                "import agent_pm.session.driver.api\nimport third_party\n__import__('agent_pm.session.adapters.base')\n"
                "import agent_pm.role.delegate\nagent_pm.role.delegate.outcome.x\n"
                "from agent_pm.session.driver.api import start\n",
                f"{OPKG}/config.py": "from agent_pm.role import composer\ncomposer.run.resolve()\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:2: uses agent_pm.role.delegate.runner",
                f"{OPKG}/a.py:3: imports agent_pm.session.driver.api",
                f"{OPKG}/a.py:4: imports third_party",
                f"{OPKG}/a.py:5: imports agent_pm.session.adapters.base",
                f"{OPKG}/a.py:7: uses agent_pm.role.delegate.outcome",
                f"{OPKG}/a.py:8: imports agent_pm.session.driver.api.start",
                f"{OPKG}/config.py:2: uses agent_pm.role.composer.run",
            ],
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                f"{OPKG}/config.py": "import os\nfrom agent_pm.role.composer import resolve\n"
                "from agent_pm.role import composer\ncomposer.index()\nimport agent_pm.role.composer\n"
                "from agent_pm.utils.files import save\n",
                f"{OPKG}/run.py": "from agent_pm_orchestrator import config\n"
                "from agent_pm.role.delegate import run, Result\nfrom agent_pm.role import delegate\n"
                "delegate.Begun\nimport agent_pm.role.delegate as d\nd.Finished\n"
                "from agent_pm.session.driver import api\nimport agent_pm.role.repos.shell\nfrom agent_pm import utils\n"
                "import agent_pm_orchestrator.linear\nfrom agent_pm_orchestrator.config import TASKS\n",
                f"{OPKG}/utils.py": "",
            }
        )
        self.assertEqual(guards.orchestrator_imports(root), [])


class OrchestratorText(Sample):
    def test_runtime_names_and_tool_strings_name_the_file_and_line(self):
        root = self.tree(
            {
                f"{OPKG}/router.py": 'X = "codex"\n',
                f"{OPKG}/a.py": 'import subprocess\nsubprocess.run(["tmux", "ls"])\nK = "adapters"\n'
                "# Claude\n",
                "config.toml": '# claude\nx = "tmux"\ny = \'adapters\'\nz = "tmux ls"  # "tmux"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(root),
            [
                "config.toml:1: runtime name claude",
                'config.toml:2: str constant "tmux"',
                'config.toml:3: str constant "adapters"',
                f'{OPKG}/a.py:2: str constant "tmux"',
                f'{OPKG}/a.py:3: str constant "adapters"',
                f"{OPKG}/a.py:4: runtime name Claude",
                f"{OPKG}/router.py:1: runtime name codex",
            ],
        )

    def test_claude_md_and_longer_strings_are_clean(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": '"""Reads CLAUDE.md."""\nP = "CLAUDE.md"\nH = "tmux attach"\n',
                "config.toml": '# see CLAUDE.md\nx = "tmux attach"\n',
            }
        )
        self.assertEqual(guards.orchestrator_text(root), [])

    def test_handoff_literals_only_in_issues(self):
        root = self.tree(
            {
                f"{OPKG}/issues.py": 'H = "Handoff from"\n'
                'S = ("## Source", "## Instructions", "## Comments")\n',
                f"{OPKG}/promote.py": 'x = f"Handoff from {i}: {u}"\n# ## Comments are quoted\n',
                f"{OPKG}/target.py": 'M = "or its ## Instructions"\nS = "## Source"\n',
                "src/issues.py": 'H = "Handoff from"\n',
                "config.toml": 'x = "## Comments"\n',
                "tests/a_test.py": 'X = "Handoff from ## Source"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(root),
            [
                f"{OPKG}/promote.py:1: Handoff literal Handoff from",
                f"{OPKG}/promote.py:2: Handoff literal ## Comments",
                f"{OPKG}/target.py:1: Handoff literal ## Instructions",
                f"{OPKG}/target.py:2: Handoff literal ## Source",
                "src/issues.py:1: Handoff literal Handoff from",
            ],
        )


class ConfigOnce(Sample):
    LOAD = "calls agent_pm_orchestrator.config.load outside an entry main"
    USE_LOGS = "calls agent_pm_orchestrator.linear.use_logs outside an entry main"

    def test_load_and_use_logs_outside_an_entry_main(self):
        root = self.tree(
            {
                f"{OPKG}/run.py": "from agent_pm_orchestrator import config, linear\n\ndef main():\n"
                "    config.load()\n    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                f"{OPKG}/writeback.py": "from agent_pm_orchestrator import config\n"
                "from agent_pm_orchestrator.linear import use_logs\n\ndef main():\n"
                "    config.load()\n    use_logs(None)\n",
                f"{OPKG}/promote.py": "import agent_pm_orchestrator.config as c\n\nclass P:\n"
                "    def main(self):\n        c.load()\n",
                "src/run.py": "import agent_pm_orchestrator.config\n\ndef main():\n"
                "    agent_pm_orchestrator.config.load()\n",
            }
        )
        self.assertEqual(
            guards.config_once(root),
            [
                f"{OPKG}/promote.py:5: {self.LOAD}",
                f"{OPKG}/run.py:8: {self.LOAD}",
                f"{OPKG}/writeback.py:5: {self.LOAD}",
                f"{OPKG}/writeback.py:6: {self.USE_LOGS}",
                f"src/run.py:4: {self.LOAD}",
            ],
        )

    def test_the_real_entries_calls_outside_an_entry(self):
        files = {}
        for name in ("run.py", "promote.py"):
            with open(
                os.path.join(ORCH_ROOT, "agent_pm_orchestrator", name), encoding="utf-8"
            ) as f:
                files[f"{OPKG}/not_{name}"] = f.read()
        found = guards.config_once(self.tree(files))
        self.assertEqual(
            {(f.split(":")[0], f.split(": ", 1)[1]) for f in found},
            {(rel, call) for rel in files for call in (self.LOAD, self.USE_LOGS)},
        )

    def test_load_helpers_outside_config_py(self):
        root = self.tree(
            {
                f"{OPKG}/run.py": "from agent_pm_orchestrator import config\nconfig.runnable(1)\n",
                f"{OPKG}/config.py": "def runnable():\n    pass\n\ndef load():\n    runnable()\n"
                "    docs()\n",
                f"{OPKG}/promote.py": "from agent_pm_orchestrator import config, linear\n\n"
                "def main():\n    try:\n        cfg = config.load()\n    except SystemExit:\n"
                "        linear.use_logs(None)\n",
                "src/config.py": "def load():\n    runnable()\n",
            }
        )
        self.assertEqual(
            guards.config_once(root),
            [
                f"{OPKG}/run.py:2: calls agent_pm_orchestrator.config.runnable",
                "src/config.py:2: calls runnable",
            ],
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in ORCH_GUARDS:
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


if __name__ == "__main__":
    unittest.main()
