from pathlib import Path

from tests import guards
from tests.case import REPO
from tests.guard_checks import imports
from tests.guard_data import STANDALONE
from tests.guard_data.linear_orchestrator import ORCH_PKG, ORCH_SESSION_CONTRACT, ORCH_SRC
from tests.guard_data.session import SESSION_SRC, TMUX_HOST
from tests.guard_samples import PKG, Sample


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

    def test_only_delegate_imports_pass_however_a_module_imports_the_session(self):
        for module, sample, expected in (
            ("engine/run.py", "from agent_pm import delegate\n", []),
            (
                "engine/run_comments.py",
                "from agent_pm.session import SessionError\n",
                [self.message("engine/run_comments.py", "SessionError")],
            ),
            *(
                ("engine/promote.py", sample, [self.message("engine/promote.py", "RunLog")])
                for sample in (
                    "from agent_pm.session import RunLog\n",
                    "import agent_pm.session as s\ns.RunLog()\n",
                    "from agent_pm import session\nsession.RunLog()\n",
                    "import agent_pm.session\nagent_pm.session.RunLog.note()\n",
                    "def f():\n    from agent_pm.session import RunLog\n",
                )
            ),
        ):
            with self.subTest(module=module, sample=sample):
                self.assertEqual(self.check(module, sample), expected)

    def test_pyproject_ignores_no_imports(self):
        config = Path(REPO, "pyproject.toml").read_text()
        contract = f'name = "{ORCH_SESSION_CONTRACT}"\n'
        self.assertIn(contract, config)
        edge = (
            'ignore_imports = ["agent_pm.linear_orchestrator.engine.prune -> agent_pm.session"]\n'
        )
        config = config.replace(contract, contract + edge, 1)
        self.assertEqual(
            self.check("engine/prune.py", "", config),
            [f"pyproject.toml: {ORCH_SESSION_CONTRACT!r} must ignore no imports"],
        )


class OrchestratorImports(Sample):
    """FR-3: linear_orchestrator imports only the standard library, itself, agent_pm.utils and the
    names delegate's and selector's __all__ export; no other package imports it.
    """

    DELEGATE = "from agent_pm.delegate.engine.base import Delegate\nclass HarnessDelegate: pass\n"
    DELEGATE_INIT = DELEGATE + '__all__ = ["Delegate", "ConfigError"]\n'
    SELECTOR_INIT = 'from agent_pm.selector.engine.base import Selector\n__all__ = ["Selector"]\n'

    def check(self, files):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/__init__.py": self.DELEGATE_INIT,
                f"selector/{PKG}/selector/__init__.py": self.SELECTOR_INIT,
                **files,
            }
        )
        return imports.orchestrator_imports(root)

    def test_the_real_tree_is_clean(self):
        self.assertEqual(imports.orchestrator_imports(REPO), [])

    def test_only_delegates_exports_utils_itself_and_the_standard_library_cross_the_layer(self):
        rel = f"linear-orchestrator/{ORCH_SRC}engine/prune.py"
        for files, expected in (
            *(
                ({rel: sample}, [])
                for sample in (
                    "from agent_pm.delegate import Delegate, ConfigError as DelegateConfigError\n",
                    "import agent_pm.delegate as d\nd.Delegate.usage('x')\n",
                    "from agent_pm import delegate\ndelegate.ConfigError\n",
                    "from agent_pm.utils import Record\nimport agent_pm.utils\n",
                    "import os.path\nfrom collections.abc import Mapping\nimport __future__\n",
                    "from agent_pm.linear_orchestrator.engine import runs\nfrom . import log\n",
                    "from agent_pm.selector import Selector\nimport agent_pm.selector\n",
                )
            ),
            *(
                ({rel: sample}, [f"{rel}: imports {name} (FR-3)"])
                for sample, name in (
                    (
                        "from agent_pm.delegate import HarnessDelegate\n",
                        "agent_pm.delegate.HarnessDelegate",
                    ),
                    ("from agent_pm.delegate import composer\n", "agent_pm.delegate.composer"),
                    (
                        "from agent_pm.selector.engine.base import Selector\n",
                        "agent_pm.selector.engine",
                    ),
                    ("from agent_pm.selector import Unexported\n", "agent_pm.selector.Unexported"),
                    (
                        "from agent_pm.delegate.composer import Composer\n",
                        "agent_pm.delegate.composer",
                    ),
                    (
                        "import agent_pm.delegate as d\nd.composer.Composer()\n",
                        "agent_pm.delegate.composer",
                    ),
                    ("import agent_pm.delegate.engine.base\n", "agent_pm.delegate.engine"),
                    ("from agent_pm.session import Session\n", "agent_pm.session"),
                    ("from agent_pm import manager\n", "agent_pm.manager"),
                    ("from agent_pm.utils.files import save\n", "agent_pm.utils.files"),
                    ("import requests\n", "requests"),
                    (
                        "def f():\n    from agent_pm.delegate.engine import harness\n",
                        "agent_pm.delegate.engine",
                    ),
                )
            ),
            *(
                ({other: sample}, [f"{other}: imports agent_pm.linear_orchestrator (FR-3)"])
                for other in (
                    f"{project}/{PKG}/{project}/engine/sample.py"
                    for project in ("manager", "delegate", "session", "utils")
                )
                for sample in (
                    "from agent_pm.linear_orchestrator import LinearRun\n",
                    "import agent_pm.linear_orchestrator.engine.runs\n",
                    "from agent_pm import linear_orchestrator\n",
                    "import agent_pm\nagent_pm.linear_orchestrator.LinearRun\n",
                )
            ),
            (
                {
                    "linear-orchestrator/tests/orchestrator_tests/sample.py": (
                        "from agent_pm.delegate.composer import Composer\n"
                    ),
                    "manager/tests/sample.py": "from agent_pm import linear_orchestrator\n",
                },
                [],
            ),
        ):
            with self.subTest(files=files):
                self.assertEqual(self.check(files), expected)


