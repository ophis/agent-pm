"""
utils/src/agent_pm/utils/paths.py: real_dirs, under, temp_dirs and printable_abs.
"""

import os
import sys
import tempfile
import unittest
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from agent_pm.utils.paths import (
    import_env,
    module_roots,
    overlaps,
    printable_abs,
    real_dirs,
    source_path,
    temp_dirs,
    under,
)

from utils.tests.case import REPO, Case
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired


class PathsTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.alias = self.root / "alias"
        self.alias.symlink_to(self.home)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))

    def test_real_dirs_expands_home_resolves_symlinks_and_deduplicates(self):
        paths = ["~/project", str(self.alias / "project"), str(self.home / "other")]
        self.assertEqual(
            real_dirs(paths, "roots"), {str(self.home / "project"), str(self.home / "other")}
        )
        self.assertEqual(real_dirs([], "roots"), frozenset())

    def test_real_dirs_rejects_invalid_collections_and_paths(self):
        for paths in (
            None,
            "~/project",
            (str(self.home),),
            [1],
            ["relative"],
        ):
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, "^roots:"):
                real_dirs(paths, "roots")

    def test_real_dirs_rejects_an_unknown_user(self):
        with (
            mock.patch("pwd.getpwnam", side_effect=KeyError("unknown-task318-user")),
            self.assertRaisesRegex(ValueError, "^roots:"),
        ):
            real_dirs(["~unknown-task318-user/project"], "roots")

    def test_under_returns_first_real_root_and_respects_path_boundaries(self):
        path = str(self.home / "project")
        self.assertEqual(under(path, [self.alias, self.root]), str(self.home))
        self.assertEqual(under(path, [self.root, self.alias]), str(self.root))
        self.assertIsNone(under(str(self.root / "home-other"), [self.home]))

    def test_under_a_root_equal_to_the_path_holds_it_and_no_roots_hold_nothing(self):
        self.assertEqual(under(str(self.home), [self.alias]), str(self.home))
        self.assertIsNone(under(str(self.home), []))

    def test_temp_dirs_collects_environment_python_system_and_darwin_roots(self):
        value = SimpleNamespace(returncode=0, stdout=str(self.root / "darwin") + "\n", stderr="")
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(self.alias)}),
            mock.patch("tempfile.gettempdir", return_value=str(self.home)),
            mock.patch("subprocess.run", return_value=value) as run,
        ):
            actual = temp_dirs()
        self.assertEqual(
            actual,
            tuple(
                dict.fromkeys(
                    os.path.realpath(path)
                    for path in (self.alias, self.home, "/tmp", "/var/tmp", self.root / "darwin")
                )
            ),
        )
        run.assert_called_once_with(
            ["getconf", "DARWIN_USER_TEMP_DIR"],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=DEVNULL,
        )

    def test_temp_dirs_keeps_roots_when_getconf_is_unavailable_or_times_out(self):
        for error in (OSError("no getconf"), TimeoutExpired("getconf", 60)):
            with self.subTest(error=error), mock.patch("subprocess.run", side_effect=error):
                self.assertIn(os.path.realpath("/var/tmp"), temp_dirs())


class PrintableAbs(Case):
    def test_a_printable_absolute_str_only(self):
        for value in ("/", "/a/b", "/a b/é", "/x/../y"):
            with self.subTest(value=value):
                self.assertTrue(printable_abs(value))
        for value in (
            "",
            "rel",
            "./a",
            "~/a",
            "/c\n",
            "/x\x1b[2J",
            "/a\0b",
            b"/a",
            None,
            1,
            ["/a"],
        ):
            with self.subTest(value=value):
                self.assertFalse(printable_abs(value))


if __name__ == "__main__":
    unittest.main()


