"""tests/guards.py: each guard on the real tree, and on violating samples written to temp trees; every test
is a Case, with no class or module fixture."""

import ast
import importlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import guards
from tests.case import ORCH_ROOT, PY, REPO, SRC, Case, child_env

PKG = "src/agent_pm"
ORCH = os.path.dirname(ORCH_ROOT)
OPKG = "src/agent_pm/linear_orchestrator"
FIXTURES = ("setUpClass", "tearDownClass", "setUpModule", "tearDownModule")
ORCH_GUARDS = (guards.orchestrator_imports, guards.orchestrator_text, guards.config_once)
PROBE = (
    "import importlib, json, sys\n"
    "print(json.dumps({n: importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"
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


def own_case(test, top):
    """A Case of the test's own tests/case.py, or a lower project's (guards.TEST_ORDER)."""
    order = guards.TEST_ORDER
    project = top.split("/")[0]
    trees = [top] if project not in order else order[: order.index(project) + 1]
    modules = {f"{t}.tests.case" if t in order else f"{t}.case" for t in trees}
    return any(c.__name__ == "Case" and c.__module__ in modules for c in type(test).__mro__)


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
            (guards.layer_imports, REPO),
            (guards.delegate_session, REPO),
            (guards.layer_commands, REPO),
            (guards.standalone, REPO),
            (guards.runtime_names, REPO),
            (guards.old_core, REPO),
            (guards.session_only, REPO),
            (guards.adapters_key, REPO),
            (guards.syntax_311, REPO),
            (guards.manager_imports, REPO),
            (guards.modes_import_api, REPO),
            (guards.tmux_calls, REPO),
            (guards.syntax_39, REPO),
            (guards.import_hygiene, REPO),
            (guards.namespace_package, REPO),
            (guards.python_commands, REPO),
            (guards.test_layers, REPO),
            (guards.config_levels, REPO),
            (guards.explicit_keys, REPO),
            *((guard, ORCH) for guard in ORCH_GUARDS),
            *((guards.private_imports, src) for src in SRC.values()),
        ):
            with self.subTest(guard.__name__, root=root):
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


