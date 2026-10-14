from tests.case import REPO
from tests.guard_checks import commands, exports, ownership
from tests.guard_data import LEGACY_HOST_NAMES
from tests.guard_data.delegate import CONFIG_FILE
from tests.guard_data.session import ADAPTERS_HOME, SESSION_SRC
from tests.guard_data.utils import CONFIG_LEVELS
from tests.guard_samples import PKG, Sample

OPKG = "src/agent_pm/linear_orchestrator"


class DelegatePackageNames(Sample):
    def test_legacy_resource_access_is_rejected_in_production_only(self):
        base = "delegate/src/agent_pm/delegate/"
        for sample in (
            "from agent_pm.utils import read_project as read\nread('delegate')\n",
            "import agent_pm.utils as u\nu.import_roots()\n",
            "DELEGATE = '/repo/delegate'\n",
            "from .config import IMPORT_ROOTS\n",
        ):
            with self.subTest(sample=sample):
                root = self.tree({base + "engine/config.py": sample})
                self.assertTrue(ownership.delegate_package_names(root))
        root = self.tree(
            {
                base + "engine/config.py": "from agent_pm.utils import read_resources\n",
                "delegate/tests/sample.py": "from agent_pm.utils import read_project\n",
            }
        )
        self.assertEqual(ownership.delegate_package_names(root), [])


class SessionNames(Sample):
    def test_layer_owned_config_entry_can_forward_to_utils(self):
        root = self.tree(
            {SESSION_SRC + "engine/config.py": "from agent_pm.utils import read_resources\n"}
        )
        self.assertEqual(ownership.session_names(root), [])

    def test_layer_owned_config_entry_cannot_read_repository_configuration(self):
        root = self.tree(
            {SESSION_SRC + "engine/config.py": "from agent_pm.utils import read_project\n"}
        )
        self.assertEqual(
            ownership.session_names(root),
            [SESSION_SRC + "engine/config.py: names read_project"],
        )

    def test_config_readers_protocol_and_team_names_are_rejected(self):
        root = self.tree(
            {
                SESSION_SRC
                + "engine/probe.py": "import tomllib\nfrom agent_pm.utils import read_project\nTAG = 'agent-pm-progress'\nTEAM = 'team/'\n"
            }
        )
        self.assertEqual(
            ownership.session_names(root),
            [
                SESSION_SRC + "engine/probe.py: names agent-pm-progress",
                SESSION_SRC + "engine/probe.py: names read_project",
                SESSION_SRC + "engine/probe.py: names team/",
                SESSION_SRC + "engine/probe.py: names tomllib",
            ],
        )

    def test_runtime_state_json_and_other_layers_are_clean(self):
        root = self.tree(
            {
                SESSION_SRC + "engine/probe.py": "import json\n",
                "delegate/src/agent_pm/delegate/probe.py": "import tomllib\n",
            }
        )
        self.assertEqual(ownership.session_names(root), [])


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
            ownership.orchestrator_text(self.wrap_orchestrator(root)),
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
        self.assertEqual(ownership.orchestrator_text(self.wrap_orchestrator(root)), [])

    def test_handoff_literals_only_in_issues(self):
        root = self.tree(
            {
                f"{OPKG}/engine/issues.py": 'H = "Handoff from"\nS = ("## Source", "## Instructions", "## Comments")\n',
                f"{OPKG}/promote.py": 'x = f"Handoff from {i}: {u}"\n# ## Comments are quoted\n',
                f"{OPKG}/target.py": 'M = "or its ## Instructions"\nS = "## Source"\n',
                "src/issues.py": 'H = "Handoff from"\n',
                "config.toml": 'x = "## Comments"\n',
                "tests/a_test.py": 'X = "Handoff from ## Source"\n',
            }
        )
        self.assertEqual(
            ownership.orchestrator_text(self.wrap_orchestrator(root)),
            [
                f"{OPKG}/promote.py:1: Handoff literal Handoff from",
                f"{OPKG}/promote.py:2: Handoff literal ## Comments",
                f"{OPKG}/target.py:1: Handoff literal ## Instructions",
                f"{OPKG}/target.py:2: Handoff literal ## Source",
                "src/issues.py:1: Handoff literal Handoff from",
            ],
        )


