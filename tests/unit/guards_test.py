import ast
import importlib
import os
from pathlib import Path
from unittest import mock

from tests import guards
from tests.case import ORCH_ROOT, REPO, SRC, Case
from tests.checks import checks
from tests.guard_samples import PKG, Sample, fixtures, test_suites

OPKG = "src/agent_pm/linear_orchestrator"


UNLISTED = "not __init__.py, cli.py, engine/, a listed subpackage or a listed entry module"


class PackageClimbs(Sample):
    def violations(self, text, sub="engine/sample.py"):
        rel = f"delegate/{PKG}/delegate/{sub}"
        root = self.tree({rel: text})
        return [v for v in guards.package_climbs(root) if v.startswith(f"{rel}:")]

    def test_rejects_every_way_to_climb_past_the_package(self):
        for expr in (
            "os.path.join(os.path.dirname(__file__), *[os.pardir] * 2)",
            "os.path.dirname(os.path.dirname(os.path.dirname(__file__)))",
            "Path(__file__).parents[2]",
            "Path(__file__).parents[-1]",
            "Path(__file__).parents[index]",
            "Path(__file__).parents[1:3]",
            "Path(__file__).parent.parent.parent",
            "Path(__file__).resolve().parent / '..' / '..'",
        ):
            with self.subTest(expr=expr):
                self.assertTrue(
                    self.violations(f"import os\nfrom pathlib import Path\nROOT = {expr}\n")
                )

    def test_tracks_aliases_and_separate_assignments(self):
        self.assertTrue(
            self.violations(
                "from pathlib import Path as P\nf = P(__file__)\np = f.parent.parent\nx = p.parent\n"
            )
        )
        self.assertTrue(
            self.violations(
                "import os as o\np = o.path.dirname(__file__)\nx = o.path.join(p, '..', '..')\n"
            )
        )

    def test_project_name_segments_inside_the_package_pass(self):
        for text in (
            "from pathlib import Path\np = Path(__file__).parent / 'session' / 'config.toml'\n",
            "from pathlib import Path\np = Path(__file__).parent.parent / 'session' / 'config.toml'\n",
            "import os\nx = os.path.join(os.path.dirname(__file__), 'linear-orchestrator/config.toml')\n",
            "import os\nx = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'linear-orchestrator/config.toml')\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.violations(text), [])

    def test_rejects_sibling_paths_and_listing_a_root_outside_the_package(self):
        for text in (
            "from pathlib import Path\np = Path(__file__).parent.parent.parent / 'session' / 'config.toml'\n",
            "import os\nx = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'linear-orchestrator/config.toml')\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.violations(text))
        self.assertTrue(
            self.violations(
                "import os\nfrom agent_pm.utils.paths import ROOT\nx = os.listdir(ROOT)\n"
            )
        )

    def test_package_resources_and_repository_tools_pass(self):
        self.assertEqual(
            self.violations(
                "import os\nTEAM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'team')\n",
                "composer/base.py",
            ),
            [],
        )
        self.assertEqual(
            self.violations(
                "import os\nHOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hosts/tmux/tmux_host.py')\n",
                "modes/tui_hook.py",
            ),
            [],
        )
        root = self.tree(
            {"delegate/tests/test.py": "from pathlib import Path\nx = Path(__file__).parents[9]\n"}
        )
        self.assertFalse(any("test.py" in v for v in guards.package_climbs(root)))

    def test_repository_helpers_cannot_escape_by_import_or_attribute(self):
        for text in (
            "from agent_pm.utils import ROOT\n",
            "from agent_pm.utils.paths import import_roots\n",
            "from agent_pm.utils import ROOT\nfrom os.path import join\nx = join(ROOT, 'bin')\n",
            "from agent_pm.utils.paths import ROOT as root\nfrom pathlib import Path\nx = Path(root) / 'bin'\n",
            "from agent_pm import utils\nx = utils.ROOT\n",
            "import agent_pm.utils.paths as paths\nx = paths.import_roots\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.violations(text))

    def test_unrelated_bases_may_have_project_names(self):
        self.assertEqual(
            self.violations(
                "import os\nfrom pathlib import Path\nx = os.path.join('/data', 'session')\ny = Path('/work') / 'delegate'\n"
            ),
            [],
        )

    def test_exemption_inventory_allows_only_ticket_removals(self):
        expected = {
            ("utils/src/agent_pm/utils/paths.py", "ROOT"): "TASK-393",
            ("utils/src/agent_pm/utils/paths.py", "import_roots"): "TASK-393",
            ("utils/src/agent_pm/utils/config_file.py", "read_project"): "TASK-393",
            (
                "session/src/agent_pm/session/adapters/codex/tui/__init__.py",
                "import_roots()",
            ): "TASK-393",
            ("delegate/src/agent_pm/delegate/engine/config.py", "DELEGATE"): "TASK-367",
            ("delegate/src/agent_pm/delegate/engine/config.py", "import_roots()"): "TASK-367",
            (
                "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/config.py",
                "ROOT",
            ): "TASK-372",
            (
                "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/config.py",
                "import_roots()",
            ): "TASK-372",
            ("manager/src/agent_pm/manager/engine/crew.py", "BIN"): "TASK-373",
        }
        self.assertLessEqual(guards.PACKAGE_CLIMB_ALLOWED.keys(), expected.keys())
        for key, owner in guards.PACKAGE_CLIMB_ALLOWED.items():
            self.assertEqual(owner, expected[key])

    def test_every_exemption_must_still_have_an_escape(self):
        root = self.tree({})
        found = guards.package_climbs(root)
        self.assertEqual(len(found), 9)
        for (rel, name), ticket in guards.PACKAGE_CLIMB_ALLOWED.items():
            self.assertIn(f"{rel}: {name}: allowlisted but no package escape ({ticket})", found)


class EveryTest(Sample):
    def test_discovery_finds_direct_and_normalized_test_packages(self):
        root = self.tree(
            {
                "project/tests/unit/__init__.py": "",
                "project/tests/project_tests/__init__.py": "",
                "project/tests/project_tests/integration/__init__.py": "",
                "project/tests/empty/unit/leftover.py": "",
            }
        )
        self.assertEqual(
            test_suites(root, "project/tests"),
            [
                (os.path.join(root, "project/tests/unit"), root),
                (
                    os.path.join(root, "project/tests/project_tests/integration"),
                    os.path.join(root, "project/tests"),
                ),
            ],
        )

    def test_has_no_class_or_module_fixture(self):
        for tests in guards.TESTS:
            self.assertEqual(fixtures(os.path.join(REPO, tests)), [])
        root = self.tree(
            {
                "a.py": "class A:\n    @classmethod\n    def setUpClass(cls): pass\n    @classmethod\n    async def tearDownClass(cls): pass\n",
                "b/c.py": (
                    "def setUpModule(): pass\ndef tearDownModule(): pass\ndef setUp(): pass\n"
                ),
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


class ObjectExports(Sample):
    EVENT = "delegate/src/agent_pm/delegate/event/"

    def test_package_must_define_all(self):
        root = self.tree({self.EVENT + "__init__.py": "class Event: pass\n"})
        self.assertEqual(
            guards.object_exports(root),
            [self.EVENT + "__init__.py: want one __all__ assignment"],
        )

    def test_missing_package_is_rejected(self):
        self.assertEqual(
            guards.object_exports(self.tree({})),
            [self.EVENT + "__init__.py: want one __all__ assignment"],
        )

    def test_all_must_name_literal_exports(self):
        for sample in ("__all__ = names\n", "__all__ = [123]\n"):
            with self.subTest(sample=sample):
                root = self.tree({self.EVENT + "__init__.py": sample})
                self.assertEqual(
                    guards.object_exports(root),
                    [self.EVENT + "__init__.py:1: __all__ must be a literal list of names"],
                )

    def test_direct_functions_and_lambdas_are_rejected(self):
        for definition in (
            "def action(): pass\n",
            "async def action(): pass\n",
            "action = lambda: None\n",
        ):
            with self.subTest(definition=definition):
                root = self.tree(
                    {self.EVENT + "__init__.py": definition + "__all__ = ['action']\n"}
                )
                self.assertEqual(
                    guards.object_exports(root),
                    [self.EVENT + "__init__.py:2: exported function action"],
                )

    def test_import_and_assignment_aliases_cannot_hide_reexported_functions(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "from .bridge import action as public\n"
                    "renamed = public\n__all__ = ['renamed']\n"
                ),
                self.EVENT + "bridge.py": "from .reply import parse as action\n",
                self.EVENT + "reply.py": "def parse(): pass\n",
            }
        )
        self.assertEqual(
            guards.object_exports(root),
            [self.EVENT + "__init__.py:3: exported function renamed"],
        )

    def test_module_attribute_aliases_cannot_hide_functions(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "import agent_pm.delegate.event.reply as replies\n"
                    "public = replies.parse\n__all__ = ['public']\n"
                ),
                self.EVENT + "reply.py": "def parse(): pass\n",
            }
        )
        self.assertEqual(
            guards.object_exports(root),
            [self.EVENT + "__init__.py:3: exported function public"],
        )

    def test_exported_class_methods_are_rejected(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "class Event:\n    @classmethod\n    def parse(cls): pass\n"
                    "public = Event.parse\n__all__ = ['public']\n"
                )
            }
        )
        self.assertEqual(
            guards.object_exports(root),
            [self.EVENT + "__init__.py:5: exported function public"],
        )

    def test_classes_exceptions_constants_and_unexported_functions_are_allowed(self):
        root = self.tree(
            {
                self.EVENT + "__init__.py": (
                    "from .reply import Event, EventError, TAG\n"
                    "def helper(): pass\n__all__: list[str] = ['Event', 'EventError', 'TAG']\n"
                ),
                self.EVENT + "reply.py": (
                    "class Event:\n    def parse(self): pass\n"
                    "class EventError(ValueError): pass\nTAG = 'event'\n"
                ),
            }
        )
        self.assertEqual(guards.object_exports(root), [])

    def test_other_packages_remain_outside_this_ticket(self):
        files = {self.EVENT + "__init__.py": "__all__ = []\n"}
        files.update(
            {
                f"{project}/src/agent_pm/{package}/__init__.py": "def run(): pass\n__all__ = ['run']\n"
                for project, package in (
                    ("utils", "utils"),
                    ("session", "session"),
                    ("delegate", "delegate"),
                    ("delegate", "delegate/composer"),
                    ("manager", "manager"),
                    ("linear-orchestrator", "linear_orchestrator"),
                )
            }
        )
        self.assertEqual(guards.object_exports(self.tree(files)), [])