class LayerImports(Sample):
    def test_adapters_importing_the_api_names_it(self):
        root = self.tree(
            {f"session/{PKG}/session/adapters/x.py": "import agent_pm.session.engine.api\n"}
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.session.engine.api"
            ],
        )

    def test_only_the_registry_of_adapters_reaches_hosts(self):
        sample = "from agent_pm.session.hosts.tmux import tmux_host\n"
        root = self.tree(
            {
                f"session/{PKG}/session/adapters/registry.py": sample,
                f"session/{PKG}/session/adapters/x.py": sample,
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.session.hosts.tmux"
            ],
        )

    def test_modes_outside_the_api_names_it(self):
        root = self.tree(
            {f"delegate/{PKG}/delegate/x.py": "from agent_pm.session.modes import headless\n"}
        )
        self.assertEqual(
            guards.layer_imports(root),
            [f"delegate/{PKG}/delegate/x.py: delegate imports agent_pm.session.modes.headless"],
        )

    def test_a_relative_import_is_resolved(self):
        root = self.tree({f"delegate/{PKG}/delegate/composer/x.py": "from ...session import api\n"})
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"delegate/{PKG}/delegate/composer/x.py: delegate.composer imports agent_pm.session.api"
            ],
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": "from agent_pm.session import api\n"
                "from agent_pm.delegate.composer import y\nfrom ..session import api\n"
                "from agent_pm.utils.files import append_line\n",
                f"session/{PKG}/session/engine/api.py": "import agent_pm.session.adapters.base\nfrom ..modes import headless\n"
                "from agent_pm.session.modes import headless\nfrom agent_pm.session.engine import records\n"
                "from agent_pm.session.engine.types import DriverError\nfrom ..hosts.tmux import tmux_host\n",
                f"session/{PKG}/session/modes/x.py": "from ..adapters.base import Event\n"
                "from agent_pm.session.engine.types import DriverError\nfrom ..hosts.tmux import tmux_host\n",
                f"session/{PKG}/session/adapters/x.py": "from agent_pm.session.engine.types import Spec\n",
                f"delegate/{PKG}/x.py": "from agent_pm.session import api\nimport agent_pm.session.engine.api\n"
                "from agent_pm.session.engine.api import start\nfrom .session import api\nimport agent_pm\n"
                "from agent_pm.session.engine.types import Spec\n",
            }
        )
        self.assertEqual(guards.layer_imports(root), [])

    def test_a_delegate_module_reaches_the_session_layer_only_through_its_package(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": "from agent_pm.session.engine.types import Usage\n",
                f"delegate/{PKG}/delegate/composer/x.py": "from agent_pm.session import api\n",
                f"session/{PKG}/session/engine/api.py": "from agent_pm.session import Session\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"delegate/{PKG}/delegate/composer/x.py: delegate.composer imports agent_pm.session.api",
                f"delegate/{PKG}/delegate/x.py: delegate imports agent_pm.session.engine.types",
                f"session/{PKG}/session/engine/api.py: session.engine.api imports agent_pm.session.Session",
            ],
        )

    def test_outside_the_layers_reaches_only_api_and_types(self):
        root = self.tree(
            {
                f"delegate/{PKG}/x.py": "import agent_pm.session.adapters\n",
                f"delegate/{PKG}/y.py": "from agent_pm.session.engine import records\n",
                f"session/{PKG}/session/engine/base.py": "from agent_pm.session.engine.types import Spec\n"
                "from agent_pm.session.modes import tui\nfrom agent_pm.session.adapters import registry\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            sorted(
                [
                    f"session/{PKG}/session/engine/base.py: outside the layers imports agent_pm.session.modes.tui",
                    f"delegate/{PKG}/x.py: outside the layers imports agent_pm.session.adapters",
                    f"delegate/{PKG}/y.py: outside the layers imports agent_pm.session.engine.records",
                ]
            ),
        )

    def test_the_host_abc_is_outside_the_layers(self):
        sample = "from agent_pm.session.engine.types import Key\n"
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/base.py": sample,
                f"session/{PKG}/session/hosts/x.py": sample,
                f"session/{PKG}/session/engine/base.py": "from agent_pm.session.hosts.base import Host\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"session/{PKG}/session/hosts/x.py: session.hosts imports agent_pm.session.engine.types"
            ],
        )

    def test_a_layer_reaches_outside_the_layers_only_utils(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": "from agent_pm.manager import roster\n",
                f"session/{PKG}/session/modes/x.py": "import agent_pm.manager.crew\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"delegate/{PKG}/delegate/x.py: delegate imports agent_pm.manager.roster",
                f"session/{PKG}/session/modes/x.py: session.modes imports agent_pm.manager.crew",
            ],
        )

    def test_nested_and_literal_imports_count(self):
        root = self.tree(
            {
                f"session/{PKG}/session/adapters/x.py": "def f():\n    import importlib\n"
                "    importlib.import_module('agent_pm.session.engine.api')\n"
                "    __import__('agent_pm.delegate.composer')\n"
                "    from agent_pm.delegate import y\n"
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.delegate.composer",
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.delegate.y",
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.session.engine.api",
            ],
        )

    def test_a_bare_name_is_a_violation(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": "from api import start\nimport composer\n"
                "from utils.paths import under\n",
                f"delegate/{PKG}/x.py": "import repo\ndef f():\n    __import__('adapters.base')\n",
                f"delegate/{PKG}/delegate/team/tools/x.py": "import tools\n",
                f"session/{PKG}/session/modes/x.py": "import repository\nimport apis\nimport types\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"delegate/{PKG}/delegate/team/tools/x.py: bare import tools",
                f"delegate/{PKG}/delegate/x.py: bare import api.start",
                f"delegate/{PKG}/delegate/x.py: bare import composer",
                f"delegate/{PKG}/delegate/x.py: bare import utils.paths",
                f"delegate/{PKG}/x.py: bare import adapters.base",
                f"delegate/{PKG}/x.py: bare import repo",
            ],
        )

    def test_importing_the_orchestrator_is_a_violation(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": "from agent_pm.linear_orchestrator.engine.config import load\n",
                f"session/{PKG}/session/modes/x.py": "from agent_pm import linear_orchestrator\n",
                f"utils/{PKG}/utils/x.py": "def f():\n    __import__('agent_pm.linear_orchestrator')\n",
                f"delegate/{PKG}/x.py": "import agent_pm.linear_orchestrator.engine.run\nimport linear_orchestrator\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            sorted(
                [
                    f"delegate/{PKG}/delegate/x.py: delegate imports agent_pm.linear_orchestrator.engine.config",
                    f"session/{PKG}/session/modes/x.py: session.modes imports agent_pm.linear_orchestrator",
                    f"utils/{PKG}/utils/x.py: outside the layers imports agent_pm.linear_orchestrator",
                    f"delegate/{PKG}/x.py: bare import linear_orchestrator",
                    f"delegate/{PKG}/x.py: outside the layers imports agent_pm.linear_orchestrator.engine.run",
                ]
            ),
        )

    def test_the_package_is_agent_pm_under_a_projects_src(self):
        sample = "import agent_pm.session.engine.api\n"
        root = self.tree(
            {
                f"session/{PKG}/session/adapters/x.py": sample,
                "session/src/pkg/session/adapters/y.py": sample,
                "session/x/agent_pm/session/adapters/z.py": sample,
                f"other/{PKG}/session/adapters/w.py": sample,
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.session.engine.api"
            ],
        )

    def test_a_module_is_in_its_longest_unit(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/event/reply.py": "from agent_pm.delegate.composer import RunConfig\n",
                f"delegate/{PKG}/delegate/event/outcome.py": "from agent_pm.delegate.composer import SCHEMA\n",
                f"delegate/{PKG}/delegate/composer/teams.py": "from agent_pm.delegate.event import PROGRESS\n"
                "from agent_pm.delegate.engine import plan\n",
                f"delegate/{PKG}/delegate/engine/plan.py": "from agent_pm.delegate.event.outcome import Outcome\n"
                "from agent_pm.manager import roster\n",
                f"session/{PKG}/session/adapters/print_prompt/in_process.py": "from agent_pm.delegate.event.reply import OUTCOME\n",
                f"session/{PKG}/session/adapters/x.py": "from agent_pm.delegate.event import PROGRESS\n",
            }
        )
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"delegate/{PKG}/delegate/composer/teams.py: delegate.composer imports agent_pm.delegate.engine.plan",
                f"delegate/{PKG}/delegate/engine/plan.py: delegate imports agent_pm.manager.roster",
                f"delegate/{PKG}/delegate/event/reply.py: outside the layers imports agent_pm.delegate.composer.RunConfig",
                f"session/{PKG}/session/adapters/print_prompt/in_process.py: session.adapters imports agent_pm.delegate.event.reply",
                f"session/{PKG}/session/adapters/x.py: session.adapters imports agent_pm.delegate.event.PROGRESS",
            ],
        )


class PureUtils(Sample):
    def test_config_imports_only_the_standard_library_and_utils(self):
        root = self.tree(
            {
                guards.CONFIG_FILE: "import os\nfrom agent_pm.utils.paths import under\n"
                "from agent_pm.session import api\nimport agent_pm.manager.crew\n"
                "from agent_pm.session.hosts import tables\nfrom agent_pm.session.hosts.tmux import tmux_host\n"
                "from agent_pm.delegate.engine import plan\n"
            }
        )
        unit = f"{guards.CONFIG_FILE}: {guards.CONFIG} imports agent_pm"
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"{unit}.delegate.engine.plan",
                f"{unit}.manager.crew",
                f"{unit}.session.api",
                f"{unit}.session.hosts.tables",
                f"{unit}.session.hosts.tmux",
            ],
        )

    def test_only_outcome_in_delegate_event_imports_the_composer(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/event/reply.py": "from agent_pm.delegate.composer import RunConfig\n"
                "from agent_pm.utils.markdown import fenced\n",
                f"delegate/{PKG}/delegate/event/outcome.py": "from agent_pm.delegate.composer import SCHEMA\n"
                "from agent_pm.delegate.engine import plan\n",
            }
        )
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"delegate/{PKG}/delegate/event/outcome.py: delegate.event.outcome imports agent_pm.delegate.engine.plan",
                f"delegate/{PKG}/delegate/event/reply.py: delegate.event imports agent_pm.delegate.composer.RunConfig",
            ],
        )


