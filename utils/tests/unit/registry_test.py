"""utils/registry.py: load. Test modules are files in a temp dir, found by a finder on sys.meta_path for the test's
span (guard import_hygiene counts a patched sys.path as a mutation)."""

import importlib.machinery
import os
import sys
import unittest
from collections.abc import Sized
from unittest import mock

from agent_pm.utils import registry

from utils.tests.case import Case

MODULE = """from collections.abc import Sized


class Ok(Sized):
    def __len__(self):
        return 0


class Half(Sized):
    pass


class Other:
    pass


def function():
    return Ok


VALUE = 1
"""
ABSENT = "agent_pm_registry_absent"


class TempFinder:
    def __init__(self, folder):
        self.folder = folder

    def find_spec(self, fullname, path=None, target=None):
        if path is not None:
            return None
        return importlib.machinery.PathFinder.find_spec(fullname, [self.folder])


class Load(Case):
    def setUp(self):
        for name in ("regpkg", "regpkg.impl", "regbroken"):
            self.addCleanup(sys.modules.pop, name, None)
        self.folder = os.path.join(self.home, "modules")
        os.makedirs(os.path.join(self.folder, "regpkg"))
        self.write("regpkg/__init__.py", "")
        self.write("regpkg/impl.py", MODULE)
        self.write("regbroken.py", "def broken(:\n")
        for patch in (
            mock.patch.dict(sys.path_importer_cache),
            mock.patch.object(sys, "meta_path", [TempFinder(self.folder), *sys.meta_path]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, name, text):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def refused(self, path, message, base=Sized):
        with self.assertRaises(ValueError) as caught:
            registry.load(path, base)
        self.assertEqual(str(caught.exception), message)

    def test_loads_a_concrete_subclass(self):
        cls = registry.load("regpkg.impl:Ok", Sized)
        self.assertIs(cls, sys.modules["regpkg.impl"].Ok)
        self.assertEqual(len(cls()), 0)

    def test_path_syntax(self):
        self.assertTrue(registry.PATH.fullmatch("a_1.b2._c:D_3"))
        for path in (
            "regpkg.impl.Ok",
            "regpkg.impl:",
            ":Ok",
            "regpkg..impl:Ok",
            "regpkg.impl:Ok.x",
            "1pkg:Ok",
            "regpkg/impl:Ok",
            "regpkg.impl:Ok\n",
            "",
            None,
        ):
            with self.subTest(path=path):
                self.refused(path, f"{path}: want module.path:Class")

    def test_a_module_that_cannot_import(self):
        self.refused(
            f"{ABSENT}:Ok", f"{ABSENT}:Ok: cannot import {ABSENT}: No module named '{ABSENT}'"
        )
        with self.assertRaises(ValueError) as caught:
            registry.load("regbroken:Ok", Sized)
        self.assertTrue(
            str(caught.exception).startswith("regbroken:Ok: cannot import regbroken: "),
            caught.exception,
        )
        self.assertIsInstance(caught.exception.__cause__, SyntaxError)

    def test_a_missing_class(self):
        self.refused("regpkg.impl:Nope", "regpkg.impl:Nope: regpkg.impl has no Nope")

    def test_not_a_concrete_subclass(self):
        for name in ("function", "VALUE", "Other", "Sized", "Half"):
            with self.subTest(name=name):
                path = f"regpkg.impl:{name}"
                self.refused(path, f"{path} must be a concrete Sized subclass")


if __name__ == "__main__":
    unittest.main()