class Standalone(Sample):
    def test_any_file_of_a_lower_project_naming_the_linear_orchestrator(self):
        team = f"delegate/{PKG}/delegate/team"
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": "# see ../linear-orchestrator/\n",
                f"{team}/roles/r.md": (
                    "x\nRun agent_pm.linear_orchestrator.engine.run, not linear-orchestrator\n"
                ),
                f"session/{PKG}/session/x.py": "import agent_pm.linear_orchestrator\n",
                f"utils/{PKG}/utils/y.py": "# linear_orchestrator\n",
                f"manager/{PKG}/manager/z.py": "# linear-orchestrator\n",
                f"delegate/{PKG}/__pycache__/x.pyc": "linear_orchestrator",
                "manager/skills/a/notes": (
                    "Linear orchestrator, linear_orchestra, agent_pm_lineage\n"
                ),
                "session/tests/a_test.py": "import agent_pm.linear_orchestrator\n",
                "linear-orchestrator/b.py": "import agent_pm.linear_orchestrator\n",
                "tests/c.py": "import agent_pm.linear_orchestrator\n",
                CONFIG_LEVELS: ('LEVELS = (("linear-orchestrator",),)\n# linear-orchestrator\n'),
            }
        )
        self.assertEqual(
            ownership.standalone(root),
            [
                "delegate/src/agent_pm/delegate/config.toml:1: names linear-orchestrator",
                f"{team}/roles/r.md:2: names linear-orchestrator",
                f"{team}/roles/r.md:2: names linear_orchestrator",
                f"manager/{PKG}/manager/z.py:1: names linear-orchestrator",
                f"session/{PKG}/session/x.py:1: names linear_orchestrator",
                f"{CONFIG_LEVELS}:2: names linear-orchestrator",
                f"utils/{PKG}/utils/y.py:1: names linear_orchestrator",
            ],
        )


class RuntimeNames(Sample):
    def test_a_name_anywhere_in_a_module_outside_adapters(self):
        root = self.tree({f"session/{PKG}/session/engine/api.py": 'NAME = "Claude"\n# codex\n'})
        self.assertEqual(
            ownership.runtime_names(root),
            [
                f"session/{PKG}/session/engine/api.py:1: runtime name Claude",
                f"session/{PKG}/session/engine/api.py:2: runtime name codex",
            ],
        )

    def test_policy_runtime_tables_are_exempt(self):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": "[policy.claude]\nflags = []\n[policy.codex]\nflags = []\n"
            }
        )
        self.assertEqual(ownership.runtime_names(root), [])

    def test_adapters_subpackage_is_exempt(self):
        root = self.tree({f"session/{PKG}/session/adapters/claude/x.py": 'NAME = "claude"\n'})
        self.assertEqual(ownership.runtime_names(root), [])

    def test_toml_table_header_implementation_line_and_comments_are_exempt(self):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": '# codex in a comment\n[adapters.codex]\nimplementation = "agent_pm.session.adapters.codex:CodexAdapter"  # codex\nkey = "x # codex"\n'
            }
        )
        self.assertEqual(
            ownership.runtime_names(root),
            ["delegate/src/agent_pm/delegate/config.toml:4: runtime name codex"],
        )

    def test_toml_implementation_line_is_exempt_only_as_module_path_class(self):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": 'implementation = "codex"\nimplementation = "my.codex:Codex"\n'
            }
        )
        self.assertEqual(
            ownership.runtime_names(root),
            ["delegate/src/agent_pm/delegate/config.toml:1: runtime name codex"],
        )

    def test_toml_runtime_line_is_exempt_under_any_table(self):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": 'runtime = "codex"\n[roles.x]\nruntime = "codex"\n[roles.y]\nruntime = "codex"  # note\n'
            }
        )
        self.assertEqual(ownership.runtime_names(root), [])

    def test_toml_name_in_a_key_value_elsewhere(self):
        root = self.tree(
            {
                "delegate/src/agent_pm/delegate/config.toml": (
                    '[adapters.codex]\nimplementation = "agent_pm.session.adapters.codex:CodexAdapter"\n[roles.pm]\nmodel = "codex"\nruntime = ["codex"]\nruntime = "codex\nruntime = "codex" x\n'
                )
            }
        )
        self.assertEqual(
            ownership.runtime_names(root),
            [
                f"delegate/src/agent_pm/delegate/config.toml:{n}: runtime name codex"
                for n in (4, 5, 6, 7)
            ],
        )


class AdaptersKey(Sample):
    def test_the_literal_key_only_in_the_session_config_parser(self):
        sample = 'cfg = {}\nvalue = cfg["adapters"]\n'
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/composer/x.py": sample,
                f"session/{PKG}/session/adapters/x.py": sample,
                CONFIG_FILE: sample,
                ADAPTERS_HOME: sample,
            }
        )
        self.assertEqual(
            ownership.adapters_key(root),
            [
                f'delegate/{PKG}/delegate/composer/x.py:2: literal key "adapters"',
                f'{CONFIG_FILE}:2: literal key "adapters"',
                f'session/{PKG}/session/adapters/x.py:2: literal key "adapters"',
            ],
        )