class DelegateSession(Sample):
    def test_a_session_import_but_a_name_in_session_all(self):
        root = self.tree(
            {
                f"session/{PKG}/session/__init__.py": '__all__ = ["api"]\n',
                f"delegate/{PKG}/delegate/a.py": "from agent_pm.session import api\nfrom ..session import api\n",
                f"delegate/{PKG}/delegate/b.py": "from agent_pm.session.engine.api import start\n"
                "from agent_pm.session import registry\n",
                f"delegate/{PKG}/delegate/composer/c.py": "import agent_pm.session\nfrom ...session.adapters import base\n"
                "def f():\n    __import__('agent_pm.session.engine.types')\n",
                f"manager/{PKG}/manager/d.py": "from agent_pm.session.adapters import registry\n",
                guards.CONFIG_FILE: "from agent_pm.session.hosts import tables\n",
            }
        )
        self.assertEqual(
            guards.delegate_session(root),
            [
                f"delegate/{PKG}/delegate/b.py: imports agent_pm.session.engine.api outside session.__all__",
                f"delegate/{PKG}/delegate/b.py: imports agent_pm.session.registry outside session.__all__",
                f"delegate/{PKG}/delegate/composer/c.py: imports agent_pm.session outside session.__all__",
                f"delegate/{PKG}/delegate/composer/c.py: imports agent_pm.session.adapters.base outside session.__all__",
                f"delegate/{PKG}/delegate/composer/c.py: imports agent_pm.session.engine.types outside session.__all__",
            ],
        )


class LayerCommands(Sample):
    SESSION, DELEGATE = (f"{layer}/{PKG}/{layer}" for layer in ("session", "delegate"))
    LINEAR = f"linear-orchestrator/{OPKG}"

    def test_a_layer_command_in_a_spawn_argv(self):
        root = self.tree(
            {
                f"{self.DELEGATE}/a.py": 'import subprocess\nsubprocess.run(["agent-pm-session", "x"])\n'
                'subprocess.Popen(cwd="/", args=("x", "agent-pm-role"))\n',
                f"{self.LINEAR}/b.py": "import os\nfrom subprocess import check_output as out\n"
                'out("agent-pm-issue run", shell=True)\nos.execvp("agent-pm-x", ["agent-pm-x"])\n'
                'os.spawnl(os.P_WAIT, "/bin/agent-pm-y", f"agent-pm-{n}")\n',
                f"{self.SESSION}/c.py": 'import subprocess as sp\nsp.run(["agent-pm-session"])\n'
                'sp.call(["agent-pm-role"])\n',
                f"{self.DELEGATE}/team/tools/d.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
            }
        )
        self.assertEqual(
            guards.layer_commands(root),
            sorted(
                [
                    f'{self.DELEGATE}/a.py:2: runs "agent-pm-session"',
                    f'{self.DELEGATE}/a.py:3: runs "agent-pm-role"',
                    f'{self.DELEGATE}/team/tools/d.py:2: runs "agent-pm-x"',
                    f'{self.SESSION}/c.py:3: runs "agent-pm-role"',
                    f'{self.LINEAR}/b.py:3: runs "agent-pm-issue"',
                    f'{self.LINEAR}/b.py:4: runs "agent-pm-x"',
                    f'{self.LINEAR}/b.py:5: runs "agent-pm-"',
                ]
            ),
        )

    def test_other_strings_calls_and_files_are_clean(self):
        root = self.tree(
            {
                f"{self.DELEGATE}/a.py": 'import subprocess\nNAME = "agent-pm-progress"\nprint("agent-pm-x")\n'
                'subprocess.run(["tmux", NAME, "/agent-pm-list"], env={"A": "agent-pm-x"})\n',
                f"manager/{PKG}/manager/crew.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
                "tests/b.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
            }
        )
        self.assertEqual(guards.layer_commands(root), [])


class Standalone(Sample):
    def test_any_file_of_a_lower_project_naming_the_linear_orchestrator(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/config.toml": "# see ../linear-orchestrator/\n",
                f"{team}/roles/r.md": "x\nRun agent_pm.linear_orchestrator.engine.run, not linear-orchestrator\n",
                f"session/{PKG}/session/x.py": "import agent_pm.linear_orchestrator\n",
                f"utils/{PKG}/utils/y.py": "# linear_orchestrator\n",
                f"manager/{PKG}/manager/z.py": "# linear-orchestrator\n",
                f"delegate/{PKG}/__pycache__/x.pyc": "linear_orchestrator",
                "manager/skills/a/notes": "Linear orchestrator, linear_orchestra, agent_pm_lineage\n",
                "session/tests/a_test.py": "import agent_pm.linear_orchestrator\n",
                "linear-orchestrator/b.py": "import agent_pm.linear_orchestrator\n",
                "tests/c.py": "import agent_pm.linear_orchestrator\n",
                guards.CONFIG_LEVELS: 'LEVELS = (("linear-orchestrator",),)\n# linear-orchestrator\n',
            }
        )
        self.assertEqual(
            guards.standalone(root),
            [
                "delegate/config.toml:1: names linear-orchestrator",
                f"{team}/roles/r.md:2: names linear-orchestrator",
                f"{team}/roles/r.md:2: names linear_orchestrator",
                f"manager/{PKG}/manager/z.py:1: names linear-orchestrator",
                f"session/{PKG}/session/x.py:1: names linear_orchestrator",
                f"{guards.CONFIG_LEVELS}:2: names linear-orchestrator",
                f"utils/{PKG}/utils/y.py:1: names linear_orchestrator",
            ],
        )


