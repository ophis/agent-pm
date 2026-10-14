"""Each tests/case.py: temporary HOME, import-root checks and child environment."""

import contextlib
import importlib
import os
import sys
import tempfile
import types
import unittest
from importlib._bootstrap_external import _NamespacePath
from pathlib import Path
from unittest import mock

import agent_pm

from tests import guards
from tests.case import REPO, ROOTS, Case
from utils.tests.sandbox import clean_env, owned_directory

CASES = (
    "tests",
    "utils/tests",
    "session/tests",
    "delegate/tests",
    "linear-orchestrator/tests",
    "manager/tests",
)


class SandboxFixtures(Case):
    def test_an_owned_directory_refuses_a_symlink_root_before_making_directories(self):
        root, target = Path(self.home, "linked"), Path(self.home, "target")
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
        with (
            mock.patch("os.makedirs") as mkdir,
            self.assertRaisesRegex(AssertionError, "test fixture root contains a symlink"),
        ):
            owned_directory(self, root, prefix="team-")
        mkdir.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])


RUN_WITH = os.pathsep.join(
    f"{p}/src" for p in ("delegate", "linear-orchestrator", "manager", "session", "utils")
)


@contextlib.contextmanager
def foreign_modules(case, name, module):
    table = dict(sys.modules)
    if module is None:
        table.pop(name, None)
    else:
        table[name] = module
    foreign = case._foreign
    with mock.patch.object(case, "_foreign", side_effect=lambda: foreign(table)):
        yield


class Checks:
    tree = ""

    def setUp(self):
        self.case = importlib.import_module(
            "orchestrator_tests.case"
            if self.tree == "linear-orchestrator/tests"
            else f"{self.tree.replace('/', '.')}.case"
        )

    def run_inner(self, test, set_up=None) -> unittest.TestResult:
        """test(inner) run as an inner Case's test, whose setUp calls set_up(inner) (if given),
        never super().
        """

        class Inner(self.case.Case):
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
            lambda _inner: None,
            set_up=lambda inner: seen.append(
                (os.environ["HOME"], inner.home, os.path.isdir(inner.agent_pm))
            ),
        )
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        ((env, home, made),) = seen
        self.assertEqual(env, home)
        self.assertNotEqual(home, self.home)
        self.assertTrue(made)

    def module(self, name, root):
        """A package module `name` (agent_pm.<sub>) found under `root`."""
        module = types.ModuleType(name)
        module.__file__ = os.path.join(root, *name.split("."), "__init__.py")
        return module

    def assert_fails_before_set_up(self, message):
        ran = []
        result = self.run_inner(ran.append, set_up=ran.append)
        ((_, failure),) = result.failures
        self.assertIn(f"{message}: run with PYTHONPATH={RUN_WITH}", failure)
        self.assertEqual((ran, result.testsRun, result.errors), ([], 1, []))

    def test_no_importable_agent_pm_fails_the_test_before_its_set_up(self):
        if not hasattr(self.case, "_missing"):
            return
        with mock.patch.object(self.case, "agent_pm", None):
            self.assert_fails_before_set_up("agent_pm is not importable")

    def test_a_subpackage_from_another_tree_fails_the_test_before_its_set_up(self):
        for sub, root in self.case.SRC.items():
            name = f"agent_pm.{sub}"
            elsewhere = {name: self.module(name, "/elsewhere/src")}
            with self.subTest(name), foreign_modules(self.case, name, elsewhere[name]):
                self.assert_fails_before_set_up(
                    f"{name} is imported from /elsewhere/src, not this tree's {root}"
                )

    def test_another_trees_agent_pm_dir_on_the_package_path_fails_the_test(self):
        with mock.patch.object(
            _NamespacePath,
            "_recalculate",
            return_value=[*agent_pm.__path__, "/elsewhere/src/agent_pm"],
        ):
            self.assert_fails_before_set_up(
                "agent_pm.__path__ holds /elsewhere/src/agent_pm, not only this tree's"
            )

    def test_a_missing_import_root_fails_the_test(self):
        manager = os.path.realpath(os.path.join(self.case.SRC["manager"], "agent_pm"))
        path = [p for p in agent_pm.__path__ if os.path.realpath(p) != manager]
        with mock.patch.object(_NamespacePath, "_recalculate", return_value=path):
            self.assert_fails_before_set_up(f"agent_pm.__path__ lacks {manager}")

    def test_the_orchestrator_absent_or_this_trees_passes(self):
        orch = self.case.SRC["linear_orchestrator"]
        self.assertEqual(orch, os.path.join(self.case.REPO, "linear-orchestrator", "src"))
        self.assertEqual(getattr(self.case, "ORCH_ROOT", orch), orch)
        name = "agent_pm.linear_orchestrator"
        self.assertTrue(os.path.isfile(self.module(name, orch).__file__))
        for found in (None, self.module(name, orch)):
            ran = []
            with self.subTest(found=found), foreign_modules(self.case, name, found):
                result = self.run_inner(ran.append)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            self.assertEqual(len(ran), 1)

    def test_agent_pm_spans_exactly_this_trees_five_src_dirs(self):
        own = {os.path.realpath(os.path.join(r, "agent_pm")) for r in self.case.ROOTS}
        self.assertEqual({os.path.realpath(p) for p in agent_pm.__path__}, own)
        self.assertEqual(len(own), 5)
        self.assertEqual(self.case.SRC, guards_src())
        self.assertEqual(self.case.ROOTS, ROOTS)
        self.assertFalse(hasattr(agent_pm, "__file__") and agent_pm.__file__)

    def test_child_env_drops_python_variables_and_sets_pythonpath_where_kept(self):
        if self.tree == "utils/tests":
            self.assertFalse(hasattr(self.case, "child_env"))
            return
        child_env, ROOTS = self.case.child_env, self.case.ROOTS
        PYTHONPATH = os.pathsep.join(ROOTS)
        base = {
            "HOME": "/h",
            "PATH": "/b",
            "PYTHONPATH": "/x",
            "PYTHONHOME": "/y",
            "PYTHONUSERBASE": "/ambient-python",
            "TMUX_TMPDIR": "/ambient-tmux",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        expected = {
            "HOME": "/h",
            "PATH": "/b",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((*ROOTS, "/r1", "/r2")),
        }
        expected.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES="/"
        )
        expected["TMUX_TMPDIR"] = clean_env({})["TMUX_TMPDIR"]
        self.assertEqual(child_env(base, "/r1", "/r2"), expected)
        with mock.patch.dict(os.environ, {"PYTHONWARNINGS": "error", "OTHER": "1"}):
            env = child_env()
        self.assertNotIn("PYTHONWARNINGS", env)
        self.assertEqual(
            (env["PYTHONPATH"], env["OTHER"], env["HOME"]), (PYTHONPATH, "1", self.home)
        )


def guards_src():
    return {sub: os.path.join(REPO, project, "src") for project, sub in guards.PROJECTS.items()}


for _tree in CASES:
    _name = "".join(p.title() for p in _tree.replace("-", "/").split("/")) + "Case"
    globals()[_name] = type(_name, (Checks, Case), {"tree": _tree})


if __name__ == "__main__":
    unittest.main()
