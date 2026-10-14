import os

from tests import guards
from tests.guard_checks import imports, placement
from tests.guard_data.linear_orchestrator import ORCH_PKG, ORCH_PROJECT
from tests.guard_data.session import SESSION_SRC
from tests.guard_samples import PKG, Sample

OPKG = "src/agent_pm/linear_orchestrator"


UNLISTED = (
    "not __init__.py, cli.py, config.toml, engine/, a listed subpackage or a listed entry module"
)


class PackageClimbs(Sample):
    def violations(self, text, sub="engine/sample.py"):
        rel = f"delegate/{PKG}/delegate/{sub}"
        root = self.tree({rel: text})
        return [v for v in placement.package_climbs(root) if v.startswith(f"{rel}:")]

    def test_rejects_every_way_to_climb_past_the_package(self):
        for expr in (
            "os.path.join(os.path.dirname(__file__), *[os.pardir] * 2)",
            "os.path.dirname(os.path.dirname(os.path.dirname(__file__)))",
            "Path(__file__).parents[2]",
            "Path(__file__).parents[-1]",
            "Path(__file__).parents[index]",
            "Path(__file__).parents[1:3]",
            "Path(__file__).parent.parent.parent",
            "Path(__file__).resolve().parent / '..' / '..'",
        ):
            with self.subTest(expr=expr):
                self.assertTrue(
                    self.violations(f"import os\nfrom pathlib import Path\nROOT = {expr}\n")
                )

    def test_tracks_aliases_and_separate_assignments(self):
        self.assertTrue(
            self.violations(
                "from pathlib import Path as P\nf = P(__file__)\np = f.parent.parent\nx = p.parent\n"
            )
        )
        self.assertTrue(
            self.violations(
                "import os as o\np = o.path.dirname(__file__)\nx = o.path.join(p, '..', '..')\n"
            )
        )

    def test_project_name_segments_inside_the_package_pass(self):
        for text in (
            "from pathlib import Path\np = Path(__file__).parent / 'session' / 'config.toml'\n",
            "from pathlib import Path\np = Path(__file__).parent.parent / 'session' / 'config.toml'\n",
            "import os\nx = os.path.join(os.path.dirname(__file__), 'linear-orchestrator/config.toml')\n",
            "import os\nx = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'linear-orchestrator/config.toml')\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.violations(text), [])

    def test_rejects_sibling_paths_and_listing_a_root_outside_the_package(self):
        for text in (
            "from pathlib import Path\np = Path(__file__).parent.parent.parent / 'session' / 'config.toml'\n",
            "import os\nx = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'linear-orchestrator/config.toml')\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.violations(text))
        self.assertTrue(
            self.violations(
                "import os\nfrom agent_pm.utils.paths import ROOT\nx = os.listdir(ROOT)\n"
            )
        )

    def test_package_resources_and_repository_tools_pass(self):
        self.assertEqual(
            self.violations(
                "import os\nTEAM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'team')\n",
                "composer/base.py",
            ),
            [],
        )
        self.assertEqual(
            self.violations(
                "import os\nHOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hosts/tmux/tmux_host.py')\n",
                "runtimes/tui_hook.py",
            ),
            [],
        )
        root = self.tree(
            {"delegate/tests/test.py": "from pathlib import Path\nx = Path(__file__).parents[9]\n"}
        )
        self.assertFalse(any("test.py" in v for v in placement.package_climbs(root)))

    def test_repository_helpers_cannot_escape_by_import_or_attribute(self):
        for text in (
            "from agent_pm.utils import ROOT\n",
            "from agent_pm.utils.paths import import_roots\n",
            "from agent_pm.utils import ROOT\nfrom os.path import join\nx = join(ROOT, 'bin')\n",
            "from agent_pm.utils.paths import ROOT as root\nfrom pathlib import Path\nx = Path(root) / 'bin'\n",
            "from agent_pm import utils\nx = utils.ROOT\n",
            "import agent_pm.utils.paths as paths\nx = paths.import_roots\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.violations(text))

    def test_unrelated_bases_may_have_project_names(self):
        self.assertEqual(
            self.violations(
                "import os\nfrom pathlib import Path\nx = os.path.join('/data', 'session')\ny = Path('/work') / 'delegate'\n"
            ),
            [],
        )

    def test_no_file_is_exempt(self):
        rel = "utils/src/agent_pm/utils/paths.py"
        root = self.tree(
            {
                rel: "import os\nROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))\n"
            }
        )
        self.assertEqual(
            placement.package_climbs(root), [f"{rel}:2: ROOT: path escapes its package"]
        )
        self.assertEqual(placement.package_climbs(self.tree({})), [])


class NamespacePackage(Sample):
    def own(self, *extra: str) -> str:
        files = [f"{top}/src/agent_pm/{sub}/__init__.py" for top, sub in guards.PROJECTS.items()]
        return self.tree({f: "" for f in (*files, *extra)})

    def test_the_own_subpackages_and_caches_are_clean(self):
        root = self.own("session/src/__pycache__/x.pyc", f"session/{PKG}/__pycache__/x.pyc")
        self.assertEqual(placement.namespace_package(root), [])

    def test_finder_and_build_leftovers_are_ignored(self):
        root = self.own(
            "utils/src/.DS_Store",
            f"utils/{PKG}/.DS_Store",
            "manager/src/agent_pm_manager.egg-info/PKG-INFO",
            f"session/{PKG}/x.egg-info/PKG-INFO",
        )
        self.assertEqual(placement.namespace_package(root), [])

    def test_an_init_py_is_refused(self):
        root = self.own(*(f"{top}/{PKG}/__init__.py" for top in guards.PROJECTS))
        self.assertEqual(
            placement.namespace_package(root),
            sorted(
                f"{top}/{PKG}/__init__.py: namespace package __init__.py" for top in guards.PROJECTS
            ),
        )

    def test_a_foreign_subpackage_or_entry_is_refused(self):
        root = self.own(
            f"delegate/{PKG}/linear_orchestrator/__init__.py",
            f"delegate/{PKG}/config.py",
            "utils/src/repo.py",
            f"linear-orchestrator/{PKG}/session/__init__.py",
            "linear-orchestrator/src/other/__init__.py",
        )
        self.assertEqual(
            placement.namespace_package(root),
            [
                f"delegate/{PKG}/config.py: not delegate's",
                f"delegate/{PKG}/linear_orchestrator: not delegate's",
                f"linear-orchestrator/{PKG}/session: not linear-orchestrator's",
                "linear-orchestrator/src/other: not linear-orchestrator's",
                "utils/src/repo.py: not utils's",
            ],
        )

    def test_a_missing_subpackage(self):
        root = self.own()
        os.remove(os.path.join(root, "manager", PKG, "manager", "__init__.py"))
        os.rmdir(os.path.join(root, "manager", PKG, "manager"))
        self.assertEqual(placement.namespace_package(root), [f"manager/{PKG}/manager: missing"])


class OrchestratorLayout(Sample):
    def test_only_cli_is_executable(self):
        root = self.tree(
            {
                f"{ORCH_PROJECT}/{OPKG}/cli.py": 'if __name__ == "__main__": pass\n',
                f"{ORCH_PROJECT}/{OPKG}/engine/router.py": 'if __name__ == "__main__": pass\n',
            }
        )
        self.assertEqual(
            placement.orchestrator_entry(root),
            [
                f"{ORCH_PROJECT}/{OPKG}/engine/router.py:1: entry outside cli.py",
            ],
        )

    def test_a_run_module_is_refused(self):
        root = self.tree(
            {
                f"{ORCH_PROJECT}/{OPKG}/cli.py": "",
                f"{ORCH_PROJECT}/{OPKG}/engine/run.py": "from agent_pm import delegate\n",
            }
        )
        self.assertEqual(
            placement.orchestrator_entry(root),
            [
                f"{ORCH_PROJECT}/{OPKG}/engine/run.py: no such module: a run is its Delegate's"
                " harness (FR-25)"
            ],
        )

    def test_entry_is_required(self):
        self.assertEqual(
            placement.orchestrator_entry(self.tree({})),
            [
                f"{ORCH_PROJECT}/{OPKG}/cli.py: missing entry",
            ],
        )

    def test_session_imports_are_frozen_at_their_owners(self):
        root = self.tree(
            {
                f"{ORCH_PROJECT}/{OPKG}/engine/router.py": "from agent_pm.session import SessionError, RunLog\n",
                f"{ORCH_PROJECT}/{OPKG}/engine/ops.py": "from agent_pm.session import SessionError\n",
                f"{ORCH_PROJECT}/{OPKG}/engine/new.py": "import agent_pm.session as session\nsession.RunLog()\n",
            }
        )
        self.assertEqual(
            imports.orchestrator_session_imports(root),
            [
                f"{ORCH_PROJECT}/{OPKG}/engine/new.py: {ORCH_PKG}.engine.new imports RunLog from agent_pm.session; go through agent_pm.delegate",
                f"{ORCH_PROJECT}/{OPKG}/engine/ops.py: {ORCH_PKG}.engine.ops imports SessionError from agent_pm.session; go through agent_pm.delegate",
                f"{ORCH_PROJECT}/{OPKG}/engine/router.py: {ORCH_PKG}.engine.router imports RunLog from agent_pm.session; go through agent_pm.delegate",
                f"{ORCH_PROJECT}/{OPKG}/engine/router.py: {ORCH_PKG}.engine.router imports SessionError from agent_pm.session; go through agent_pm.delegate",
            ],
        )


class PackageTop(Sample):
    def test_package_config_resource_is_allowed(self):
        files = {
            path: text
            for project in guards.PROJECTS
            for path, text in self.package(project, "__init__.py", "engine/x.py").items()
        }
        files[SESSION_SRC + "config.toml"] = ""
        self.assertEqual(placement.package_top(self.tree(files)), [])

    def package(self, project, *entries):
        sub = guards.PROJECTS[project]
        return {f"{project}/{PKG}/{sub}/{e}": "" for e in entries}

    def test_only_init_cli_engine_and_listed_subpackages_and_entry_modules(self):
        files = {
            **self.package("utils", "__init__.py", "x.py"),
            **self.package(
                "session", "__init__.py", "extension.py", "engine/x.py", "runtimes/x.py", "extra.py"
            ),
            **self.package(
                "delegate",
                "__init__.py",
                "cli.py",
                "config.py",
                "engine/x.py",
                "team/x.py",
                "misc/x.py",
            ),
            **self.package(ORCH_PROJECT, "__init__.py", "engine/x.py", "config.toml"),
            **self.package("manager", "__init__.py", "engine/crew.py", "engine/roster.py", "y.py"),
        }
        self.assertEqual(
            placement.package_top(self.tree(files)),
            [
                f"delegate/{PKG}/delegate/misc: {UNLISTED}",
                f"manager/{PKG}/manager/y.py: {UNLISTED}",
                f"session/{PKG}/session/extra.py: {UNLISTED}",
            ],
        )

    def test_a_missing_entry(self):
        files = self.package("delegate", "__init__.py", "cli.py")
        self.assertIn(
            f"delegate/{PKG}/delegate/engine: missing", placement.package_top(self.tree(files))
        )