class ManagerImports(Sample):
    def test_each_named_exception_is_allowed(self):
        root = self.tree(
            {
                f"manager/{PKG}/manager/crew.py": "import os\nimport agent_pm.delegate.engine.config\n"
                "from agent_pm.manager import roster\nfrom agent_pm.session import Session, api\n"
                "from agent_pm.utils.files import save\nfrom . import roster\n",
                f"manager/{PKG}/manager/roster.py": "import json\nfrom agent_pm.session import api\n"
                "from agent_pm.utils.config_file import read_project\n",
            }
        )
        self.assertEqual(guards.manager_imports(root), [])

    def test_another_agent_pm_import_from_the_manager_is_refused(self):
        root = self.tree(
            {
                f"manager/{PKG}/manager/crew.py": "from agent_pm.session.engine import types\n"
                "from agent_pm.delegate import composer\nimport agent_pm.linear_orchestrator\n",
                f"manager/{PKG}/manager/roster.py": "from agent_pm.session import Session\n"
                "from agent_pm.manager import crew\n",
                f"manager/{PKG}/manager/x.py": "from agent_pm.manager import roster\n"
                "from agent_pm.session import api\nimport agent_pm.delegate.engine.config\n",
            }
        )
        crew, roster, x = (
            f"manager/{PKG}/manager/{m}.py: manager.{m} imports" for m in ("crew", "roster", "x")
        )
        self.assertEqual(
            guards.manager_imports(root),
            [
                f"{crew} agent_pm.delegate.composer",
                f"{crew} agent_pm.linear_orchestrator",
                f"{crew} agent_pm.session.engine.types",
                f"{roster} agent_pm.manager.crew",
                f"{roster} agent_pm.session.Session",
                f"{x} agent_pm.delegate.engine.config",
                f"{x} agent_pm.manager.roster",
                f"{x} agent_pm.session.api",
            ],
        )

    def test_each_lower_subpackage_importing_the_manager_is_refused(self):
        files = {
            f"{top}/{PKG}/{sub}/x.py": "from agent_pm.manager import roster\n"
            for top, sub in guards.PROJECTS.items()
            if sub != "manager"
        }
        root = self.tree(files)
        self.assertEqual(
            guards.manager_imports(root),
            sorted(f"{rel}: imports agent_pm.manager.roster" for rel in files),
        )


