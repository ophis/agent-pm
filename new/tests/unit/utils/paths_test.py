"""new/core/src/utils/paths.py: printable_abs, under and temp_dirs (moved from repo.py's tests)."""
import os
import subprocess
import tempfile
import unittest
import unittest.mock

from new.tests import hermetic  # noqa: F401
from utils import paths


class PrintableAbs(unittest.TestCase):
    def test_a_printable_absolute_str_only(self):
        for value in ("/", "/a/b", "/a b/é", "/x/../y"):
            with self.subTest(value=value):
                self.assertTrue(paths.printable_abs(value))
        for value in ("", "rel", "./a", "~/a", "/c\n", "/x\x1b[2J", "/a\0b", b"/a", None, 1, ["/a"]):
            with self.subTest(value=value):
                self.assertFalse(paths.printable_abs(value))


class Under(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def test_the_first_root_holding_the_path_by_real_path(self):
        a, b = os.path.join(self.root, "a"), os.path.join(self.root, "b")
        os.mkdir(a)
        os.symlink(a, b)
        inner = os.path.join(a, "x")
        self.assertEqual(paths.under(inner, [os.path.join(self.root, "other"), b, self.root]), a)
        self.assertEqual(paths.under(a, [a]), a)
        self.assertIsNone(paths.under(inner, [os.path.join(self.root, "a2"), os.path.join(self.root, "other")]))
        self.assertIsNone(paths.under(inner, []))


class TempDirs(unittest.TestCase):
    def test_every_temp_dir_counts(self):
        getconf = subprocess.CompletedProcess([], 0, "/x/d/\n", "")
        with unittest.mock.patch.dict(os.environ, {"TMPDIR": "/x/e/"}), \
                unittest.mock.patch("tempfile.gettempdir", return_value="/x/t"), \
                unittest.mock.patch("subprocess.run", return_value=getconf) as sub:
            dirs = paths.temp_dirs()
        self.assertEqual(sub.call_args.args[0], ["getconf", "DARWIN_USER_TEMP_DIR"])
        self.assertEqual(dirs, tuple(dict.fromkeys(os.path.realpath(d) for d in ("/x/e", "/x/t", "/tmp", "/var/tmp",
                                                                                   "/x/d"))))

    def test_getconf_failing_is_ignored(self):
        with unittest.mock.patch("subprocess.run", side_effect=OSError("no getconf")):
            self.assertIn(os.path.realpath("/var/tmp"), paths.temp_dirs())


if __name__ == "__main__":
    unittest.main()
