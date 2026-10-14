import ast
import os

from tests import guards
from tests.case import ORCH_ROOT, REPO
from tests.guard_checks import configuration, ownership
from tests.guard_data.linear_orchestrator import CONFIG_PY, LOAD_HELPERS, ORCH_PROJECT
from tests.guard_data.session import SESSION_SRC
from tests.guard_data.utils import CONFIG_LEVELS
from tests.guard_samples import Sample

OPKG = "src/agent_pm/linear_orchestrator"


class ConfigOnce(Sample):
    LOAD = "calls agent_pm.linear_orchestrator.engine.config.load outside an entry main"
    USE_LOGS = "calls agent_pm.linear_orchestrator.engine.log.use_logs outside an entry main"

    def test_load_and_use_logs_outside_an_entry_main(self):
        root = self.tree(
            {
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\nfrom agent_pm.linear_orchestrator.engine import log as linear\ndef main():\n    config.load()\n    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                f"{OPKG}/engine/writeback.py": (
                    "from agent_pm.linear_orchestrator.engine import config\nfrom agent_pm.linear_orchestrator.engine.log import use_logs\n\ndef main():\n    config.load()\n    use_logs(None)\n"
                ),
                f"{OPKG}/engine/promote.py": (
                    "import agent_pm.linear_orchestrator.engine.config as c\n\nclass P:\n    def main(self):\n        c.load()\n"
                ),
                "src/run.py": "import agent_pm.linear_orchestrator.engine.config\n\ndef main():\n    agent_pm.linear_orchestrator.engine.config.load()\n",
            }
        )
        self.assertEqual(
            configuration.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/promote.py:5: {self.LOAD}",
                f"{OPKG}/engine/run.py:8: {self.LOAD}",
                f"{OPKG}/engine/writeback.py:5: {self.LOAD}",
                f"{OPKG}/engine/writeback.py:6: {self.USE_LOGS}",
                f"src/run.py:4: {self.LOAD}",
            ],
        )

    def test_load_helpers_outside_config_py(self):
        root = self.tree(
            {
                f"{OPKG}/engine/run.py": (
                    "from agent_pm.linear_orchestrator.engine import config\nconfig._runnable(1)\n"
                ),
                f"{OPKG}/engine/config.py": (
                    "def _runnable():\n    pass\n\ndef load():\n    _runnable()\n    _docs()\n    _load_config()\n"
                ),
                f"{OPKG}/engine/promote.py": (
                    "from agent_pm.linear_orchestrator.engine import config\nfrom agent_pm.linear_orchestrator.engine import log as linear\ndef main():\n    try:\n        cfg = config.load()\n    except SystemExit:\n        linear.use_logs(None)\n"
                ),
                "src/config.py": "def load():\n    _runnable()\n",
            }
        )
        self.assertEqual(
            configuration.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/run.py:2: calls agent_pm.linear_orchestrator.engine.config._runnable",
                "src/config.py:2: calls _runnable",
            ],
        )

    def test_the_load_helpers_are_config_py_functions(self):
        with open(os.path.join(REPO, ORCH_PROJECT, CONFIG_PY), encoding="utf-8") as f:
            defined = {n.name for n in ast.parse(f.read()).body if isinstance(n, ast.FunctionDef)}
        self.assertLessEqual(set(LOAD_HELPERS), defined)

    def test_the_real_entries_calls_outside_an_entry(self):
        files = {}
        for name in ("run.py", "promote.py"):
            with open(
                os.path.join(ORCH_ROOT, "agent_pm", "linear_orchestrator", "engine", name),
                encoding="utf-8",
            ) as f:
                files[f"{OPKG}/not_{name}"] = f.read()
        found = configuration.config_once(self.wrap_orchestrator(self.tree(files)))
        self.assertEqual(
            {(f.split(":")[0], f.split(": ", 1)[1]) for f in found},
            {(rel, call) for rel in files for call in (self.LOAD, self.USE_LOGS)},
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in (ownership.orchestrator_text, configuration.config_once):
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


class ConfigLevels(Sample):
    def levels(self, text):
        return configuration.config_levels(self.tree({CONFIG_LEVELS: text}))

    def test_the_layer_order_and_the_config_levels(self):
        self.assertEqual(guards.LAYERS, ("utils", "session", "delegate", ORCH_PROJECT, "manager"))
        self.assertEqual(
            self.levels(
                'LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",))\n'
            ),
            [],
        )

    def test_each_project_once_and_only_projects(self):
        at = CONFIG_LEVELS
        self.assertEqual(
            self.levels('LEVELS = (("utils", "session"), ("delegate", "x"), ("delegate",))\n'),
            [
                f"{at}: LEVELS holds 'delegate' 2 times",
                f"{at}: LEVELS holds 'linear-orchestrator' 0 times",
                f"{at}: LEVELS holds 'manager' 0 times",
                f"{at}: LEVELS holds 'x', not a project",
            ],
        )
        self.assertEqual(self.levels("X = 1\n"), [f"{at}: want one LEVELS assignment"])
        self.assertEqual(
            self.levels("LEVELS = tuple(x)\n"), [f"{at}: LEVELS is not a literal tuple of tuples"]
        )


class ExplicitKeys(Sample):
    def test_packaged_defaults_supply_keys_for_local_examples(self):
        root = self.tree(
            {
                SESSION_SRC + "config.toml": "[hosts.tmux]\nstatus_line = false\n",
                "delegate/config.toml": "# local: [session.hosts.tmux]\n# local: status_line = true\n",
            }
        )
        self.assertEqual(configuration.explicit_keys(root), [])

    def test_packaged_defaults_reject_commented_only_keys(self):
        rel = SESSION_SRC + "config.toml"
        root = self.tree(
            {rel: "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n"}
        )
        self.assertEqual(
            configuration.explicit_keys(root),
            [f"{rel}: `# local:` key hosts.tmux.status_line has no real line"],
        )

    def test_an_example_with_a_real_line_its_shape_suffix_or_empty_table_passes(self):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": (
                    'cwd = ""\n# local: cwd = "/x"\n[output]\ntype = "local"\ndir = ""\n[roles.a.output]\ntype = "x"\n# local: [roles.b]\n# local: cwd = "/y"\n# local: [roles.b.output]\n# local: dir = "d"\n'
                ),
                f"{ORCH_PROJECT}/{OPKG}/config.toml": (
                    '[labels]\n# local: [labels]\n# local: x = "1"\n[roles.a]\nkey = ""\n# local: [roles.c]\n# local: key = "k"\n# local: [core.roles.a]\n# local: cwd = "/z"\n'
                ),
            }
        )
        self.assertEqual(configuration.explicit_keys(root), [])

    def test_sectioned_examples_keep_dynamic_empty_tables_and_reject_missing_keys(self):
        root = self.tree(
            {
                f"{ORCH_PROJECT}/{OPKG}/config.toml": (
                    '[labels]\n# local: [linear_orchestrator.labels]\n# local: x = "1"\n'
                    '[roles.a]\nkey = ""\n# local: [linear_orchestrator.roles.b]\n# local: key = "k"\n'
                    "# local: [linear_orchestrator]\n# local: typo = 1\n# local: [session]\n"
                )
            }
        )
        self.assertEqual(
            configuration.explicit_keys(root),
            [
                f"{ORCH_PROJECT}/{OPKG}/config.toml: `# local:` key linear_orchestrator.typo has no real line",
            ],
        )

    def test_a_commented_only_key_is_refused(self):
        root = self.tree(
            {
                "session/config.toml": (
                    "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n"
                ),
                "delegate/src/agent_pm/delegate/config.toml": (
                    '# local: team_dirs = ["~/t"]\n[roles.a]\nt = 1\n# local: [roles.a]\n# local: [roles.a.deep]\n# local: y = 2\n'
                ),
                "manager/config.toml": "# local: [bad\n",
            }
        )
        found = configuration.explicit_keys(root)
        self.assertEqual(
            [f for f in found if not f.startswith("manager/")],
            [
                "delegate/src/agent_pm/delegate/config.toml: `# local:` key roles.a.deep.y has no real line",
                "delegate/src/agent_pm/delegate/config.toml: `# local:` key team_dirs has no real line",
                "session/config.toml: `# local:` key hosts.tmux.status_line has no real line",
            ],
        )
        self.assertEqual(len([f for f in found if f.startswith("manager/config.toml: ")]), 1)
