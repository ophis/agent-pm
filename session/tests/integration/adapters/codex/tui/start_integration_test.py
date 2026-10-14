"""adapters/codex/tui/start.py's isolated scripts run as their own processes."""

import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from agent_pm.session.adapters.codex import tui
from agent_pm.session.adapters.codex.common import app_server
from agent_pm.session.adapters.codex.tui import start as tui_start

from session.tests import case
from session.tests.case import Case
from session.tests.source_roots import source_roots_cases
from session.tests.unit.adapters.codex.support import Base

INSPECT = """import dataclasses, json, runpy, sys, typing
script, data = sys.argv[1:]
sys.argv = [script, data]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as error:
    status = error.code
from agent_pm.session.adapters import common as shared
from agent_pm.session.adapters.codex import common, tui
from agent_pm.session.adapters.codex.common import app_server
from agent_pm.session.adapters.codex.common import types
from agent_pm.session.adapters.codex.tui import start
roots = json.loads(data)["import_roots"]
before = list(sys.path)
start._bootstrap([data])
print(json.dumps([
    status,
    sys.path[:len(roots)] == roots,
    all(sys.path.count(root) == 1 for root in roots),
    sys.path == before,
    type(next(shared.scan(["--add-dir=/x"], common.VALUED))) is shared.Flag,
    dataclasses.is_dataclass(common.SessionState),
    common.SessionState is types.SessionState,
    typing.get_type_hints(common.SessionState)["snapshot"] is types.Snapshot,
    start.common is common,
    tui.SessionStore is common.SessionStore,
    tui.RecoveryError is common.RecoveryError,
    start.app_server is app_server,
    start.HOOKS is tui.HOOKS,
    tui.on_hook.__module__ == tui.__name__,
    sorted(name for name in sys.modules if name.startswith("_agent_pm_codex_")),
    common.__file__,
    sys.modules["agent_pm.utils"].__file__,
]))
"""


