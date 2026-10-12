import os
import tempfile
from pathlib import Path

from new.tests import guards
from new.tests.case import CORE, NEW, Case

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

    def test_no_module_imports_agent_tools(self):
        tools = "agent_pm.agent_tools"
        files = {
            f"{PKG}/{layer.replace('.', '/')}/x.py": f"import {tools}.repo\n"
            for layer in guards.LAYERS
        }
        files[f"{PKG}/x.py"] = f"from {tools}.repo import read_config\n"
        files[f"{PKG}/delegate/composer/w.py"] = (
            "from ...agent_tools import repo\nimport repository\n"
        )
        files[f"{PKG}/utils/z.py"] = f"def f():\n    __import__('{tools}.repo')\n"
        files[f"{PKG}/utils/y.py"] = "import agent_tools.repo\n"
        files[f"{PKG}/agent_tools/x.py"] = "from agent_pm import agent_tools\nfrom . import y\n"
        root = self.tree(files)
        self.assertEqual(
            guards.layer_imports(root),
            [
                f"{PKG}/agent_tools/x.py: outside the layers imports {tools}",
                f"{PKG}/agent_tools/x.py: outside the layers imports {tools}.y",
                f"{PKG}/delegate/composer/w.py: delegate.composer imports {tools}.repo",
                f"{PKG}/delegate/composer/x.py: delegate.composer imports {tools}.repo",
                f"{PKG}/delegate/x.py: delegate imports {tools}.repo",
                f"{PKG}/session/adapters/x.py: session.adapters imports {tools}.repo",
                f"{PKG}/session/api/x.py: session.api imports {tools}.repo",
                f"{PKG}/session/hosts/x.py: session.hosts imports {tools}.repo",
                f"{PKG}/session/modes/x.py: session.modes imports {tools}.repo",
                f"{PKG}/session/records/x.py: session.records imports {tools}.repo",
                f"{PKG}/session/selection/x.py: session.selection imports {tools}.repo",
                f"{PKG}/session/types/x.py: session.types imports {tools}.repo",
                f"{PKG}/utils/y.py: bare import agent_tools.repo",
                f"{PKG}/utils/z.py: outside the layers imports {tools}.repo",
                f"{PKG}/x.py: outside the layers imports {tools}.repo",
            ],
        )

    def test_agent_tools_import_only_the_standard_library_and_utils(self):
        root = self.tree(
            {
                f"{PKG}/agent_tools/x.py": "import json\nfrom agent_pm.utils.paths import under\n"
                "from ..utils import args\n",
                f"{PKG}/agent_tools/y.py": "import agent_pm.session.api\nimport third_party\n"
                "from agent_pm.delegate.composer import run\n",
            }
        )
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"{PKG}/agent_tools/y.py: agent_tools imports agent_pm.delegate.composer.run",
                f"{PKG}/agent_tools/y.py: agent_tools imports agent_pm.session.api",
                f"{PKG}/agent_tools/y.py: agent_tools imports third_party",
            ],
        )

    def test_production_tree_does_not_import_agent_tools_as_a_library(self):
        self.assertEqual(guards.layer_imports(CORE), [])

    def test_every_layer_may_import_utils(self):
        files = {
            f"{PKG}/utils/config_file.py": "import os\nimport tomllib\n"
            "from agent_pm.utils.paths import CORE\nfrom . import markdown\n"
        }
        files.update(
            {
                f"{PKG}/{layer.replace('.', '/')}/x.py": "from agent_pm.utils.config_file import read_config\n"
                for layer in guards.LAYERS
            }
        )
        root = self.tree(files)
        self.assertEqual(guards.layer_imports(root), [])
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_may_import_utils(self):
        root = self.tree(
            {
                f"{PKG}/utils/x.py": "from agent_pm.utils.shell import SHORT\nfrom . import git\n"
                "import agent_pm.utils.text\n"
            }
        )
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_reject_layer_external_and_literal_imports(self):
        root = self.tree(
            {
                f"{PKG}/utils/x.py": "import agent_pm.session.api\nimport third_party\n"
                "def f():\n    import importlib\n    importlib.import_module('agent_pm.session.adapters.base')\n"
                "    __import__('agent_pm.delegate.composer')\n",
                f"{PKG}/utils/y.py": "from ..session import modes\nimport agent_pm.agent_tools.repo\nimport utils.paths\n",
                f"{PKG}/session/modes/x.py": "import third_party\n",
            }
        )
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"{PKG}/utils/x.py: utils imports agent_pm.delegate.composer",
                f"{PKG}/utils/x.py: utils imports agent_pm.session.adapters.base",
                f"{PKG}/utils/x.py: utils imports agent_pm.session.api",
                f"{PKG}/utils/x.py: utils imports third_party",
                f"{PKG}/utils/y.py: utils imports agent_pm.agent_tools.repo",
                f"{PKG}/utils/y.py: utils imports agent_pm.session.modes",
                f"{PKG}/utils/y.py: utils imports utils.paths",
            ],
        )