class LiteralImports(Sample):
    def test_unknown_contract_types_fail_loudly(self):
        root = self.tree(
            {
                "pyproject.toml": "[[tool.importlinter.contracts]]\ntype = 'unknown'\n",
                "session/src/agent_pm/session/clients/probe.py": "__import__('agent_pm.utils')",
            }
        )
        with self.assertRaisesRegex(ValueError, "unsupported import contract type: 'unknown'"):
            imports.literal_imports(root)

    def check(self, sample, project="session", module="clients/probe.py"):
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

    def test_lower_public_packages_and_variable_names_pass_but_internals_do_not(self):
        for sample, project, module, expected in (
            (
                "import importlib\nimportlib.import_module('agent_pm.utils')\nimportlib.import_module(implementation)",
                "session",
                "clients/probe.py",
                [],
            ),
            ("importlib.import_module('agent_pm.session')", "delegate", "engine/probe.py", []),
            (
                "__import__('agent_pm.session.engine.hosted_session')",
                "delegate",
                "engine/probe.py",
                [
                    "delegate/src/agent_pm/delegate/engine/probe.py:1: imports agent_pm.session.engine.hosted_session"
                ],
            ),
            (
                "__import__('agent_pm.delegate.engine.future')",
                "delegate",
                "engine/config.py",
                [
                    "delegate/src/agent_pm/delegate/engine/config.py:1: imports agent_pm.delegate.engine.future"
                ],
            ),
        ):
            with self.subTest(sample=sample):
                self.assertEqual(self.check(sample, project, module), expected)


