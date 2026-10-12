"""new/core/src/utils/config_file.py: merge, read_config and the core config's pair (CONFIG, LOCAL)."""

from new.tests import CORE, NEW, hermetic

import copy
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.utils.config_file import CONFIG, LOCAL, merge, read_config

LOCAL_SHOWN = "~/.agent-pm/core-new.local.toml"


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", NEW))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "config.toml"
        self.local = self.root / "local.toml"

    def test_merge_nested_tables_and_replace_other_values_without_changing_inputs(self):
        base = {"table": {"keep": 1, "change": {"x": 1}, "list": [1]}, "value": 1}
        overlay = {"table": {"change": {"y": 2}, "list": [2]}, "value": {"x": 3}}
        original = copy.deepcopy((base, overlay))
        self.assertEqual(
            merge(base, overlay),
            {"table": {"keep": 1, "change": {"x": 1, "y": 2}, "list": [2]}, "value": {"x": 3}},
        )
        self.assertEqual((base, overlay), original)

    def test_merge_adds_keys_replaces_a_table_by_a_value_and_merges_at_any_depth(self):
        base = {"a": 1, "s": "x", "u": {"k": 1}, "t": {"x": 1, "l": [1, 2], "n": {"p": 1, "q": 2}}}
        over = {"b": 2, "s": {"k": 1}, "u": 5, "t": {"y": 2, "l": [3], "n": {"q": 3}}}
        self.assertEqual(
            merge(base, over),
            {
                "a": 1,
                "b": 2,
                "s": {"k": 1},
                "u": 5,
                "t": {"x": 1, "y": 2, "l": [3], "n": {"p": 1, "q": 3}},
            },
        )

    def test_read_merges_an_explicit_overlay(self):
        self.path.write_text('value = 1\n[table]\nkeep = "base"\nchange = "base"\n')
        self.local.write_text('value = 2\n[table]\nchange = "local"\n')
        self.assertEqual(
            read_config(str(self.path), str(self.local)),
            {"value": 2, "table": {"keep": "base", "change": "local"}},
        )

    def test_read_expands_overlay_home_without_reading_user_config(self):
        self.path.write_text("value = 1\n")
        self.local.write_text("value = 2\n")
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(read_config(str(self.path), "~/local.toml"), {"value": 2})

    def test_absent_or_directory_overlay_is_ignored(self):
        self.path.write_text("value = 1\n")
        for overlay in (self.local, self.root):
            with self.subTest(overlay=overlay):
                self.assertEqual(read_config(str(self.path), str(overlay)), {"value": 1})

    def test_missing_base_and_malformed_files_propagate(self):
        with self.assertRaises(FileNotFoundError):
            read_config(str(self.path), str(self.local))
        self.path.write_text("malformed = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            read_config(str(self.path), str(self.local))
        self.path.write_text("value = 1\n")
        self.local.write_text("malformed = [\n")
        with self.assertRaises(tomllib.TOMLDecodeError):
            read_config(str(self.path), str(self.local))


class Defaults(unittest.TestCase):
    def setUp(self):
        self.home = hermetic.home(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.toml")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('users = ["a", "b"]\n[t]\nx = 1\ny = 1\n')

    def local(self, text, name="core-new.local.toml"):
        """`text` as this test's ~/.agent-pm/<name>."""
        with open(os.path.join(self.home, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_the_new_cores_config_pair(self):
        self.assertEqual(CONFIG, os.path.join(CORE, "config.toml"))
        self.assertEqual(LOCAL, LOCAL_SHOWN)

    def test_both_paths_are_required_and_devnull_merges_nothing(self):
        self.local('extra = "x"\n')
        for args in ((), (CONFIG,), (self.path,)):
            with self.subTest(args=args), self.assertRaises(TypeError):
                read_config(*args)
        self.assertEqual(
            read_config(self.path, os.devnull), {"users": ["a", "b"], "t": {"x": 1, "y": 1}}
        )

    def test_the_home_local_file_goes_on_top_and_the_old_cores_is_not_read(self):
        self.local('users = ["old"]\n', "core.local.toml")
        self.assertEqual(
            read_config(self.path, LOCAL), {"users": ["a", "b"], "t": {"x": 1, "y": 1}}
        )
        self.local('users = ["c"]\n[t]\ny = 2\n[t.n]\nz = 3\n')
        self.assertEqual(
            read_config(self.path, LOCAL), {"users": ["c"], "t": {"x": 1, "y": 2, "n": {"z": 3}}}
        )


if __name__ == "__main__":
    unittest.main()
