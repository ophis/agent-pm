import os
import tempfile
import unittest
from pathlib import Path

from new.tests import CORE, NEW, guards


class UtilsImportsTest(unittest.TestCase):
    def tree(self, files):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", NEW))
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        return str(root)

    def test_real_utils_have_only_standard_library_imports(self):
        self.assertEqual(guards.pure_utils(CORE), [])

    def test_every_layer_may_import_utils(self):
        files = {"src/utils/config_file.py": "import os\nimport tomllib\n"}
        files.update({f"src/{layer}/x.py": "from utils.config_file import read_config\n"
                      for layer in guards.LAYERS})
        root = self.tree(files)
        self.assertEqual(guards.layer_imports(root), [])
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_reject_layer_external_and_literal_imports(self):
        root = self.tree({"src/utils/x.py": "import driver.api\nimport third_party\n"
                         "def f():\n    import importlib\n    importlib.import_module('adapters.base')\n"
                         "    __import__('composer')\n"})
        self.assertEqual(guards.pure_utils(root), ["src/utils/x.py: utils imports adapters.base",
                                                  "src/utils/x.py: utils imports composer",
                                                  "src/utils/x.py: utils imports driver.api",
                                                  "src/utils/x.py: utils imports third_party"])