class SessionNames(Sample):
    def test_configuration_forwarding_exemption_names_its_owner(self):
        self.assertEqual(
            guards.SESSION_NAMES_ALLOWED,
            {
                (guards.SESSION_SRC + "engine/config.py", "read_project"): "TASK-392",
            },
        )

    def test_layer_owned_config_entry_can_forward_to_utils(self):
        root = self.tree(
            {guards.SESSION_SRC + "engine/config.py": "from agent_pm.utils import read_project\n"}
        )
        self.assertEqual(guards.session_names(root), [])

    def test_config_readers_protocol_and_team_names_are_rejected(self):
        root = self.tree(
            {
                guards.SESSION_SRC
                + "engine/probe.py": "import tomllib\nfrom agent_pm.utils import read_project\nTAG = 'agent-pm-progress'\nTEAM = 'team/'\n"
            }
        )
        self.assertEqual(
            guards.session_names(root),
            [
                guards.SESSION_SRC + "engine/probe.py: names agent-pm-progress",
                guards.SESSION_SRC + "engine/probe.py: names read_project",
                guards.SESSION_SRC + "engine/probe.py: names team/",
                guards.SESSION_SRC + "engine/probe.py: names tomllib",
            ],
        )

    def test_runtime_state_json_and_other_layers_are_clean(self):
        root = self.tree(
            {
                guards.SESSION_SRC + "engine/probe.py": "import json\n",
                "delegate/src/agent_pm/delegate/probe.py": "import tomllib\n",
            }
        )
        self.assertEqual(guards.session_names(root), [])


class LiteralImports(Sample):
    def test_unknown_contract_types_fail_loudly(self):
        root = self.tree(
            {
                "pyproject.toml": "[[tool.importlinter.contracts]]\ntype = 'unknown'\n",
                "session/src/agent_pm/session/adapters/probe.py": "__import__('agent_pm.utils')",
            }
        )
        with self.assertRaisesRegex(ValueError, "unsupported import contract type: 'unknown'"):
            guards.literal_imports(root)

    def check(self, sample, project="session", module="adapters/probe.py"):
        config = Path(REPO, "pyproject.toml").read_text()
        root = self.tree(
            {
                "pyproject.toml": config,
                f"{project}/src/agent_pm/{guards.PROJECTS[project]}/{module}": sample,
            }
        )
        return guards.literal_imports(root)

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


