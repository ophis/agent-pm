import ast
import importlib
import json
import os
import subprocess
import unittest
from pathlib import Path

from tests import guards
from tests.case import ORCH_ROOT, PY, REPO, SRC, Case, child_env
from tests.checks import checks
from tests.guard_samples import PKG, Sample, fixtures, flat, own_case, test_suites

OPKG = "src/agent_pm/linear_orchestrator"


PROBE = "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"


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
            for start, import_root in test_suites(REPO, top):
                tests = loader.discover(start, "*_test.py", import_root)
                found += [test.id() for test in flat(tests) if not own_case(test, top)]
        self.assertEqual((loader.errors, found), ([], []))

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


class SourceSuppressions(Sample):
    def scoped(self, scope, comments, *, comment_only=False):
        lines = []
        parts = scope.split(".")
        for depth, name in enumerate(parts):
            declaration = f"class {name}:" if name[0].isupper() else f"def {name}():"
            lines.append("    " * depth + declaration)
        indent = "    " * len(parts)
        lines.extend(indent + ("" if comment_only else "pass  ") + comment for comment in comments)
        if comment_only:
            lines.append(indent + "pass")
        return "\n".join(lines) + "\n"

    def found(self, extra=None, *, replace=None):
        files = {}
        for (path, code, scope), reason in guards.NOQA_ALLOWED.items():
            files[path] = files.get(path, "") + self.scoped(scope, [f"# noqa: {code}  # {reason}"])
        files.update(extra or {})
        if replace is not None:
            files.pop(replace)
        return guards.source_suppressions(self.tree(files))

    def test_the_real_inventory_has_matching_comments(self):
        self.assertEqual(guards.source_suppressions(REPO), [])

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

    def test_namedtuple_enum_and_typeddict_are_counted(self):
        for source in (
            "from typing import NamedTuple\nclass A(NamedTuple):\n    value: int\n",
            "from enum import Enum\nclass A(Enum):\n    value = 1\n",
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

    def test_dunders_properties_and_abstract_methods_are_allowed(self):
        source = "from abc import ABC, abstractmethod\nclass Interface(ABC):\n    def __init__(self): self.value = 1\n    @property\n    def value(self): return 1\n    @abstractmethod\n    def run(self): pass\n"
        self.assertEqual(self.found(source), [])

    def test_dataclass_methods_may_work_on_their_own_fields_and_methods(self):
        source = "from dataclasses import dataclass\n@dataclass\nclass Team:\n    root: str\n    def custom(self, path): return path != self.root\n    def source(self, path): return path if self.custom(path) else 'built-in'\n"
        self.assertEqual(self.found(source), [])

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


class CommentWrap(Sample):
    def check(self, source):
        return guards.comment_wrap(self.tree({"tests/a.py": source}))

    def test_comments_and_docstrings_report_the_previous_line(self):
        for source in ("# Short words\n# fit here\nx = 1\n", '"""Short words\nfit here."""\n'):
            with self.subTest(source=source):
                self.assertEqual(
                    self.check(source), ["tests/a.py:1: next word 'fit' fits within 100 columns"]
                )

    def test_a_full_line_a_last_line_and_list_starters_are_clean(self):
        for prefix in ("# ", '"""'):
            for following in ("fit", "- item", "* item", "1. item", "2) item"):
                first = "word " * 19 + "end"
                closing = "\n" if prefix == "# " else '"""\n'
                second = "# " if prefix == "# " else ""
                with self.subTest(prefix=prefix, following=following):
                    self.assertEqual(
                        self.check(prefix + first + "\n" + second + following + closing), []
                    )
        self.assertEqual(self.check("# Alone\nx = 1\n"), [])

    def test_blank_lines_different_indent_and_inline_comments_split_blocks(self):
        for source in (
            "# first\n\n# second\n",
            "# first\n#\n# second\n",
            "# first\ndef f():\n    # second\n    return 1\n",
            "x = 1 # first\ny = 2 # second\n",
            '"""first\n\nsecond"""\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(self.check(source), [])

    def test_code_paragraphs_preserve_fences_indent_and_doctests(self):
        for source in (
            '"""Example.\n\n```python\nx = 1\ny = 2\n```\n"""\n',
            '"""Example.\n\n    x = 1\n    y = 2\n"""\n',
            "# Example.\n#\n#     x = 1\n#     y = 2\n",
            '"""Example.\n\n>>> x = 1\n>>> y = 2\n"""\n',
        ):
            with self.subTest(source=source):
                self.assertEqual(self.check(source), [])


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
            ("patch.dict(api.MODES, {})", "own project dictionary patch"),
        ):
            with self.subTest(statement=statement):
                self.assertEqual(
                    self.violations(prefix + statement, "session"),
                    [f"session/tests/probe.py:3: {reason}"],
                )

    def test_standard_library_and_lower_layer_fakes_are_allowed(self):
        sample = "from unittest.mock import patch\nimport os\nfrom session.tests import fake_adapter\npatch.object(os, 'getcwd')\npatch.object(fake_adapter, 'start')\nfake_adapter.start = object()\npatch.dict(os.environ, {})\n"
        self.assertEqual(self.violations(sample, "delegate"), [])

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


PROBE = "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"


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


PROBE = "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"


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
                "delegate/config.toml": '# codex in a comment\n[adapters.codex]\nimplementation = "adapters.codex:CodexAdapter"  # codex\nkey = "x # codex"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), ["delegate/config.toml:4: runtime name codex"])

    def test_toml_print_prompt_table_header_and_implementation_line_are_exempt(self):
        root = self.tree(
            {
                "delegate/config.toml": (
                    '# [adapters.print_prompt]  act-as\'s runtime\n[adapters.print_prompt]\nimplementation = "adapters.print_prompt:PrintPromptAdapter"\n'
                )
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

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
                    '[adapters.codex]\nimplementation = "adapters.codex:CodexAdapter"\n[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\nruntime = "codex" x\n'
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