class TmuxCalls(Sample):
    def test_the_tmux_constant_only_in_the_host_package(self):
        sample = 'import subprocess\nsubprocess.run(["tmux", "kill-server"])\n'
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": sample,
                f"session/{PKG}/session/hosts/tmux/commands.py": sample,
                f"session/{PKG}/session/hosts/tmux/model/validation.py": sample,
                f"session/{PKG}/session/hosts/tmux_external/x.py": sample,
                f"session/{PKG}/session/modes/x.py": sample,
                f"delegate/{PKG}/delegate/composer/y.py": 'TOOL = "tmux"\n',
                "session/src/hosts/tmux/tmux_host.py": sample,
                "session/tests/z.py": sample,
            }
        )
        self.assertEqual(
            ownership.tmux_calls(root),
            sorted(
                [
                    f'delegate/{PKG}/delegate/composer/y.py:1: str constant "tmux"',
                    'session/src/hosts/tmux/tmux_host.py:2: str constant "tmux"',
                    f"session/{PKG}/session/modes/x.py:2: tmux outside session/hosts/tmux/",
                    f"session/{PKG}/session/hosts/tmux_external/x.py:2: tmux outside session/hosts/tmux/",
                ]
            ),
        )

    def test_tmux_inside_a_longer_string_is_rejected(self):
        root = self.tree(
            {
                f"session/{PKG}/session/modes/x.py": (
                    'HINT = "tmux attach -t =s"\nPATH = "/usr/bin/tmux"\n'
                )
            }
        )
        self.assertEqual(
            ownership.tmux_calls(root),
            [
                f"session/{PKG}/session/modes/x.py:{line}: tmux outside session/hosts/tmux/"
                for line in (1, 2)
            ],
        )

    def test_identifiers_imports_and_f_strings_cannot_hide_tmux(self):
        source = (
            "from agent_pm.session.hosts.tmux import TmuxHost\n"
            "import agent_pm.session.hosts.tmux as transport\n"
            "def _tmux(tmux_path):\n"
            "    return f'{transport.TmuxHost}'\n"
            "class TmuxProcess: pass\n"
        )
        base = f"session/{PKG}/session/cli.py"
        self.assertEqual(
            ownership.tmux_calls(self.tree({base: source})),
            [f"{base}:{line}: tmux outside session/hosts/tmux/" for line in range(1, 6)],
        )

    def test_packaged_configuration_and_test_fixtures_can_name_the_host(self):
        root = self.tree(
            {
                f"session/{PKG}/session/config.toml": '[hosts]\ndefault = "tmux"\n',
                "session/tests/unit/sample_test.py": 'HOST = "tmux"\n',
            }
        )
        self.assertEqual(ownership.tmux_calls(root), [])


class SessionDisplay(Sample):
    def test_display_names_are_rejected_in_each_scoped_source(self):
        for base in (
            f"session/{PKG}/session/cli.py",
            f"session/{PKG}/session/modes/sample.py",
            f"session/{PKG}/session/hosts/other/sample.py",
            f"utils/{PKG}/utils/sample.py",
            CONFIG_FILE,
        ):
            for source in (
                "def f(pane): return pane\n",
                "def f(_pane): return _pane\n",
                "def f(pane_id): return pane_id\n",
                "def opener(): pass\n",
                "class split_from: pass\n",
                "x = value.opener\n",
                'x = f"{value.pane}"\n',
                "from transport import pane as screen\n",
                'x = "--split"\n',
                'x = "--split-from"\n',
                'x = "@opener"\n',
                'x = "@record"\n',
                'x = "@pane"\n',
            ):
                with self.subTest(base=base, source=source):
                    self.assertTrue(ownership.session_display(self.tree({base: source})))

    def test_host_implementation_and_other_packages_are_out_of_scope(self):
        source = 'def f(pane, opener, split_from): return "@record"\n'
        root = self.tree(
            {
                f"session/{PKG}/session/hosts/tmux/transport.py": source,
                f"delegate/{PKG}/delegate/engine/other.py": source,
                f"linear-orchestrator/{OPKG}/engine/sample.py": source,
                "session/tests/unit/sample_test.py": source,
            }
        )
        self.assertEqual(ownership.session_display(root), [])

    def test_string_split_decorators_and_unrelated_words_are_clean(self):
        source = (
            '@record\ndef f(display): return display.split("/")\n'
            'screenplay = "panel"\nreopener = "reopener"\n'
        )
        root = self.tree({f"session/{PKG}/session/engine/sample.py": source})
        self.assertEqual(ownership.session_display(root), [])