class TestLayers(Sample):
    def test_a_project_test_imports_its_own_lower_projects_and_the_stdlib(self):
        root = self.tree(
            {
                "utils/tests/a.py": "import os\nfrom agent_pm.utils import files\nfrom utils.tests import case\n",
                "session/tests/unit/a.py": "from agent_pm.utils import files\nfrom utils.tests.case import Case\n"
                "from agent_pm.session import api\nfrom ..case import Case\n",
                f"{guards.ORCH_PROJECT}/tests/unit/a.py": "from ..case import Case\n"
                "from delegate.tests import case\nfrom agent_pm.delegate import composer\n",
                "manager/tests/a.py": "from agent_pm.linear_orchestrator import x\nfrom session.tests import y\n",
            }
        )
        self.assertEqual(guards.test_layers(root), [])

    def test_the_root_tests_and_a_higher_project_are_refused(self):
        root = self.tree(
            {
                "utils/tests/a.py": "from tests.case import Case\nfrom agent_pm.session import api\n"
                "from session.tests import case\n",
                "session/tests/fakes/b.py": "import tests.guards\nfrom agent_pm.delegate.engine import config\n"
                "from agent_pm.manager import roster\n",
                "delegate/tests/c.py": "from tests import case\nimport agent_pm.linear_orchestrator.x\n"
                "from manager.tests import y\n",
                f"{guards.ORCH_PROJECT}/tests/d.py": "from agent_pm.manager import crew\n",
            }
        )
        self.assertEqual(
            guards.test_layers(root),
            [
                "delegate/tests/c.py: imports agent_pm.linear_orchestrator.x",
                "delegate/tests/c.py: imports manager.tests",
                "delegate/tests/c.py: imports tests.case",
                f"{guards.ORCH_PROJECT}/tests/d.py: imports agent_pm.manager.crew",
                "session/tests/fakes/b.py: imports agent_pm.delegate.engine.config",
                "session/tests/fakes/b.py: imports agent_pm.manager.roster",
                "session/tests/fakes/b.py: imports tests.guards",
                "utils/tests/a.py: imports agent_pm.session.api",
                "utils/tests/a.py: imports session.tests",
                "utils/tests/a.py: imports tests.case",
            ],
        )


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({f"session/{PKG}/session/engine/api.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(
            guards.runtime_names(root),
            [
                f"session/{PKG}/session/engine/api.py:1: runtime name Claude",
                f"session/{PKG}/session/engine/api.py:2: runtime name codex",
            ],
        )

    def test_print_prompt_is_a_runtime_name(self):
        root = self.tree({f"delegate/{PKG}/delegate/x.py": 'RUNTIME = "print_prompt"\n'})
        self.assertEqual(
            guards.runtime_names(root),
            [f"delegate/{PKG}/delegate/x.py:1: runtime name print_prompt"],
        )

    def test_policy_runtime_tables_are_exempt(self):
        root = self.tree(
            {"delegate/config.toml": "[policy.claude]\nflags = []\n[policy.codex]\nflags = []\n"}
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({f"session/{PKG}/session/adapters/claude/x.py": 'NAME = "claude"\n'})
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_table_header_implementation_line_and_comments_are_exempt(self):
        root = self.tree(
            {
                "delegate/config.toml": "# codex in a comment\n[adapters.codex]\n"
                'implementation = "adapters.codex:CodexAdapter"  # codex\n'
                'key = "x # codex"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), ["delegate/config.toml:4: runtime name codex"])

    def test_toml_print_prompt_table_header_and_implementation_line_are_exempt(self):
        root = self.tree(
            {
                "delegate/config.toml": "# [adapters.print_prompt]  act-as's runtime\n[adapters.print_prompt]\n"
                'implementation = "adapters.print_prompt:PrintPromptAdapter"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_runtime_line_is_exempt_under_any_table(self):
        root = self.tree(
            {
                "delegate/config.toml": 'runtime = "codex"\n[roles.x]\nruntime = "codex"\n'
                '[roles.y]\nruntime = "codex"  # note\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_name_in_a_key_value_elsewhere(self):
        root = self.tree(
            {
                "delegate/config.toml": '[adapters.codex]\nimplementation = "adapters.codex:CodexAdapter"\n'
                '[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\n'
                'runtime = "codex" x\n'
            }
        )
        self.assertEqual(
            guards.runtime_names(root),
            [f"delegate/config.toml:{n}: runtime name codex" for n in (4, 5, 6, 7)],
        )


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
            guards.old_core(root), ["session/b.py: imports core", "tests/a.py: imports core.src"]
        )


class AdaptersKey(Sample):
    def test_the_literal_key_only_in_the_session_config_parser(self):
        sample = 'cfg = {}\nvalue = cfg["adapters"]\n'
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/composer/x.py": sample,
                f"session/{PKG}/session/adapters/x.py": sample,
                guards.CONFIG_FILE: sample,
                guards.ADAPTERS_HOME: sample,
            }
        )
        self.assertEqual(
            guards.adapters_key(root),
            [
                f'delegate/{PKG}/delegate/composer/x.py:2: literal key "adapters"',
                f'{guards.CONFIG_FILE}:2: literal key "adapters"',
                f'session/{PKG}/session/adapters/x.py:2: literal key "adapters"',
            ],
        )


class SessionOnly(Sample):
    def test_session_imports_only_stdlib_session_and_utils(self):
        root = self.tree(
            {
                f"session/{PKG}/session/engine/base.py": "import os\nfrom agent_pm.session.adapters import registry\n"
                "from agent_pm.utils.record import Record\nfrom agent_pm.delegate.engine import config\n",
                f"session/{PKG}/session/adapters/x.py": "import agent_pm.delegate.engine.config\nfrom agent_pm.delegate.event import reply\n",
                f"session/{PKG}/session/hosts/y.py": "from agent_pm.manager import roster\n",
                f"delegate/{PKG}/delegate/x.py": "from agent_pm.delegate.engine import config\n",
            }
        )
        self.assertEqual(
            guards.session_only(root),
            [
                f"session/{PKG}/session/adapters/x.py: names agent_pm.delegate.engine.config",
                f"session/{PKG}/session/adapters/x.py: session imports agent_pm.delegate.engine.config",
                f"session/{PKG}/session/adapters/x.py: session imports agent_pm.delegate.event.reply",
                f"session/{PKG}/session/engine/base.py: session imports agent_pm.delegate.engine.config",
                f"session/{PKG}/session/hosts/y.py: session imports agent_pm.manager.roster",
            ],
        )

    def test_print_prompt_has_no_exception(self):
        path = f"session/{PKG}/session/adapters/print_prompt/in_process.py"
        root = self.tree({path: "from agent_pm.delegate.event.reply import OUTCOME\n"})
        self.assertEqual(
            guards.session_only(root), [f"{path}: session imports agent_pm.delegate.event.reply"]
        )

    def test_no_session_source_names_a_config_reader_the_reply_protocol_or_the_team(self):
        root = self.tree(
            {
                f"session/{PKG}/session/x.py": "from agent_pm.utils.config_file import read_config\n",
                f"session/{PKG}/session/y.py": "# tomllib\n",
                f"session/{PKG}/session/z.py": 'P = "[agent-pm-progress:start]"\nO = "```agent-pm-outcome"\n'
                'T = "team/roles"\n',
                f"utils/{PKG}/utils/z.py": 'import tomllib\nP = "agent-pm-progress"\n',
            }
        )
        self.assertEqual(
            guards.session_only(root),
            [
                f"session/{PKG}/session/x.py: names config_file",
                f"session/{PKG}/session/y.py: names tomllib",
                f"session/{PKG}/session/z.py: names agent-pm-outcome",
                f"session/{PKG}/session/z.py: names agent-pm-progress",
                f"session/{PKG}/session/z.py: names team/",
            ],
        )


class ModesImportApi(Sample):
    def test_a_mode_importing_the_api_any_way_is_a_violation(self):
        root = self.tree(
            {
                f"session/{PKG}/session/modes/a.py": "from agent_pm.session import api\n",
                f"session/{PKG}/session/modes/b.py": "import agent_pm.session.engine.api\n",
                f"session/{PKG}/session/modes/c.py": "from ..engine.api import DriverError\n",
                f"session/{PKG}/session/modes/d.py": "def f():\n    from .. import api\n",
                f"session/{PKG}/session/modes/__init__.py": "__import__('agent_pm.session.engine.api')\n",
            }
        )
        self.assertEqual(
            guards.modes_import_api(root),
            [
                f"session/{PKG}/session/modes/__init__.py: imports agent_pm.session.engine.api",
                f"session/{PKG}/session/modes/a.py: imports agent_pm.session.api",
                f"session/{PKG}/session/modes/b.py: imports agent_pm.session.engine.api",
                f"session/{PKG}/session/modes/c.py: imports agent_pm.session.engine.api",
                f"session/{PKG}/session/modes/d.py: imports agent_pm.session.api",
            ],
        )

    def test_types_in_a_mode_and_the_api_outside_modes_are_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/modes/a.py": "from agent_pm.session.engine.types import DriverError\n"
                "from agent_pm.session.engine import records\nfrom ..engine.types import DriverError\n",
                f"session/{PKG}/session/x.py": "from agent_pm.session import api\n",
                f"delegate/{PKG}/y.py": "import agent_pm.session.engine.api\n",
            }
        )
        self.assertEqual(guards.modes_import_api(root), [])


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": sample,
                f"session/{PKG}/session/modes/x.py": sample,
                f"delegate/{PKG}/delegate/composer/y.py": 'TOOL = "tmux"\n',
                "session/src/hosts/tmux/tmux_host.py": sample,
                "session/tests/z.py": sample,
            }
        )
        self.assertEqual(
            guards.tmux_calls(root),
            sorted(
                [
                    f'delegate/{PKG}/delegate/composer/y.py:1: str constant "tmux"',
                    'session/src/hosts/tmux/tmux_host.py:2: str constant "tmux"',
                    f'session/{PKG}/session/modes/x.py:2: str constant "tmux"',
                ]
            ),
        )

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/modes/x.py": 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'
            }
        )
        self.assertEqual(guards.tmux_calls(root), [])


