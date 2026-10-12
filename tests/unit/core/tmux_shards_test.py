import importlib.util
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[3] / ".github" / "tmux_shards.py"
spec = importlib.util.spec_from_file_location("tmux_shards", PATH)
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)


class Inventory(unittest.TestCase):
    def test_current_inventory(self):
        shards.validate(
            shards.discover(shards.DIRECTORY),
            shards.SHARDS,
            shards.workflow_shards(shards.WORKFLOW),
        )

    def test_missing_duplicate_and_stale_assignments_fail(self):
        actual = {"one_test.First", "one_test.Second"}
        for groups in (
            {"A": ["one_test.First"]},
            {"A": sorted(actual), "B": ["one_test.First"]},
            {"A": [*sorted(actual), "one_test.Removed"]},
        ):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                shards.validate(actual, groups, list(groups))

    def test_new_modules_and_classes_are_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            body = "import unittest\nclass First(unittest.TestCase):\n def test_one(self): pass\n"
            (root / "one_integration_test.py").write_text(body)
            (root / "two_integration_test.py").write_text(body.replace("First", "Second"))
            actual = shards.discover(root)
            self.assertEqual(actual, {"one_integration_test.First", "two_integration_test.Second"})
            with self.assertRaisesRegex(ValueError, "two_integration_test.Second"):
                shards.validate(actual, {"A": ["one_integration_test.First"]}, ["A"])

    def test_import_errors_fail_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "broken_integration_test.py").write_text(
                "raise RuntimeError('broken import')"
            )
            with self.assertRaisesRegex(ValueError, "broken import"):
                shards.discover(Path(folder))

    def test_workflow_labels_must_match_helper_and_be_unique(self):
        groups = {"A": ["one_test.First"], "B": ["one_test.Second"]}
        actual = {name for names in groups.values() for name in names}
        for labels in (["A"], ["A", "B", "C"]):
            with self.subTest(labels=labels), self.assertRaisesRegex(ValueError, "workflow shards"):
                shards.validate(actual, groups, labels)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.yml"
            for matrix in ('["A", "A"]', '["A", 2]', "[]", "fromJSON(other)"):
                path.write_text(
                    "jobs:\n  integration-tmux:\n    strategy:\n      matrix:\n        shard: "
                    + matrix
                    + "\n"
                )
                with self.subTest(matrix=matrix), self.assertRaises(ValueError):
                    shards.workflow_shards(path)
