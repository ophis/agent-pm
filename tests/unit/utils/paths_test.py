"""core/src/agent_pm/utils/paths.py: IMPORT_ROOT, CORE, real_dirs, under, temp_dirs and printable_abs."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.utils.paths import CORE, IMPORT_ROOT, printable_abs, real_dirs, temp_dirs, under

from tests.case import REPO, ROOT, Case


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
            ["~unknown-task318-user/project"],
        ):
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, "^roots:"):
                real_dirs(paths, "roots")

    def test_under_returns_first_real_root_and_respects_path_boundaries(self):
        path = str(self.home / "project")
        self.assertEqual(under(path, [self.alias, self.root]), str(self.home))
        self.assertEqual(under(path, [self.root, self.alias]), str(self.root))
        self.assertIsNone(under(str(self.root / "home-other"), [self.home]))

    def test_under_a_root_equal_to_the_path_holds_it_and_no_roots_hold_nothing(self):
        self.assertEqual(under(str(self.home), [self.alias]), str(self.home))
        self.assertIsNone(under(str(self.home), []))

    def test_temp_dirs_collects_environment_python_system_and_darwin_roots(self):
        value = subprocess.CompletedProcess([], 0, str(self.root / "darwin") + "\n", "")
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
            stdin=subprocess.DEVNULL,
        )

    def test_temp_dirs_keeps_roots_when_getconf_is_unavailable_or_times_out(self):
        for error in (OSError("no getconf"), subprocess.TimeoutExpired("getconf", 60)):
            with self.subTest(error=error), mock.patch("subprocess.run", side_effect=error):
                self.assertIn(os.path.realpath("/var/tmp"), temp_dirs())


class Roots(Case):
    def test_the_import_root_holds_the_package_and_the_core_dir_holds_it(self):
        self.assertEqual(IMPORT_ROOT, ROOT)
        self.assertEqual(IMPORT_ROOT, os.path.join(REPO, "core", "src"))
        self.assertEqual(CORE, os.path.join(REPO, "core"))
        self.assertTrue(os.path.isfile(os.path.join(IMPORT_ROOT, "agent_pm", "utils", "paths.py")))
        self.assertTrue(os.path.isfile(os.path.join(CORE, "config.toml")))


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
