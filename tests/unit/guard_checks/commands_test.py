from tests.guard_checks import commands
from tests.guard_data.linear_orchestrator import ORCH_PROJECT
from tests.guard_samples import PKG, Sample

OPKG = "src/agent_pm/linear_orchestrator"


class LayerCommands(Sample):
    SESSION, DELEGATE = (f"{layer}/{PKG}/{layer}" for layer in ("session", "delegate"))
    LINEAR = f"linear-orchestrator/{OPKG}"

    def test_a_layer_command_in_a_spawn_argv(self):
        root = self.tree(
            {
                f"{self.DELEGATE}/a.py": (
                    'import subprocess\nsubprocess.run(["agent-pm-session", "x"])\nsubprocess.Popen(cwd="/", args=("x", "agent-pm-role"))\n'
                ),
                f"{self.LINEAR}/b.py": 'import os\nfrom subprocess import check_output as out\nout("agent-pm-issue run", shell=True)\nos.execvp("agent-pm-x", ["agent-pm-x"])\nos.spawnl(os.P_WAIT, "/bin/agent-pm-y", f"agent-pm-{n}")\n',
                f"{self.SESSION}/c.py": 'import subprocess as sp\nsp.run(["agent-pm-session"])\nsp.call(["agent-pm-role"])\n',
                f"{self.DELEGATE}/team/tools/d.py": (
                    'import subprocess\nsubprocess.run(["agent-pm-x"])\n'
                ),
            }
        )
        self.assertEqual(
            commands.layer_commands(root),
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
                f"{self.DELEGATE}/a.py": (
                    'import subprocess\nNAME = "agent-pm-progress"\nprint("agent-pm-x")\nsubprocess.run(["tmux", NAME, "/agent-pm-list"], env={"A": "agent-pm-x"})\n'
                ),
                f"manager/{PKG}/manager/crew.py": (
                    'import subprocess\nsubprocess.run(["agent-pm-x"])\n'
                ),
                "tests/b.py": 'import subprocess\nsubprocess.run(["agent-pm-x"])\n',
            }
        )
        self.assertEqual(commands.layer_commands(root), [])


class PythonCommands(Sample):
    def test_a_python_command_other_than_p_m_agent_pm_or_a_team_tool(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": 'commands = ["python3 x.py", "python3 -m agent_pm.x"]\n# python3 -P {{team}}/tools/repo.py\ngate = "python3 -P {{team}}/x.py"\n',
                f"{ORCH_PROJECT}/{OPKG}/config.toml": 'gate = "python3 -P -m agent_pmx.y"\n',
                "delegate/skills/a/SKILL.md": "Run `python3.11 x.py`.\n- Bash(python -P -m agent_pm.delegate.cli --list)\n",
                "manager/skills/b/SKILL.md": "Run `python3 y.py`.\n",
                f"{team}/roles/r.md": (
                    "1. `python3 -P agent_pm.x`\n2. /usr/bin/python3 -I -m agent_pm.x\n3. `env PYTHONPATH=x python3 -P -m agent_pm.linear_orchestrator.engine.run`\n4. `python3 -P <team>/tools/repo.py status --dir x`\n5. `python3 -P <team>/tools/../repo.py`\n6. `python3 -P <team>/tools/repo.pyc`\n7. `python3 -P <role team>/tools/x.py`\n"
                ),
            }
        )
        self.assertEqual(
            commands.python_commands(root),
            [
                'delegate/skills/a/SKILL.md:1: python command "python3.11 x.py"',
                'delegate/src/agent_pm/delegate/config.toml:1: python command "python3 -m agent_pm.x"',
                'delegate/src/agent_pm/delegate/config.toml:1: python command "python3 x.py"',
                'delegate/src/agent_pm/delegate/config.toml:3: python command "python3 -P {{team}}/x.py"',
                f'{team}/roles/r.md:1: python command "python3 -P agent_pm.x"',
                f'{team}/roles/r.md:2: python command "python3 -I -m agent_pm.x"',
                f'{team}/roles/r.md:5: python command "python3 -P <team>/tools/../repo.py"',
                f'{team}/roles/r.md:6: python command "python3 -P <team>/tools/repo.pyc"',
                f'{ORCH_PROJECT}/{OPKG}/config.toml:1: python command "python3 -P -m agent_pmx.y"',
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
        self.assertEqual(commands.python_commands(root), [])


class BinCommands(Sample):
    SCRIPT = '#!/bin/sh\nexport PYTHONPATH=x\nexec python3 -P -m agent_pm.{0}.cli "$@"\n'
    SKILL = "---\nname: a\nallowed-tools:\n  - Read\n  - Bash({0})\n---\n\nRun `{0}`.\n"

    def shipped(self, files=()):
        return self.tree(
            {
                "bin/agent-pm-delegate": self.SCRIPT.format("delegate"),
                f"delegate/{PKG}/delegate/cli.py": "",
                "delegate/skills/a/SKILL.md": self.SKILL.format("agent-pm-delegate run --list"),
                f"delegate/{PKG}/delegate/config.toml": 'commands = ["python3 -P {{team}}/tools/repo.py *"]\n',
                f"{ORCH_PROJECT}/{OPKG}/config.toml": '[core.roles.r]\ngate = "agent-pm-delegate run --list"\n',
                **dict(files),
            }
        )

    def test_shipped_commands_and_their_modules_pass(self):
        self.assertEqual(commands.bin_commands(self.shipped()), [])

    def test_a_skill_pre_approving_a_command_not_in_bin(self):
        for rule in ("agent-pm-nope run", "python3 -P -m agent_pm.delegate.cli run --list"):
            with self.subTest(rule=rule):
                root = self.shipped({"manager/skills/b/SKILL.md": self.SKILL.format(rule)})
                self.assertEqual(
                    commands.bin_commands(root),
                    [f'manager/skills/b/SKILL.md: pre-approves "{rule}", not a bin/ command'],
                )

    def test_a_config_gate_or_command_naming_an_agent_pm_command_not_in_bin(self):
        root = self.shipped(
            {
                f"{ORCH_PROJECT}/{OPKG}/config.toml": '[core.roles.r]\ngate = "agent-pm-linear router --brake r"\ncommands = ["agent-pm-crew x *", "true"]\n'
            }
        )
        rel = f"{ORCH_PROJECT}/{OPKG}/config.toml"
        self.assertEqual(
            commands.bin_commands(root),
            [
                f'{rel}: runs "agent-pm-crew x *", not a bin/ command',
                f'{rel}: runs "agent-pm-linear router --brake r", not a bin/ command',
            ],
        )

    def test_a_bin_command_runs_exactly_one_existing_module_of_its_own(self):
        two = self.SCRIPT.format("delegate") + "exec python3 -P -m agent_pm.session.cli\n"
        for script, want in (
            ("#!/bin/sh\nexec true\n", "bin/agent-pm-x: runs 0 modules, not 1"),
            (two, "bin/agent-pm-x: runs 2 modules, not 1"),
            (self.SCRIPT.format("nope"), "bin/agent-pm-x: no module agent_pm.nope.cli"),
            (
                self.SCRIPT.format("delegate"),
                "bin/agent-pm-x: runs agent_pm.delegate.cli, as bin/agent-pm-delegate does",
            ),
        ):
            with self.subTest(want=want):
                root = self.shipped({"bin/agent-pm-x": script})
                self.assertEqual(commands.bin_commands(root), [want])
