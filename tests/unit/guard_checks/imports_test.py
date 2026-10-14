from pathlib import Path

from tests import guards
from tests.case import REPO
from tests.guard_checks import imports
from tests.guard_data import DELEGATE_SESSION_FORWARDING_ALLOWED, IMPORT_BOOTSTRAPS, STANDALONE
from tests.guard_data.linear_orchestrator import ORCH_PKG, ORCH_SESSION_CONTRACT, ORCH_SRC
from tests.guard_data.session import SESSION_SRC, TMUX_HOST
from tests.guard_samples import PKG, Sample


class DelegateSessionForwarding(Sample):
    BASE = "delegate/src/agent_pm/delegate/"

    def references(self, sample, rel="engine/runner.py"):
        root = self.tree({self.BASE + rel: sample})
        return [
            item
            for item in imports.delegate_session_forwarding(root)
            if item.startswith(self.BASE + rel + ":")
            and "allowlisted but no forwarding reference" not in item
        ]

    def test_session_wrappers_are_rejected_even_when_aliased(self):
        for wrapper in ("Driver", "Handle", "close", "note", "session_record", "sessions"):
            for sample in (
                f"from agent_pm.session import {wrapper} as old\nold()\n",
                f"import agent_pm.session as s\ns.{wrapper}()\n",
                f"from agent_pm import session as s\nold = s.{wrapper}\nold()\n",
                f"from agent_pm.session.engine.forwarding import {wrapper} as old\nold()\n",
            ):
                with self.subTest(wrapper=wrapper, sample=sample):
                    self.assertTrue(self.references(sample))

    def test_object_methods_and_delegate_own_driver_pass(self):
        sample = (
            "from agent_pm.session import Session, RunLog\n"
            "from agent_pm.delegate.engine.drivers import Driver\n"
            "Session.attach('name').close()\nRunLog('workdir').note('kind')\nDriver()\n"
        )
        self.assertEqual(self.references(sample), [])

    def test_compatibility_exception_is_exact_and_expires(self):
        public = self.BASE + "__init__.py", "agent_pm.session.Driver"
        self.assertEqual(set(DELEGATE_SESSION_FORWARDING_ALLOWED), {public})
        self.assertIn("configure()", DELEGATE_SESSION_FORWARDING_ALLOWED[public])
        self.assertIn("356-14", DELEGATE_SESSION_FORWARDING_ALLOWED[public])
        sample = (
            "from agent_pm.session import Driver as SessionDriver\nSessionDriver.from_config({})\n"
        )
        self.assertEqual(self.references(sample, "__init__.py"), [])
        for rel in ("engine/plan.py", "cli.py"):
            with self.subTest(rel=rel):
                self.assertTrue(self.references(sample, rel))
        found = imports.delegate_session_forwarding(self.tree({}))
        self.assertEqual(
            found, [f"{public[0]}: {public[1]}: allowlisted but no forwarding reference (FR-34)"]
        )


