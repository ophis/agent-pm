"""Repository guards on independent sample trees and the checkout."""

import os

from tests import guards
from tests.guard_samples import Sample

OPKG = "src/agent_pm/linear_orchestrator"
PROBE = (
    "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in "
    "sys.argv[1:]}))\n"
)


class OrchestratorText(Sample):
    def test_runtime_names_and_tool_strings_name_the_file_and_line(self):
        root = self.tree(
            {
                f"{OPKG}/router.py": 'X = "codex"\n',
                f"{OPKG}/a.py": (
                    'import subprocess\nsubprocess.run(["tmux", "ls"])\nK = "adapters"\n# Claude\n'
                ),
                "config.toml": '# claude\nx = "tmux"\ny = \'adapters\'\nz = "tmux ls"  # "tmux"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(self.wrap_orchestrator(root)),
            [
                "config.toml:1: runtime name claude",
                'config.toml:2: str constant "tmux"',
                'config.toml:3: str constant "adapters"',
                f'{OPKG}/a.py:2: str constant "tmux"',
                f'{OPKG}/a.py:3: str constant "adapters"',
                f"{OPKG}/a.py:4: runtime name Claude",
                f"{OPKG}/router.py:1: runtime name codex",
            ],
        )

    def test_agents_md_and_longer_strings_are_clean(self):
        root = self.tree(
            {
                f"{OPKG}/a.py": '"""Reads AGENTS.md."""\nP = "AGENTS.md"\nH = "tmux attach"\n',
                "config.toml": '# see AGENTS.md\nx = "tmux attach"\n',
            }
        )
        self.assertEqual(guards.orchestrator_text(self.wrap_orchestrator(root)), [])

    def test_handoff_literals_only_in_issues(self):
        root = self.tree(
            {
                f"{OPKG}/linear/issues.py": 'H = "Handoff from"\n'
                'S = ("## Source", "## Instructions", "## Comments")\n',
                f"{OPKG}/promote.py": 'x = f"Handoff from {i}: {u}"\n# ## Comments are quoted\n',
                f"{OPKG}/target.py": 'M = "or its ## Instructions"\nS = "## Source"\n',
                "src/issues.py": 'H = "Handoff from"\n',
                "config.toml": 'x = "## Comments"\n',
                "tests/a_test.py": 'X = "Handoff from ## Source"\n',
            }
        )
        self.assertEqual(
            guards.orchestrator_text(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/promote.py:1: Handoff literal Handoff from",
                f"{OPKG}/promote.py:2: Handoff literal ## Comments",
                f"{OPKG}/target.py:1: Handoff literal ## Instructions",
                f"{OPKG}/target.py:2: Handoff literal ## Source",
                "src/issues.py:1: Handoff literal Handoff from",
            ],
        )


class ConfigOnce(Sample):
    LOAD = "calls agent_pm.linear_orchestrator.engine.config.load outside an entry main"
    USE_LOGS = "calls agent_pm.linear_orchestrator.linear.linear.use_logs outside an entry main"

    def test_load_and_use_logs_outside_an_entry_main(self):
        root = self.tree(
            {
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm.linear_orchestrator.linear import linear\ndef main():\n"
                "    config.load()\n    linear.use_logs(None)\n\ndef inner():\n    config.load()\n",
                f"{OPKG}/linear/writeback.py": (
                    "from agent_pm.linear_orchestrator.engine import config\n"
                    "from agent_pm.linear_orchestrator.linear.linear import use_logs\n\ndef "
                    "main():\n"
                    "    config.load()\n    use_logs(None)\n"
                ),
                f"{OPKG}/engine/promote.py": (
                    "import agent_pm.linear_orchestrator.engine.config "
                    "as c\n\nclass P:\n"
                    "    def main(self):\n        c.load()\n"
                ),
                "src/run.py": "import agent_pm.linear_orchestrator.engine.config\n\ndef main():\n"
                "    agent_pm.linear_orchestrator.engine.config.load()\n",
            }
        )
        self.assertEqual(
            guards.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/promote.py:5: {self.LOAD}",
                f"{OPKG}/engine/run.py:8: {self.LOAD}",
                f"{OPKG}/linear/writeback.py:5: {self.LOAD}",
                f"{OPKG}/linear/writeback.py:6: {self.USE_LOGS}",
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
                    "def _runnable():\n    pass\n\ndef load():\n    "
                    "_runnable()\n"
                    "    _docs()\n    _load_config()\n"
                ),
                f"{OPKG}/engine/promote.py": (
                    "from agent_pm.linear_orchestrator.engine import "
                    "config\n"
                    "from "
                    "agent_pm.linear_orchestrator.linear import linear\n"
                    "def main():\n    try:\n        cfg "
                    "= config.load()\n    except SystemExit:\n"
                    "        linear.use_logs(None)\n"
                ),
                "src/config.py": "def load():\n    _runnable()\n",
            }
        )
        self.assertEqual(
            guards.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/run.py:2: "
                "calls agent_pm.linear_orchestrator.engine.config._runnable",
                "src/config.py:2: calls _runnable",
            ],
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in (guards.orchestrator_text, guards.config_once):
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


class ConfigLevels(Sample):
    def levels(self, text):
        return guards.config_levels(self.tree({guards.CONFIG_LEVELS: text}))

    def test_the_layer_order_and_the_config_levels(self):
        self.assertEqual(
            guards.LAYERS, ("utils", "session", "delegate", guards.ORCH_PROJECT, "manager")
        )
        self.assertEqual(
            self.levels(
                'LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), '
                '("manager",))\n'
            ),
            [],
        )

    def test_each_project_once_and_only_projects(self):
        at = guards.CONFIG_LEVELS
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
    def test_an_example_with_a_real_line_its_shape_suffix_or_empty_table_passes(self):
        root = self.tree(
            {
                "delegate/config.toml": (
                    'cwd = ""\n# local: cwd = "/x"\n[output]\ntype = '
                    '"local"\ndir = ""\n'
                    '[roles.a.output]\ntype = "x"\n# local: [roles.b]\n# local: cwd = "/y"\n'
                    '# local: [roles.b.output]\n# local: dir = "d"\n'
                ),
                f"{guards.ORCH_PROJECT}/config.toml": (
                    "[labels]\n# local: [labels]\n# local: x = "
                    '"1"\n'
                    '[roles.a]\nkey = ""\n# local: [roles.c]\n# local: key = "k"\n'
                    '# local: [core.roles.a]\n# local: cwd = "/z"\n'
                ),
            }
        )
        self.assertEqual(guards.explicit_keys(root), [])

    def test_a_commented_only_key_is_refused(self):
        root = self.tree(
            {
                "session/config.toml": (
                    "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n"
                ),
                "delegate/config.toml": (
                    '# local: team_dirs = ["~/t"]\n[roles.a]\nt = 1\n# '
                    "local: [roles.a]\n"
                    "# local: [roles.a.deep]\n# local: y = 2\n"
                ),
                "manager/config.toml": "# local: [bad\n",
            }
        )
        found = guards.explicit_keys(root)
        self.assertEqual(
            [f for f in found if not f.startswith("manager/")],
            [
                "delegate/config.toml: `# local:` key roles.a.deep.y has no real line",
                "delegate/config.toml: `# local:` key team_dirs has no real line",
                "session/config.toml: `# local:` key hosts.tmux.status_line has no real line",
            ],
        )
        self.assertEqual(len([f for f in found if f.startswith("manager/config.toml: ")]), 1)
