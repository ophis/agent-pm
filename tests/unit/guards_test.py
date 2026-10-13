"""Repository guards on independent sample trees and the checkout."""

import importlib
import inspect
import json
import os
import subprocess
import unittest
from pathlib import Path

from tests import guards
from tests.case import ORCH_ROOT, PY, REPO, SRC, Case, child_env
from tests.guard_samples import PKG, Sample, fixtures, flat, own_case

OPKG = "src/agent_pm/linear_orchestrator"
PROBE = "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"


class RealTree(Case):
    def test_each_guard_is_clean(self):
        for name, guard in inspect.getmembers(guards, inspect.isfunction):
            if name.startswith("_") or guard.__module__ != guards.__name__:
                continue
            roots = SRC.values() if name == "private_imports" else (REPO,)
            for root in roots:
                with self.subTest(name, root=root):
                    self.assertEqual(guard(root), [])

    def test_utils_holds_none_of_the_modules_that_left_it(self):
        moved = ("protocol",)
        utils = os.listdir(os.path.join(SRC["utils"], "agent_pm", "utils"))
        self.assertEqual([n for n in utils if n.endswith(".py") and n[:-3] in moved], [])

    def test_no_tree_names_the_old_orchestrator_package(self):
        old = "_".join(("agent_pm", "orchestrator"))
        named = []
        for tree in guards.TREES:
            for top, dirs, files in os.walk(os.path.join(REPO, tree)):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(files):
                    rel = os.path.relpath(os.path.join(top, name), REPO)
                    text = Path(top, name).read_text(encoding="utf-8", errors="replace")
                    if old in rel or old in text:
                        named.append(rel)
        self.assertEqual(named, [])

    def test_src_modules_load_from_src(self):
        for root in (r for sub, r in SRC.items() if sub != "linear_orchestrator"):
            src = os.path.realpath(root)
            for top, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for name in sorted(f for f in files if f.endswith(".py")):
                    parts = os.path.relpath(os.path.join(top, name), root)[:-3].split(os.sep)
                    parts = parts[:-1] if parts[-1] == "__init__" else parts
                    module = importlib.import_module(".".join(parts))
                    real = os.path.realpath(module.__file__)
                    self.assertEqual(os.path.commonpath([src, real]), src)

    def test_orchestrator_loads_from_its_root(self):
        package = os.path.join(ORCH_ROOT, "agent_pm", "linear_orchestrator")
        names = sorted(
            ".".join(path.relative_to(ORCH_ROOT).with_suffix("").parts).removesuffix(".__init__")
            for path in Path(package).rglob("*.py")
        )
        self.assertIn("agent_pm.linear_orchestrator.engine.run", names)
        out = subprocess.run(
            [*PY, "-c", PROBE, *names],
            env=child_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        loaded = json.loads(out)
        real = os.path.realpath(package)
        for name in names:
            self.assertEqual(os.path.commonpath([real, os.path.realpath(loaded[name])]), real)


class EveryTest(Sample):
    def test_is_a_case(self):
        loader, found = unittest.TestLoader(), []
        for top in guards.TESTS:
            for suite in ("unit", "integration"):
                start = os.path.join(REPO, top, suite)
                if os.path.isdir(start):
                    tests = loader.discover(start, "*_test.py", REPO)
                    found += [test.id() for test in flat(tests) if not own_case(test, top)]
        self.assertEqual((loader.errors, found), ([], []))

    def test_has_no_class_or_module_fixture(self):
        for tests in guards.TESTS:
            self.assertEqual(fixtures(os.path.join(REPO, tests)), [])
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


class ImportHygiene(Sample):
    def test_any_sys_path_mutation(self):
        root = self.tree(
            {
                "tests/a.py": "import sys\nsys.path.insert(0, 'x')\ndef f():\n    sys.path.append('x')\n",
                "tests/b.py": "import sys\nsys.path = []\nsys.path[0] = 'x'\nsys.path += ['x']\n"
                "del sys.path[0]\n",
                "tests/c.py": "import sys as s\nfrom sys import path as p\nfrom unittest import mock\n"
                "s.path.extend([])\np.remove('x')\np[:0] = ['x']\np += ['x']\n"
                "mock.patch('sys.path', [])\n@mock.patch.object(s, 'path', [])\ndef g(): pass\n",
                "tests/d.py": "import sys\nfrom sys import path\nfrom unittest import mock\n"
                "x = [sys.path[0], sys.path.index('x'), list(sys.path), sys.path.copy()]\n"
                "path = []\nmock.patch('os.path.join')\nmock.patch.object(sys, 'argv', [])\n",
            }
        )
        self.assertEqual(
            guards.import_hygiene(root),
            [f"{at}: sys.path mutation" for at in ("tests/a.py:2", "tests/a.py:4")]
            + [f"tests/b.py:{n}: sys.path mutation" for n in range(2, 6)]
            + [f"tests/c.py:{n}: sys.path mutation" for n in range(4, 10)],
        )

    def test_a_module_level_environment_write(self):
        root = self.tree(
            {
                "tests/a.py": "import os\nos.environ['A'] = '1'\nos.environ.update(A='1')\n"
                "del os.environ['A']\nos.environ |= {}\nos.putenv('A', '1')\n",
                "tests/b.py": "import os as o\nfrom os import environ, putenv\nfrom unittest import mock\n"
                "class C:\n    environ.setdefault('A', '1')\nif True:\n    putenv('A', '1')\n"
                "mock.patch.dict(environ, {'A': '1'}).start()\no.environ.pop('A')\n",
                "tests/c.py": "import os\nfrom unittest import mock\nA = os.environ.get('A', os.environ['B'])\n"
                "def f():\n    os.environ['A'] = '1'\n    os.putenv('A', '1')\n"
                "g = lambda: os.environ.clear()\n@mock.patch.dict(os.environ, {'A': '1'})\n"
                "class D:\n    @mock.patch.dict(os.environ, {'A': '1'})\n    def h(self): pass\n",
            }
        )
        self.assertEqual(
            guards.import_hygiene(root),
            [f"tests/a.py:{n}: module-level environment write" for n in range(2, 7)]
            + [f"tests/b.py:{n}: module-level environment write" for n in (5, 7, 8, 9)],
        )


class SharedTypes(Sample):
    DATA = "from dataclasses import dataclass\n\n\n@dataclass\nclass A:\n    x: int\n"
    UTILS = f"utils/{PKG}/utils"

    def found(self, home: str, user: str = "b.py", extra=None) -> list[str]:
        user_text = f"from agent_pm.utils.{home[:-3]} import A\n"
        return guards.shared_types(
            self.tree(
                {
                    f"{self.UTILS}/{home}": self.DATA,
                    f"{self.UTILS}/{user}": user_text,
                    **(extra or {}),
                }
            )
        )

    def test_a_dataclass_another_module_imports_lives_in_a_types_py(self):
        self.assertEqual(
            self.found("a.py"),
            [f"{self.UTILS}/b.py:1: agent_pm.utils.a.A: a shared class belongs in a types.py"],
        )
        self.assertEqual(self.found("types.py"), [])

    def test_a_relative_import_and_a_module_attribute_count(self):
        for user in ("from .a import A\n", "from agent_pm.utils import a\n\nx = a.A\n"):
            with self.subTest(user):
                root = self.tree({f"{self.UTILS}/a.py": self.DATA, f"{self.UTILS}/b.py": user})
                ((found,),) = [guards.shared_types(root)]
                self.assertIn(": agent_pm.utils.a.A: a shared class", found)

    def test_a_package_re_export_is_clean(self):
        self.assertEqual(self.found("a.py", "__init__.py"), [])

    def test_plain_namedtuple_and_enum_classes_are_counted(self):
        for source in (
            "class A:\n    pass\n",
            "from typing import NamedTuple\nclass A(NamedTuple):\n    value: int\n",
            "from enum import Enum\nclass A(Enum):\n    value = 1\n",
        ):
            with self.subTest(source=source):
                root = self.tree(
                    {
                        f"{self.UTILS}/a.py": source,
                        f"{self.UTILS}/b.py": "from agent_pm.utils.a import A\n",
                    }
                )
                self.assertEqual(
                    guards.shared_types(root),
                    [
                        f"{self.UTILS}/b.py:1: agent_pm.utils.a.A: a shared class belongs in a types.py"
                    ],
                )


class NamespacePackage(Sample):
    def own(self, *extra: str) -> str:
        files = [f"{top}/src/agent_pm/{sub}/__init__.py" for top, sub in guards.PROJECTS.items()]
        return self.tree({f: "" for f in (*files, *extra)})

    def test_the_own_subpackages_and_caches_are_clean(self):
        root = self.own("session/src/__pycache__/x.pyc", f"session/{PKG}/__pycache__/x.pyc")
        self.assertEqual(guards.namespace_package(root), [])

    def test_finder_and_build_leftovers_are_ignored(self):
        root = self.own(
            "utils/src/.DS_Store",
            f"utils/{PKG}/.DS_Store",
            "manager/src/agent_pm_manager.egg-info/PKG-INFO",
            f"session/{PKG}/x.egg-info/PKG-INFO",
        )
        self.assertEqual(guards.namespace_package(root), [])

    def test_an_init_py_is_refused(self):
        root = self.own(*(f"{top}/{PKG}/__init__.py" for top in guards.PROJECTS))
        self.assertEqual(
            guards.namespace_package(root),
            sorted(
                f"{top}/{PKG}/__init__.py: namespace package __init__.py" for top in guards.PROJECTS
            ),
        )

    def test_a_foreign_subpackage_or_entry_is_refused(self):
        root = self.own(
            f"delegate/{PKG}/linear_orchestrator/__init__.py",
            f"delegate/{PKG}/config.py",
            "utils/src/repo.py",
            f"linear-orchestrator/{PKG}/session/__init__.py",
            "linear-orchestrator/src/other/__init__.py",
        )
        self.assertEqual(
            guards.namespace_package(root),
            [
                f"delegate/{PKG}/config.py: not delegate's",
                f"delegate/{PKG}/linear_orchestrator: not delegate's",
                f"linear-orchestrator/{PKG}/session: not linear-orchestrator's",
                "linear-orchestrator/src/other: not linear-orchestrator's",
                "utils/src/repo.py: not utils's",
            ],
        )

    def test_a_missing_subpackage(self):
        root = self.own()
        os.remove(os.path.join(root, "manager", PKG, "manager", "__init__.py"))
        os.rmdir(os.path.join(root, "manager", PKG, "manager"))
        self.assertEqual(guards.namespace_package(root), [f"manager/{PKG}/manager: missing"])


class PackageTop(Sample):
    def package(self, project, *entries):
        sub = guards.PROJECTS[project]
        return {f"{project}/{PKG}/{sub}/{e}": "" for e in entries}

    def test_only_init_cli_engine_and_listed_subpackages(self):
        files = {
            **self.package("utils", "__init__.py", "x.py"),
            **self.package("session", "__init__.py", "engine/x.py", "modes/x.py", "extra.py"),
            **self.package(
                "delegate", "__init__.py", "cli.py", "engine/x.py", "team/x.py", "misc/x.py"
            ),
            **self.package(guards.ORCH_PROJECT, "__init__.py", "engine/x.py", "linear/x.py"),
            **self.package("manager", "__init__.py", "engine/crew.py", "engine/roster.py", "y.py"),
        }
        self.assertEqual(
            guards.package_top(self.tree(files)),
            [
                f"delegate/{PKG}/delegate/misc: not __init__.py, cli.py, engine/ or a listed subpackage",
                f"manager/{PKG}/manager/y.py: not __init__.py, cli.py, engine/ or a listed subpackage",
                f"session/{PKG}/session/extra.py: not __init__.py, cli.py, engine/ or a listed subpackage",
            ],
        )

    def test_a_missing_entry(self):
        files = self.package("delegate", "__init__.py", "cli.py")
        self.assertIn(
            f"delegate/{PKG}/delegate/engine: missing", guards.package_top(self.tree(files))
        )
