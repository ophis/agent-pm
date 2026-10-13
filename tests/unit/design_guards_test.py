"""Repository guards on independent sample trees and the checkout."""

import os

from tests import guards
from tests.case import REPO
from tests.guard_samples import PKG, Sample

OPKG = "src/agent_pm/linear_orchestrator"
PROBE = "import importlib,json,sys\nprint(json.dumps({n:importlib.import_module(n).__file__ for n in sys.argv[1:]}))\n"


class LayerCommands(Sample):
    SESSION, DELEGATE = (f"{layer}/{PKG}/{layer}" for layer in ("session", "delegate"))
    LINEAR = f"linear-orchestrator/{OPKG}"

    def test_a_layer_command_in_a_spawn_argv(self):
        root = self.tree(
            {
                f"{self.DELEGATE}/a.py": 'import subprocess\nsubprocess.run(["agent-pm-session", "x"])\n'
                'subprocess.Popen(cwd="/", args=("x", "agent-pm-role"))\n',
                f"{self.LINEAR}/b.py": "import os\nfrom subprocess import check_output as out\n"
                'out("agent-pm-issue run", shell=True)\nos.execvp("agent-pm-x", ["agent-pm-x"])\n'
                'os.spawnl(os.P_WAIT, "/bin/agent-pm-y", f"agent-pm-{n}")\n',
                f"{self.SESSION}/c.py": 'import subprocess as sp\nsp.run(["agent-pm-session"])\n'
                'sp.call(["agent-pm-role"])\n',
                f"{self.DELEGATE}/team/tools/d.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
            }
        )
        self.assertEqual(
            guards.layer_commands(root),
            sorted(
                [
                    f'{self.DELEGATE}/a.py:2: runs "agent-pm-session"',
                    f'{self.DELEGATE}/a.py:3: runs "agent-pm-role"',
                    f'{self.DELEGATE}/team/tools/d.py:2: runs "agent-pm-x"',
                    f'{self.SESSION}/c.py:3: runs "agent-pm-role"',
                    f'{self.LINEAR}/b.py:3: runs "agent-pm-issue"',
                    f'{self.LINEAR}/b.py:4: runs "agent-pm-x"',
                    f'{self.LINEAR}/b.py:5: runs "agent-pm-"',
                ]
            ),
        )

    def test_other_strings_calls_and_files_are_clean(self):
        root = self.tree(
            {
                f"{self.DELEGATE}/a.py": 'import subprocess\nNAME = "agent-pm-progress"\nprint("agent-pm-x")\n'
                'subprocess.run(["tmux", NAME, "/agent-pm-list"], env={"A": "agent-pm-x"})\n',
                f"manager/{PKG}/manager/crew.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
                "tests/b.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
            }
        )
        self.assertEqual(guards.layer_commands(root), [])


