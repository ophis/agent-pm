"""new/tests/case.py: Case.run's temp HOME, registry resets and import-root checks, on inner tests; child_env."""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from agent_pm.session.driver import registry
from agent_pm.utils import paths

from new.tests.case import NEW, ORCH_ROOT, ROOT, Case, child_env


class CaseTest(Case):
    def run_inner(self, test, set_up=None) -> unittest.TestResult:
        """test(inner) run as an inner Case's test, whose setUp calls set_up(inner) (if given), never super()."""

        class Inner(Case):
            def setUp(inner):
                if set_up:
                    set_up(inner)

            def runTest(inner):
                test(inner)

        result = unittest.TestResult()
        Inner().run(result)
        return result

    def test_each_test_gets_an_empty_temp_home_restored_after(self):
        seen = {}

        def test(inner):
            seen.update(
                env=os.environ["HOME"],
                expanded=os.path.expanduser("~"),
                home=inner.home,
                agent_pm=inner.agent_pm,
                entries=(os.listdir(inner.home), os.listdir(inner.agent_pm)),
            )

        before = os.environ["HOME"]
        result = self.run_inner(test)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertEqual(before, self.home)
        self.assertEqual(os.environ["HOME"], before)
        home = seen["home"]
        self.assertNotEqual(home, before)
        self.assertEqual(os.path.dirname(home), tempfile.gettempdir())
        self.assertEqual((seen["env"], seen["expanded"]), (home, home))
        self.assertEqual(seen["agent_pm"], os.path.join(home, ".agent-pm"))
        self.assertEqual(seen["entries"], ([".agent-pm"], []))
        self.assertFalse(os.path.exists(home))

    def test_a_set_up_without_super_runs_in_the_temp_home(self):
        seen = []
        result = self.run_inner(
            lambda inner: None,
            set_up=lambda inner: seen.append(
                (os.environ["HOME"], inner.home, os.path.isdir(inner.agent_pm))
            ),
        )
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        ((env, home, made),) = seen
        self.assertEqual(env, home)
        self.assertNotEqual(home, self.home)
        self.assertTrue(made)

    def test_the_registry_is_reset_before_and_after_a_failing_test_too(self):
        calls = []

        def test(inner):
            calls.append("test")
            inner.fail("boom")

        with mock.patch.object(registry, "reset", side_effect=lambda: calls.append("reset")):
            result = self.run_inner(test)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(calls, ["reset", "test", "reset"])

    def test_agent_pm_from_another_tree_fails_the_test_before_its_set_up(self):
        ran = []
        with mock.patch.object(paths, "IMPORT_ROOT", "/elsewhere/src"):
            result = self.run_inner(ran.append, set_up=ran.append)
        ((_, failure),) = result.failures
        self.assertIn(
            f"agent_pm is imported from /elsewhere/src, not this tree's {ROOT}: "
            "run with PYTHONPATH=new/core/src",
            failure,
        )
        self.assertEqual((ran, result.testsRun, result.errors), ([], 1, []))

    def orchestrator(self, root):
        """An agent_pm_linear package module found under `root`."""
        module = types.ModuleType("agent_pm_linear")
        module.__file__ = os.path.join(root, "agent_pm_linear", "__init__.py")
        return module

    def test_agent_pm_linear_from_another_tree_fails_the_test_before_its_set_up(self):
        ran = []
        elsewhere = {"agent_pm_linear": self.orchestrator("/elsewhere/src")}
        with mock.patch.dict(sys.modules, elsewhere):
            result = self.run_inner(ran.append, set_up=ran.append)
        ((_, failure),) = result.failures
        self.assertIn(
            f"agent_pm_linear is imported from /elsewhere/src, not this tree's {ORCH_ROOT}: "
            "run with PYTHONPATH=new/core/src:new/linear-orchestrator/src",
            failure,
        )
        self.assertEqual((ran, result.testsRun, result.errors), ([], 1, []))

    def test_agent_pm_linear_absent_or_this_trees_passes(self):
        self.assertEqual(ORCH_ROOT, os.path.join(NEW, "linear-orchestrator", "src"))
        self.assertTrue(os.path.isfile(self.orchestrator(ORCH_ROOT).__file__))
        for found in (None, self.orchestrator(ORCH_ROOT)):
            ran = []
            with self.subTest(found=found), mock.patch.dict(sys.modules):
                sys.modules.pop("agent_pm_linear", None)
                if found:
                    sys.modules["agent_pm_linear"] = found
                result = self.run_inner(ran.append)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            self.assertEqual(len(ran), 1)

    def test_child_env_drops_python_variables_and_sets_pythonpath(self):
        base = {
            "HOME": "/h",
            "PATH": "/b",
            "PYTHONPATH": "/x",
            "PYTHONHOME": "/y",
            "PYTHONSAFEPATH": "1",
        }
        self.assertEqual(
            child_env(base, "/r1", "/r2"),
            {"HOME": "/h", "PATH": "/b", "PYTHONPATH": os.pathsep.join((ROOT, "/r1", "/r2"))},
        )
        with mock.patch.dict(os.environ, {"PYTHONWARNINGS": "error", "OTHER": "1"}):
            env = child_env()
        self.assertNotIn("PYTHONWARNINGS", env)
        self.assertEqual((env["PYTHONPATH"], env["OTHER"], env["HOME"]), (ROOT, "1", self.home))


if __name__ == "__main__":
    unittest.main()
