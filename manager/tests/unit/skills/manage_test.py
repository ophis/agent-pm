"""manager/skills/manage/SKILL.md (/agent-pm:manage): it leaves commands to /agent-pm:tmux, every
section it cites there exists, it writes no retired command, and its rules for closing sessions,
Codex issues, decision markers, board proposals and the one commit exception agree.
"""

import os
import re

from manager.tests.case import REPO, Case
from manager.tests.skills.manage import rule
from manager.tests.skills.tmux import (
    BIN,
    body,
    examples,
    paths,
    readme_sections,
    references,
    section,
    sections,
)

RETIRED = (
    "crew.py",
    "drive.py",
    "tmux_host.py",
    "router.py",
    "--runner",
    "attach --resume",
    "ctmux's",
    "${CLAUDE_SKILL_DIR}/../../src",
    "tmux ls",
    "agent-pm-<role>-<ID>",
)


class Manage(Case):
    def test_each_cited_tmux_section_exists(self):
        cited = references("manage")
        self.assertGreaterEqual(set(cited), {"Start", "Events", "Launch", "Direct", "Recover"})
        for name in cited:
            self.assertIn(name, sections("tmux"))

    def test_commands_are_bin_commands_and_no_retired_name(self):
        for command in examples(body("manage")):
            self.assertIn(command[0], sorted(os.listdir(BIN)), command)
        for word in RETIRED:
            self.assertNotIn(word, body("manage"))
        self.assertNotIn("## Crew commands", body("manage"))

    def test_events_arrive_with_their_content_and_need_no_pane_read(self):
        monitor = rule("`monitor:`")
        self.assertNotIn("confirm by reading the pane", monitor)
        self.assertIn("`/agent-pm:tmux` › Events", monitor)

    def test_a_roster_entry_closes_through_crew(self):
        close = rule("AppleScript")
        self.assertIn("`agent-pm-crew close <name>`", close)
        self.assertRegex(close, r"tmux only for sessions outside the Roster")

    def test_a_codex_issue_worker_starts_by_name_with_its_prompt_and_closes_on_merge(self):
        codex = rule("An issue for Codex")
        self.assertRegex(
            codex,
            r"`agent-pm-crew worker <name> --client codex "
            r"--cwd <work dir>/agent --prompt 'Read <work dir>/agent/prompt\.md[^`]+'",
        )
        self.assertRegex(codex, r"[Cc]lose it .*PR merges")

    def test_one_decision_marker_and_one_commit_exception(self):
        self.assertIn("`(the user, <YYYY-MM-DD>)`", rule("Record each decision"))
        self.assertNotIn("<the user's name>", body("manage"))
        self.assertRegex(rule("Clear small"), r"commit only, no push")
        self.assertIn(".gitignore", rule("Commit or push only when asked"))

    def test_approved_board_proposals_run_after_a_state_reread(self):
        board = section("manage", "The task board")
        proposals = rule("**Board proposals**")
        self.assertIn(proposals, board)
        for literal in (
            "once the user approves",
            "without asking again",
            "Before a cancel or split residue, re-read the old issue's state",
            "no longer Backlog, Todo or In Review → cross-link instead and tell the user",
            "**Split residue** first",
            "in the old issue's state (In Review → Todo)",
            "its project, assignee and labels",
            "`related` to both the proposing issue and the old one",
            "re-point each issue it blocks in the old issue's team to be blocked by the proposing issue "
            "(and the residue issue, if any)",
            "comment on each blocked issue in another team",
            "Canceled as a Duplicate of the proposing issue",
            "comment the reason and the proposing issue's link",
            "**Cross-link**: a `related` link and a comment with the reason on each",
        ):
            self.assertIn(literal, proposals, literal)

    def test_the_files_and_readme_sections_it_names_exist(self):
        self.assertTrue(paths("manage"))
        for path in paths("manage"):
            self.assertTrue(os.path.isfile(path), path)
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        cited = readme_sections("manage")
        self.assertIn(["Using the board", "Finish"], cited)
        for top, *inner in cited:
            part = re.search(rf"^## {re.escape(top)}\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
            self.assertIsNotNone(part, top)
            for name in inner:
                self.assertRegex(part[1], rf"(?m)^#+ {re.escape(name)}$|\*\*{re.escape(name)}:\*\*")

    def test_a_plugin_update_names_each_clients_command(self):
        update = rule("bumps the agent-pm plugin version")
        self.assertIn("`claude plugin update agent-pm@agent-pm`", update)
        self.assertIn("`codex plugin marketplace upgrade agent-pm`", update)
        self.assertIn("how a client without this plugin's `bin/` on its PATH", body("manage"))
