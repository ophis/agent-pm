"""new/core/src/utils/config_file.py: the core config pair, merge, read and trusted_dirs (moved from the registry's
and repo.py's tests)."""
import os
import tempfile
import unittest
import unittest.mock

from new.tests import CORE, hermetic
from utils import config_file

LOCAL_SHOWN = "~/.agent-pm/core-new.local.toml"


class Base(unittest.TestCase):
    def setUp(self):
        self.home = hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, text) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def local(self, text, name="core-new.local.toml"):
        """`text` as this test's ~/.agent-pm/<name>."""
        with open(os.path.join(self.home, name), "w", encoding="utf-8") as f:
            f.write(text)


class Constants(unittest.TestCase):
    def test_the_new_cores_config_pair(self):
        self.assertEqual(config_file.CONFIG, os.path.join(CORE, "config.toml"))
        self.assertEqual(config_file.LOCAL, LOCAL_SHOWN)


class Merge(unittest.TestCase):
    def test_tables_merge_key_by_key_and_the_other_value_wins(self):
        for base, over, want in (
                ({"a": {"x": 1, "y": [1, 2], "t": {"p": 1, "q": 2}}, "b": 1, "c": {"k": 1}},
                 {"a": {"y": [3], "z": 2, "t": {"q": 3}}, "b": {"n": 1}, "c": 5, "d": "new"},
                 {"a": {"x": 1, "y": [3], "z": 2, "t": {"p": 1, "q": 3}}, "b": {"n": 1}, "c": 5, "d": "new"}),
                ({"a": 1, "s": "x", "u": {"k": 1}, "t": {"x": 1, "l": [1, 2], "n": {"p": 1, "q": 2}}},
                 {"b": 2, "s": {"k": 1}, "u": 5, "t": {"y": 2, "l": [3], "n": {"q": 3}}},
                 {"a": 1, "b": 2, "s": {"k": 1}, "u": 5, "t": {"x": 1, "y": 2, "l": [3], "n": {"p": 1, "q": 3}}})):
            with self.subTest(base=base):
                self.assertEqual(config_file.merge(base, over), want)

    def test_leaves_its_arguments_unchanged(self):
        base, over = {"a": {"x": 1}}, {"a": {"x": 2}}
        config_file.merge(base, over)
        self.assertEqual((base, over), ({"a": {"x": 1}}, {"a": {"x": 2}}))


class Read(Base):
    def test_merges_the_local_file_over_the_base_file(self):
        path = self.write("config.toml", 'top = 1\nlist = [1, 2]\n[t]\na = "base"\nb = "base"\n')
        local = self.write("local.toml", 'list = [3]\n[t]\nb = "local"\nc = "local"\n')
        self.assertEqual(config_file.read(path, local),
                         {"top": 1, "list": [3], "t": {"a": "base", "b": "local", "c": "local"}})

    def test_expands_tilde_in_the_local_path(self):
        path = self.write("config.toml", "a = 1\n")
        self.local("a = 2\n")
        self.assertEqual(config_file.read(path, LOCAL_SHOWN), {"a": 2})

    def test_reads_the_local_file_only_when_it_is_a_file(self):
        path = self.write("config.toml", "a = 1\n")
        self.assertEqual(config_file.read(path, os.path.join(self.dir, "absent.toml")), {"a": 1})
        self.assertEqual(config_file.read(path, self.dir), {"a": 1})

    def test_defaults_to_the_committed_config_and_the_home_local_file(self):
        self.local('extra = "x"\n')
        self.assertEqual(config_file.read()["extra"], "x")

    def test_the_local_file_goes_on_top_and_the_old_cores_is_not_read(self):
        path = self.write("config.toml", 'users = ["a", "b"]\n[t]\nx = 1\ny = 1\n')
        self.local('users = ["old"]\n', "core.local.toml")
        self.assertEqual(config_file.read(path), {"users": ["a", "b"], "t": {"x": 1, "y": 1}})
        self.local('users = ["c"]\n[t]\ny = 2\n[t.n]\nz = 3\n')
        self.assertEqual(config_file.read(path), {"users": ["c"], "t": {"x": 1, "y": 2, "n": {"z": 3}}})
        other = self.write("other.toml", 'users = ["d"]\n')
        self.assertEqual(config_file.read(path, other), {"users": ["d"], "t": {"x": 1, "y": 1}})

    def test_an_unreadable_or_invalid_file_raises(self):
        absent = os.path.join(self.dir, "absent.toml")
        with self.assertRaises(OSError):
            config_file.read(absent, absent)
        with self.assertRaises(ValueError):
            config_file.read(self.write("config.toml", "[t\n"), absent)


class TrustedDirs(unittest.TestCase):
    def test_unset_is_empty_and_paths_are_real_paths(self):
        self.assertEqual(config_file.trusted_dirs({}), frozenset())
        with unittest.mock.patch.dict(os.environ, {"HOME": "/var/h"}):
            got = config_file.trusted_dirs({"trusted_dirs": ["~/c", "/x/../y"]})
        self.assertEqual(got, {os.path.realpath("/var/h/c"), os.path.realpath("/y")})

    def test_refuses_what_is_no_list_of_absolute_or_expandable_paths(self):
        for value in ("/a", [1], ["a/b"], ["~nosuchuser-agent-pm/x"]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^trusted_dirs: "):
                config_file.trusted_dirs({"trusted_dirs": value})


if __name__ == "__main__":
    unittest.main()
