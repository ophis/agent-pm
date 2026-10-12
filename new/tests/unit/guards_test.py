"""new/tests/guards.py: each guard on the real tree, and on violating samples written to temp trees."""

from new.tests import CORE, NEW, ORCH_SRC, ORCHESTRATOR, SRC, guards

import importlib
import os
import sys
import tempfile
import unittest

OLD = "/".join(("core", "src"))
ORCH_GUARDS = (guards.orchestrator_imports, guards.orchestrator_text, guards.config_once)


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
            *((guard, ORCHESTRATOR) for guard in ORCH_GUARDS),
            (guards.private_imports, ORCHESTRATOR),
            (guards.syntax_311, ORCHESTRATOR),
            (guards.old_core, ORCHESTRATOR),
        ):
            with self.subTest(guard.__name__, root=root):
                self.assertEqual(guard(root), [])

    def test_src_and_orchestrator_never_reach_the_old_core(self):
        old = os.path.realpath(os.path.join(os.path.dirname(NEW), "core", "src"))
        src = os.path.realpath(SRC)
        for top, dirs, files in os.walk(SRC):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(f for f in files if f.endswith(".py")):
                parts = os.path.relpath(os.path.join(top, name), SRC)[:-3].split(os.sep)
                parts = parts[:-1] if parts[-1] == "__init__" else parts
                module = importlib.import_module(".".join(parts))
                self.assertEqual(os.path.commonpath([src, os.path.realpath(module.__file__)]), src)
        orch = os.path.realpath(ORCH_SRC)
        for name in (
            sorted(f for f in os.listdir(ORCH_SRC) if f.endswith(".py"))
            if os.path.isdir(ORCH_SRC)
            else []
        ):
            module = importlib.import_module(name[:-3])
            self.assertEqual(os.path.dirname(os.path.realpath(module.__file__)), orch)
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
        root = self.tree({"src/delegate/x.py": "from driver.modes import headless\n"})
        self.assertEqual(
            guards.layer_imports(root), ["src/delegate/x.py: delegate imports driver.modes"]
        )

    def test_a_relative_import_is_resolved(self):
        root = self.tree({"src/composer/x.py": "from ..driver import api\n"})
        self.assertEqual(
            guards.layer_imports(root), ["src/composer/x.py: composer imports driver.api"]
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                "src/delegate/x.py": "from driver.api import start\nfrom composer import y\n",
                "src/driver/x.py": "import adapters.base\nfrom .modes import headless\n"
                "from driver.modes import headless\n",
                "src/x.py": "from driver import api\nimport driver.api\nfrom driver.api import start\n",
            }
        )
        self.assertEqual(guards.layer_imports(root), [])

    def test_outside_the_layers_reaches_only_driver_api(self):
        root = self.tree(
            {"src/x.py": "import adapters\n", "src/y.py": "from driver import registry\n"}
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                "src/x.py: outside the layers imports adapters",
                "src/y.py: outside the layers imports driver.registry",
            ],
        )

    def test_nested_and_literal_imports_count(self):
        root = self.tree(
            {
                "src/adapters/x.py": "def f():\n    import importlib\n"
                "    importlib.import_module('driver.registry')\n"
                "    __import__('composer')\n"
                "    from delegate import y\n"
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                "src/adapters/x.py: adapters imports composer",
                "src/adapters/x.py: adapters imports delegate.y",
                "src/adapters/x.py: adapters imports driver.registry",
            ],
        )


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({"src/driver/x.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(
            guards.runtime_names(root),
            ["src/driver/x.py:1: runtime name Claude", "src/driver/x.py:2: runtime name codex"],
        )

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({"src/adapters/claude/x.py": 'NAME = "claude"\n'})
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
        root = self.tree({"src/composer/x.py": sample, "src/driver/x.py": sample})
        self.assertEqual(guards.adapters_key(root), ['src/composer/x.py:2: literal key "adapters"'])


class ModesImportApi(Sample):
    def test_a_mode_importing_driver_api_any_way_is_a_violation(self):
        root = self.tree(
            {
                "src/driver/modes/a.py": "from driver import api\n",
                "src/driver/modes/b.py": "import driver.api\n",
                "src/driver/modes/c.py": "from ..api import DriverError\n",
                "src/driver/modes/d.py": "def f():\n    from .. import api\n",
                "src/driver/modes/__init__.py": "__import__('driver.api')\n",
            }
        )
        self.assertEqual(
            guards.modes_import_api(root),
            [
                "src/driver/modes/__init__.py: imports driver.api",
                "src/driver/modes/a.py: imports driver.api",
                "src/driver/modes/b.py: imports driver.api",
                "src/driver/modes/c.py: imports driver.api",
                "src/driver/modes/d.py: imports driver.api",
            ],
        )

    def test_driver_errors_in_a_mode_and_driver_api_outside_modes_are_clean(self):
        root = self.tree(
            {
                "src/driver/modes/a.py": "from driver.errors import DriverError\nfrom driver import records\n"
                "from ..errors import DriverError\n",
                "src/driver/x.py": "from driver import api\n",
                "src/y.py": "import driver.api\n",
            }
        )
        self.assertEqual(guards.modes_import_api(root), [])


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                "src/driver/tmux_host.py": sample,
                "src/driver/modes/x.py": sample,
                "src/composer/y.py": 'TOOL = "tmux"\n',
                "tests/z.py": sample,
            }
        )
        self.assertEqual(
            guards.tmux_calls(root),
            [
                'src/composer/y.py:1: str constant "tmux"',
                'src/driver/modes/x.py:2: str constant "tmux"',
                'tests/z.py:2: str constant "tmux"',
            ],
        )

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree(
            {"src/driver/x.py": 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'}
        )
        self.assertEqual(guards.tmux_calls(root), [])


class Syntax39(Sample):
    def test_newer_syntax_in_a_standalone_file_is_a_violation(self):
        root = self.tree(
            {
                "src/driver/tmux_host.py": "def f(x: int | None): pass\nmatch x:\n    case _: pass\n",
                "src/driver/api.py": "match x:\n    case _: pass\n",
            }
        )
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("src/driver/tmux_host.py: "), found)

    def test_the_tui_hook_is_standalone_too(self):
        root = self.tree({"src/driver/modes/tui_hook.py": "match x:\n    case _: pass\n"})
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("src/driver/modes/tui_hook.py: "), found)

    def test_3_9_syntax_is_clean(self):
        root = self.tree(
            {
                "src/driver/tmux_host.py": "from __future__ import annotations\n"
                "def f(x: int | None) -> list[str]: return []\n"
            }
        )
        self.assertEqual(guards.syntax_39(root), [])