class OrchestratorSessionImports(Sample):
    def check(self, module, sample, config=None):
        root = self.tree(
            {
                "pyproject.toml": config or Path(REPO, "pyproject.toml").read_text(),
                f"linear-orchestrator/{ORCH_SRC}{module}": sample,
            }
        )
        return imports.orchestrator_session_imports(root)

    def message(self, rel, name):
        module = f"{ORCH_PKG}.{rel.removesuffix('.py').replace('/', '.')}"
        return (
            f"linear-orchestrator/{ORCH_SRC}{rel}: {module} imports {name} from "
            "agent_pm.session; go through agent_pm.delegate"
        )

    def test_delegate_import_passes(self):
        self.assertEqual(self.check("engine/run.py", "from agent_pm import delegate\n"), [])

    def test_formerly_allowed_module_fails(self):
        self.assertEqual(
            self.check("engine/run_comments.py", "from agent_pm.session import DriverError\n"),
            [self.message("engine/run_comments.py", "DriverError")],
        )

    def test_a_new_module_fails_however_it_imports(self):
        for sample in (
            "from agent_pm.session import Driver\n",
            "import agent_pm.session as s\ns.Driver()\n",
            "from agent_pm import session\nsession.Driver()\n",
            "import agent_pm.session\nagent_pm.session.Driver.run()\n",
            "def f():\n    from agent_pm.session import Driver\n",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(
                    self.check("engine/promote.py", sample),
                    [self.message("engine/promote.py", "Driver")],
                )

    def test_pyproject_ignored_imports_cannot_grow(self):
        config = Path(REPO, "pyproject.toml").read_text()
        edge = '    "agent_pm.linear_orchestrator.engine.prune -> agent_pm.session",\n'
        config = config.replace("ignore_imports = [\n", "ignore_imports = [\n" + edge, 1)
        self.assertEqual(
            self.check("engine/run.py", "", config),
            [f"pyproject.toml: {ORCH_SESSION_CONTRACT!r} must ignore exactly ORCH_SESSION_IMPORTS"],
        )


class LiteralImports(Sample):
    def test_unknown_contract_types_fail_loudly(self):
        root = self.tree(
            {
                "pyproject.toml": "[[tool.importlinter.contracts]]\ntype = 'unknown'\n",
                "session/src/agent_pm/session/adapters/probe.py": "__import__('agent_pm.utils')",
            }
        )
        with self.assertRaisesRegex(ValueError, "unsupported import contract type: 'unknown'"):
            imports.literal_imports(root)

    def check(self, sample, project="session", module="adapters/probe.py"):
        config = Path(REPO, "pyproject.toml").read_text()
        root = self.tree(
            {
                "pyproject.toml": config,
                f"{project}/src/agent_pm/{guards.PROJECTS[project]}/{module}": sample,
            }
        )
        return imports.literal_imports(root)

    def test_dynamic_calls_cannot_bypass_unit_contracts(self):
        for sample in (
            "import importlib\nimportlib.import_module('agent_pm.session.hosts.tmux.tmux_host')",
            "from importlib import import_module as load\nload('agent_pm.session.modes.tui')",
            "__import__('agent_pm.manager')",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(len(self.check(sample)), 1)

    def test_lower_public_packages_and_variable_adapter_names_are_allowed(self):
        self.assertEqual(
            self.check(
                "import importlib\nimportlib.import_module('agent_pm.utils')\nimportlib.import_module(implementation)"
            ),
            [],
        )
        self.assertEqual(
            self.check(
                "importlib.import_module('agent_pm.session')", "delegate", "engine/probe.py"
            ),
            [],
        )

    def test_lower_internals_and_pure_config_imports_are_rejected(self):
        self.assertEqual(
            self.check("__import__('agent_pm.session.engine.api')", "delegate", "engine/probe.py"),
            [
                "delegate/src/agent_pm/delegate/engine/probe.py:1: imports agent_pm.session.engine.api"
            ],
        )
        self.assertEqual(
            self.check(
                "__import__('agent_pm.delegate.engine.future')", "delegate", "engine/config.py"
            ),
            [
                "delegate/src/agent_pm/delegate/engine/config.py:1: imports agent_pm.delegate.engine.future"
            ],
        )


class PrivateImports(Sample):
    def test_documented_os_exit_is_public_despite_its_underscore(self):
        root = self.tree(
            {
                "a.py": "import os as system\nfrom os import _exit\nsystem._exit(0)\n_exit(0)\nsystem._secret()\n"
            }
        )
        self.assertEqual(imports.private_imports(root), ["a.py:5: private system._secret"])

    def test_an_underscore_name_from_another_module(self):
        root = self.tree(
            {
                f"{PKG}/a.py": "from b import _x\nfrom . import _y\n",
                f"{PKG}/c.py": (
                    "import b.d\nfrom e import f as g\n\ndef h():\n    return b.d._z, g._w\n"
                ),
            }
        )
        self.assertEqual(
            imports.private_imports(root),
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
                f"{PKG}/a.py": "import b as _b\nfrom c import d as _d\n_b.e, _d.f, _b.__file__\n\nclass G:\n    def h(self, i):\n        return self._j, i._k\n"
            }
        )
        self.assertEqual(imports.private_imports(root), [])


class ImportHygiene(Sample):
    def test_bootstrap_owners_may_prepend_roots_in_their_startup_function(self):
        root = self.tree(
            {
                path: (
                    f"import sys\ndef {function}(roots):\n"
                    "    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]\n"
                )
                for path, function in IMPORT_BOOTSTRAPS.items()
            }
        )
        self.assertEqual(imports.import_hygiene(root), [])

    def test_bootstrap_permission_does_not_cover_other_scopes(self):
        assignment = "sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]"
        for path, function in IMPORT_BOOTSTRAPS.items():
            with self.subTest(path=path):
                root = self.tree(
                    {
                        path: (
                            f"import sys\n{assignment}\ndef {function}(roots):\n"
                            f"    {assignment}\n"
                            f"    def nested():\n        {assignment}\n"
                            f"def other(roots):\n    {assignment}\n"
                            f"class Other:\n    def {function}(self, roots):\n"
                            f"        {assignment}\n"
                        )
                    }
                )
                self.assertEqual(
                    imports.import_hygiene(root),
                    sorted(f"{path}:{line}: sys.path mutation" for line in (2, 6, 8, 11)),
                )

    def test_bootstrap_permission_covers_only_one_root_prefix_assignment(self):
        for path, function in IMPORT_BOOTSTRAPS.items():
            with self.subTest(path=path):
                root = self.tree(
                    {
                        path: (
                            f"import sys\ndef {function}(roots):\n"
                            "    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]\n"
                            "    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]\n"
                            "    sys.path[:0] = ['arbitrary']\n    sys.path[:] = roots\n"
                            "    sys.path[0:0] = roots\n    sys.path[:False] = roots\n"
                            "    sys.path.append(roots)\n    sys.path.insert(0, roots)\n"
                            "    sys.path = roots\n    sys.path += roots\n"
                        )
                    }
                )
                self.assertEqual(
                    imports.import_hygiene(root),
                    sorted(f"{path}:{line}: sys.path mutation" for line in range(4, 13)),
                )

    def test_named_bootstrap_functions_in_other_files_cannot_change_search_paths(self):
        assignment = "sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]"
        root = self.tree(
            {
                "tests/other.py": (
                    f"import sys\ndef _bootstrap(roots):\n    {assignment}\n"
                    f"def main(roots):\n    {assignment}\n"
                )
            }
        )
        self.assertEqual(
            imports.import_hygiene(root),
            ["tests/other.py:3: sys.path mutation", "tests/other.py:5: sys.path mutation"],
        )

    def test_any_sys_path_mutation(self):
        root = self.tree(
            {
                "tests/a.py": (
                    "import sys\nsys.path.insert(0, 'x')\ndef f():\n    sys.path.append('x')\n"
                ),
                "tests/b.py": "import sys\nsys.path = []\nsys.path[0] = 'x'\nsys.path += ['x']\ndel sys.path[0]\n",
                "tests/c.py": (
                    "import sys as s\nfrom sys import path as p\nfrom unittest import mock\ns.path.extend([])\np.remove('x')\np[:0] = ['x']\np += ['x']\nmock.patch('sys.path', [])\n@mock.patch.object(s, 'path', [])\ndef g(): pass\n"
                ),
                "tests/d.py": "import sys\nfrom sys import path\nfrom unittest import mock\nx = [sys.path[0], sys.path.index('x'), list(sys.path), sys.path.copy()]\npath = []\nmock.patch('os.path.join')\nmock.patch.object(sys, 'argv', [])\n",
            }
        )
        self.assertEqual(
            imports.import_hygiene(root),
            [f"{at}: sys.path mutation" for at in ("tests/a.py:2", "tests/a.py:4")]
            + [f"tests/b.py:{n}: sys.path mutation" for n in range(2, 6)]
            + [f"tests/c.py:{n}: sys.path mutation" for n in range(4, 10)],
        )

    def test_a_module_level_environment_write(self):
        root = self.tree(
            {
                "tests/a.py": "import os\nos.environ['A'] = '1'\nos.environ.update(A='1')\ndel os.environ['A']\nos.environ |= {}\nos.putenv('A', '1')\n",
                "tests/b.py": (
                    "import os as o\nfrom os import environ, putenv\nfrom unittest import mock\nclass C:\n    environ.setdefault('A', '1')\nif True:\n    putenv('A', '1')\nmock.patch.dict(environ, {'A': '1'}).start()\no.environ.pop('A')\n"
                ),
                "tests/c.py": (
                    "import os\nfrom unittest import mock\nA = os.environ.get('A', os.environ['B'])\ndef f():\n    os.environ['A'] = '1'\n    os.putenv('A', '1')\ng = lambda: os.environ.clear()\n@mock.patch.dict(os.environ, {'A': '1'})\nclass D:\n    @mock.patch.dict(os.environ, {'A': '1'})\n    def h(self): pass\n"
                ),
            }
        )
        self.assertEqual(
            imports.import_hygiene(root),
            [f"tests/a.py:{n}: module-level environment write" for n in range(2, 7)]
            + [f"tests/b.py:{n}: module-level environment write" for n in (5, 7, 8, 9)],
        )


class StandaloneImports(Sample):
    def test_tui_hook_bootstraps_package_imports(self):
        path = f"{SESSION_SRC}modes/tui_hook.py"
        self.assertNotIn(path, STANDALONE)
        self.assertEqual(IMPORT_BOOTSTRAPS[path], "_bootstrap")

    def test_tmux_host_uses_package_imports(self):
        self.assertNotIn(TMUX_HOST, STANDALONE)

    def test_tmux_command_bootstraps_supplied_package_roots(self):
        path = f"{SESSION_SRC}hosts/tmux/entry.py"
        self.assertNotIn(path, STANDALONE)
        self.assertEqual(IMPORT_BOOTSTRAPS[path], "_bootstrap")

    def found(self, source):
        files = dict.fromkeys(STANDALONE, source)
        return imports.standalone_imports(self.tree(files))

    def test_stdlib_imports_aliases_and_nested_imports_are_clean(self):
        self.assertEqual(
            self.found(
                "from __future__ import annotations\nimport os.path as path, sys\nfrom collections.abc import Mapping\nimport builtins\ndef load():\n    import json as j\n    from pathlib import Path\n"
            ),
            [],
        )

    def test_third_party_own_package_and_relative_imports_are_reported(self):
        source = "import requests as r, agent_pm.utils\nfrom agent_pm.session import Driver\nfrom . import sibling\ndef load():\n    from unknown.child import thing\n"
        self.assertEqual(
            self.found(source),
            sorted(
                f"{path}:{line}: non-stdlib import {name}"
                for path in STANDALONE
                for line, name in (
                    (1, "requests"),
                    (1, "agent_pm.utils"),
                    (2, "agent_pm.session"),
                    (3, "."),
                    (5, "unknown.child"),
                )
            ),
        )

    def test_missing_inventory_file_is_reported(self):
        missing = min(STANDALONE)
        root = self.tree({path: "" for path in STANDALONE if path != missing})
        self.assertEqual(
            imports.standalone_imports(root), [f"{missing}: missing standalone script"]
        )

    def test_every_branch_is_checked_unconditionally(self):
        source = """if __package__:
    from agent_pm.session.adapters import flags
else:
    import runpy
    import requests
import agent_pm.utils
"""
        self.assertEqual(
            self.found(source),
            sorted(
                f"{path}:{line}: non-stdlib import {name}"
                for path in STANDALONE
                for line, name in (
                    (2, "agent_pm.session.adapters"),
                    (5, "requests"),
                    (6, "agent_pm.utils"),
                )
            ),
        )

    def test_nested_or_other_conditions_do_not_hide_standalone_imports(self):
        source = """def load():
    if __package__:
        import agent_pm.utils
if __package__ is not None:
    import requests
if not __package__:
    import other
"""
        self.assertEqual(
            self.found(source),
            sorted(
                f"{path}:{line}: non-stdlib import {name}"
                for path in STANDALONE
                for line, name in ((3, "agent_pm.utils"), (5, "requests"), (7, "other"))
            ),
        )

    def test_unlisted_module_is_not_a_standalone_script(self):
        files = dict.fromkeys(STANDALONE, "")
        files[f"{SESSION_SRC}engine/ordinary.py"] = "import agent_pm.utils\n"
        self.assertEqual(imports.standalone_imports(self.tree(files)), [])


class OldCore(Sample):
    def test_a_core_import_in_a_tree(self):
        root = self.tree(
            {
                "tests/a.py": "from core.src import drive\n",
                "session/b.py": "import core\nimport core_x\n",
                ".github/c.py": "import core\n",
                "docs/d.py": "import core\n",
            }
        )
        self.assertEqual(
            imports.old_core(root),
            [
                ".github/c.py: imports core",
                "session/b.py: imports core",
                "tests/a.py: imports core",
            ],
        )