class LegacyHostNames(Sample):
    def baseline(self):
        files = {}
        for (path, name), (count, _) in LEGACY_HOST_NAMES.items():
            files[path] = files.get(path, "") + f"{name} = None\n" * count
        return files

    def test_only_the_exact_existing_names_and_counts_are_allowed(self):
        root = self.tree(self.baseline())
        self.assertEqual(ownership.legacy_host_names(root), [])
        self.assertEqual(ownership.tmux_calls(root), [])
        self.assertEqual(ownership.session_display(root), [])

    def test_an_extra_use_on_the_same_line_is_rejected(self):
        files = self.baseline()
        path = f"{SESSION_SRC}engine/forwarding.py"
        files[path] += "opener = opener\n"
        self.assertEqual(
            ownership.legacy_host_names(self.tree(files)),
            [f"{path}: opener: expected 4 legacy occurrences, found 6 (TASK-375)"],
        )

    def test_a_removed_name_or_file_is_stale(self):
        files = self.baseline()
        path = f"{SESSION_SRC}engine/forwarding.py"
        files[path] = files[path].replace("\nopener = None", "\n").replace("\n\n", "\n")
        self.assertEqual(
            ownership.legacy_host_names(self.tree(files)),
            [f"{path}: opener: expected 4 legacy occurrences, found 0 (TASK-375)"],
        )
        del files[path]
        self.assertEqual(
            len(ownership.legacy_host_names(self.tree(files))),
            len([key for key in LEGACY_HOST_NAMES if key[0] == path]),
        )

    def test_new_names_and_other_modules_have_no_exemption(self):
        files = self.baseline()
        path = f"{SESSION_SRC}engine/forwarding.py"
        files[path] += "pane = None\nnew_tmux = None\n"
        self.assertTrue(ownership.session_display(self.tree(files)))
        self.assertTrue(ownership.tmux_calls(self.tree(files)))
        files[f"{SESSION_SRC}engine/other.py"] = "opener = None\nTmuxHost = None\n"
        self.assertEqual(len(ownership.session_display(self.tree(files))), 2)
        self.assertEqual(len(ownership.tmux_calls(self.tree(files))), 2)


class StatusLogFormat(Sample):
    CLOCK = 'CLOCK = "%H:%M:%S"\n'

    def test_the_real_tree_is_clean(self):
        self.assertEqual(ownership.status_log_format(REPO), [])

    def test_the_clock_in_another_projects_module_or_an_f_string(self):
        root = self.tree(
            {
                f"delegate/{PKG}/delegate/x.py": self.CLOCK,
                f"manager/{PKG}/manager/y.py": 'import time\n\nline = f"{time.strftime(\'%H:%M:%S\')} {name}"\nhook = f"$(date +%H:%M:%S) {name} dead"\n',
                f"linear-orchestrator/{OPKG}/z.py": 'import time\nat = time.strftime("%H:%M:%S")\n',
            }
        )
        outside = "status log line format outside session/engine/status_log.py"
        self.assertEqual(
            ownership.status_log_format(root),
            sorted(
                [
                    f"delegate/{PKG}/delegate/x.py:1: {outside}",
                    f"manager/{PKG}/manager/y.py:3: {outside}",
                    f"manager/{PKG}/manager/y.py:4: {outside}",
                    f"linear-orchestrator/{OPKG}/z.py:2: {outside}",
                ]
            ),
        )

    def test_only_status_log_owns_the_clock(self):
        root = self.tree(
            {
                f"session/{PKG}/session/engine/status_log.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/tmux_host.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/lifecycle.py": self.CLOCK,
                f"session/{PKG}/session/hosts/tmux/model/clock.py": self.CLOCK,
                "session/tests/x_test.py": self.CLOCK,
                "tests/y_test.py": self.CLOCK,
            }
        )
        outside = "status log line format outside session/engine/status_log.py"
        self.assertEqual(
            ownership.status_log_format(root),
            [
                f"session/{PKG}/session/hosts/tmux/lifecycle.py:1: {outside}",
                f"session/{PKG}/session/hosts/tmux/model/clock.py:1: {outside}",
                f"session/{PKG}/session/hosts/tmux/tmux_host.py:1: {outside}",
            ],
        )

    def test_a_similarly_named_subsystem_cannot_own_the_clock(self):
        root = self.tree({f"session/{PKG}/session/hosts/tmux_external/x.py": self.CLOCK})
        self.assertEqual(
            ownership.status_log_format(root),
            [
                f"session/{PKG}/session/hosts/tmux_external/x.py:1: status log line format outside session/engine/status_log.py"
            ],
        )


