"""Child imports rely on installed roots or only the explicitly supplied packages."""

import os
import sys
import unittest
from importlib.machinery import ModuleSpec
from types import ModuleType
from unittest import mock

from agent_pm.utils.paths import import_env, module_roots


class ImportEnvironment(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            mock.patch(
                "sysconfig.get_paths",
                return_value={"purelib": "/installation", "platlib": "/installation"},
            )
        )
        self.packages = []
        for name in ("delegate", "session", "utils"):
            module = ModuleType(f"agent_pm.{name}")
            module.__file__ = f"/checkout/{name}/src/agent_pm/{name}/__init__.py"
            self.packages.append(module)
        self.roots = tuple(f"/checkout/{name}/src" for name in ("delegate", "session", "utils"))

    def test_normal_module_and_package_find_the_same_source_root(self):
        module = ModuleType("agent_pm.delegate.engine.config")
        module.__file__ = "/checkout/delegate/src/agent_pm/delegate/engine/config.py"
        module.__spec__ = ModuleSpec(module.__name__, loader=None)
        self.assertEqual(module_roots(module, self.packages[0]), (self.roots[0],))

    def test_bootstrap_paths_are_supplied_in_package_order_without_unrelated_roots(self):
        with (
            mock.patch.object(sys, "path", ["/stdlib"]),
            mock.patch.dict(os.environ, {"PYTHONPATH": "/unrelated"}),
        ):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join(self.roots)}
            )

    def test_roots_available_only_from_pythonpath_still_need_child_bootstrap(self):
        with (
            mock.patch.object(sys, "path", list(self.roots)),
            mock.patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join(self.roots)}),
        ):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join(self.roots)}
            )

    def test_installed_shared_root_needs_no_pythonpath(self):
        for module in self.packages:
            module.__file__ = (
                f"/installation/agent_pm/{module.__name__.rsplit('.', 1)[-1]}/__init__.py"
            )
        with (
            mock.patch.object(sys, "path", ["/stdlib", "/installation"]),
            mock.patch.dict(os.environ, {"PYTHONPATH": "/unrelated"}),
        ):
            self.assertEqual(import_env(*self.packages), {})

    def test_manually_bootstrapped_source_paths_still_need_child_bootstrap(self):
        with (
            mock.patch.object(sys, "path", list(self.roots)),
            mock.patch.dict(os.environ, {"PYTHONPATH": ""}),
        ):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join(self.roots)}
            )

    def test_mixed_installation_needs_all_supplied_roots(self):
        with (
            mock.patch.object(sys, "path", list(self.roots[:-1])),
            mock.patch.dict(os.environ, {"PYTHONPATH": ""}),
        ):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join(self.roots)}
            )
