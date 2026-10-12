import copy
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from utils.config_file import merge, read_config

from new.tests import NEW


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
        self.assertEqual(merge(base, overlay),
                         {"table": {"keep": 1, "change": {"x": 1, "y": 2}, "list": [2]}, "value": {"x": 3}})
        self.assertEqual((base, overlay), original)

    def test_read_merges_an_explicit_overlay(self):
        self.path.write_text('value = 1\n[table]\nkeep = "base"\nchange = "base"\n')
        self.local.write_text('value = 2\n[table]\nchange = "local"\n')
        self.assertEqual(read_config(str(self.path), str(self.local)),
                         {"value": 2, "table": {"keep": "base", "change": "local"}})

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
