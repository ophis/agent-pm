import os

from orchestrator_test_helpers.unit.board_ids import HEADER, STATES, TEAM, role
from orchestrator_test_helpers.unit.config.data import (
    BASE,
    CORE_LOCAL,
    P1,
    P2,
    PIPELINE,
    ROLES,
    fields,
    old_key,
    write,
)
from orchestrator_test_helpers.unit.config.local_fixtures import Local as LocalBase


class Local1(LocalBase):
    def test_local_on_top(self):
        self.local(
            HEADER
            + 'human_members = ["b@x.com"]\n'
            + role("researcher", "max_runs = 2")
            + role("pm")
            + f'[project_repos]\n"{P2}" = "ophis/y"\n'
        )
        cfg = self.load(
            'human_members = ["a@x.com", "c@x.com"]\n[roles.researcher]\nnext = "pm"\nmax_runs = 3\n'
            f'[project_repos]\n"{P1}" = "ophis/x"\n[core.roles.pm]\ntier = 1\n'
        )
        self.assertEqual((cfg.team, cfg.states, cfg.human_members), (TEAM, STATES, ("b@x.com",)))
        self.assertEqual(
            fields(cfg.roles),
            {
                "researcher": (
                    "researcher@agents.test",
                    "linear-api-key-researcher",
                    ROLES["researcher"][2],
                    2,
                    "pm",
                ),
                "pm": ("pm@agents.test", "linear-api-key-pm", ROLES["pm"][2], 1, None),
            },
        )
        self.assertEqual(cfg.project_repos, {P1: "ophis/x", P2: "ophis/y"})
        self.assertEqual(cfg.roles["pm"].run.tier, 1)

    def test_local_alone_and_no_local(self):
        self.local(PIPELINE)
        self.assertEqual(self.load("").roles, self.load(PIPELINE).roles)
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.assertEqual(self.load(PIPELINE).team, TEAM)

    def test_an_empty_local_role_table_adds_no_role(self):
        self.local(PIPELINE)
        want = set(self.load("").roles)
        self.local(PIPELINE + "[roles.ghost]\n")
        self.assertEqual(set(self.load("").roles), want)

    def test_old_task_keys_in_the_local_overlay_stop_runnable(self):
        self.local('[core.roles.researcher.tasks.deep-research]\ngate = "x"\n')
        self.fails(old_key("tasks"), PIPELINE)

    def test_a_bad_local_value_is_rejected(self):
        self.local(f'team = "{TEAM}"\nstates = {{ done = "Done" }}\n')
        self.fails(
            "linear-orchestrator/config.toml: states.done must be a Linear workflow state id (UUID): 'Done'",
            BASE,
        )
