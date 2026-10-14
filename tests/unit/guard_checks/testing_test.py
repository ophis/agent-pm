import os
from unittest import mock

from tests import guards
from tests.case import REPO
from tests.guard_checks import testing
from tests.guard_samples import Sample, fixtures, test_suites


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


class UnitTestModules(Sample):
    def test_empty_name_does_not_match_the_containing_source_directory(self):
        test = "session/tests/unit/_test.py"
        root = self.tree({test: "", "session/src/agent_pm/session/__init__.py": ""})
        self.assertEqual(
            testing.unit_test_modules(root),
            [f"{test}: missing source module session/src/agent_pm/session/.py"],
        )

    def test_matching_modules_in_every_project_are_accepted(self):
        files = {}
        for project, layer in guards.PROJECTS.items():
            files[f"{project}/tests/unit/engine/config_test.py"] = ""
            files[f"{project}/src/agent_pm/{layer}/engine/config.py"] = ""
        self.assertEqual(testing.unit_test_modules(self.tree(files)), [])

    def test_missing_nested_module_reports_both_repository_paths(self):
        test = "session/tests/unit/adapters/codex/removed_test.py"
        source = "session/src/agent_pm/session/adapters/codex/removed.py"
        self.assertEqual(
            testing.unit_test_modules(self.tree({test: ""})),
            [f"{test}: missing source module {source}"],
        )

    def test_wrapped_test_namespace_has_the_same_relative_mapping(self):
        root = self.tree(
            {
                "linear-orchestrator/tests/orchestrator_tests/unit/engine/router_test.py": "",
                "linear-orchestrator/src/agent_pm/linear_orchestrator/engine/router.py": "",
            }
        )
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_package_test_is_named_after_its_source_package(self):
        root = self.tree(
            {
                "delegate/tests/unit/composer_test.py": "",
                "delegate/src/agent_pm/delegate/composer/__init__.py": "",
            }
        )
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_markdown_directory_matches_a_unit_test(self):
        root = self.tree(
            {
                "delegate/tests/unit/team/roles_test.py": "",
                "delegate/src/agent_pm/delegate/team/roles/engineer.md": "charter",
            }
        )
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_empty_directory_does_not_match_a_unit_test(self):
        test = "delegate/tests/unit/team_test.py"
        source = "delegate/src/agent_pm/delegate/team.py"
        root = self.tree({test: ""})
        os.makedirs(os.path.join(root, "delegate/src/agent_pm/delegate/team"))
        self.assertEqual(
            testing.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_fake_suite_matches_a_helper_under_the_project_tests_root(self):
        root = self.tree(
            {
                "session/tests/unit/fakes/host_test.py": "",
                "session/tests/fakes/host.py": "",
            }
        )
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_sibling_helper_does_not_replace_the_project_test_helper_path(self):
        root = self.tree(
            {
                "delegate/tests/unit/composer/support_test.py": "",
                "delegate/tests/unit/composer/support.py": "",
            }
        )
        self.assertEqual(
            testing.unit_test_modules(root),
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
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_source_directory_at_another_relative_path_does_not_match(self):
        test = "delegate/tests/unit/composer/roles_test.py"
        source = "delegate/src/agent_pm/delegate/composer/roles.py"
        root = self.tree(
            {test: "", "delegate/src/agent_pm/delegate/team/roles/engineer.md": "charter"}
        )
        self.assertEqual(
            testing.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_helper_at_another_relative_path_does_not_match(self):
        test = "session/tests/unit/host_test.py"
        source = "session/src/agent_pm/session/host.py"
        root = self.tree({test: "", "session/tests/fakes/host.py": ""})
        self.assertEqual(
            testing.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_source_package_init_suite_uses_the_package_name(self):
        test = "delegate/tests/unit/composer/__init___test.py"
        root = self.tree({test: "", "delegate/src/agent_pm/delegate/composer/__init__.py": ""})
        self.assertEqual(len(testing.unit_test_modules(root)), 1)

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
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_integration_and_repository_checks_are_not_checked(self):
        root = self.tree(
            {
                "session/tests/integration/unit/unmatched_test.py": "",
                "session/tests/session_tests/integration/unmatched_test.py": "",
                "tests/unit/unmatched_test.py": "",
                ".github/unit/unmatched_test.py": "",
            }
        )
        self.assertEqual(testing.unit_test_modules(root), [])

    def test_a_module_in_another_project_does_not_match(self):
        test = "manager/tests/unit/config_test.py"
        source = "manager/src/agent_pm/manager/config.py"
        root = self.tree({test: "", "utils/src/agent_pm/utils/config.py": ""})
        self.assertEqual(
            testing.unit_test_modules(root), [f"{test}: missing source module {source}"]
        )

    def test_missing_modules_are_reported_in_path_order(self):
        root = self.tree(
            {
                "session/tests/unit/z_test.py": "",
                "manager/tests/unit/a_test.py": "",
                "session/tests/unit/a_test.py": "",
            }
        )
        found = testing.unit_test_modules(root)
        self.assertEqual(found, sorted(found))
        self.assertEqual(len(found), 3)


class TestBoundaries(Sample):
    def violations(self, sample, own, *, shipped=False):
        return testing.test_boundaries(
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
        self.assertEqual(testing.test_boundaries(root), [])
        self.assertTrue(
            self.violations(
                "from unittest.mock import patch\npatch(target='agent_pm.session.start')", "session"
            )
        )


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
                found = testing.test_gate_mutation(self.tree({path: text}))
                self.assertTrue(found, text)
                self.assertTrue(all(line.startswith(path + ":") for line in found), found)

    def test_no_migration_exemption_can_be_reintroduced(self):
        root = self.tree({"session/tests/unit/present_test.py": ""})
        self.assertEqual(testing.unit_isolation_allowlist(root), [])
        with mock.patch.dict(
            guards.UNIT_ISOLATION_ALLOWED, {"session/tests/unit/present_test.py": "escape"}
        ):
            self.assertEqual(
                testing.unit_isolation_allowlist(root),
                [f"{root}/tests/guards.py: unit isolation allowlist must remain empty"],
            )
