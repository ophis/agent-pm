import tomllib

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.board_ids import HEADER, TEAM, role
from orchestrator_test_helpers.unit.config.data import DEPLOYMENT, ROLES, fields, missing
from orchestrator_test_helpers.unit.config.real_config_fixtures import RealConfig as RealConfigBase
from orchestrator_test_helpers.unit.config_support import GATE


class RealConfig1(RealConfigBase):
    def test_committed_config_holds_no_deployment_values(self):
        with open(config.CONFIG, "rb") as f:
            committed = tomllib.load(f)
        self.assertEqual(
            {k: committed[k] for k in DEPLOYMENT},
            {
                **dict.fromkeys(("team", "harness_key", "task_label_group"), ""),
                "states": dict.fromkeys(config.STATES, ""),
                "human_members": [],
                **dict.fromkeys(("task_labels", "project_repos", "local_clones"), {}),
                "work_dir": "~/.agent-pm",
            },
        )
        self.assertEqual(
            {r: {k: p[k] for k in ("account", "key")} for r, p in committed["roles"].items()},
            dict.fromkeys(("researcher", "pm", "engineer"), {"account": "", "key": ""}),
        )

    def test_without_local_names_what_is_missing(self):
        with self.assertRaises(config.ConfigError) as cm:
            self.load()
        self.assertEqual(
            str(cm.exception),
            "linear-orchestrator/config.toml: "
            + missing(
                "team, harness_key, task_label_group, states.todo, states.in_progress, states.in_review, states.handoff, "
                "states.done, states.canceled, roles.researcher.account, roles.pm.account, roles.engineer.account, "
                "roles.researcher.key, roles.pm.key, roles.engineer.key"
            ),
        )

    def test_real_config(self):
        cfg = self.load(HEADER + role("researcher") + role("pm") + role("engineer"))
        self.assertEqual(
            {r: (*v, cfg.roles[r].require_instructions) for r, v in fields(cfg.roles).items()},
            {
                "researcher": (*ROLES["researcher"][:3], 3, "pm", True),
                "pm": (*ROLES["pm"][:3], 3, "engineer", False),
                "engineer": (*ROLES["engineer"][:3], 3, None, True),
            },
        )
        self.assertEqual(list(cfg.roles), ["researcher", "pm", "engineer"])
        self.assertEqual(
            cfg.docs,
            config.Docs(
                "ophis/private_docs", "main", {"researcher": "Research/", "pm": "Product Design/"}
            ),
        )
        self.assertEqual(
            (cfg.team, cfg.task_labels, cfg.project_repos, cfg.local_clones), (TEAM, {}, {}, {})
        )
        self.assertEqual(cfg.stages, {"researcher": 0, "pm": 1, "engineer": 2})
        self.assertEqual(
            {r: (x.run.gate, x.run.task, x.run.language) for r, x in cfg.roles.items()},
            {
                "researcher": (GATE, "", "Chinese"),
                "pm": ("", "", "Chinese"),
                "engineer": ("", "", "Chinese"),
            },
        )

    def test_local_lines_uncommented_set_every_required_key(self):
        with open(config.CONFIG) as f:
            local = "".join(
                line.removeprefix("# local: ") for line in f if line.startswith("# local: ")
            )
        with self.assertRaises(config.ConfigError) as cm:
            self.load(local)
        zero = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(
            str(cm.exception),
            f"linear-orchestrator/config.toml: project_repos.{zero} must map a Linear project id "
            "(UUID) to <owner>/<name>: '<owner>/<name>'",
        )
