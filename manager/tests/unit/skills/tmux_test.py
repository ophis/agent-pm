"""manager/skills/tmux/SKILL.md (/agent-pm:tmux): every layer command it writes is one of the four
bin/ commands; it launches all three kinds through agent-pm-crew run, handles every event word,
answers dialogs with keys and recovers one entry at a time, through no retired path.
"""

import os
import re
import tomllib

from manager.tests.case import REPO, Case
from manager.tests.skills.tmux import (
    BIN,
    SKILLS,
    body,
    examples,
    flags,
    paths,
    rules,
    section,
    sections,
    spans,
)

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

    def test_pre_approved_rules_are_read_only(self):
        for rule in rules("tmux"):
            self.assertNotRegex(
                rule, r"\b(run(?! --list)|start|send|stop|forget|close|resume|restart|tile)\b"
            )

    def test_a_client_without_bin_on_its_path_reaches_this_plugins_bin(self):
        fallback = "${CLAUDE_SKILL_DIR}/../../../bin/<command>"
        self.assertIn(fallback, body("tmux"))
        own = os.path.join(SKILLS, "tmux", "../../../bin")
        self.assertEqual(os.path.realpath(own), os.path.realpath(BIN))

    def test_crew_launches_take_no_session_id_and_direct_detaches_name_their_events(self):
        for commands in spans(body("tmux")):
            outer = commands[0][:2] == ["agent-pm-crew", "run"]
            for command in commands[1:] if outer else commands:
                if outer:
                    self.assertFalse({"--sid", "--resume"} & set(flags(command)), command)
                elif command[:2] == ["agent-pm-delegate", "run"] and "--detach" in flags(command):
                    self.assertIn("--events", flags(command))

    def test_no_retired_path_flag_or_manual_repair(self):
        for word in (*RETIRED, *REPAIRS):
            self.assertNotIn(word, body("tmux"))
        self.assertNotIn("--split", body("tmux"))


class Names(Case):
    def test_the_files_it_names_exist_and_ctmux_runs(self):
        named = paths("tmux")
        self.assertIn(os.path.join(SKILLS, "tmux", "scripts", "ctmux"), named)
        for path in named:
            self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(os.access(os.path.join(SKILLS, "tmux", "scripts", "ctmux"), os.X_OK))

    def test_the_grid_keys_it_names_are_the_tmux_hosts(self):
        [config] = {p for p in paths("tmux") if p.endswith("config.toml")}
        with open(config, "rb") as f:
            tmux = tomllib.load(f)["hosts"]["tmux"]
        for key in ("workers_per_column", "grid_retile"):
            self.assertIn(key, tmux)
            self.assertIn(f"`{key}`", body("tmux"))

    def test_only_the_commander_has_it_as_the_session_config_turns_it_off(self):
        self.assertIn("so a worker or role run starts no sessions of its own", body("tmux"))
        self.assertIn("refuse inside them (root README › Install)", body("tmux"))
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            self.assertIn("`AGENT_PM_AGENT_SESSION=1`", f.read())
        [config] = {p for p in paths("tmux") if p.endswith("config.toml")}
        with open(config, "rb") as f:
            adapters = tomllib.load(f)["adapters"]
        for client in ("claude", "codex"):
            self.assertTrue(
                any("agent-pm@agent-pm" in flag for flag in adapters[client]["flags"]), client
            )

    def test_the_upgrading_row_carries_the_one_home_of_each_plugin_off_entry(self):
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
            [row] = [line for line in f if line.startswith("| a local `flags` list")]
        with open(os.path.join(REPO, "session/src/agent_pm/session/config.toml"), "rb") as f:
            adapters = tomllib.load(f)["adapters"]
        with open(os.path.join(REPO, "delegate/src/agent_pm/delegate/config.toml"), "rb") as f:
            policy = tomllib.load(f)["policy"]
        for client in ("claude", "codex"):
            flags = adapters[client]["flags"]
            [at] = [i for i, flag in enumerate(flags) if "agent-pm@agent-pm" in flag]
            entry = f'"{flags[at - 1]}", ' + (
                f"'{flags[at]}'" if '"' in flags[at] else f'"{flags[at]}"'
            )
            with self.subTest(client=client):
                self.assertIn(f"`[session.adapters.{client}]`", row)
                self.assertIn(entry, row)
                self.assertFalse([f for f in policy[client]["flags"] if "agent-pm@agent-pm" in f])
        self.assertNotIn("delegate.policy", row)

    def test_the_skills_it_names_are_this_plugins(self):
        for skill in re.findall(r"`/agent-pm:([a-z-]+)`", body("tmux") + body("manage")):
            homes = [os.path.join(REPO, d, "skills", skill) for d in ("manager", "delegate")]
            self.assertTrue(any(os.path.isfile(os.path.join(h, "SKILL.md")) for h in homes), skill)


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

    def test_start_attaches_reads_its_output_and_arms_one_listener(self):
        start = section("tmux", "Start")
        for phrase in (
            "`ctmux <name>`",
            "agent-pm-crew hold --manager",
            "TaskStop",
            "`agent-pm-crew attach`",
            "lease",
            "`name kind sid state note cwd attach`",
            "backlog",
            "`resume: agent-pm-crew resume … <name>`",
            "`agent-pm-crew next-event … --content --follow`",
            "`next-event:`",
            "`--content`",
            "event line, then its content",
            "`monitor expired:`",
            "run_in_background",
            "--after <line>",
        ):
            self.assertIn(phrase, start)

    def test_direct_names_every_daily_operation(self):
        direct = section("tmux", "Direct")
        for phrase in (
            "`agent-pm-session send <name> '<text>'`",
            "`agent-pm-session read <name> [--lines <n>]`",
            "`agent-pm-session reply <name>`",
            "`agent-pm-session restart <name>`",
            "`agent-pm-crew note <name> '<text>'`",
            "`''` clears",
            "`agent-pm-crew status [<name>…]`",
            "`agent-pm-crew report`",
            "PR's URL",
            "`agent-pm-crew stop <name>`",
            "`agent-pm-crew stop-all [--dry-run]`",
            "`agent-pm-crew forget <name>`",
            "`--force`",
            "`agent-pm-crew release`",
        ):
            self.assertIn(phrase, direct)

    def test_color_is_left_to_the_session_but_a_start_dialog(self):
        (worker,) = [
            line for line in section("tmux", "Launch").splitlines() if "**Worker**" in line
        ]
        self.assertEqual(body("tmux").count("/color"), 1)
        self.assertIn("/color", worker)
        self.assertIn('"color not set"', worker)
        self.assertNotIn("Color", section("tmux", "Direct"))

    def test_events_handles_every_word(self):
        events = section("tmux", "Events")
        for word in ("done", "blocked", "dead", "exited", "outcome", "Monitor expired"):
            self.assertRegex(events, rf"\*\*{word}\b[^*]*\*\*", word)

    def test_an_events_content_is_its_read_and_only_numbered_lines_are_events(self):
        events = section("tmux", "Events")
        self.assertNotIn("confirm each by reading", events)
        for phrase in ("content", "`content unavailable", "untrusted", "continue"):
            self.assertIn(phrase, events)
        self.assertIn("agent-pm-crew report", rules("tmux"))
        self.assertIn("agent-pm-crew report *", rules("tmux"))

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
