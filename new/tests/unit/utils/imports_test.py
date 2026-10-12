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

    def test_no_layer_or_outer_module_imports_repo(self):
        files = {f"src/{layer}/x.py": "import repo\n" for layer in guards.LAYERS}
        files["src/x.py"] = "from repo import read_config\n"
        files["src/composer/w.py"] = "from .. import repo\nimport repository\n"
        files["src/utils/z.py"] = "def f():\n    __import__('repo')\n"
        root = self.tree(files)
        self.assertEqual(guards.layer_imports(root), ["src/adapters/x.py: adapters imports repo",
                                                     "src/composer/w.py: composer imports repo",
                                                     "src/composer/x.py: composer imports repo",
                                                     "src/delegate/x.py: delegate imports repo",
                                                     "src/driver/x.py: driver imports repo",
                                                     "src/utils/z.py: outside the layers imports repo",
                                                     "src/x.py: outside the layers imports repo.read_config"])

    def test_production_tree_does_not_import_repo_as_a_library(self):
        self.assertEqual(guards.layer_imports(CORE), [])

    def test_every_layer_may_import_utils(self):
        files = {"src/utils/config_file.py": "import os\nimport tomllib\n"}
        files.update({f"src/{layer}/x.py": "from utils.config_file import read_config\n"
                      for layer in guards.LAYERS})
        root = self.tree(files)
        self.assertEqual(guards.layer_imports(root), [])
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_may_import_utils(self):
        root = self.tree({"src/utils/x.py": "from utils.shell import SHORT\nfrom . import git\nimport utils.text\n"})
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_reject_layer_external_and_literal_imports(self):
        root = self.tree({"src/utils/x.py": "import driver.api\nimport third_party\n"
                         "def f():\n    import importlib\n    importlib.import_module('adapters.base')\n"
                         "    __import__('composer')\n",
                          "src/utils/y.py": "from .. import driver\nimport repo\n",
                          "src/driver/x.py": "import third_party\n"})
        self.assertEqual(guards.pure_utils(root), ["src/utils/x.py: utils imports adapters.base",
                                                  "src/utils/x.py: utils imports composer",
                                                  "src/utils/x.py: utils imports driver.api",
                                                  "src/utils/x.py: utils imports third_party",
                                                  "src/utils/y.py: utils imports driver",
                                                  "src/utils/y.py: utils imports repo"])
