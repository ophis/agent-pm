"""manager/skills/tmux/SKILL.md (/agent-pm:tmux): every layer command it writes is one of the four
bin/ commands; it launches all three kinds through agent-pm-crew run, handles every event word,
answers dialogs with keys and recovers one entry at a time, through no retired path.
"""

import os
import re

from manager.tests.case import Case
from manager.tests.skills.tmux import BIN, body, examples, rules, section, sections

RETIRED = (
    "crew.py",
    "drive.py",
    "tmux_host.py",
    "router.py",
    "--runner",
    "attach --resume",
    "${CLAUDE_SKILL_DIR}/../../src",
    "manager/src",
)
REPAIRS = ("rename-session", "/rename", "@grid-busy", "@opener", "@pane", "send-keys", "set-option")


class Commands(Case):
    def test_every_example_is_one_of_the_four_bin_commands(self):
        commands = examples(body("tmux"))
        self.assertGreater(len(commands), 20)
        for command in commands:
            self.assertIn(command[0], sorted(os.listdir(BIN)), command)

    def test_pre_approved_rules_are_read_only_bin_commands(self):
        for rule in rules("tmux"):
            self.assertIn(rule.split()[0], sorted(os.listdir(BIN)), rule)
            self.assertNotRegex(
                rule, r"\b(run(?! --list)|start|send|stop|forget|close|resume|restart|tile)\b"
            )

    def test_no_retired_path_flag_or_manual_repair(self):
        for word in (*RETIRED, *REPAIRS):
            self.assertNotIn(word, body("tmux"))
        self.assertNotIn("--split", body("tmux"))


class Flows(Case):
    def test_each_kind_launches_through_crew_run(self):
        launch = section("tmux", "Launch")
        for inner in (
            r"agent-pm-session start <name> --client <client>",
            r"agent-pm-delegate run --role <role> .*--mode tui --detach",
            r"agent-pm-linear router --issue <ID> --tui",
        ):
            self.assertRegex(launch, rf"`agent-pm-crew run <[A-Za-z]+> (\[[^]]+\] )?-- {inner}")
        self.assertIn("--mode headless", launch)
        self.assertIn("--display D", launch)
        self.assertIn("agent-pm-session --help", launch)

    def test_events_handles_every_word(self):
        events = section("tmux", "Events")
        for word in ("done", "blocked", "dead", "exited", "outcome", "Monitor expired"):
            self.assertRegex(events, rf"\*\*{word}\b[^*]*\*\*", word)

    def test_a_dialog_is_read_then_answered_with_keys_never_with_text(self):
        events = section("tmux", "Events")
        blocked = re.search(r"\*\*blocked\*\*(.*?)(?=^- \*\*)", events, re.M | re.S)[1]
        self.assertIn("agent-pm-session read <name>", blocked)
        for keys in ("--key Down Enter", "--key 2", "--key Escape", "--key C-c"):
            self.assertIn(keys, blocked)
        self.assertRegex(blocked, r"send no text")
        self.assertRegex(blocked, r"read (it )?again")
        self.assertRegex(blocked, r"not by the last event")
        self.assertIn("ask the user", blocked)

    def test_recovery_is_per_entry_and_a_waiting_role_is_answered_by_its_sid(self):
        recover = section("tmux", "Recover")
        self.assertIn("agent-pm-crew resume <name>", recover)
        self.assertRegex(recover, r"agent-pm-delegate run .*--resume --sid <sid> --input")
        self.assertRegex(recover, r"no resume-all")

    def test_grid_names_its_keys_and_the_public_retile(self):
        grid = section("tmux", "Grid")
        for word in ("workers_per_column", "grid_retile", "agent-pm-session tile <window>"):
            self.assertIn(word, grid)

    def test_sections(self):
        self.assertEqual(
            sections("tmux"), ["Start", "Events", "Launch", "Grid", "Direct", "Recover"]
        )
