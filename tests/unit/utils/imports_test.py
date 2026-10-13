import os
import tempfile
from pathlib import Path

from tests import guards
from tests.case import REPO, Case

PKG = "src/agent_pm"


class UtilsImportsTest(Case):
    def tree(self, files):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        return str(root)

    def test_real_utils_have_only_standard_library_imports(self):
        self.assertEqual(guards.pure_utils(REPO), [])

    def test_no_module_imports_the_team_tools(self):
        tools = "agent_pm.delegate.team.tools"
        files = {
            f"{layer.split('.')[0]}/{PKG}/{layer.replace('.', '/')}/x.py": f"import {tools}.repo\n"
            for layer in guards.LAYERS
        }
        files[f"delegate/{PKG}/x.py"] = f"from {tools}.repo import read_config\n"
        files[f"delegate/{PKG}/delegate/composer/w.py"] = (
            "from ..team.tools.repo import main\nimport repository\n"
        )
        files[f"utils/{PKG}/utils/z.py"] = f"def f():\n    __import__('{tools}.repo')\n"
        files[f"utils/{PKG}/utils/y.py"] = "import tools.repo\n"
        files[f"delegate/{PKG}/delegate/team/tools/x.py"] = (
            "from agent_pm.delegate.team import tools\nfrom .y import z\n"
        )
        root = self.tree(files)
        self.assertEqual(
            guards.layer_imports(root),
            sorted(
                [
                    f"delegate/{PKG}/delegate/composer/w.py: delegate.composer imports {tools}.repo",
                    f"delegate/{PKG}/delegate/composer/x.py: delegate.composer imports {tools}.repo",
                    f"delegate/{PKG}/delegate/team/tools/x.py: outside the layers imports {tools}",
                    f"delegate/{PKG}/delegate/team/tools/x.py: outside the layers imports {tools}.y",
                    f"delegate/{PKG}/delegate/x.py: delegate imports {tools}.repo",
                    f"session/{PKG}/session/adapters/x.py: session.adapters imports {tools}.repo",
                    f"session/{PKG}/session/engine/api/x.py: session.engine.api imports {tools}.repo",
                    f"session/{PKG}/session/engine/records/x.py: session.engine.records imports {tools}.repo",
                    f"session/{PKG}/session/engine/types/x.py: session.engine.types imports {tools}.repo",
                    f"session/{PKG}/session/hosts/x.py: session.hosts imports {tools}.repo",
                    f"session/{PKG}/session/modes/x.py: session.modes imports {tools}.repo",
                    f"utils/{PKG}/utils/y.py: bare import tools.repo",
                    f"utils/{PKG}/utils/z.py: outside the layers imports {tools}.repo",
                    f"delegate/{PKG}/x.py: outside the layers imports {tools}.repo",
                ]
            ),
        )

    def test_team_tools_import_only_the_standard_library_utils_and_config(self):
        tools = f"delegate/{PKG}/delegate/team/tools"
        root = self.tree(
            {
                f"{tools}/x.py": "import json\nfrom agent_pm.utils.paths import under\n"
                "from ....utils import args\nfrom agent_pm.delegate.engine.config import CONFIG\n",
                f"{tools}/y.py": "import agent_pm.session.engine.api\nimport third_party\n"
                "from agent_pm.delegate.composer import run\n",
            }
        )
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"{tools}/y.py: delegate.team.tools imports agent_pm.delegate.composer.run",
                f"{tools}/y.py: delegate.team.tools imports agent_pm.session.engine.api",
                f"{tools}/y.py: delegate.team.tools imports third_party",
            ],
        )

    def test_production_tree_does_not_import_the_team_tools_as_a_library(self):
        self.assertEqual(guards.layer_imports(REPO), [])

    def test_every_layer_may_import_utils(self):
        files = {
            f"utils/{PKG}/utils/config_file.py": "import os\nimport tomllib\n"
            "from agent_pm.utils.paths import under\nfrom . import markdown\n"
        }
        files.update(
            {
                f"{layer.split('.')[0]}/{PKG}/{layer.replace('.', '/')}/x.py": "from agent_pm.utils.config_file import read_config\n"
                for layer in guards.LAYERS
            }
        )
        root = self.tree(files)
        self.assertEqual(guards.layer_imports(root), [])
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_may_import_utils(self):
        root = self.tree(
            {
                f"utils/{PKG}/utils/x.py": "from agent_pm.utils.shell import SHORT\nfrom . import git\n"
                "import agent_pm.utils.text\n"
            }
        )
        self.assertEqual(guards.pure_utils(root), [])

    def test_utils_reject_layer_external_and_literal_imports(self):
        root = self.tree(
            {
                f"utils/{PKG}/utils/x.py": "import agent_pm.session.engine.api\nimport third_party\n"
                "def f():\n    import importlib\n    importlib.import_module('agent_pm.session.adapters.base')\n"
                "    __import__('agent_pm.delegate.composer')\n",
                f"utils/{PKG}/utils/y.py": "from ..session import modes\nimport agent_pm.delegate.team.tools.repo\nimport utils.paths\n",
                f"session/{PKG}/session/modes/x.py": "import third_party\n",
            }
        )
        self.assertEqual(
            guards.pure_utils(root),
            [
                f"utils/{PKG}/utils/x.py: utils imports agent_pm.delegate.composer",
                f"utils/{PKG}/utils/x.py: utils imports agent_pm.session.adapters.base",
                f"utils/{PKG}/utils/x.py: utils imports agent_pm.session.engine.api",
                f"utils/{PKG}/utils/x.py: utils imports third_party",
                f"utils/{PKG}/utils/y.py: utils imports agent_pm.delegate.team.tools.repo",
                f"utils/{PKG}/utils/y.py: utils imports agent_pm.session.modes",
                f"utils/{PKG}/utils/y.py: utils imports utils.paths",
            ],
        )