class Syntax39(Sample):
    def test_newer_syntax_in_a_standalone_file_is_a_violation(self):
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": "def f(x: int | None): pass\nmatch x:\n    case _: pass\n",
                f"session/{PKG}/session/engine/api.py": "match x:\n    case _: pass\n",
            }
        )
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(
            found[0].startswith(f"session/{PKG}/session/hosts/tmux/tmux_host.py: "), found
        )

    def test_the_tui_hook_is_standalone_too(self):
        root = self.tree(
            {f"session/{PKG}/session/modes/tui_hook.py": "match x:\n    case _: pass\n"}
        )
        found = guards.syntax_39(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith(f"session/{PKG}/session/modes/tui_hook.py: "), found)

    def test_3_9_syntax_is_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": "from __future__ import annotations\n"
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
        root = self.tree(
            {
                "linear-orchestrator/x.py": "type X = int\n",
                "tests/y.py": "X = int\n",
                "work/z.py": "type X = int\n",
            }
        )
        found = guards.syntax_311(root)
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("linear-orchestrator/x.py: "), found)


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

    def test_e402_i001_and_isort_comments(self):
        root = self.tree(
            {
                "tests/a.py": "import x  # noqa: E402\nimport y  # noqa: F401,E402\nimport z  # NOQA:I001\n"
                "# isort: skip_file\n# ruff: noqa: E402\n",
                "tests/b.py": "S = '# noqa: E402'\nimport w  # noqa: F401\n# ruff: noqa: E501\n# E402 isort\n",
            }
        )
        self.assertEqual(
            guards.import_hygiene(root),
            [
                "tests/a.py:1: comment # noqa: E402",
                "tests/a.py:2: comment # noqa: F401,E402",
                "tests/a.py:3: comment # NOQA:I001",
                "tests/a.py:4: comment # isort: skip_file",
                "tests/a.py:5: comment # ruff: noqa: E402",
            ],
        )


class NamespacePackage(Sample):
    def own(self, *extra: str) -> str:
        files = [f"{top}/src/agent_pm/{sub}/__init__.py" for top, sub in guards.PROJECTS.items()]
        return self.tree({f: "" for f in (*files, *extra)})

    def test_the_own_subpackages_and_caches_are_clean(self):
        root = self.own("session/src/__pycache__/x.pyc", f"session/{PKG}/__pycache__/x.pyc")
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


class PythonCommands(Sample):
    def test_a_python_command_other_than_p_m_agent_pm_or_a_team_tool(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/config.toml": 'commands = ["python3 x.py", "python3 -m agent_pm.x"]\n'
                "# python3 -P {{team}}/tools/repo.py\n"
                'gate = "python3 -P {{team}}/x.py"\n',
                "linear-orchestrator/config.toml": 'gate = "python3 -P -m agent_pmx.y"\n',
                "delegate/skills/a/SKILL.md": "Run `python3.11 x.py`.\n"
                "- Bash(python -P -m agent_pm.delegate.cli --list)\n",
                "manager/skills/b/SKILL.md": "Run `python3 y.py`.\n",
                f"{team}/roles/r.md": "1. `python3 -P agent_pm.x`\n2. /usr/bin/python3 -I -m agent_pm.x\n"
                "3. `env PYTHONPATH=x python3 -P -m agent_pm.linear_orchestrator.engine.run`\n"
                "4. `python3 -P <team>/tools/repo.py status --dir x`\n"
                "5. `python3 -P <team>/tools/../repo.py`\n6. `python3 -P <team>/tools/repo.pyc`\n",
            }
        )
        self.assertEqual(
            guards.python_commands(root),
            [
                'delegate/config.toml:1: python command "python3 -m agent_pm.x"',
                'delegate/config.toml:1: python command "python3 x.py"',
                'delegate/config.toml:3: python command "python3 -P {{team}}/x.py"',
                'delegate/skills/a/SKILL.md:1: python command "python3.11 x.py"',
                f'{team}/roles/r.md:1: python command "python3 -P agent_pm.x"',
                f'{team}/roles/r.md:2: python command "python3 -I -m agent_pm.x"',
                f'{team}/roles/r.md:5: python command "python3 -P <team>/tools/../repo.py"',
                f'{team}/roles/r.md:6: python command "python3 -P <team>/tools/repo.pyc"',
                'linear-orchestrator/config.toml:1: python command "python3 -P -m agent_pmx.y"',
                'manager/skills/b/SKILL.md:1: python command "python3 y.py"',
            ],
        )

    def test_other_words_and_files_are_clean(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/team/a.md": "pythonic, my_python3 x.py, Python 3.11\n",
                "delegate/skills/a/notes.txt": "python3 x.py\n",
                "delegate/README.md": "python3 x.py\n",
                "linear-orchestrator/other.toml": 'x = "python3 x.py"\n',
                "tests/a.md": "python3 x.py\n",
            }
        )
        self.assertEqual(guards.python_commands(root), [])


