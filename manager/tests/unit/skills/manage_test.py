"""manager/skills/manage/SKILL.md (/agent-pm:manage): it leaves commands to /agent-pm:tmux, every
section it cites there exists, it writes no retired command, and its rules for closing sessions,
Codex issues, decision markers and the one commit exception agree.
"""

import os

from manager.tests.case import Case
from manager.tests.skills.manage import rule
from manager.tests.skills.tmux import BIN, body, examples, references, sections

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

    def test_a_roster_entry_closes_through_crew(self):
        close = rule("AppleScript")
        self.assertIn("`agent-pm-crew stop <name>`", close)
        self.assertRegex(close, r"tmux only for sessions outside the Roster")

    def test_a_codex_issue_worker_starts_by_name_with_its_prompt_and_closes_at_in_review(self):
        codex = rule("An issue for Codex")
        self.assertRegex(
            codex,
            r"`agent-pm-crew run <name> -- agent-pm-session start <name> --client codex "
            r"--cwd <work dir> --prompt '[^`]+'",
        )
        self.assertRegex(codex, r"[Cc]lose it .*In Review")

    def test_one_decision_marker_and_one_commit_exception(self):
        self.assertIn("`(the user, <YYYY-MM-DD>)`", rule("Record each decision"))
        self.assertNotIn("<the user's name>", body("manage"))
        self.assertRegex(rule("Clear small"), r"commit only, no push")
        self.assertIn(".gitignore", rule("Commit or push only when asked"))