class PrivateImports(Sample):
    def test_underscore_names_from_other_modules_but_os_exit_aliases_and_dunders(self):
        for files, expected in (
            (
                {
                    "a.py": "import os as system\nfrom os import _exit\nsystem._exit(0)\n_exit(0)\nsystem._secret()\n"
                },
                ["a.py:5: private system._secret"],
            ),
            (
                {
                    f"{PKG}/a.py": "from b import _x\nfrom . import _y\n",
                    f"{PKG}/c.py": (
                        "import b.d\nfrom e import f as g\n\ndef h():\n    return b.d._z, g._w\n"
                    ),
                },
                [
                    f"{PKG}/a.py:1: private b._x",
                    f"{PKG}/a.py:2: private ._y",
                    f"{PKG}/c.py:5: private b.d._z",
                    f"{PKG}/c.py:5: private g._w",
                ],
            ),
            (
                {
                    f"{PKG}/a.py": "import b as _b\nfrom c import d as _d\n_b.e, _d.f, _b.__file__\n\nclass G:\n    def h(self, i):\n        return self._j, i._k\n"
                },
                [],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(imports.private_imports(self.tree(files)), expected)


class ImportHygiene(Sample):
    def test_any_sys_path_mutation_and_module_level_environment_write(self):
        assignment = "sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]"
        script = f"import sys\ndef _bootstrap(roots):\n    {assignment}\ndef main(roots):\n    {assignment}\n"
        for files, expected in (
            *(
                ({path: script}, [f"{path}:3: sys.path mutation", f"{path}:5: sys.path mutation"])
                for path in (f"{SESSION_SRC}modes/tui_hook.py", "tests/other.py")
            ),
            (
                {
                    "tests/a.py": (
                        "import sys\nsys.path.insert(0, 'x')\ndef f():\n    sys.path.append('x')\n"
                    ),
                    "tests/b.py": "import sys\nsys.path = []\nsys.path[0] = 'x'\nsys.path += ['x']\ndel sys.path[0]\n",
                    "tests/c.py": (
                        "import sys as s\nfrom sys import path as p\nfrom unittest import mock\ns.path.extend([])\np.remove('x')\np[:0] = ['x']\np += ['x']\nmock.patch('sys.path', [])\n@mock.patch.object(s, 'path', [])\ndef g(): pass\n"
                    ),
                    "tests/d.py": "import sys\nfrom sys import path\nfrom unittest import mock\nx = [sys.path[0], sys.path.index('x'), list(sys.path), sys.path.copy()]\npath = []\nmock.patch('os.path.join')\nmock.patch.object(sys, 'argv', [])\n",
                },
                [f"{at}: sys.path mutation" for at in ("tests/a.py:2", "tests/a.py:4")]
                + [f"tests/b.py:{n}: sys.path mutation" for n in range(2, 6)]
                + [f"tests/c.py:{n}: sys.path mutation" for n in range(4, 10)],
            ),
            (
                {
                    "tests/a.py": "import os\nos.environ['A'] = '1'\nos.environ.update(A='1')\ndel os.environ['A']\nos.environ |= {}\nos.putenv('A', '1')\n",
                    "tests/b.py": (
                        "import os as o\nfrom os import environ, putenv\nfrom unittest import mock\nclass C:\n    environ.setdefault('A', '1')\nif True:\n    putenv('A', '1')\nmock.patch.dict(environ, {'A': '1'}).start()\no.environ.pop('A')\n"
                    ),
                    "tests/c.py": (
                        "import os\nfrom unittest import mock\nA = os.environ.get('A', os.environ['B'])\ndef f():\n    os.environ['A'] = '1'\n    os.putenv('A', '1')\ng = lambda: os.environ.clear()\n@mock.patch.dict(os.environ, {'A': '1'})\nclass D:\n    @mock.patch.dict(os.environ, {'A': '1'})\n    def h(self): pass\n"
                    ),
                },
                [f"tests/a.py:{n}: module-level environment write" for n in range(2, 7)]
                + [f"tests/b.py:{n}: module-level environment write" for n in (5, 7, 8, 9)],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(imports.import_hygiene(self.tree(files)), expected)


class StandaloneImports(Sample):
    def test_the_tui_hook_and_tmux_host_use_package_imports(self):
        for path in (f"{SESSION_SRC}modes/tui_hook.py", TMUX_HOST):
            with self.subTest(path=path):
                self.assertNotIn(path, STANDALONE)

    def found(self, source):
        files = dict.fromkeys(STANDALONE, source)
        return imports.standalone_imports(self.tree(files))

    def test_only_stdlib_imports_in_every_branch_of_a_standalone_script(self):
        def reported(*items):
            return sorted(
                f"{path}:{line}: non-stdlib import {name}"
                for path in STANDALONE
                for line, name in items
            )

        for source, expected in (
            (
                "from __future__ import annotations\nimport os.path as path, sys\nfrom collections.abc import Mapping\nimport builtins\ndef load():\n    import json as j\n    from pathlib import Path\n",
                [],
            ),
            (
                "import requests as r, agent_pm.utils\nfrom agent_pm.session import Driver\nfrom . import sibling\ndef load():\n    from unknown.child import thing\n",
                reported(
                    (1, "requests"),
                    (1, "agent_pm.utils"),
                    (2, "agent_pm.session"),
                    (3, "."),
                    (5, "unknown.child"),
                ),
            ),
            (
                "if __package__:\n    from agent_pm.session.clients import flags\nelse:\n    import runpy\n    import requests\nimport agent_pm.utils\n",
                reported((2, "agent_pm.session.clients"), (5, "requests"), (6, "agent_pm.utils")),
            ),
            (
                "def load():\n    if __package__:\n        import agent_pm.utils\nif __package__ is not None:\n    import requests\nif not __package__:\n    import other\n",
                reported((3, "agent_pm.utils"), (5, "requests"), (7, "other")),
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.found(source), expected)

    def test_missing_inventory_file_is_reported(self):
        missing = min(STANDALONE)
        root = self.tree({path: "" for path in STANDALONE if path != missing})
        self.assertEqual(
            imports.standalone_imports(root), [f"{missing}: missing standalone script"]
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