class Standalone(Sample):
    def test_any_file_of_a_lower_project_naming_the_linear_orchestrator(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/config.toml": "# see ../linear-orchestrator/\n",
                f"{team}/roles/r.md": "x\nRun agent_pm.linear_orchestrator.engine.run, not linear-orchestrator\n",
                f"session/{PKG}/session/x.py": "import agent_pm.linear_orchestrator\n",
                f"utils/{PKG}/utils/y.py": "# linear_orchestrator\n",
                f"manager/{PKG}/manager/z.py": "# linear-orchestrator\n",
                f"delegate/{PKG}/__pycache__/x.pyc": "linear_orchestrator",
                "manager/skills/a/notes": "Linear orchestrator, linear_orchestra, agent_pm_lineage\n",
                "session/tests/a_test.py": "import agent_pm.linear_orchestrator\n",
                "linear-orchestrator/b.py": "import agent_pm.linear_orchestrator\n",
                "tests/c.py": "import agent_pm.linear_orchestrator\n",
                guards.CONFIG_LEVELS: 'LEVELS = (("linear-orchestrator",),)\n# linear-orchestrator\n',
            }
        )
        self.assertEqual(
            guards.standalone(root),
            [
                "delegate/config.toml:1: names linear-orchestrator",
                f"{team}/roles/r.md:2: names linear-orchestrator",
                f"{team}/roles/r.md:2: names linear_orchestrator",
                f"manager/{PKG}/manager/z.py:1: names linear-orchestrator",
                f"session/{PKG}/session/x.py:1: names linear_orchestrator",
                f"{guards.CONFIG_LEVELS}:2: names linear-orchestrator",
                f"utils/{PKG}/utils/y.py:1: names linear_orchestrator",
            ],
        )


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({f"session/{PKG}/session/engine/api.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(
            guards.runtime_names(root),
            [
                f"session/{PKG}/session/engine/api.py:1: runtime name Claude",
                f"session/{PKG}/session/engine/api.py:2: runtime name codex",
            ],
        )

    def test_print_prompt_is_a_runtime_name(self):
        root = self.tree({f"delegate/{PKG}/delegate/x.py": 'RUNTIME = "print_prompt"\n'})
        self.assertEqual(
            guards.runtime_names(root),
            [f"delegate/{PKG}/delegate/x.py:1: runtime name print_prompt"],
        )

    def test_policy_runtime_tables_are_exempt(self):
        root = self.tree(
            {"delegate/config.toml": "[policy.claude]\nflags = []\n[policy.codex]\nflags = []\n"}
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({f"session/{PKG}/session/adapters/claude/x.py": 'NAME = "claude"\n'})
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_table_header_implementation_line_and_comments_are_exempt(self):
        root = self.tree(
            {
                "delegate/config.toml": "# codex in a comment\n[adapters.codex]\n"
                'implementation = "adapters.codex:CodexAdapter"  # codex\n'
                'key = "x # codex"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), ["delegate/config.toml:4: runtime name codex"])

    def test_toml_print_prompt_table_header_and_implementation_line_are_exempt(self):
        root = self.tree(
            {
                "delegate/config.toml": "# [adapters.print_prompt]  act-as's runtime\n[adapters.print_prompt]\n"
                'implementation = "adapters.print_prompt:PrintPromptAdapter"\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_runtime_line_is_exempt_under_any_table(self):
        root = self.tree(
            {
                "delegate/config.toml": 'runtime = "codex"\n[roles.x]\nruntime = "codex"\n'
                '[roles.y]\nruntime = "codex"  # note\n'
            }
        )
        self.assertEqual(guards.runtime_names(root), [])

    def test_toml_name_in_a_key_value_elsewhere(self):
        root = self.tree(
            {
                "delegate/config.toml": '[adapters.codex]\nimplementation = "adapters.codex:CodexAdapter"\n'
                '[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\n'
                'runtime = "codex" x\n'
            }
        )
        self.assertEqual(
            guards.runtime_names(root),
            [f"delegate/config.toml:{n}: runtime name codex" for n in (4, 5, 6, 7)],
        )


class OldCore(Sample):
    def test_a_core_import_in_a_tree(self):
        root = self.tree(
            {
                "tests/a.py": "from core.src import drive\n",
                "session/b.py": "import core\nimport core_x\n",
                ".github/c.py": "import core\n",
                "docs/d.py": "import core\n",
            }
        )
        self.assertEqual(
            guards.old_core(root), ["session/b.py: imports core", "tests/a.py: imports core"]
        )


class AdaptersKey(Sample):
    def test_the_literal_key_only_in_the_session_config_parser(self):
        sample = 'cfg = {}\nvalue = cfg["adapters"]\n'
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/composer/x.py": sample,
                f"session/{PKG}/session/adapters/x.py": sample,
                guards.CONFIG_FILE: sample,
                guards.ADAPTERS_HOME: sample,
            }
        )
        self.assertEqual(
            guards.adapters_key(root),
            [
                f'delegate/{PKG}/delegate/composer/x.py:2: literal key "adapters"',
                f'{guards.CONFIG_FILE}:2: literal key "adapters"',
                f'session/{PKG}/session/adapters/x.py:2: literal key "adapters"',
            ],
        )


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_tmux_host(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": sample,
                f"session/{PKG}/session/modes/x.py": sample,
                f"delegate/{PKG}/delegate/composer/y.py": 'TOOL = "tmux"\n',
                "session/src/hosts/tmux/tmux_host.py": sample,
                "session/tests/z.py": sample,
            }
        )
        self.assertEqual(
            guards.tmux_calls(root),
            sorted(
                [
                    f'delegate/{PKG}/delegate/composer/y.py:1: str constant "tmux"',
                    'session/src/hosts/tmux/tmux_host.py:2: str constant "tmux"',
                    f'session/{PKG}/session/modes/x.py:2: str constant "tmux"',
                ]
            ),
        )

    def test_tmux_inside_a_longer_string_is_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/modes/x.py": 'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'
            }
        )
        self.assertEqual(guards.tmux_calls(root), [])


class PythonCommands(Sample):
    def test_a_python_command_other_than_p_m_agent_pm_or_a_team_tool(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/config.toml": 'commands = ["python3 x.py", "python3 -m agent_pm.x"]\n'
                "# python3 -P {{team}}/tools/repo.py\n"
                'gate = "python3 -P {{team}}/x.py"\n',
                "linear-orchestrator/config.toml": 'gate = "python3 -P -m agent_pmx.y"\n',
                "delegate/skills/a/SKILL.md": "Run `python3.11 x.py`.\n"
                "- Bash(python -P -m agent_pm.delegate.cli --list)\n",
                "manager/skills/b/SKILL.md": "Run `python3 y.py`.\n",
                f"{team}/roles/r.md": "1. `python3 -P agent_pm.x`\n2. /usr/bin/python3 -I -m agent_pm.x\n"
                "3. `env PYTHONPATH=x python3 -P -m agent_pm.linear_orchestrator.engine.run`\n"
                "4. `python3 -P <team>/tools/repo.py status --dir x`\n"
                "5. `python3 -P <team>/tools/../repo.py`\n6. `python3 -P <team>/tools/repo.pyc`\n",
            }
        )
        self.assertEqual(
            guards.python_commands(root),
            [
                'delegate/config.toml:1: python command "python3 -m agent_pm.x"',
                'delegate/config.toml:1: python command "python3 x.py"',
                'delegate/config.toml:3: python command "python3 -P {{team}}/x.py"',
                'delegate/skills/a/SKILL.md:1: python command "python3.11 x.py"',
                f'{team}/roles/r.md:1: python command "python3 -P agent_pm.x"',
                f'{team}/roles/r.md:2: python command "python3 -I -m agent_pm.x"',
                f'{team}/roles/r.md:5: python command "python3 -P <team>/tools/../repo.py"',
                f'{team}/roles/r.md:6: python command "python3 -P <team>/tools/repo.pyc"',
                'linear-orchestrator/config.toml:1: python command "python3 -P -m agent_pmx.y"',
                'manager/skills/b/SKILL.md:1: python command "python3 y.py"',
            ],
        )

    def test_other_words_and_files_are_clean(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/team/a.md": "pythonic, my_python3 x.py, Python 3.11\n",
                "delegate/skills/a/notes.txt": "python3 x.py\n",
                "delegate/README.md": "python3 x.py\n",
                "linear-orchestrator/other.toml": 'x = "python3 x.py"\n',
                "tests/a.md": "python3 x.py\n",
            }
        )
        self.assertEqual(guards.python_commands(root), [])