class PrivateImports(Sample):
    def test_an_underscore_name_from_another_module(self):
        root = self.tree(
            {
                "src/a.py": "from b import _x\nfrom . import _y\n",
                "src/c.py": "import b.d\nfrom e import f as g\n\ndef h():\n    return b.d._z, g._w\n",
            }
        )
        self.assertEqual(
            guards.private_imports(root),
            [
                "src/a.py:1: private b._x",
                "src/a.py:2: private ._y",
                "src/c.py:5: private b.d._z",
                "src/c.py:5: private g._w",
            ],
        )

    def test_an_alias_dunders_and_other_objects_are_clean(self):
        root = self.tree(
            {
                "src/a.py": "import b as _b\nfrom c import d as _d\n_b.e, _d.f, _b.__file__\n\n"
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


class OrchestratorImports(Sample):
    def test_forbidden_imports_name_the_file_and_line(self):
        root = self.tree(
            {
                "src/a.py": "import adapters\nfrom driver.modes import x\nimport tmux_host\nimport repo\n",
                "src/b.py": "from delegate import runner\nfrom delegate.outcome import Outcome\nimport core\n"
                "import attended\n",
                "src/run.py": "from composer import resolve\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                "src/a.py:1: imports adapters",
                "src/a.py:2: imports driver.modes.x",
                "src/a.py:3: imports tmux_host",
                "src/a.py:4: imports repo",
                "src/b.py:1: imports delegate.runner",
                "src/b.py:2: imports delegate.outcome.Outcome",
                "src/b.py:3: imports core",
                "src/b.py:4: imports attended",
                "src/run.py:1: imports composer.resolve",
            ],
        )

    def test_submodules_by_attribute_other_forms_and_module_names(self):
        root = self.tree(
            {
                "src/a.py": "import delegate as d\nd.runner.run()\nimport driver.api\nfrom . import b\n"
                "import third_party\n__import__('adapters.base')\n",
                "src/config.py": "import composer\ncomposer.run.resolve()\n",
                "src/utils.py": "",
                "src/attended.py": "",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                "src/a.py:2: uses delegate.runner",
                "src/a.py:3: imports driver.api",
                "src/a.py:4: imports .b",
                "src/a.py:5: imports third_party",
                "src/a.py:6: imports adapters.base",
                "src/attended.py: module named attended",
                "src/config.py:2: uses composer.run",
                "src/utils.py: module named utils",
            ],
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                "src/config.py": "import os\nfrom composer import resolve\nimport composer\n"
                "composer.index()\nfrom utils.files import save\n",
                "src/run.py": "import config\nfrom delegate import run, Result\nimport delegate\n"
                "delegate.Begun\nfrom driver import api\nimport utils.shell\n",
            }
        )
        self.assertEqual(guards.orchestrator_imports(root), [])


class OrchestratorText(Sample):
    def test_runtime_names_and_tool_strings_name_the_file_and_line(self):
        root = self.tree(
            {
                "src/router.py": 'X = "codex"\n',
                "src/a.py": 'import subprocess\nsubprocess.run(["tmux", "ls"])\nK = "adapters"\n# Claude\n',
                "config.toml": '# claude\nx = "tmux"\ny = \'adapters\'\nz = "tmux ls"  # "tmux"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(root),
            [
                "config.toml:1: runtime name claude",
                'config.toml:2: str constant "tmux"',
                'config.toml:3: str constant "adapters"',
                'src/a.py:2: str constant "tmux"',
                'src/a.py:3: str constant "adapters"',
                "src/a.py:4: runtime name Claude",
                "src/router.py:1: runtime name codex",
            ],
        )

    def test_claude_md_and_longer_strings_are_clean(self):
        root = self.tree(
            {
                "src/a.py": '"""Reads CLAUDE.md."""\nP = "CLAUDE.md"\nH = "tmux attach"\n',
                "config.toml": '# see CLAUDE.md\nx = "tmux attach"\n',
            }
        )
        self.assertEqual(guards.orchestrator_text(root), [])

    def test_handoff_literals_only_in_issues(self):
        root = self.tree(
            {
                "src/issues.py": 'H = "Handoff from"\nS = ("## Source", "## Instructions", "## Comments")\n',
                "src/promote.py": 'x = f"Handoff from {i}: {u}"\n# ## Comments are quoted\n',
                "src/target.py": 'M = "or its ## Instructions"\nS = "## Source"\n',
                "config.toml": 'x = "## Comments"\n',
                "tests/a_test.py": 'X = "Handoff from ## Source"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(root),
            [
                "src/promote.py:1: Handoff literal Handoff from",
                "src/promote.py:2: Handoff literal ## Comments",
                "src/target.py:1: Handoff literal ## Instructions",
                "src/target.py:2: Handoff literal ## Source",
            ],
        )


class ConfigOnce(Sample):
    def test_load_and_use_logs_outside_an_entry_main(self):
        root = self.tree(
            {
                "src/run.py": "import config\nimport linear\n\ndef main():\n    config.load()\n"
                "    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                "src/writeback.py": "import config\nfrom linear import use_logs\n\ndef main():\n"
                "    config.load()\n    use_logs(None)\n",
                "src/promote.py": "import config as c\n\nclass P:\n    def main(self):\n        c.load()\n",
            }
        )
        self.assertEqual(
            guards.config_once(root),
            [
                "src/promote.py:5: calls config.load outside an entry main",
                "src/run.py:9: calls config.load outside an entry main",
                "src/writeback.py:5: calls config.load outside an entry main",
                "src/writeback.py:6: calls linear.use_logs outside an entry main",
            ],
        )

    def test_load_helpers_outside_config_py(self):
        root = self.tree(
            {
                "src/run.py": "import config\nconfig.runnable(1)\n",
                "src/config.py": "def runnable():\n    pass\n\ndef load():\n    runnable()\n    docs()\n",
                "src/promote.py": "import config\nimport linear\n\ndef main():\n    try:\n"
                "        cfg = config.load()\n    except SystemExit:\n"
                "        linear.use_logs(None)\n",
            }
        )
        self.assertEqual(guards.config_once(root), ["src/run.py:2: calls config.runnable"])


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in ORCH_GUARDS:
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


if __name__ == "__main__":
    unittest.main()