class BootstrapTest(Case):
    def data(self, roots=None):
        return {
            "executable": sys.executable,
            "selection": [],
            "prompt": "",
            "hook": [sys.executable],
            "workdir": self.home,
            "sid": "bootstrap",
            "store": "/uncreated-agent-pm-state/recovery/codex",
            "import_roots": roots or [case.SRC["session"], case.SRC["utils"]],
            "protect": [],
        }

    def isolated(self, data, *, code=None, cwd=None, env=None):
        command = (
            [sys.executable, "-I", "-c", code, tui_start.__file__, json.dumps(data)]
            if code is not None
            else [sys.executable, "-I", tui_start.__file__, json.dumps(data)]
        )
        return subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env)

    def test_isolated_launcher_uses_canonical_modules_dataclasses_and_flag_type(self):
        data = self.data()
        result = self.isolated(data, code=INSPECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "codex tui: Codex tui launch: missing recovery state\n")
        values = json.loads(result.stdout)
        self.assertEqual(values[:15], [1, *([True] * 13), []])
        self.assertTrue(values[15].startswith(data["import_roots"][0] + os.sep))
        self.assertTrue(values[16].startswith(data["import_roots"][1] + os.sep))

    def test_isolated_launcher_decodes_launch_data_once(self):
        code = """import json, runpy, sys
from unittest import mock
script, data = sys.argv[1:]
sys.argv = [script, data]
with mock.patch("json.loads", wraps=json.loads) as decode:
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as error:
        status = error.code
print(json.dumps([status, decode.call_count]))
"""
        result = self.isolated(self.data(), code=code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "codex tui: Codex tui launch: missing recovery state\n")
        self.assertEqual(json.loads(result.stdout), [1, 1])

    def test_supplied_alternate_layout_ignores_cwd_and_pythonpath_decoys(self):
        layout = pathlib.Path(self.home) / "layout"
        layout.mkdir()
        roots = []
        for package in ("session", "utils"):
            root = layout / (package + "-python")
            root.symlink_to(case.SRC[package], target_is_directory=True)
            roots.append(str(root))
        decoy = pathlib.Path(self.home) / "untrusted"
        decoy.mkdir()
        (decoy / "agent_pm.py").write_text('raise RuntimeError("untrusted agent_pm")\n')
        (decoy / "common.py").write_text('raise RuntimeError("untrusted sessions")\n')
        result = self.isolated(
            self.data(roots),
            code=INSPECT,
            cwd=decoy,
            env={**os.environ, "PYTHONPATH": str(decoy)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "codex tui: Codex tui launch: missing recovery state\n")
        values = json.loads(result.stdout)
        self.assertEqual(values[:15], [1, *([True] * 13), []])
        self.assertTrue(values[15].startswith(roots[0] + os.sep))
        self.assertTrue(values[16].startswith(roots[1] + os.sep))

    def test_package_imports_keep_sys_path_and_share_canonical_modules(self):
        code = """import json, sys
before = list(sys.path)
from agent_pm.session.adapters import common as shared
from agent_pm.session.adapters.codex import common, tui
from agent_pm.session.adapters.codex.tui import start as tui_start
print(json.dumps([
    sys.path == before,
    type(next(shared.scan(["--add-dir=/x"], common.VALUED))) is shared.Flag,
    tui_start.common is common,
    tui.SessionStore is common.SessionStore,
    sorted(name for name in sys.modules if name.startswith("_agent_pm_codex_")),
]))
"""
        result = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [True, True, True, True, []])

    def test_bootstrap_rejects_missing_or_invalid_import_roots_before_package_imports(self):
        data = self.data()
        _, invalid = source_roots_cases(self.home)
        cases = [({key: value for key, value in data.items() if key != "import_roots"}, "missing")]
        cases.extend(({**data, "import_roots": value}, name) for name, value in invalid.items())
        for launch, value in cases:
            with self.subTest(roots=value):
                result = self.isolated(launch)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("codex tui: Codex tui launch:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("No module named", result.stderr)

    def test_valid_import_roots_reach_launch_validation(self):
        valid, _ = source_roots_cases(self.home)
        for name, roots in valid.items():
            with self.subTest(roots=name):
                result = self.isolated(self.data(roots))
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (1, "", "codex tui: Codex tui launch: missing recovery state\n"),
                )

    def test_run_by_path_reports_bad_launch_data_without_a_traceback(self):
        result = subprocess.run(
            [sys.executable, "-I", tui_start.__file__, "not-json"], capture_output=True, text=True
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (1, "", "codex tui: Codex tui launch: malformed launch data\n"),
        )

    def test_run_by_path_requires_launch_argument_and_bounds_it_before_decoding(self):
        result = subprocess.run(
            [sys.executable, "-I", tui_start.__file__], capture_output=True, text=True
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (1, "", "codex tui: Codex tui launch: expected bounded launch data\n"),
        )
        with (
            mock.patch("json.loads") as decode,
            self.assertRaisesRegex(tui_start.StartError, "expected bounded launch data"),
        ):
            tui_start._bootstrap([" " * (tui_start.ANSWER_MAX + 1)])
        decode.assert_not_called()

    def test_bootstrap_reports_lone_surrogate_as_one_line_and_failure_status(self):
        code = """import runpy, sys
sys.argv = [sys.argv[1], chr(0xD800)]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, tui_start.__file__],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "codex tui: 'utf-8' codec can't encode character '\\ud800' in position 0: surrogates not allowed\n",
        )


class StartTest(Base):
    def test_shared_schema_preserves_hooks_in_isolated_scripts_and_adapter(self):
        expected = (
            ("SessionStart", "sessionStart", "activity"),
            ("UserPromptSubmit", "userPromptSubmit", "activity"),
            ("PostToolUse", "postToolUse", "activity"),
            ("PermissionRequest", "permissionRequest", "blocked"),
            ("Stop", "stop", "turn_end"),
            ("Interrupt", "interrupt", "turn_end"),
        )
        self.assertEqual(tui.HOOKS, expected)
        self.assertEqual(self.adapter.tui().hooks, {label: kind for label, _, kind in expected})
        self.assertEqual(tui_start.HOOKS, expected)
        self.assertEqual(set(tui.CodexTui.hooks), {label for label, _, _ in expected})
        code = """import json, sys
sys.path[:0] = json.loads(sys.argv[1])
from agent_pm.session.adapters.codex.tui import HOOKS
print(json.dumps(HOOKS))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                code,
                json.dumps([case.SRC["session"], case.SRC["utils"]]),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [list(row) for row in expected])

    def test_standalone_account_probe_uses_stdlib(self):
        script = """import json, runpy, sys
imports = set()
def record(event, args):
    if event == "import":
        imports.add(args[0].partition(".")[0])
sys.addaudithook(record)
runpy.run_path(sys.argv[1])
print(json.dumps(sorted((imports & sys.modules.keys()) - sys.stdlib_module_names)))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(pathlib.Path(app_server.__file__))],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