class OrchestratorText(Sample):
    def test_runtime_names_and_tool_strings_name_the_file_and_line(self):
        root = self.tree(
            {
                f"{OPKG}/router.py": 'X = "codex"\n',
                f"{OPKG}/a.py": 'import subprocess\nsubprocess.run(["tmux", "ls"])\nK = "adapters"\n'
                "# Claude\n",
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
                f"{OPKG}/linear/writeback.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm.linear_orchestrator.linear.linear import use_logs\n\ndef main():\n"
                "    config.load()\n    use_logs(None)\n",
                f"{OPKG}/engine/promote.py": "import agent_pm.linear_orchestrator.engine.config as c\n\nclass P:\n"
                "    def main(self):\n        c.load()\n",
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
                f"{OPKG}/engine/run.py": "from agent_pm.linear_orchestrator.engine import config\nconfig._runnable(1)\n",
                f"{OPKG}/engine/config.py": "def _runnable():\n    pass\n\ndef load():\n    _runnable()\n"
                "    _docs()\n    _load_config()\n",
                f"{OPKG}/engine/promote.py": "from agent_pm.linear_orchestrator.engine import config\n"
                "from agent_pm.linear_orchestrator.linear import linear\n"
                "def main():\n    try:\n        cfg = config.load()\n    except SystemExit:\n"
                "        linear.use_logs(None)\n",
                "src/config.py": "def load():\n    _runnable()\n",
            }
        )
        self.assertEqual(
            guards.config_once(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/engine/run.py:2: calls agent_pm.linear_orchestrator.engine.config._runnable",
                "src/config.py:2: calls _runnable",
            ],
        )