class OrchestratorImports(Sample):
    def test_forbidden_imports_name_the_file_and_line(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "import agent_pm.session.adapters\nfrom agent_pm.session.modes import x\n"
                "from agent_pm.session.hosts.tmux import tmux_host\nimport agent_pm.delegate.team.tools.repo\n",
                f"{OPKG}/b.py": "from agent_pm.delegate.engine import runner\n"
                "from agent_pm.delegate.engine.plan import Plan\nimport core\nimport agent_pm\n",
                f"{OPKG}/run.py": "from agent_pm.delegate.composer import resolve\nfrom agent_pm.delegate import composer\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:1: imports agent_pm.session.adapters",
                f"{OPKG}/a.py:2: imports agent_pm.session.modes.x",
                f"{OPKG}/a.py:3: imports agent_pm.session.hosts.tmux.tmux_host",
                f"{OPKG}/a.py:4: imports agent_pm.delegate.team.tools.repo",
                f"{OPKG}/b.py:1: imports agent_pm.delegate.engine.runner",
                f"{OPKG}/b.py:2: imports agent_pm.delegate.engine.plan.Plan",
                f"{OPKG}/b.py:3: imports core",
                f"{OPKG}/b.py:4: imports agent_pm",
                f"{OPKG}/run.py:1: imports agent_pm.delegate.composer.resolve",
                f"{OPKG}/run.py:2: imports agent_pm.delegate.composer",
            ],
        )

    def test_session_s_abc_only_in_config_py(self):
        sample = "from agent_pm.session import Session, api\n"
        root = self.tree({f"{OPKG}/engine/config.py": sample, f"{OPKG}/engine/router.py": sample})
        self.assertEqual(
            guards.orchestrator_imports(root),
            [f"{OPKG}/engine/router.py:1: imports agent_pm.session.Session"],
        )

    def test_bare_and_relative_names(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "import config\nfrom linear import use_logs\nfrom session import api\n"
                "import utils.shell\nimport delegate\nfrom . import b\nfrom .c import d\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:1: imports config",
                f"{OPKG}/a.py:2: imports linear.use_logs",
                f"{OPKG}/a.py:3: imports session.api",
                f"{OPKG}/a.py:4: imports utils.shell",
                f"{OPKG}/a.py:5: imports delegate",
                f"{OPKG}/a.py:6: imports .b",
                f"{OPKG}/a.py:7: imports .c.d",
            ],
        )

    def test_submodules_by_attribute_and_other_forms(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "from agent_pm import delegate as d\nd.engine.runner.run()\n"
                "import agent_pm.session.engine.api\nimport third_party\n__import__('agent_pm.session.adapters.base')\n"
                "import agent_pm.delegate\nagent_pm.delegate.engine.detach.x\n"
                "from agent_pm.session.engine.api import start\n",
                f"{OPKG}/engine/config.py": "from agent_pm.delegate import composer\ncomposer.run.resolve()\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:2: uses agent_pm.delegate.engine",
                f"{OPKG}/a.py:3: imports agent_pm.session.engine.api",
                f"{OPKG}/a.py:4: imports third_party",
                f"{OPKG}/a.py:5: imports agent_pm.session.adapters.base",
                f"{OPKG}/a.py:7: uses agent_pm.delegate.engine",
                f"{OPKG}/a.py:8: imports agent_pm.session.engine.api.start",
                f"{OPKG}/engine/config.py:2: uses agent_pm.delegate.composer.run",
            ],
        )

    def test_allowed_imports_are_clean(self):
        root = self.tree(
            {
                f"{OPKG}/engine/config.py": "import os\nfrom agent_pm.delegate.composer import resolve\n"
                "from agent_pm.delegate import composer\ncomposer.index()\nimport agent_pm.delegate.composer\n"
                "from agent_pm.utils.files import save\nimport agent_pm.delegate.engine.config\n"
                "agent_pm.delegate.engine.config.CONFIG\n",
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm import linear_orchestrator\nfrom agent_pm.delegate import run, Result\nfrom agent_pm import delegate\n"
                "delegate.Begun\nimport agent_pm.delegate as d\nd.Finished\n"
                "from agent_pm.session import api\nimport agent_pm.utils.shell\nfrom agent_pm import utils\n"
                "import agent_pm.linear_orchestrator.linear.linear\nfrom agent_pm.linear_orchestrator.engine.config import TASKS\n",
                f"{OPKG}/utils.py": "",
            }
        )
        self.assertEqual(guards.orchestrator_imports(root), [])

    def test_the_delegate_package_resolves_an_exporter_then_the_module(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": "from agent_pm.delegate import run, plan\nfrom agent_pm.delegate import event\n"
                "from agent_pm.delegate.event.outcome import Outcome\nfrom agent_pm.delegate import engine\n"
                "from agent_pm.delegate import composer\n",
            }
        )
        self.assertEqual(
            guards.orchestrator_imports(root),
            [
                f"{OPKG}/a.py:4: imports agent_pm.delegate.engine",
                f"{OPKG}/a.py:5: imports agent_pm.delegate.composer",
            ],
        )


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
                f"{OPKG}/linear/issues.py": 'H = "Handoff from"\n'
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
    LOAD = "calls agent_pm.linear_orchestrator.engine.config.load outside an entry main"
    USE_LOGS = "calls agent_pm.linear_orchestrator.linear.linear.use_logs outside an entry main"

    def test_load_and_use_logs_outside_an_entry_main(self):
        root = self.tree(
            {
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm.linear_orchestrator.linear import linear\ndef main():\n"
                "    config.load()\n    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                f"{OPKG}/linear/writeback.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm.linear_orchestrator.linear.linear import use_logs\n\ndef main():\n"
                "    config.load()\n    use_logs(None)\n",
                f"{OPKG}/engine/promote.py": "import agent_pm.linear_orchestrator.engine.config as c\n\nclass P:\n"
                "    def main(self):\n        c.load()\n",
                "src/run.py": "import agent_pm.linear_orchestrator.engine.config\n\ndef main():\n"
                "    agent_pm.linear_orchestrator.engine.config.load()\n",
            }
        )
        self.assertEqual(
            guards.config_once(root),
            [
                f"{OPKG}/engine/promote.py:5: {self.LOAD}",
                f"{OPKG}/engine/run.py:8: {self.LOAD}",
                f"{OPKG}/linear/writeback.py:5: {self.LOAD}",
                f"{OPKG}/linear/writeback.py:6: {self.USE_LOGS}",
                f"src/run.py:4: {self.LOAD}",
            ],
        )

    def test_the_real_entries_calls_outside_an_entry(self):
        files = {}
        for name in ("run.py", "promote.py"):
            with open(
                os.path.join(ORCH_ROOT, "agent_pm", "linear_orchestrator", "engine", name),
                encoding="utf-8",
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
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\nconfig.runnable(1)\n",
                f"{OPKG}/engine/config.py": "def runnable():\n    pass\n\ndef load():\n    runnable()\n"
                "    docs()\n",
                f"{OPKG}/engine/promote.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm.linear_orchestrator.linear import linear\n"
                "def main():\n    try:\n        cfg = config.load()\n    except SystemExit:\n"
                "        linear.use_logs(None)\n",
                "src/config.py": "def load():\n    runnable()\n",
            }
        )
        self.assertEqual(
            guards.config_once(root),
            [
                f"{OPKG}/engine/run.py:2: calls agent_pm.linear_orchestrator.engine.config.runnable",
                "src/config.py:2: calls runnable",
            ],
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in ORCH_GUARDS:
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


class StatusLogFormat(Sample):
    CLOCK = 'CLOCK = "%H:%M:%S"\n'

    def test_the_real_tree_is_clean(self):
        self.assertEqual(guards.status_log_format(REPO), [])

    def test_the_clock_in_another_projects_module_or_an_f_string(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": self.CLOCK,
                f"manager/{PKG}/manager/y.py": "import time\n\n"
                "line = f\"{time.strftime('%H:%M:%S')} {name}\"\n"
                'hook = f"$(date +%H:%M:%S) {name} dead"\n',
                f"linear-orchestrator/{OPKG}/z.py": 'import time\nat = time.strftime("%H:%M:%S")\n',
            }
        )
        outside = "status log line format outside session/engine/status_log.py"
        self.assertEqual(
            guards.status_log_format(root),
            sorted(
                [
                    f"delegate/{PKG}/delegate/x.py:1: {outside}",
                    f"manager/{PKG}/manager/y.py:3: {outside}",
                    f"manager/{PKG}/manager/y.py:4: {outside}",
                    f"linear-orchestrator/{OPKG}/z.py:2: {outside}",
                ]
            ),
        )

    def test_status_log_py_tmux_host_and_files_outside_src_are_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/engine/status_log.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": self.CLOCK,
                "session/tests/x_test.py": self.CLOCK,
                "tests/y_test.py": self.CLOCK,
            }
        )
        self.assertEqual(guards.status_log_format(root), [])