class ModuleRootsTest(Case):
    def test_separate_package_roots_follow_module_files_in_order(self):
        session = ModuleType("agent_pm.session")
        session.__file__ = "/installation-one/agent_pm/session/__init__.py"
        utils = ModuleType("agent_pm.utils")
        utils.__file__ = "/installation-two/agent_pm/utils/__init__.py"
        self.assertEqual(module_roots(session, utils), ("/installation-one", "/installation-two"))

    def test_a_shared_installation_root_is_supplied_once(self):
        session = ModuleType("agent_pm.session")
        session.__file__ = "/installation/agent_pm/session/__init__.py"
        utils = ModuleType("agent_pm.utils")
        utils.__file__ = "/installation/agent_pm/utils/__init__.py"
        self.assertEqual(module_roots(session, utils), ("/installation",))

    def test_a_package_without_a_file_is_rejected(self):
        package = ModuleType("namespace")
        package.__file__ = None
        with self.assertRaisesRegex(ValueError, "package has no source file"):
            module_roots(package)


class Overlaps(Case):
    def test_native_missing_suffixes_respect_filesystem_case_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / "parent"
            parent.mkdir()
            insensitive = (root / "PARENT").exists()
            self.assertEqual(overlaps(str(parent / "new"), str(parent / "NEW")), insensitive)
            self.assertEqual(
                overlaps(str(parent / "new"), str(parent / "NEW" / "child")), insensitive
            )
            self.assertEqual(
                overlaps(str(parent / "NEW" / "child"), str(parent / "new")), insensitive
            )
            self.assertFalse(overlaps(str(parent / "new"), str(parent / "other")))

    def test_missing_suffix_case_matches_the_ancestors_filesystem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / "parent"
            parent.mkdir()
            for sensitive in (0, 1):
                with (
                    self.subTest(sensitive=sensitive),
                    mock.patch("sys.platform", "darwin"),
                    mock.patch("os.pathconf", return_value=sensitive) as pathconf,
                ):
                    for a, b, expected in (
                        (parent / "new", parent / "NEW", sensitive == 0),
                        (parent / "new", parent / "NEW" / "Child", sensitive == 0),
                        (parent / "NEW" / "Child", parent / "new", sensitive == 0),
                        (parent / "new" / "child", parent / "NEW" / "CHILD", sensitive == 0),
                        (parent / "new", parent / "other", False),
                    ):
                        with self.subTest(a=a, b=b):
                            self.assertEqual(overlaps(str(a), str(b)), expected)
                    self.assertTrue(pathconf.call_args_list)
                    self.assertTrue(
                        all(call.args == (str(parent), 11) for call in pathconf.call_args_list)
                    )

    def test_stat_permission_errors_propagate(self):
        with tempfile.TemporaryDirectory() as temporary:
            error = PermissionError("inaccessible ancestor")
            with (
                mock.patch("os.stat", side_effect=error),
                self.assertRaisesRegex(PermissionError, "^inaccessible ancestor$") as caught,
            ):
                overlaps(os.path.join(temporary, "missing"), temporary)
            self.assertIs(caught.exception, error)

    def test_case_aliases_use_filesystem_identity_even_with_missing_suffixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / "parent"
            parent.mkdir()
            variant = root / "PARENT"
            native_stat = os.stat

            def case_stat(path, *args, **kwargs):
                return native_stat(str(path).replace(str(variant), str(parent), 1), *args, **kwargs)

            with mock.patch("os.stat", side_effect=case_stat):
                for a, b, expected in (
                    (parent, variant, True),
                    (parent, variant / "missing" / "child", True),
                    (variant / "missing" / "child", parent, True),
                    (parent / "missing", variant / "missing" / "child", True),
                    (parent / "missing", variant / "other", False),
                    (parent, root / "parent-other", False),
                ):
                    with self.subTest(a=a, b=b):
                        self.assertEqual(overlaps(str(a), str(b)), expected)

    def test_equality_both_directions_siblings_and_missing_suffixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = root / "parent"
            parent.mkdir()
            alias = root / "alias"
            alias.symlink_to(parent)
            for a, b, expected in (
                (parent, parent, True),
                (parent, parent / "new" / "child", True),
                (parent / "new" / "child", parent, True),
                (parent, root / "parent-other", False),
                (alias / "new", parent / "new" / "child", True),
                (parent / "new-a", parent / "new-b", False),
            ):
                with self.subTest(a=a, b=b):
                    self.assertEqual(overlaps(str(a), str(b)), expected)