class OrchestratorAbsent(Sample):
    def test_an_absent_or_empty_tree_is_clean(self):
        for root in (os.path.join(self.tree({}), "missing"), self.tree({"config.toml": ""})):
            for guard in (guards.orchestrator_text, guards.config_once):
                with self.subTest(guard.__name__, root=root):
                    self.assertEqual(guard(root), [])


class StatusLogFormat(Sample):
    CLOCK = 'CLOCK = "%H:%M:%S"\n'

    def test_the_real_tree_is_clean(self):
        self.assertEqual(guards.status_log_format(REPO), [])

    def test_the_clock_in_another_projects_module_or_an_f_string(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": self.CLOCK,
                f"manager/{PKG}/manager/y.py": "import time\n\n"
                "line = f\"{time.strftime('%H:%M:%S')} {name}\"\n"
                'hook = f"$(date +%H:%M:%S) {name} dead"\n',
                f"linear-orchestrator/{OPKG}/z.py": 'import time\nat = time.strftime("%H:%M:%S")\n',
            }
        )
        outside = "status log line format outside session/engine/status_log.py"
        self.assertEqual(
            guards.status_log_format(root),
            sorted(
                [
                    f"delegate/{PKG}/delegate/x.py:1: {outside}",
                    f"manager/{PKG}/manager/y.py:3: {outside}",
                    f"manager/{PKG}/manager/y.py:4: {outside}",
                    f"linear-orchestrator/{OPKG}/z.py:2: {outside}",
                ]
            ),
        )

    def test_status_log_py_tmux_host_and_files_outside_src_are_clean(self):
        root = self.tree(
            {
                f"session/{PKG}/session/engine/status_log.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": self.CLOCK,
                "session/tests/x_test.py": self.CLOCK,
                "tests/y_test.py": self.CLOCK,
            }
        )
        self.assertEqual(guards.status_log_format(root), [])


class ConfigLevels(Sample):
    def levels(self, text):
        return guards.config_levels(self.tree({guards.CONFIG_LEVELS: text}))

    def test_the_layer_order_and_the_config_levels(self):
        self.assertEqual(
            guards.LAYERS, ("utils", "session", "delegate", guards.ORCH_PROJECT, "manager")
        )
        self.assertEqual(
            self.levels(
                'LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",))\n'
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
                "delegate/config.toml": 'cwd = ""\n# local: cwd = "/x"\n[output]\ntype = "local"\ndir = ""\n'
                '[roles.a.output]\ntype = "x"\n# local: [roles.b]\n# local: cwd = "/y"\n'
                '# local: [roles.b.output]\n# local: dir = "d"\n',
                f"{guards.ORCH_PROJECT}/config.toml": '[labels]\n# local: [labels]\n# local: x = "1"\n'
                '[roles.a]\nkey = ""\n# local: [roles.c]\n# local: key = "k"\n'
                '# local: [core.roles.a]\n# local: cwd = "/z"\n',
            }
        )
        self.assertEqual(guards.explicit_keys(root), [])

    def test_a_commented_only_key_is_refused(self):
        root = self.tree(
            {
                "session/config.toml": "[hosts.tmux]\nx = 1\n# local: [hosts.tmux]\n# local: status_line = true\n",
                "delegate/config.toml": '# local: team_dirs = ["~/t"]\n[roles.a]\nt = 1\n# local: [roles.a]\n'
                "# local: [roles.a.deep]\n# local: y = 2\n",
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