class ManagerBoundaries(Sample):
    def test_only_orphan_listing_is_hardcoded(self):
        base = "manager/src/agent_pm/manager/engine/crew.py"
        root = self.tree(
            {
                base: 'LS = ("agent-pm-session", "ls", "--here", "--records")\nBAD = ["agent-pm-session", "status"]\n'
            }
        )
        self.assertEqual(
            commands.manager_commands(root), [f"{base}:2: hardcoded layer command agent-pm-session"]
        )

    def test_display_is_opaque(self):
        base = "manager/src/agent_pm/manager/engine/crew.py"
        root = self.tree({base: 'def f(pane): return "@opener"\n'})
        self.assertEqual(len(ownership.manager_display(root)), 2)
        clean = self.tree({base: 'def f(display): return display.split(":")\n'})
        self.assertEqual(ownership.manager_display(clean), [])

    def test_exports_require_all_and_reject_reexported_functions(self):
        base = "manager/src/agent_pm/manager/"
        root = self.tree(
            {
                base
                + "__init__.py": 'from agent_pm.manager.engine.crew import f\n__all__ = ["f"]\n',
                base + "engine/crew.py": "def f(): pass\n",
            }
        )
        self.assertEqual(
            exports.manager_exports(root), [f"{base}__init__.py:2: exports function f"]
        )
        missing = self.tree({base + "__init__.py": ""})
        self.assertEqual(exports.manager_exports(missing), [f"{base}__init__.py: missing __all__"])
        clean = self.tree(
            {
                base + "__init__.py": 'class Crew: pass\n__all__ = ["Crew"]\n',
                "utils/src/agent_pm/utils/__init__.py": 'def f(): pass\n__all__ = ["f"]\n',
            }
        )
        self.assertEqual(exports.manager_exports(clean), [])

    def test_function_aliases_lambdas_stdlib_and_modules_are_not_exports(self):
        base = "manager/src/agent_pm/manager/__init__.py"
        root = self.tree(
            {
                base: 'from os import getcwd as CWD\nimport os as OS\ndef f(): pass\ng = f\nLAMBDA = lambda: 0\n__all__ = ["CWD", "OS", "g", "LAMBDA"]\n'
            }
        )
        self.assertEqual(
            exports.manager_exports(root),
            [
                f"{base}:6: exports function CWD",
                f"{base}:6: exports function LAMBDA",
                f"{base}:6: exports function g",
                f"{base}:6: exports nonconstant OS",
            ],
        )
        clean = self.tree(
            {base: 'from pathlib import Path\nVALUE = "one"\n__all__ = ["Path", "VALUE"]\n'}
        )
        self.assertEqual(exports.manager_exports(clean), [])

    def test_exports_require_a_literal_sequence_of_names(self):
        base = "manager/src/agent_pm/manager/__init__.py"
        for source in ("__all__ = discover()\n", "__all__ = [1]\n"):
            self.assertEqual(
                exports.manager_exports(self.tree({base: source})),
                [f"{base}:1: __all__ must be a literal sequence of names"],
            )

    def test_crew_never_reads_an_inherited_sid(self):
        base = "manager/src/agent_pm/manager/engine/crew.py"
        for source in (
            'import os\nx = os.getenv("AGENT_PM_SID")\n',
            'import os\nx = os.environ.get("AGENT_PM_SID")\n',
            'import os\nx = os.environ["AGENT_PM_SID"]\n',
        ):
            self.assertEqual(
                ownership.manager_sid(self.tree({base: source})),
                [f"{base}:2: reads inherited AGENT_PM_SID"],
            )
        self.assertEqual(
            ownership.manager_sid(self.tree({base: 'env = {"AGENT_PM_SID": sid}\n'})), []
        )

    def test_roster_has_no_session_liveness_callback(self):
        base = "manager/src/agent_pm/manager/engine/roster.py"
        self.assertEqual(
            ownership.manager_lease(
                self.tree(
                    {base: 'class Roster:\n    def probe(self): return self._alive("manager")\n'}
                )
            ),
            [f"{base}:2: lease liveness callback"],
        )