class ConfigLevels(Sample):
    def levels(self, text):
        return guards.config_levels(self.tree({guards.CONFIG_LEVELS: text}))

    def test_the_order_is_one_constant_test_order_flattens(self):
        self.assertEqual(
            guards.TEST_ORDER, ("utils", "session", "delegate", guards.ORCH_PROJECT, "manager")
        )
        self.assertEqual(
            self.levels(
                'LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",))\n'
            ),
            [],
        )

    def test_each_project_once_and_only_projects(self):
        at = guards.CONFIG_LEVELS
        self.assertEqual(
            self.levels('LEVELS = (("utils", "session"), ("delegate", "x"), ("delegate",))\n'),
            [
                f"{at}: LEVELS holds 'delegate' 2 times",
                f"{at}: LEVELS holds 'linear-orchestrator' 0 times",
                f"{at}: LEVELS holds 'manager' 0 times",
                f"{at}: LEVELS holds 'x', not a project",
            ],
        )
        self.assertEqual(self.levels("X = 1\n"), [f"{at}: want one LEVELS assignment"])
        self.assertEqual(
            self.levels("LEVELS = tuple(x)\n"), [f"{at}: LEVELS is not a literal tuple of tuples"]
        )


class ExplicitKeys(Sample):
    def test_an_example_with_a_real_line_its_shape_suffix_or_empty_table_passes(self):
        root = self.tree(
            {
                "delegate/config.toml": 'cwd = ""\n# local: cwd = "/x"\n[output]\ntype = "local"\ndir = ""\n'
                '[roles.a.output]\ntype = "x"\n# local: [roles.b]\n# local: cwd = "/y"\n'
                '# local: [roles.b.output]\n# local: dir = "d"\n',
                f"{guards.ORCH_PROJECT}/config.toml": '[labels]\n# local: [labels]\n# local: x = "1"\n'
                '[roles.a]\nkey = ""\n# local: [roles.c]\n# local: key = "k"\n'
                '# local: [core.roles.a]\n# local: cwd = "/z"\n',
            }
        )
        self.assertEqual(guards.explicit_keys(root), [])

    def test_a_commented_only_key_is_refused(self):
        root = self.tree(
            {
                "session/config.toml": "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n",
                "delegate/config.toml": '# local: team_dirs = ["~/t"]\n[roles.a]\nt = 1\n# local: [roles.a]\n'
                "# local: [roles.a.deep]\n# local: y = 2\n",
                "manager/config.toml": "# local: [bad\n",
            }
        )
        found = guards.explicit_keys(root)
        self.assertEqual(
            [f for f in found if not f.startswith("manager/")],
            [
                "delegate/config.toml: `# local:` key roles.a.deep.y has no real line",
                "delegate/config.toml: `# local:` key team_dirs has no real line",
                "session/config.toml: `# local:` key hosts.tmux.status_line has no real line",
            ],
        )
        self.assertEqual(len([f for f in found if f.startswith("manager/config.toml: ")]), 1)


if __name__ == "__main__":
    unittest.main()
