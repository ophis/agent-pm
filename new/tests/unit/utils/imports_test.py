from new.tests import guards
from new.tests.case import CORE, NEW, Case

import os
import tempfile
from pathlib import Path

PKG = "src/agent_pm"


class UtilsImportsTest(Case):
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
        files = {f"{PKG}/{layer}/x.py": "import agent_pm.repo\n" for layer in guards.LAYERS}
        files[f"{PKG}/x.py"] = "from agent_pm.repo import read_config\n"
        files[f"{PKG}/composer/w.py"] = "from .. import repo\nimport repository\n"
        files[f"{PKG}/utils/z.py"] = "def f():\n    __import__('agent_pm.repo')\n"
        files[f"{PKG}/utils/y.py"] = "import repo\n"
        root = self.tree(files)
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/adapters/x.py: adapters imports agent_pm.repo",
                f"{PKG}/composer/w.py: composer imports agent_pm.repo",
                f"{PKG}/composer/x.py: composer imports agent_pm.repo",
                f"{PKG}/delegate/x.py: delegate imports agent_pm.repo",
                f"{PKG}/driver/x.py: driver imports agent_pm.repo",
                f"{PKG}/utils/y.py: bare import repo",
                f"{PKG}/utils/z.py: outside the layers imports agent_pm.repo",
                f"{PKG}/x.py: outside the layers imports agent_pm.repo.read_config",
            ],
        )

    def test_production_tree_does_not_import_repo_as_a_library(self):
        self.assertEqual(guards.layer_imports(CORE), [])

    def test_every_layer_may_import_utils(self):
        files = {
            f"{PKG}/utils/config_file.py": "import os\nimport tomllib\n"
            "from agent_pm.utils.paths import CORE\nfrom . import markdown\n"
        }
        files.update(
            {
                f"{PKG}/{layer}/x.py": "from agent_pm.utils.config_file import read_config\n"
                for layer in guards.LAYERS
            }
        )
        root = self.tree(files)
        self.assertEqual(guards.layer_imports(root), [])
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_reject_layer_external_and_literal_imports(self):
        root = self.tree(
            {
                f"{PKG}/utils/x.py": "import agent_pm.driver.api\nimport third_party\n"
                "def f():\n    import importlib\n    importlib.import_module('agent_pm.adapters.base')\n"
                "    __import__('agent_pm.composer')\n",
                f"{PKG}/utils/y.py": "from .. import driver\nimport agent_pm.repo\nimport utils.paths\n",
                f"{PKG}/driver/x.py": "import third_party\n",
            }
        )
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"{PKG}/utils/x.py: utils imports agent_pm.adapters.base",
                f"{PKG}/utils/x.py: utils imports agent_pm.composer",
                f"{PKG}/utils/x.py: utils imports agent_pm.driver.api",
                f"{PKG}/utils/x.py: utils imports third_party",
                f"{PKG}/utils/y.py: utils imports agent_pm.driver",
                f"{PKG}/utils/y.py: utils imports agent_pm.repo",
                f"{PKG}/utils/y.py: utils imports utils.paths",
            ],
        )