class SourceSuppressions(Sample):
    def scoped(self, scope, comments, *, comment_only=False):
        lines = []
        parts = scope.split(".")
        for depth, name in enumerate(parts):
            declaration = (
                'if __name__ == "__main__":'
                if name == "__main__"
                else f"class {name}:"
                if name[0].isupper()
                else f"def {name}():"
            )
            lines.append("    " * depth + declaration)
        indent = "    " * len(parts)
        lines.extend(indent + ("" if comment_only else "pass  ") + comment for comment in comments)
        if comment_only:
            lines.append(indent + "pass")
        return "\n".join(lines) + "\n"

    def found(self, extra=None, *, replace=None):
        files = {}
        for (path, code, scope), reason in guards.NOQA_ALLOWED.items():
            comments = (
                [f"# pylint: disable={code}  # {reason}", f"# pylint: enable={code}"]
                if code == "duplicate-code"
                else [f"# noqa: {code}  # {reason}"]
            )
            files[path] = files.get(path, "") + self.scoped(scope, comments)
        files.update(extra or {})
        if replace is not None:
            files.pop(replace)
        return guards.source_suppressions(self.tree(files))

    def test_the_real_inventory_has_matching_comments(self):
        self.assertEqual(guards.source_suppressions(REPO), [])

    def test_hook_suppression_is_only_at_the_main_boundary(self):
        path = "session/src/agent_pm/session/modes/tui_hook.py"
        reason = guards.NOQA_ALLOWED[path, "BLE001", "__main__"]
        comment = f"# noqa: BLE001  # {reason}"
        self.assertEqual(
            self.found(
                {path: self.scoped("__main__", [comment]) + self.scoped("other", [comment])}
            ),
            [f"{path}:4: suppression BLE001 in other is not allowed"],
        )

    def test_pylint_disable_requires_a_matching_enable(self):
        path = "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/router.py"
        reason = guards.NOQA_ALLOWED[path, "duplicate-code", "outer"]
        other = self.scoped(
            "start_run", [f"# noqa: BLE001  # {guards.NOQA_ALLOWED[path, 'BLE001', 'start_run']}"]
        )
        self.assertEqual(
            self.found(
                {
                    path: self.scoped("outer", [f"# pylint: disable=duplicate-code  # {reason}"])
                    + other
                }
            ),
            [f"{path}: suppression duplicate-code in outer has no matching enable"],
        )

    def test_unlisted_pylint_blocks_and_unmatched_enables_are_rejected(self):
        path = "tests/other.py"
        self.assertEqual(
            self.found(
                {
                    path: self.scoped(
                        "other",
                        [
                            "# pylint: disable=duplicate-code  # not approved",
                            "# pylint: enable=duplicate-code",
                        ],
                    )
                }
            ),
            [
                f"{path}:2: suppression duplicate-code in other is not allowed",
                f"{path}:3: suppression duplicate-code in other has no matching disable",
            ],
        )

    def test_pylint_enable_cannot_close_another_scope(self):
        path = "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/router.py"
        reason = guards.NOQA_ALLOWED[path, "duplicate-code", "outer"]
        other = self.scoped(
            "start_run", [f"# noqa: BLE001  # {guards.NOQA_ALLOWED[path, 'BLE001', 'start_run']}"]
        )
        self.assertEqual(
            self.found(
                {
                    path: self.scoped("outer", [f"# pylint: disable=duplicate-code  # {reason}"])
                    + self.scoped("other", ["# pylint: enable=duplicate-code"])
                    + other
                }
            ),
            [
                f"{path}:4: suppression duplicate-code in other has no matching disable",
                f"{path}: suppression duplicate-code in outer has no matching enable",
            ],
        )

    def test_strings_docstrings_and_unrelated_comments_are_ignored(self):
        self.assertEqual(
            self.found(
                {
                    f"{PKG}/utils/unused.py": "# noqa\n",
                    "utils/src/agent_pm/utils/strings.py": (
                        '"""Example: # noqa and # type: ignore."""\nvalue = \'# noqa: F401\'\n# Suppressions are checked elsewhere.\n'
                    ),
                }
            ),
            [],
        )

    def test_bare_noqa_empty_codes_and_missing_reasons_are_rejected(self):
        path = "utils/src/agent_pm/utils/a.py"
        for comment in ("# noqa", "# noqa:", "# noqa: F401", "# noqa: BLE001"):
            with self.subTest(comment):
                self.assertEqual(
                    self.found({path: f"value = 1  {comment}\n"}),
                    [f"{path}:1: suppression needs explicit codes and a recorded reason"],
                )

    def test_type_ignores_are_rejected_with_or_without_codes(self):
        path = "delegate/src/agent_pm/delegate/a.py"
        for comment in ("# type: ignore", "# type: ignore[assignment]", "#type:ignore"):
            with self.subTest(comment):
                self.assertEqual(
                    self.found({path: f"value = 1  {comment}\n"}),
                    [f"{path}:1: suppression needs explicit codes and a recorded reason"],
                )

    def test_every_tree_rejects_tool_pragmas(self):
        comments = (
            "# ruff: noqa",
            "# ruff: noqa: E402",
            "# flake8: noqa",
            "# noqa: E402, I001",
            "# type: ignore",
            "# pyright: ignore",
            "# pylint: disable=all",
            "# isort: skip",
        )
        paths = (
            "session/src/agent_pm/session/sample.py",
            "session/tests/unit/sample_test.py",
            "tests/unit/sample_test.py",
            ".github/sample.py",
        )
        for path in paths:
            for comment in comments:
                with self.subTest(path=path, comment=comment):
                    self.assertEqual(
                        self.found({path: f"value = 1  {comment}\n"}),
                        [f"{path}:1: suppression needs explicit codes and a recorded reason"],
                    )

    def test_e402_i001_and_isort_comments(self):
        comments = (
            "# noqa: E402",
            "# noqa: F401,E402",
            "# NOQA:I001",
            "# isort: skip_file",
            "# ruff: noqa: E402",
        )
        path = "tests/a.py"
        source = "\n".join(f"import x  {comment}" for comment in comments) + "\n"
        self.assertEqual(
            self.found({path: source}),
            [
                f"{path}:{line}: suppression needs explicit codes and a recorded reason"
                for line in range(1, 6)
            ],
        )

    def test_a_rule_in_the_wrong_file_is_rejected(self):
        path = "utils/src/agent_pm/utils/a.py"
        self.assertEqual(
            self.found({path: "value = 1  # noqa: BLE001  # Forward failures.\n"}),
            [f"{path}:1: suppression BLE001 in <module> is not allowed"],
        )

    def test_compound_codes_cannot_hide_an_unlisted_rule(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        self.assertEqual(
            self.found({path: self.scoped(scope, [f"# noqa: {code}, F401  # {reason}"])}),
            [f"{path}:3: suppression F401 in {scope} is not allowed"],
        )

    def test_reason_must_match_the_inventory(self):
        (path, code, scope), _ = next(iter(guards.NOQA_ALLOWED.items()))
        self.assertEqual(
            self.found({path: self.scoped(scope, [f"# noqa: {code}  # Different reason."])}),
            [
                f"{path}:3: suppression {code} in {scope} reason differs from NOQA_ALLOWED",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )

    def test_each_inventory_entry_needs_a_comment(self):
        path, code, scope = next(iter(guards.NOQA_ALLOWED))
        for files, replace in (({path: "value = 1\n"}, None), ({}, path)):
            with self.subTest(replace=replace):
                self.assertEqual(
                    self.found(files, replace=replace),
                    [f"{path}: suppression {code} in {scope} has no matching comment"],
                )

    def test_duplicate_comments_cannot_reuse_an_entry(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        comment = f"# noqa: {code}  # {reason}"
        self.assertEqual(
            self.found({path: self.scoped(scope, [comment, comment])}),
            [f"{path}:4: duplicate suppression {code} in {scope}"],
        )

    def test_whitespace_case_and_comment_only_lines_are_checked(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        self.assertEqual(
            self.found(
                {
                    path: self.scoped(
                        scope, [f"#  NOQA : {code.lower()} # {reason}"], comment_only=True
                    )
                }
            ),
            [],
        )
        self.assertEqual(
            self.found({path: self.scoped(scope, [f"# noqa: {code} # {reason} # type: ignore"])}),
            [
                f"{path}:3: suppression {code} in {scope} reason differs from NOQA_ALLOWED",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )

    def test_same_file_and_rule_do_not_allow_another_function(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        comment = f"# noqa: {code} # {reason}"
        for other in ("other", "Other._pump", "HeadlessMode.other", "HeadlessMode._pump.nested"):
            with self.subTest(other):
                source = self.scoped(scope, [comment]) + self.scoped(other, [comment])
                line = len(scope.split(".")) + len(other.split(".")) + 2
                self.assertEqual(
                    self.found({path: source}),
                    [f"{path}:{line}: suppression {code} in {other} is not allowed"],
                )

    def test_nested_functions_use_the_innermost_qualified_name(self):
        self.assertEqual(self.found(), [])
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        comment = f"# noqa: {code} # {reason}"
        source = self.scoped(scope + ".nested", [comment])
        self.assertEqual(
            self.found({path: source}),
            [
                f"{path}:4: suppression {code} in {scope}.nested is not allowed",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )

    def test_async_methods_and_moved_module_comments_cannot_borrow_an_entry(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        source = self.scoped(scope, [f"# noqa: {code} # {reason}"])
        self.assertEqual(self.found({path: source.replace("def _pump", "async def _pump")}), [])
        self.assertEqual(
            self.found({path: f"pass  # noqa: {code} # {reason}\n"}),
            [
                f"{path}:1: suppression {code} in <module> is not allowed",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )


class PrivateImports(Sample):
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
                f"{PKG}/a.py": "import b as _b\nfrom c import d as _d\n_b.e, _d.f, _b.__file__\n\nclass G:\n    def h(self, i):\n        return self._j, i._k\n"
            }
        )
        self.assertEqual(guards.private_imports(root), [])


class ImportHygiene(Sample):
    def test_bootstrap_owners_may_prepend_roots_in_their_startup_function(self):
        root = self.tree(
            {
                path: (
                    f"import sys\ndef {function}(roots):\n"
                    "    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]\n"
                )
                for path, function in guards.IMPORT_BOOTSTRAPS.items()
            }
        )
        self.assertEqual(guards.import_hygiene(root), [])

    def test_bootstrap_permission_does_not_cover_other_scopes(self):
        assignment = "sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]"
        for path, function in guards.IMPORT_BOOTSTRAPS.items():
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
                    guards.import_hygiene(root),
                    sorted(f"{path}:{line}: sys.path mutation" for line in (2, 6, 8, 11)),
                )

    def test_bootstrap_permission_covers_only_one_root_prefix_assignment(self):
        for path, function in guards.IMPORT_BOOTSTRAPS.items():
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
                    guards.import_hygiene(root),
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
            guards.import_hygiene(root),
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
            guards.import_hygiene(root),
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
            guards.import_hygiene(root),
            [f"tests/a.py:{n}: module-level environment write" for n in range(2, 7)]
            + [f"tests/b.py:{n}: module-level environment write" for n in (5, 7, 8, 9)],
        )


class StandaloneImports(Sample):
    def test_tui_hook_bootstraps_package_imports(self):
        path = f"{guards.SESSION_SRC}modes/tui_hook.py"
        self.assertNotIn(path, guards.STANDALONE)
        self.assertEqual(guards.IMPORT_BOOTSTRAPS[path], "_bootstrap")

    def found(self, source):
        files = dict.fromkeys(guards.STANDALONE, source)
        return guards.standalone_imports(self.tree(files))

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
                for path in guards.STANDALONE
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
        missing = min(guards.STANDALONE)
        root = self.tree({path: "" for path in guards.STANDALONE if path != missing})
        self.assertEqual(guards.standalone_imports(root), [f"{missing}: missing standalone script"])

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
                for path in guards.STANDALONE
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
                for path in guards.STANDALONE
                for line, name in ((3, "agent_pm.utils"), (5, "requests"), (7, "other"))
            ),
        )

    def test_unlisted_module_is_not_a_standalone_script(self):
        files = dict.fromkeys(guards.STANDALONE, "")
        files[f"{guards.SESSION_SRC}engine/ordinary.py"] = "import agent_pm.utils\n"
        self.assertEqual(guards.standalone_imports(self.tree(files)), [])


class UnitTestModules(Sample):
    def test_empty_name_does_not_match_the_containing_source_directory(self):
        test = "session/tests/unit/_test.py"
        root = self.tree({test: "", "session/src/agent_pm/session/__init__.py": ""})
        self.assertEqual(
            guards.unit_test_modules(root),
            [f"{test}: missing source module session/src/agent_pm/session/.py"],
        )

    def test_matching_modules_in_every_project_are_accepted(self):
        files = {}
        for project, layer in guards.PROJECTS.items():
            files[f"{project}/tests/unit/engine/config_test.py"] = ""
            files[f"{project}/src/agent_pm/{layer}/engine/config.py"] = ""
        self.assertEqual(guards.unit_test_modules(self.tree(files)), [])

    def test_missing_nested_module_reports_both_repository_paths(self):
        test = "session/tests/unit/adapters/codex/removed_test.py"
        source = "session/src/agent_pm/session/adapters/codex/removed.py"
        self.assertEqual(
            guards.unit_test_modules(self.tree({test: ""})),
            [f"{test}: missing source module {source}"],
        )

    def test_wrapped_test_namespace_has_the_same_relative_mapping(self):
        root = self.tree(
            {
                "linear-orchestrator/tests/orchestrator_tests/unit/engine/router_test.py": "",
                "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/router.py": "",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_package_test_is_named_after_its_source_package(self):
        root = self.tree(
            {
                "delegate/tests/unit/composer_test.py": "",
                "delegate/src/agent_pm/delegate/composer/__init__.py": "",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_markdown_directory_matches_a_unit_test(self):
        root = self.tree(
            {
                "delegate/tests/unit/team/roles_test.py": "",
                "delegate/src/agent_pm/delegate/team/roles/engineer.md": "charter",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_empty_directory_does_not_match_a_unit_test(self):
        test = "delegate/tests/unit/team_test.py"
        source = "delegate/src/agent_pm/delegate/team.py"
        root = self.tree({test: ""})
        os.makedirs(os.path.join(root, "delegate/src/agent_pm/delegate/team"))
        self.assertEqual(
            guards.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_fake_suite_matches_a_helper_under_the_project_tests_root(self):
        root = self.tree(
            {
                "session/tests/unit/fakes/host_test.py": "",
                "session/tests/fakes/host.py": "",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_sibling_helper_does_not_replace_the_project_test_helper_path(self):
        root = self.tree(
            {
                "delegate/tests/unit/composer/support_test.py": "",
                "delegate/tests/unit/composer/support.py": "",
            }
        )
        self.assertEqual(
            guards.unit_test_modules(root),
            [
                "delegate/tests/unit/composer/support_test.py: missing source module delegate/src/agent_pm/delegate/composer/support.py"
            ],
        )

    def test_layer_package_suite_matches_its_public_init(self):
        root = self.tree(
            {
                "session/tests/unit/session_test.py": "",
                "session/src/agent_pm/session/__init__.py": "",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_source_directory_at_another_relative_path_does_not_match(self):
        test = "delegate/tests/unit/composer/roles_test.py"
        source = "delegate/src/agent_pm/delegate/composer/roles.py"
        root = self.tree(
            {test: "", "delegate/src/agent_pm/delegate/team/roles/engineer.md": "charter"}
        )
        self.assertEqual(
            guards.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_helper_at_another_relative_path_does_not_match(self):
        test = "session/tests/unit/host_test.py"
        source = "session/src/agent_pm/session/host.py"
        root = self.tree({test: "", "session/tests/fakes/host.py": ""})
        self.assertEqual(
            guards.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_source_package_init_suite_uses_the_package_name(self):
        test = "delegate/tests/unit/composer/__init___test.py"
        root = self.tree({test: "", "delegate/src/agent_pm/delegate/composer/__init__.py": ""})
        self.assertEqual(len(guards.unit_test_modules(root)), 1)

    def test_helpers_and_other_filename_extensions_are_not_checked(self):
        root = self.tree(
            {
                "utils/tests/unit/case.py": "",
                "utils/tests/unit/fakes/record.py": "",
                "utils/tests/unit/live_tmux.py": "",
                "utils/tests/unit/files_test.py.backup": "",
                "utils/tests/unit/files_test.txt": "",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_integration_and_repository_checks_are_not_checked(self):
        root = self.tree(
            {
                "session/tests/integration/unit/unmatched_test.py": "",
                "session/tests/session_tests/integration/unmatched_test.py": "",
                "tests/unit/unmatched_test.py": "",
                ".github/unit/unmatched_test.py": "",
            }
        )
        self.assertEqual(guards.unit_test_modules(root), [])

    def test_a_module_in_another_project_does_not_match(self):
        test = "manager/tests/unit/config_test.py"
        source = "manager/src/agent_pm/manager/config.py"
        root = self.tree({test: "", "utils/src/agent_pm/utils/config.py": ""})
        self.assertEqual(
            guards.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_missing_modules_are_reported_in_path_order(self):
        root = self.tree(
            {
                "session/tests/unit/z_test.py": "",
                "manager/tests/unit/a_test.py": "",
                "session/tests/unit/a_test.py": "",
            }
        )
        found = guards.unit_test_modules(root)
        self.assertEqual(found, sorted(found))
        self.assertEqual(len(found), 3)


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
            [f"{self.UTILS}/b.py:1: agent_pm.utils.a.A: a shared data type belongs in a types.py"],
        )
        self.assertEqual(self.found("types.py"), [])

    def test_a_relative_import_and_a_module_attribute_count(self):
        for user in ("from .a import A\n", "from agent_pm.utils import a\n\nx = a.A\n"):
            with self.subTest(user):
                root = self.tree({f"{self.UTILS}/a.py": self.DATA, f"{self.UTILS}/b.py": user})
                ((found,),) = [guards.shared_types(root)]
                self.assertIn(": agent_pm.utils.a.A: a shared data type", found)

    def test_a_package_re_export_is_clean(self):
        self.assertEqual(self.found("a.py", "__init__.py"), [])

    def test_package_data_types_stay_with_their_own_consumers(self):
        root = self.tree(
            {
                f"{self.UTILS}/owner/__init__.py": self.DATA,
                f"{self.UTILS}/owner/start.py": "from agent_pm.utils.owner import A\n",
                f"{self.UTILS}/outside.py": "from agent_pm.utils.owner import A\n",
            }
        )
        self.assertEqual(
            guards.shared_types(root),
            [
                f"{self.UTILS}/outside.py:1: agent_pm.utils.owner.A: a shared data type belongs in a types.py"
            ],
        )

    def test_namedtuple_enum_and_typeddict_are_counted(self):
        for source in (
            "from typing import NamedTuple\nclass A(NamedTuple):\n    value: int\n",
            "from enum import Enum\nclass A(Enum):\n    value = 1\n",
            "from enum import StrEnum\nclass A(StrEnum):\n    VALUE = 'value'\n",
            "from typing import TypedDict\nclass A(TypedDict):\n    value: int\n",
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
                        f"{self.UTILS}/b.py:1: agent_pm.utils.a.A: a shared data type belongs in a types.py"
                    ],
                )

    def test_data_type_import_aliases_and_qualified_names_are_counted(self):
        for source in (
            "from dataclasses import dataclass as dc\n@dc(frozen=True)\nclass A:\n    value: int\n",
            "import dataclasses as dc\n@dc.dataclass\nclass A:\n    value: int\n",
            "from typing import NamedTuple as NT\nclass A(NT):\n    value: int\n",
            "import typing as t\nclass A(t.NamedTuple):\n    value: int\n",
            "from enum import Enum as E\nclass A(E):\n    value = 1\n",
            "import enum as e\nclass A(e.Enum):\n    value = 1\n",
            "from typing import TypedDict as TD\nclass A(TD):\n    value: int\n",
            "import typing as t\nclass A(t.TypedDict):\n    value: int\n",
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
                        f"{self.UTILS}/b.py:1: agent_pm.utils.a.A: a shared data type belongs in a types.py"
                    ],
                )

    def test_behavior_classes_and_interfaces_stay_in_named_modules(self):
        for source in (
            "class A:\n    def run(self):\n        return 1\n",
            "from abc import ABC, abstractmethod\nclass A(ABC):\n    @abstractmethod\n    def run(self):\n        pass\n",
            "from typing import Protocol\nclass A(Protocol):\n    def run(self): ...\n",
            "class A(Exception):\n    pass\n",
        ):
            with self.subTest(source=source):
                root = self.tree(
                    {
                        f"{self.UTILS}/a.py": source,
                        f"{self.UTILS}/b.py": "from agent_pm.utils.a import A\n",
                    }
                )
                self.assertEqual(guards.shared_types(root), [])

    def test_standalone_definitions_and_consumers_keep_local_data_types(self):
        for path in guards.STANDALONE:
            with self.subTest(path=path):
                module = path.split("/src/")[1].removesuffix(".py").replace("/", ".")
                root = self.tree(
                    {
                        path: self.DATA,
                        f"{guards.SESSION_SRC}engine/ordinary.py": f"from {module} import A\n",
                    }
                )
                self.assertEqual(guards.shared_types(root), [])
                root = self.tree(
                    {
                        path: "from agent_pm.session.engine.ordinary import A\n",
                        f"{guards.SESSION_SRC}engine/ordinary.py": self.DATA,
                        f"{guards.SESSION_SRC}engine/consumer.py": (
                            "from agent_pm.session.engine.ordinary import A\n"
                        ),
                    }
                )
                self.assertEqual(
                    guards.shared_types(root),
                    [
                        f"{guards.SESSION_SRC}engine/consumer.py:1: agent_pm.session.engine.ordinary.A: a shared data type belongs in a types.py"
                    ],
                )


class TypesOnly(Sample):
    HOME = f"utils/{PKG}/utils"

    def found(self, source, name="types.py"):
        return guards.types_only(self.tree({f"{self.HOME}/{name}": source}))

    def test_strenum_is_a_data_type(self):
        self.assertEqual(
            self.found("from enum import StrEnum\nclass State(StrEnum):\n    LIVE = 'live'\n"), []
        )

    def test_top_level_sync_and_async_functions_are_reported(self):
        self.assertEqual(
            self.found("def one(): pass\nasync def two(): pass\n"),
            [
                f"{self.HOME}/types.py:1: types module function one",
                f"{self.HOME}/types.py:2: types module function two",
            ],
        )

    def test_functions_in_types_packages_and_conditionals_count(self):
        self.assertEqual(
            self.found("if True:\n    def helper(): pass\n", "types/helpers.py"),
            [f"{self.HOME}/types/helpers.py:2: types module function helper"],
        )
        self.assertEqual(self.found("def helper(): pass\n", "logic.py"), [])

    def test_private_helpers_used_inside_the_file_are_allowed(self):
        source = "from dataclasses import dataclass\ndef _valid(value): return value > 0\nasync def _async(value): return _valid(value)\n@dataclass\nclass Data:\n    value: int\n    def __post_init__(self): assert _valid(self.value)\n"
        self.assertEqual(self.found(source), [])

    def test_private_helpers_cannot_be_imported_or_used_in_other_files(self):
        for source, line in (
            ("from agent_pm.utils.types import _valid as valid\n", 1),
            ("from agent_pm.utils import types as data\ndata._valid(1)\n", 2),
            ("import agent_pm.utils.types as data\ndata._valid(1)\n", 2),
        ):
            with self.subTest(source=source):
                root = self.tree(
                    {
                        f"{self.HOME}/types.py": "def _valid(value): return value > 0\n",
                        f"{self.HOME}/consumer.py": source,
                    }
                )
                self.assertEqual(
                    guards.types_only(root),
                    [
                        f"{self.HOME}/consumer.py:{line}: private types helper agent_pm.utils.types._valid used outside its module"
                    ],
                )

    def test_plain_classes_are_reported(self):
        self.assertEqual(
            self.found("class Worker:\n    pass\n"),
            [f"{self.HOME}/types.py:1: Worker: not a type class"],
        )

    def test_allowed_class_kinds_and_aliases(self):
        source = "from dataclasses import dataclass as dc\nfrom typing import NamedTuple, TypedDict, Protocol\nfrom enum import Enum\nfrom abc import ABC\n@dc\nclass Data:\n    value: int\nclass Tuple(NamedTuple):\n    value: int\nclass Table(TypedDict):\n    value: int\nclass Choice(Enum):\n    value = 1\nclass Interface(Protocol):\n    def run(self): ...\nclass Abstract(ABC):\n    pass\nclass Error(Exception):\n    pass\nclass Child(Error):\n    pass\n"
        self.assertEqual(self.found(source), [])

    def test_a_str_enum_is_a_data_type(self):
        for source in (
            "from enum import StrEnum\nclass Choice(StrEnum):\n    ONE = 'one'\n",
            "import enum as e\nclass Choice(e.StrEnum):\n    ONE = 'one'\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(self.found(source), [])

    def test_dunders_properties_and_abstract_methods_are_allowed(self):
        source = "from abc import ABC, abstractmethod\nclass Interface(ABC):\n    def __init__(self): self.value = 1\n    @property\n    def value(self): return 1\n    @abstractmethod\n    def run(self): pass\n"
        self.assertEqual(self.found(source), [])

    def test_dataclass_methods_may_work_on_their_own_fields_and_methods(self):
        source = "from dataclasses import dataclass\n@dataclass\nclass Team:\n    root: str\n    def custom(self, path): return path != self.root\n    def source(self, path): return path if self.custom(path) else 'built-in'\n"
        self.assertEqual(self.found(source), [])

    def test_a_dataclass_classmethod_building_the_class_is_allowed(self):
        source = "from dataclasses import dataclass\n@dataclass\nclass Data:\n    value: int\n    @classmethod\n    def parse(cls, fields): return cls(fields['v']) if 'v' in fields else None\n    @classmethod\n    def other(cls, value): return value\n"
        self.assertEqual(
            self.found(source),
            [f"{self.HOME}/types.py:8: Data.other: business method in types module"],
        )

    def test_methods_on_other_kinds_and_unrelated_dataclass_methods_are_reported(self):
        source = "from dataclasses import dataclass\nfrom abc import ABC\nclass Interface(ABC):\n    def run(self): return 1\n@dataclass\nclass Data:\n    value: int\n    def run(self): return 1\n    def other(self): return self.unknown\n    @staticmethod\n    def factory(value): return value\n"
        self.assertEqual(
            self.found(source),
            [
                f"{self.HOME}/types.py:11: Data.factory: business method in types module",
                f"{self.HOME}/types.py:4: Interface.run: business method in types module",
                f"{self.HOME}/types.py:8: Data.run: business method in types module",
                f"{self.HOME}/types.py:9: Data.other: business method in types module",
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

    def test_only_init_cli_engine_and_listed_subpackages_and_entry_modules(self):
        files = {
            **self.package("utils", "__init__.py", "x.py"),
            **self.package(
                "session", "__init__.py", "extension.py", "engine/x.py", "modes/x.py", "extra.py"
            ),
            **self.package(
                "delegate", "__init__.py", "cli.py", "engine/x.py", "team/x.py", "misc/x.py"
            ),
            **self.package(guards.ORCH_PROJECT, "__init__.py", "engine/x.py", "linear/x.py"),
            **self.package("manager", "__init__.py", "engine/crew.py", "engine/roster.py", "y.py"),
        }
        self.assertEqual(
            guards.package_top(self.tree(files)),
            [
                f"delegate/{PKG}/delegate/misc: {UNLISTED}",
                f"manager/{PKG}/manager/y.py: {UNLISTED}",
                f"session/{PKG}/session/extra.py: {UNLISTED}",
            ],
        )

    def test_a_missing_entry(self):
        files = self.package("delegate", "__init__.py", "cli.py")
        self.assertIn(
            f"delegate/{PKG}/delegate/engine: missing", guards.package_top(self.tree(files))
        )


class TestBoundaries(Sample):
    def violations(self, sample, own, *, shipped=False):
        return guards.test_boundaries(
            self.tree(
                {
                    f"{own}/src/agent_pm/{own}/probe.py"
                    if shipped
                    else f"{own}/tests/probe.py": sample
                }
            )
        )

    def test_aliases_cannot_hide_own_module_mutation(self):
        prefixes = (
            "import agent_pm.session.engine.api as module\n",
            "from agent_pm.session.engine import api as module\n",
            "import agent_pm.session.engine.api as first\nmodule = first\n",
        )
        calls = (
            "patch.object(module, 'start')",
            "module.start = object()",
            "del module.start",
            "setattr(module, 'start', object())",
        )
        for prefix in prefixes:
            for call in calls:
                with self.subTest(prefix=prefix, call=call):
                    sample = "from unittest.mock import patch\n" + prefix + call
                    self.assertTrue(self.violations(sample, "session"))

    def test_patch_aliases_and_concatenated_targets_are_rejected(self):
        for call in (
            "p('agent_pm.session.start')",
            "p('agent_' + 'pm.session.start')",
            "p.dict(modules, {})",
        ):
            sample = "from unittest.mock import patch as p\nfrom sys import modules\n" + call
            with self.subTest(call=call):
                self.assertTrue(self.violations(sample, "session"))

    def test_module_table_mutations_have_exact_diagnostics(self):
        statements = (
            "modules['fake'] = object()",
            "del modules['fake']",
            "modules.pop('fake', None)",
            "modules.update({'fake': object()})",
            "cleanup(modules.pop, 'fake', None)",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertEqual(
                    self.violations("from sys import modules\n" + statement, "session"),
                    ["session/tests/probe.py:2: sys.modules mutation"],
                )

    def test_multiple_and_own_dictionary_patches_have_exact_diagnostics(self):
        prefix = "from unittest.mock import patch\nimport agent_pm.session.engine.api as api\n"
        for statement, reason in (
            ("patch.multiple('agent_pm.session.engine.api', start=object())", "shipped code patch"),
            ("patch.dict(api.MODES, {})", "shipped dictionary patch"),
        ):
            with self.subTest(statement=statement):
                self.assertEqual(
                    self.violations(prefix + statement, "session"),
                    [f"session/tests/probe.py:3: {reason}"],
                )

    def test_standard_library_and_lower_layer_fakes_are_allowed(self):
        sample = "from unittest.mock import patch\nimport os\nfrom session.tests import fake_adapter\npatch.object(os, 'getcwd')\npatch.object(fake_adapter, 'start')\nfake_adapter.start = object()\npatch.dict(os.environ, {})\n"
        self.assertEqual(self.violations(sample, "delegate"), [])

    def test_lower_layer_patches_have_exact_diagnostics(self):
        prefix = "from unittest.mock import patch\nfrom agent_pm import session\n"
        for statement, reason in (
            ("patch.object(session, 'sessions')", "shipped object patch"),
            ("patch('agent_pm.session.sessions')", "shipped code patch"),
            ("patch.multiple(session, sessions=object())", "shipped code patch"),
            ("patch.dict(session.MODES, {})", "shipped dictionary patch"),
        ):
            with self.subTest(statement=statement):
                self.assertEqual(
                    self.violations(prefix + statement, "manager"),
                    [f"manager/tests/probe.py:3: {reason}"],
                )

    def test_test_imports_are_rejected_including_dynamic_imports(self):
        for statement in (
            "import session.tests.unit.api_test as helper",
            "from session.tests.unit import api_test",
            "from .api_test import Helper",
            "importlib.import_module('session.tests.unit.api_test')",
        ):
            with self.subTest(statement=statement):
                self.assertTrue(self.violations(statement, "session"))

    def test_shipped_test_switches_are_rejected(self):
        for variable in ("ADAPTER_TEST", "FORCE_FAILED"):
            sample = f"import os\nos.getenv({variable!r})"
            with self.subTest(variable=variable):
                self.assertTrue(self.violations(sample, "session", shipped=True))
        self.assertEqual(self.violations("label = 'TEST_RESULT'", "session", shipped=True), [])

    def test_nested_standard_library_boundaries_and_keyword_patch_targets(self):
        root = self.tree(
            {
                "session/src/agent_pm/session/engine/api.py": "import time\n",
                "session/tests/probe.py": (
                    "from unittest.mock import patch\nfrom agent_pm.session.engine import api\npatch.object(api.time, 'sleep')\n"
                ),
            }
        )
        self.assertEqual(guards.test_boundaries(root), [])
        self.assertTrue(
            self.violations(
                "from unittest.mock import patch\npatch(target='agent_pm.session.start')", "session"
            )
        )


OPKG = "src/agent_pm/linear_orchestrator"


class OrchestratorText(Sample):
    def test_runtime_names_and_tool_strings_name_the_file_and_line(self):
        root = self.tree(
            {
                f"{OPKG}/router.py": 'X = "codex"\n',
                f"{OPKG}/a.py": (
                    'import subprocess\nsubprocess.run(["tmux", "ls"])\nK = "adapters"\n# Claude\n'
                ),
                "config.toml": '# claude\nx = "tmux"\ny = \'adapters\'\nz = "tmux ls"  # "tmux"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(self.wrap_orchestrator(root)),
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

    def test_agents_md_and_longer_strings_are_clean(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": '"""Reads AGENTS.md."""\nP = "AGENTS.md"\nH = "tmux attach"\n',
                "config.toml": '# see AGENTS.md\nx = "tmux attach"\n',
            }
        )
        self.assertEqual(guards.orchestrator_text(self.wrap_orchestrator(root)), [])

    def test_handoff_literals_only_in_issues(self):
        root = self.tree(
            {
                f"{OPKG}/linear/issues.py": 'H = "Handoff from"\nS = ("## Source", "## Instructions", "## Comments")\n',
                f"{OPKG}/promote.py": 'x = f"Handoff from {i}: {u}"\n# ## Comments are quoted\n',
                f"{OPKG}/target.py": 'M = "or its ## Instructions"\nS = "## Source"\n',
                "src/issues.py": 'H = "Handoff from"\n',
                "config.toml": 'x = "## Comments"\n',
                "tests/a_test.py": 'X = "Handoff from ## Source"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(self.wrap_orchestrator(root)),
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
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\nfrom agent_pm.linear_orchestrator.linear import linear\ndef main():\n    config.load()\n    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                f"{OPKG}/linear/writeback.py": (
                    "from agent_pm.linear_orchestrator.engine import config\nfrom agent_pm.linear_orchestrator.linear.linear import use_logs\n\ndef main():\n    config.load()\n    use_logs(None)\n"
                ),
                f"{OPKG}/engine/promote.py": (
                    "import agent_pm.linear_orchestrator.engine.config as c\n\nclass P:\n    def main(self):\n        c.load()\n"
                ),
                "src/run.py": "import agent_pm.linear_orchestrator.engine.config\n\ndef main():\n    agent_pm.linear_orchestrator.engine.config.load()\n",
            }
        )
        self.assertEqual(
            guards.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/promote.py:5: {self.LOAD}",
                f"{OPKG}/engine/run.py:8: {self.LOAD}",
                f"{OPKG}/linear/writeback.py:5: {self.LOAD}",
                f"{OPKG}/linear/writeback.py:6: {self.USE_LOGS}",
                f"src/run.py:4: {self.LOAD}",
            ],
        )

    def test_load_helpers_outside_config_py(self):
        root = self.tree(
            {
                f"{OPKG}/engine/run.py": (
                    "from agent_pm.linear_orchestrator.engine import config\nconfig._runnable(1)\n"
                ),
                f"{OPKG}/engine/config.py": (
                    "def _runnable():\n    pass\n\ndef load():\n    _runnable()\n    _docs()\n    _load_config()\n"
                ),
                f"{OPKG}/engine/promote.py": (
                    "from agent_pm.linear_orchestrator.engine import config\nfrom agent_pm.linear_orchestrator.linear import linear\ndef main():\n    try:\n        cfg = config.load()\n    except SystemExit:\n        linear.use_logs(None)\n"
                ),
                "src/config.py": "def load():\n    _runnable()\n",
            }
        )
        self.assertEqual(
            guards.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/run.py:2: calls agent_pm.linear_orchestrator.engine.config._runnable",
                "src/config.py:2: calls _runnable",
            ],
        )

    def test_the_load_helpers_are_config_py_functions(self):
        with open(os.path.join(REPO, guards.ORCH_PROJECT, guards.CONFIG_PY), encoding="utf-8") as f:
            defined = {n.name for n in ast.parse(f.read()).body if isinstance(n, ast.FunctionDef)}
        self.assertLessEqual(set(guards.LOAD_HELPERS), defined)

    def test_the_real_entries_calls_outside_an_entry(self):
        files = {}
        for name in ("run.py", "promote.py"):
            with open(
                os.path.join(ORCH_ROOT, "agent_pm", "linear_orchestrator", "engine", name),
                encoding="utf-8",
            ) as f:
                files[f"{OPKG}/not_{name}"] = f.read()
        found = guards.config_once(self.wrap_orchestrator(self.tree(files)))
        self.assertEqual(
            {(f.split(":")[0], f.split(": ", 1)[1]) for f in found},
            {(rel, call) for rel in files for call in (self.LOAD, self.USE_LOGS)},
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in (guards.orchestrator_text, guards.config_once):
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


class ConfigLevels(Sample):
    def levels(self, text):
        return guards.config_levels(self.tree({guards.CONFIG_LEVELS: text}))

    def test_the_layer_order_and_the_config_levels(self):
        self.assertEqual(
            guards.LAYERS, ("utils", "session", "delegate", guards.ORCH_PROJECT, "manager")
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
                "delegate/config.toml": (
                    'cwd = ""\n# local: cwd = "/x"\n[output]\ntype = "local"\ndir = ""\n[roles.a.output]\ntype = "x"\n# local: [roles.b]\n# local: cwd = "/y"\n# local: [roles.b.output]\n# local: dir = "d"\n'
                ),
                f"{guards.ORCH_PROJECT}/config.toml": (
                    '[labels]\n# local: [labels]\n# local: x = "1"\n[roles.a]\nkey = ""\n# local: [roles.c]\n# local: key = "k"\n# local: [core.roles.a]\n# local: cwd = "/z"\n'
                ),
            }
        )
        self.assertEqual(guards.explicit_keys(root), [])

    def test_sectioned_examples_keep_dynamic_empty_tables_and_reject_missing_keys(self):
        root = self.tree(
            {
                f"{guards.ORCH_PROJECT}/config.toml": (
                    '[labels]\n# local: [linear_orchestrator.labels]\n# local: x = "1"\n'
                    '[roles.a]\nkey = ""\n# local: [linear_orchestrator.roles.b]\n# local: key = "k"\n'
                    "# local: [linear_orchestrator]\n# local: typo = 1\n# local: [session]\n"
                )
            }
        )
        self.assertEqual(
            guards.explicit_keys(root),
            [
                f"{guards.ORCH_PROJECT}/config.toml: `# local:` key linear_orchestrator.typo has no real line",
            ],
        )

    def test_a_commented_only_key_is_refused(self):
        root = self.tree(
            {
                "session/config.toml": (
                    "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n"
                ),
                "delegate/config.toml": (
                    '# local: team_dirs = ["~/t"]\n[roles.a]\nt = 1\n# local: [roles.a]\n# local: [roles.a.deep]\n# local: y = 2\n'
                ),
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


OPKG = "src/agent_pm/linear_orchestrator"


class LayerCommands(Sample):
    SESSION, DELEGATE = (f"{layer}/{PKG}/{layer}" for layer in ("session", "delegate"))
    LINEAR = f"linear-orchestrator/{OPKG}"

    def test_a_layer_command_in_a_spawn_argv(self):
        root = self.tree(
            {
                f"{self.DELEGATE}/a.py": (
                    'import subprocess\nsubprocess.run(["agent-pm-session", "x"])\nsubprocess.Popen(cwd="/", args=("x", "agent-pm-role"))\n'
                ),
                f"{self.LINEAR}/b.py": 'import os\nfrom subprocess import check_output as out\nout("agent-pm-issue run", shell=True)\nos.execvp("agent-pm-x", ["agent-pm-x"])\nos.spawnl(os.P_WAIT, "/bin/agent-pm-y", f"agent-pm-{n}")\n',
                f"{self.SESSION}/c.py": 'import subprocess as sp\nsp.run(["agent-pm-session"])\nsp.call(["agent-pm-role"])\n',
                f"{self.DELEGATE}/team/tools/d.py": (
                    'import subprocess\nsubprocess.run(["agent-pm-x"])\n'
                ),
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
                f"{self.DELEGATE}/a.py": (
                    'import subprocess\nNAME = "agent-pm-progress"\nprint("agent-pm-x")\nsubprocess.run(["tmux", NAME, "/agent-pm-list"], env={"A": "agent-pm-x"})\n'
                ),
                f"manager/{PKG}/manager/crew.py": (
                    'import subprocess\nsubprocess.run(["agent-pm-x"])\n'
                ),
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
                f"{team}/roles/r.md": (
                    "x\nRun agent_pm.linear_orchestrator.engine.run, not linear-orchestrator\n"
                ),
                f"session/{PKG}/session/x.py": "import agent_pm.linear_orchestrator\n",
                f"utils/{PKG}/utils/y.py": "# linear_orchestrator\n",
                f"manager/{PKG}/manager/z.py": "# linear-orchestrator\n",
                f"delegate/{PKG}/__pycache__/x.pyc": "linear_orchestrator",
                "manager/skills/a/notes": (
                    "Linear orchestrator, linear_orchestra, agent_pm_lineage\n"
                ),
                "session/tests/a_test.py": "import agent_pm.linear_orchestrator\n",
                "linear-orchestrator/b.py": "import agent_pm.linear_orchestrator\n",
                "tests/c.py": "import agent_pm.linear_orchestrator\n",
                guards.CONFIG_LEVELS: (
                    'LEVELS = (("linear-orchestrator",),)\n# linear-orchestrator\n'
                ),
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
                "delegate/config.toml": '# codex in a comment\n[adapters.codex]\nimplementation = "agent_pm.session.adapters.codex:CodexAdapter"  # codex\nkey = "x # codex"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), ["delegate/config.toml:4: runtime name codex"])

    def test_toml_print_prompt_table_header_and_implementation_line_are_exempt(self):
        root = self.tree(
            {
                "delegate/config.toml": (
                    '# [adapters.print_prompt]  act-as\'s runtime\n[adapters.print_prompt]\nimplementation = "agent_pm.session.adapters.print_prompt:PrintPromptAdapter"\n'
                )
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_implementation_line_is_exempt_only_as_module_path_class(self):
        root = self.tree(
            {
                "delegate/config.toml": 'implementation = "codex"\nimplementation = "my.codex:Codex"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), ["delegate/config.toml:1: runtime name codex"])

    def test_toml_runtime_line_is_exempt_under_any_table(self):
        root = self.tree(
            {
                "delegate/config.toml": 'runtime = "codex"\n[roles.x]\nruntime = "codex"\n[roles.y]\nruntime = "codex"  # note\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_name_in_a_key_value_elsewhere(self):
        root = self.tree(
            {
                "delegate/config.toml": (
                    '[adapters.codex]\nimplementation = "agent_pm.session.adapters.codex:CodexAdapter"\n[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\nruntime = "codex" x\n'
                )
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
            guards.old_core(root),
            [
                ".github/c.py: imports core",
                "session/b.py: imports core",
                "tests/a.py: imports core",
            ],
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


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": sample,
                f"session/{PKG}/session/hosts/tmux/commands.py": sample,
                f"session/{PKG}/session/hosts/tmux/model/validation.py": sample,
                f"session/{PKG}/session/hosts/tmux_external/x.py": sample,
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
                    f'session/{PKG}/session/hosts/tmux/commands.py:2: str constant "tmux"',
                    f'session/{PKG}/session/hosts/tmux/model/validation.py:2: str constant "tmux"',
                    'session/src/hosts/tmux/tmux_host.py:2: str constant "tmux"',
                    f'session/{PKG}/session/modes/x.py:2: str constant "tmux"',
                    f'session/{PKG}/session/hosts/tmux_external/x.py:2: str constant "tmux"',
                ]
            ),
        )

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/modes/x.py": (
                    'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'
                )
            }
        )
        self.assertEqual(guards.tmux_calls(root), [])


class PythonCommands(Sample):
    def test_a_python_command_other_than_p_m_agent_pm_or_a_team_tool(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/config.toml": 'commands = ["python3 x.py", "python3 -m agent_pm.x"]\n# python3 -P {{team}}/tools/repo.py\ngate = "python3 -P {{team}}/x.py"\n',
                "linear-orchestrator/config.toml": 'gate = "python3 -P -m agent_pmx.y"\n',
                "delegate/skills/a/SKILL.md": "Run `python3.11 x.py`.\n- Bash(python -P -m agent_pm.delegate.cli --list)\n",
                "manager/skills/b/SKILL.md": "Run `python3 y.py`.\n",
                f"{team}/roles/r.md": (
                    "1. `python3 -P agent_pm.x`\n2. /usr/bin/python3 -I -m agent_pm.x\n3. `env PYTHONPATH=x python3 -P -m agent_pm.linear_orchestrator.engine.run`\n4. `python3 -P <team>/tools/repo.py status --dir x`\n5. `python3 -P <team>/tools/../repo.py`\n6. `python3 -P <team>/tools/repo.pyc`\n"
                ),
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


class StatusLogFormat(Sample):
    CLOCK = 'CLOCK = "%H:%M:%S"\n'

    def test_the_real_tree_is_clean(self):
        self.assertEqual(guards.status_log_format(REPO), [])

    def test_the_clock_in_another_projects_module_or_an_f_string(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": self.CLOCK,
                f"manager/{PKG}/manager/y.py": 'import time\n\nline = f"{time.strftime(\'%H:%M:%S\')} {name}"\nhook = f"$(date +%H:%M:%S) {name} dead"\n',
                f"linear-orchestrator/{OPKG}/z.py": 'import time\nat = time.strftime("%H:%M:%S")\n',
            }
        )
        outside = (
            "status log line format outside session/engine/status_log.py or hosts/tmux/tmux_host.py"
        )
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

    def test_status_log_and_tmux_host_own_the_clock(self):
        root = self.tree(
            {
                f"session/{PKG}/session/engine/status_log.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/lifecycle.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/model/clock.py": self.CLOCK,
                "session/tests/x_test.py": self.CLOCK,
                "tests/y_test.py": self.CLOCK,
            }
        )
        outside = (
            "status log line format outside session/engine/status_log.py or hosts/tmux/tmux_host.py"
        )
        self.assertEqual(
            guards.status_log_format(root),
            [
                f"session/{PKG}/session/hosts/tmux/lifecycle.py:1: {outside}",
                f"session/{PKG}/session/hosts/tmux/model/clock.py:1: {outside}",
            ],
        )

    def test_a_similarly_named_subsystem_cannot_own_the_clock(self):
        root = self.tree({f"session/{PKG}/session/hosts/tmux_external/x.py": self.CLOCK})
        self.assertEqual(
            guards.status_log_format(root),
            [
                f"session/{PKG}/session/hosts/tmux_external/x.py:1: status log line format outside session/engine/status_log.py or hosts/tmux/tmux_host.py"
            ],
        )


class OptionTuples(Sample):
    FAILS = "a fixed option set is a StrEnum (FR-49)"
    STALE = "allowlisted but not an option tuple"

    def found(self, extra=None, *, instead=None):
        files = {}
        for key in guards.OPTION_TUPLES_ALLOWED:
            rel, name = key
            files[rel] = files.get(rel, "") + (instead or {}).get(key, f'{name} = ("a", "b")\n')
        for rel, text in (extra or {}).items():
            files[rel] = files.get(rel, "") + text
        return guards.option_tuples(self.tree(files))

    def test_the_real_tree_is_clean(self):
        self.assertEqual(guards.option_tuples(REPO), [])

    def test_allowlisted_tuples_pass_each_with_a_one_line_reason(self):
        self.assertEqual(self.found(), [])
        for key, reason in guards.OPTION_TUPLES_ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip())
                self.assertNotIn("\n", reason)

    def test_a_module_level_tuple_of_option_words_fails(self):
        rel = f"delegate/{PKG}/delegate/x.py"
        source = (
            'X = ("a", "b")\n'
            '_HOOKS: tuple = ("pane-exited", "window-resized")\n'
            'Y = Z = ("a_b", "c2", "d")\n'
            'TIERS = ("1", "2", "3")\n'
        )
        self.assertEqual(
            self.found({rel: source}),
            [
                f"{rel}:1: X: {self.FAILS}",
                f"{rel}:2: _HOOKS: {self.FAILS}",
                f"{rel}:3: Y: {self.FAILS}",
                f"{rel}:3: Z: {self.FAILS}",
                f"{rel}:4: TIERS: {self.FAILS}",
            ],
        )

    def test_top_level_if_and_try_blocks_and_standalone_scripts_count(self):
        rel = f"session/{PKG}/session/modes/tui_hook.py"
        source = (
            'if True:\n    A = ("a", "b")\nelse:\n    B = ("a", "b")\n'
            'try:\n    C = ("a", "b")\nexcept ImportError:\n    D = ("a", "b")\n'
            'finally:\n    E = ("a", "b")\n'
        )
        self.assertEqual(
            self.found({rel: source}),
            sorted(
                f"{rel}:{line}: {name}: {self.FAILS}"
                for line, name in ((2, "A"), (4, "B"), (6, "C"), (8, "D"), (10, "E"))
            ),
        )

    def test_other_shapes_places_and_trees_pass(self):
        source = (
            'ONE = ("a",)\n'
            'MIXED = ("a", 1)\n'
            'PATHS = ("crew.py", "drive.py")\n'
            'FLAGS = ("-P", "-m")\n'
            'UPPER = ("A", "B")\n'
            'SPACED = ("a b", "c")\n'
            'VERSIONS = ("1.0", "2.0")\n'
            'SIGNED = ("-1", "2")\n'
            'LISTED = ["a", "b"]\n'
            'NESTED = (("a", "b"), ("c", "d"))\n'
            'lower = ("a", "b")\n'
            'Mixed = ("a", "b")\n'
            "BARE: tuple[str, ...]\n"
            'A, B = ("a", "b")\n'
            'def f():\n    LOCAL = ("a", "b")\n'
            'class C:\n    FIELD = ("a", "b")\n'
            'with open("x") as f:\n    WITH = ("a", "b")\n'
        )
        self.assertEqual(
            self.found(
                {
                    f"delegate/{PKG}/delegate/x.py": source,
                    "delegate/tests/unit/x_test.py": 'X = ("a", "b")\n',
                    "delegate/skills/tool.py": 'X = ("a", "b")\n',
                    "tests/x.py": 'X = ("a", "b")\n',
                }
            ),
            [],
        )

    def test_an_allowlisted_name_that_is_no_longer_an_option_tuple_is_stale(self):
        key = rel, name = next(iter(guards.OPTION_TUPLES_ALLOWED))
        for source in (
            "",
            f"{name} = tuple(range(2))\n",
            f'{name} = ("a",)\n',
            f'def f():\n    {name} = ("a", "b")\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    self.found(instead={key: source}), [f"{rel}: {name}: {self.STALE}"]
                )

    def test_every_entry_is_stale_in_an_empty_tree(self):
        self.assertEqual(
            guards.option_tuples(self.tree({})),
            sorted(f"{rel}: {name}: {self.STALE}" for rel, name in guards.OPTION_TUPLES_ALLOWED),
        )


class ObjectExports(Sample):
    SESSION = f"session/{PKG}/session/"
    INIT = SESSION + "__init__.py"
    EXTENSION = SESSION + "extension.py"
    WRAPPERS = ("close", "note", "session_record", "sessions")
    ENTRY_FUNCTIONS = (*WRAPPERS, "read_config")
    BAD = ("OS", "Shut", "modes", "f", "g", "LAMBDA", "Ghost")
    BAD_SOURCE = (
        "import os as OS\n"
        "from agent_pm.session.engine.forwarding import close as Shut\n"
        "from agent_pm.session import modes\n"
        "def f(): pass\n"
        "g = f\n"
        "LAMBDA = lambda: 0\n"
    )

    def init(self, source="", names=()):
        """A session __init__ exporting the allowlisted wrappers (forwarding.py's), then `names`."""
        imported = f"from agent_pm.session.engine.forwarding import {', '.join(self.WRAPPERS)}\n"
        imported += "from agent_pm.session.engine.config import read_config\n"
        return f"{imported}{source}__all__ = {[*self.ENTRY_FUNCTIONS, *names]!r}\n"

    def found(self, files=None):
        tree = {
            self.SESSION + "engine/forwarding.py": "".join(
                f"def {name}(): pass\n" for name in self.WRAPPERS
            ),
            self.SESSION + "engine/config.py": "def read_config(): pass\n",
            self.SESSION + "modes/__init__.py": "",
            self.INIT: self.init(),
            self.EXTENSION: "__all__ = []\n",
            **(files or {}),
        }
        return guards.object_exports(self.tree(tree))

    def test_the_real_tree_is_clean(self):
        self.assertEqual(guards.object_exports(REPO), [])

    def test_the_allowlisted_wrappers_pass_each_with_a_one_line_reason(self):
        self.assertEqual(self.found(), [])
        self.assertEqual(
            set(guards.OBJECT_EXPORTS_ALLOWED),
            {("agent_pm.session", n) for n in self.ENTRY_FUNCTIONS},
        )
        for key, reason in guards.OBJECT_EXPORTS_ALLOWED.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip())
                self.assertNotIn("\n", reason)

    def test_a_function_alias_lambda_module_or_unbound_name_fails_in_either_entry_module(self):
        for rel, text in (
            (self.INIT, self.init(self.BAD_SOURCE, self.BAD)),
            (self.EXTENSION, f"{self.BAD_SOURCE}__all__ = {list(self.BAD)!r}\n"),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(
                    self.found({rel: text}),
                    sorted(f"{rel}: __all__ exports function {n} (FR-44)" for n in self.BAD),
                )

    def test_a_conditional_alias_or_partial_of_a_function_fails_in_either_entry_module(self):
        source = "import functools\ndef f(): pass\nalias = f if True else f\npart = functools.partial(f)\n"
        names = ("alias", "part")
        for rel, text in (
            (self.INIT, self.init(source, names)),
            (self.EXTENSION, f"{source}__all__ = {list(names)!r}\n"),
        ):
            with self.subTest(rel=rel):
                self.assertEqual(
                    self.found({rel: text}),
                    sorted(f"{rel}: __all__ exports unrecognised value {n} (FR-44)" for n in names),
                )

    def test_classes_exceptions_enums_constants_and_type_aliases_pass(self):
        extension = (
            "import signal\n"
            "from pathlib import Path\n"
            "from agent_pm.session.engine.types import Error, Event, Mode, Spec\n"
            "from agent_pm.utils import Display\n"
            "Handle = Spec\n"
            "SIGNALS = (signal.SIGHUP, signal.SIGTERM)\n"
            'NAME = "x"\n'
        )
        names = ["Spec", "Mode", "Error", "Event", "Display", "Handle", "SIGNALS", "NAME", "Path"]
        types = (
            "from enum import StrEnum\n"
            "class Spec: pass\n"
            'class Mode(StrEnum):\n    A = "a"\n'
            "class Error(Exception): pass\n"
            "Event = Spec | Mode\n"
        )
        utils = f"utils/{PKG}/utils/"
        self.assertEqual(
            self.found(
                {
                    self.EXTENSION: f"{extension}__all__ = {names!r}\n",
                    self.SESSION + "engine/types.py": types,
                    utils + "__init__.py": "from agent_pm.utils.record import Display\n"
                    'def helper(): pass\n__all__ = ["Display", "helper"]\n',
                    utils + "record.py": "Display = str | None\n",
                }
            ),
            [],
        )

    def test_without_a_literal_all_of_names_an_entry_module_fails(self):
        for source in ("", "__all__ = names()\n", "__all__ = [1]\n", '__all__ = ("A",)\n'):
            with self.subTest(source=source):
                self.assertEqual(
                    self.found({self.EXTENSION: source}), [f"{self.EXTENSION}: no literal __all__"]
                )

    def test_an_allowlisted_name_no_longer_exported_as_a_function_is_stale(self):
        stale = "allowlisted but not an exported function (FR-44)"
        init = 'class close: pass\n__all__ = ["close"]\n'
        self.assertEqual(
            self.found({self.INIT: init}),
            sorted(f"agent_pm.session: {name}: {stale}" for name in self.ENTRY_FUNCTIONS),
        )


class ManagerBoundaries(Sample):
    def test_only_orphan_listing_is_hardcoded(self):
        base = "manager/src/agent_pm/manager/engine/crew.py"
        root = self.tree(
            {
                base: 'LS = ("agent-pm-session", "ls", "--here", "--records")\nBAD = ["agent-pm-session", "status"]\n'
            }
        )
        self.assertEqual(
            guards.manager_commands(root), [f"{base}:2: hardcoded layer command agent-pm-session"]
        )

    def test_display_is_opaque(self):
        base = "manager/src/agent_pm/manager/engine/crew.py"
        root = self.tree({base: 'def f(pane): return "@opener"\n'})
        self.assertEqual(len(guards.manager_display(root)), 2)
        clean = self.tree({base: 'def f(display): return display.split(":")\n'})
        self.assertEqual(guards.manager_display(clean), [])

    def test_exports_require_all_and_reject_reexported_functions(self):
        base = "manager/src/agent_pm/manager/"
        root = self.tree(
            {
                base
                + "__init__.py": 'from agent_pm.manager.engine.crew import f\n__all__ = ["f"]\n',
                base + "engine/crew.py": "def f(): pass\n",
            }
        )
        self.assertEqual(guards.manager_exports(root), [f"{base}__init__.py:2: exports function f"])
        missing = self.tree({base + "__init__.py": ""})
        self.assertEqual(guards.manager_exports(missing), [f"{base}__init__.py: missing __all__"])
        clean = self.tree(
            {
                base + "__init__.py": 'class Crew: pass\n__all__ = ["Crew"]\n',
                "utils/src/agent_pm/utils/__init__.py": 'def f(): pass\n__all__ = ["f"]\n',
            }
        )
        self.assertEqual(guards.manager_exports(clean), [])

    def test_function_aliases_lambdas_stdlib_and_modules_are_not_exports(self):
        base = "manager/src/agent_pm/manager/__init__.py"
        root = self.tree(
            {
                base: 'from os import getcwd as CWD\nimport os as OS\ndef f(): pass\ng = f\nLAMBDA = lambda: 0\n__all__ = ["CWD", "OS", "g", "LAMBDA"]\n'
            }
        )
        self.assertEqual(
            guards.manager_exports(root),
            [
                f"{base}:6: exports function CWD",
                f"{base}:6: exports function LAMBDA",
                f"{base}:6: exports function g",
                f"{base}:6: exports nonconstant OS",
            ],
        )
        clean = self.tree(
            {base: 'from pathlib import Path\nVALUE = "one"\n__all__ = ["Path", "VALUE"]\n'}
        )
        self.assertEqual(guards.manager_exports(clean), [])

    def test_exports_require_a_literal_sequence_of_names(self):
        base = "manager/src/agent_pm/manager/__init__.py"
        for source in ("__all__ = discover()\n", "__all__ = [1]\n"):
            self.assertEqual(
                guards.manager_exports(self.tree({base: source})),
                [f"{base}:1: __all__ must be a literal sequence of names"],
            )

    def test_crew_never_reads_an_inherited_sid(self):
        base = "manager/src/agent_pm/manager/engine/crew.py"
        for source in (
            'import os\nx = os.getenv("AGENT_PM_SID")\n',
            'import os\nx = os.environ.get("AGENT_PM_SID")\n',
            'import os\nx = os.environ["AGENT_PM_SID"]\n',
        ):
            self.assertEqual(
                guards.manager_sid(self.tree({base: source})),
                [f"{base}:2: reads inherited AGENT_PM_SID"],
            )
        self.assertEqual(guards.manager_sid(self.tree({base: 'env = {"AGENT_PM_SID": sid}\n'})), [])

    def test_roster_has_no_session_liveness_callback(self):
        base = "manager/src/agent_pm/manager/engine/roster.py"
        self.assertEqual(
            guards.manager_lease(
                self.tree(
                    {base: 'class Roster:\n    def probe(self): return self._alive("manager")\n'}
                )
            ),
            [f"{base}:2: lease liveness callback"],
        )


class RealTree(Case):
    def test_each_guard_is_clean(self):
        for name, found in checks(REPO):
            with self.subTest(name):
                self.assertEqual(found, [])

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


class UnitIsolation(Sample):
    def test_tests_cannot_patch_or_reach_the_interpreter_gate(self):
        samples = (
            "from unittest import mock\nmock.patch('isolation.Gate.audit')\n",
            "from unittest.mock import patch\npatch.object('isolation.Gate', 'audit')\n",
            "import run_tests as runner\nrunner.GATE.unit = False\n",
            "from run_tests import GATE as gate\ngate.unit = False\n",
            "import run_tests\nrun_tests.isolation.Gate.audit = lambda: None\n",
        )
        for index, text in enumerate(samples):
            with self.subTest(text=text):
                path = f".github/sample{index}_test.py"
                found = guards.test_gate_mutation(self.tree({path: text}))
                self.assertTrue(found, text)
                self.assertTrue(all(line.startswith(path + ":") for line in found), found)

    def test_no_migration_exemption_can_be_reintroduced(self):
        root = self.tree({"session/tests/unit/present_test.py": ""})
        self.assertEqual(guards.unit_isolation_allowlist(root), [])
        with mock.patch.dict(
            guards.UNIT_ISOLATION_ALLOWED, {"session/tests/unit/present_test.py": "escape"}
        ):
            self.assertEqual(
                guards.unit_isolation_allowlist(root),
                [f"{root}/tests/guards.py: unit isolation allowlist must remain empty"],
            )
