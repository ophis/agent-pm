import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pipeline  # noqa: E402

BASE = """team = "T"
[projects."Deep Research"]
next = "Product Design"
[projects."Product Design"]
prefix = "PRD"
next = "Engineering"
[projects.Engineering]
prefix = "TDD"
[projects.Solo]
"""


class Config(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def load(self, text):
        path = os.path.join(self.dir, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def test_stage_order(self):
        self.assertEqual(pipeline.stage_order(self.load(BASE)),
                         {"Deep Research": 0, "Product Design": 1, "Engineering": 2, "Solo": 0})

    def test_cycle_rejected(self):
        with self.assertRaises(SystemExit):
            self.load(BASE + '[projects.A]\nprefix = "A"\nnext = "B"\n[projects.B]\nprefix = "B"\nnext = "A"\n')

    def test_next_without_prefix_rejected(self):
        with self.assertRaises(SystemExit):
            self.load(BASE.replace('prefix = "TDD"\n', ""))

    def test_runnable(self):
        cfg = self.load(BASE.replace('next = "Product Design"', 'next = "Product Design"\ninstructions = "stages/x.md"\n'
                                     'model = "opus"\neffort = "high"'))
        os.makedirs(os.path.join(self.dir, "stages"))
        with self.assertRaises(SystemExit):  # instructions file missing
            pipeline.runnable(cfg, root=self.dir)
        open(os.path.join(self.dir, "stages", "x.md"), "w").close()
        self.assertEqual(list(pipeline.runnable(cfg, root=self.dir)), ["Deep Research"])

    def test_runnable_needs_model_and_effort(self):
        cfg = self.load(BASE.replace('next = "Product Design"', 'next = "Product Design"\ninstructions = "stages/x.md"'))
        with self.assertRaises(SystemExit):
            pipeline.runnable(cfg, root=self.dir)

    def test_missing_stage_file_does_not_break_load_config(self):
        cfg = self.load(BASE.replace('next = "Product Design"', 'next = "Product Design"\ninstructions = "stages/gone.md"\n'
                                     'model = "opus"\neffort = "high"'))
        self.assertIn("Deep Research", cfg["projects"])


class Paths(unittest.TestCase):
    def test_slug_and_project_log(self):
        self.assertEqual(pipeline.slug("Deep Research"), "deep-research")
        with tempfile.TemporaryDirectory() as d:
            path = pipeline.project_log("Product Design", logs=d)
            self.assertEqual(path, os.path.join(d, "projects", "product-design.log"))
            self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_transcripts_follow_work(self):
        self.assertEqual(pipeline.WORK, os.path.join(pipeline.ROOT, "work"))
        self.assertTrue(pipeline.TRANSCRIPTS.endswith("/" + pipeline.WORK.replace("/", "-").replace(".", "-")))


if __name__ == "__main__":
    unittest.main()