class ImportEnvironment(Case):
    def setUp(self):
        self.enterContext(
            mock.patch(
                "sysconfig.get_paths",
                return_value={"purelib": "/installation", "platlib": "/installation"},
            )
        )
        self.packages = []
        for name in ("delegate", "session", "utils"):
            module = ModuleType(f"agent_pm.{name}")
            module.__file__ = f"/checkout/{name}/src/agent_pm/{name}/__init__.py"
            self.packages.append(module)
        self.roots = tuple(f"/checkout/{name}/src" for name in ("delegate", "session", "utils"))

    def test_normal_module_and_package_find_the_same_source_root(self):
        module = ModuleType("agent_pm.delegate.engine.config")
        module.__file__ = "/checkout/delegate/src/agent_pm/delegate/engine/config.py"
        module.__spec__ = ModuleSpec(module.__name__, loader=None)
        self.assertEqual(module_roots(module, self.packages[0]), (self.roots[0],))

    def test_bootstrap_paths_are_supplied_in_package_order_without_unrelated_roots(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/unrelated"}):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join(self.roots)}
            )

    def test_roots_available_only_from_pythonpath_still_need_child_bootstrap(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join(self.roots)}):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join(self.roots)}
            )

    def test_installed_shared_root_needs_no_pythonpath(self):
        root = os.path.realpath(next(path for path in sys.path if path))
        for module in self.packages:
            module.__file__ = f"{root}/agent_pm/{module.__name__.rsplit('.', 1)[-1]}/__init__.py"
        with (
            mock.patch("sysconfig.get_paths", return_value={"purelib": root, "platlib": root}),
            mock.patch.dict(os.environ, {"PYTHONPATH": "/unrelated"}),
        ):
            self.assertEqual(import_env(*self.packages), {})

    def test_manually_bootstrapped_source_paths_still_need_child_bootstrap(self):
        root = os.path.realpath(next(path for path in sys.path if path))
        for module in self.packages:
            module.__file__ = f"{root}/agent_pm/{module.__name__.rsplit('.', 1)[-1]}/__init__.py"
        with mock.patch.dict(os.environ, {"PYTHONPATH": ""}):
            self.assertEqual(import_env(*self.packages), {"PYTHONPATH": root})

    def test_mixed_installation_needs_all_supplied_roots(self):
        root = os.path.realpath(next(path for path in sys.path if path))
        self.packages[0].__file__ = f"{root}/agent_pm/delegate/__init__.py"
        with (
            mock.patch("sysconfig.get_paths", return_value={"purelib": root, "platlib": root}),
            mock.patch.dict(os.environ, {"PYTHONPATH": ""}),
        ):
            self.assertEqual(
                import_env(*self.packages), {"PYTHONPATH": os.pathsep.join((root, *self.roots[1:]))}
            )


class SourcePath(Case):
    def setUp(self):
        self.installed = os.path.realpath(next(path for path in sys.path if path))
        self.enterContext(
            mock.patch(
                "sysconfig.get_paths",
                return_value={"purelib": self.installed, "platlib": self.installed},
            )
        )

    def test_roots_once_in_order_without_the_installed_ones(self):
        roots = ("/checkout/delegate/src", self.installed, "/repo", "/checkout/delegate/src")
        self.assertEqual(
            source_path(roots), {"PYTHONPATH": os.pathsep.join(("/checkout/delegate/src", "/repo"))}
        )

    def test_nothing_left_to_supply_is_no_pythonpath(self):
        self.assertEqual(source_path(()), {})
        self.assertEqual(source_path((self.installed,)), {})

    def test_an_installed_path_missing_from_sys_path_is_still_supplied(self):
        with mock.patch(
            "sysconfig.get_paths", return_value={"purelib": "/off-path", "platlib": "/off-path"}
        ):
            self.assertEqual(source_path(("/off-path",)), {"PYTHONPATH": "/off-path"})
