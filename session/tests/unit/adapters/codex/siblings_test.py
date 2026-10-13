"""Codex path loading preserves private module identity and cleans up failures."""

import json
import os
import subprocess
import sys
from unittest import mock

from agent_pm.session.adapters.codex import siblings

from session.tests.case import Case

DIRECTORY = os.path.dirname(siblings.__file__)


class SiblingsTest(Case):
    def _run_loader(self, code, *args):
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, siblings.__file__, *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_cache_requires_the_same_path_and_keeps_module_identity(self):
        code = """import json, os, runpy, sys, types
load = runpy.run_path(sys.argv[1])["load"]
directory = os.path.dirname(sys.argv[1])
module = load(directory, "hook_schema")
result = [module is load(directory, "hook_schema")]
for path in ("/foreign/hook_schema.py", None):
    foreign = types.ModuleType("_agent_pm_codex_hook_schema")
    if path is not None:
        foreign.__file__ = path
    sys.modules[foreign.__name__] = foreign
    try:
        load(directory, "hook_schema")
    except ImportError as error:
        result.append(str(error))
print(json.dumps(result))
"""
        result = self._run_loader(code)
        self.assertEqual(result[0], True)
        expected = os.path.join(DIRECTORY, "hook_schema.py")
        for index, path in enumerate(("/foreign/hook_schema.py", None), 1):
            self.assertEqual(
                result[index], f"_agent_pm_codex_hook_schema is {path}, not {expected}"
            )

    def test_a_failed_execution_removes_its_private_registration(self):
        fixture = os.path.join(self.home, "broken.py")
        with open(fixture, "w", encoding="utf-8") as file:
            file.write('raise RuntimeError("broken module")\n')
        code = """import importlib.util, json, os, runpy, sys
load = runpy.run_path(sys.argv[1])["load"]
directory = os.path.dirname(sys.argv[1])
original = importlib.util.spec_from_file_location
importlib.util.spec_from_file_location = lambda name, _path: original(name, sys.argv[2])
try:
    load(directory, "broken")
except RuntimeError as error:
    print(json.dumps([str(error), "_agent_pm_codex_broken" in sys.modules]))
"""
        self.assertEqual(self._run_loader(code, fixture), ["broken module", False])

    def test_a_missing_loader_is_reported_as_import_error(self):
        with (
            mock.patch("importlib.util.spec_from_file_location", return_value=None),
            self.assertRaisesRegex(ImportError, "cannot load Python module"),
        ):
            siblings.load(DIRECTORY, "missing")

    def test_dataclass_annotations_resolve_from_the_registered_module(self):
        fixture = os.path.join(self.home, "record.py")
        with open(fixture, "w", encoding="utf-8") as file:
            file.write(
                """from __future__ import annotations
from dataclasses import dataclass
@dataclass
class Record:
    value: int
"""
            )
        code = """import importlib.util, json, os, runpy, sys
load = runpy.run_path(sys.argv[1])["load"]
directory = os.path.dirname(sys.argv[1])
original = importlib.util.spec_from_file_location
importlib.util.spec_from_file_location = lambda name, _path: original(name, sys.argv[2])
module = load(directory, "record")
print(json.dumps([sys.modules["_agent_pm_codex_record"] is module, module.Record(7).value]))
"""
        self.assertEqual(self._run_loader(code, fixture), [True, 7])

    def test_shared_flags_load_from_the_adapters_directory(self):
        scanner = siblings.load(os.path.dirname(DIRECTORY), "flags")
        self.assertEqual(scanner.__file__, os.path.join(os.path.dirname(DIRECTORY), "flags.py"))
        self.assertIs(scanner, siblings.load(os.path.dirname(DIRECTORY), "flags"))
