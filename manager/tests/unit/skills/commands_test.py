"""manager/skills/manage/SKILL.md (/agent-pm:manage): every layer command it writes is one of the four
bin/ commands; it launches all three kinds through Crew, handles every event word,
answers dialogs with keys and recovers one entry at a time, through no retired path.
"""

import os
import re
import tomllib

from manager.tests.case import REPO, Case
from manager.tests.skills.commands import (
    BIN,
    SKILLS,
    body,
    example,
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
    def test_worker_examples_select_generic_or_codex_launch(self):
        generic = example("manage", "agent-pm-crew worker", "--client <client>")
        codex = example("manage", "agent-pm-crew worker", "--client codex")
        self.assertIn("[--prompt '<text>']", generic)
        self.assertIn("--cwd <work dir>/agent", codex)

    def test_every_example_is_one_of_the_four_bin_commands(self):
        commands = examples(body("manage"))
        self.assertGreater(len(commands), 20)
        for command in commands:
            self.assertIn(command[0], sorted(os.listdir(BIN)), command)

    def test_pre_approved_rules_are_read_only(self):
        for rule in rules("manage"):
            self.assertNotRegex(
                rule, r"\b(run(?! --list)|start|send|stop|forget|close|resume|restart|tile)\b"
            )

    def test_a_client_without_bin_on_its_path_reaches_this_plugins_bin(self):
        fallback = "${CLAUDE_SKILL_DIR}/../../../bin/<command>"
        self.assertIn(fallback, body("manage"))
        own = os.path.join(SKILLS, "manage", "../../../bin")
        self.assertEqual(os.path.realpath(own), os.path.realpath(BIN))

    def test_crew_launches_take_no_session_id(self):
        for commands in spans(body("manage")):
            outer = (
                len(commands[0]) > 1
                and commands[0][0] == "agent-pm-crew"
                and commands[0][1] in {"worker", "issue", "role"}
            )
            for command in commands[1:] if outer else commands:
                if outer:
                    self.assertFalse({"--sid", "--resume"} & set(flags(command)), command)

    def test_content_uses_crew_without_unused_layer_permissions(self):
        for command in ("agent-pm-session", "agent-pm-delegate", "agent-pm-linear"):
            self.assertNotIn(f"{command} content *", rules("manage"))
        self.assertIn("entry's ctl", section("manage", "Start"))

    def test_fr33_has_no_retired_launch_or_resume_forms(self):
        for retired in (
            "agent-pm-crew run <name> -- agent-pm-session start",
            "--dry-run",
            "--resume --sid",
        ):
            self.assertNotIn(retired, body("manage"))

    def test_only_manage_owns_commands_permissions_and_scripts(self):
        self.assertFalse(os.path.isfile(os.path.join(SKILLS, "tmux", "SKILL.md")))
        self.assertTrue(rules("manage"))

    def test_role_launch_has_no_separate_dry_run(self):
        [role] = [
            line
            for line in section("manage", "Launch").splitlines()
            if line.startswith("- **Role run**:")
        ]
        self.assertNotIn("--dry-run", role)

    def test_no_retired_path_flag_or_manual_repair(self):
        for word in (*RETIRED, *REPAIRS):
            self.assertNotIn(word, body("manage"))
        self.assertNotIn("--split", body("manage"))


class Names(Case):
    def test_the_files_it_names_exist_and_ctmux_runs(self):
        named = paths("manage")
        self.assertIn(os.path.join(SKILLS, "manage", "scripts", "ctmux"), named)
        for path in named:
            self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(os.access(os.path.join(SKILLS, "manage", "scripts", "ctmux"), os.X_OK))

    def test_the_grid_keys_it_names_are_the_tmux_hosts(self):
        [config] = {p for p in paths("manage") if p.endswith("config.toml")}
        with open(config, "rb") as f:
            tmux = tomllib.load(f)["hosts"]["tmux"]
        for key in ("workers_per_column", "grid_retile"):
            self.assertIn(key, tmux)
            self.assertIn(f"`{key}`", body("manage"))

    def test_only_the_commander_has_it_as_the_session_config_turns_it_off(self):
        self.assertIn("so a worker or role run starts no sessions of its own", body("manage"))
        self.assertIn("refuse inside them (`docs/sessions.md` › Session settings)", body("manage"))
        with open(os.path.join(REPO, "docs", "sessions.md"), encoding="utf-8") as f:
            self.assertIn("`AGENT_PM_AGENT_SESSION=1`", f.read())
        [config] = {p for p in paths("manage") if p.endswith("config.toml")}
        with open(config, "rb") as f:
            clients = tomllib.load(f)["clients"]
        for client in ("claude", "codex"):
            self.assertTrue(
                any("agent-pm@agent-pm" in flag for flag in clients[client]["flags"]), client
            )

    def test_the_upgrading_row_carries_the_one_home_of_each_plugin_off_entry(self):
        with open(os.path.join(REPO, "docs", "upgrading.md"), encoding="utf-8") as f:
            [row] = [line for line in f if line.startswith("| a local `flags` list")]
        with open(os.path.join(REPO, "session/src/agent_pm/session/config.toml"), "rb") as f:
            clients = tomllib.load(f)["clients"]
        with open(os.path.join(REPO, "delegate/src/agent_pm/delegate/config.toml"), "rb") as f:
            policy = tomllib.load(f)["policy"]
        for client in ("claude", "codex"):
            flags = clients[client]["flags"]
            [at] = [i for i, flag in enumerate(flags) if "agent-pm@agent-pm" in flag]
            entry = f'"{flags[at - 1]}", ' + (
                f"'{flags[at]}'" if '"' in flags[at] else f'"{flags[at]}"'
            )
            with self.subTest(client=client):
                self.assertIn(f"`[session.clients.{client}]`", row)
                self.assertIn(entry, row)
                self.assertFalse([f for f in policy[client]["flags"] if "agent-pm@agent-pm" in f])
        self.assertNotIn("delegate.policy", row)

    def test_the_skills_it_names_are_this_plugins(self):
        for skill in re.findall(r"`/agent-pm:([a-z-]+)`", body("manage")):
            homes = [os.path.join(REPO, d, "skills", skill) for d in ("manager", "delegate")]
            self.assertTrue(any(os.path.isfile(os.path.join(h, "SKILL.md")) for h in homes), skill)


class Flows(Case):
    def test_each_kind_launches_through_its_crew_shorthand(self):
        launch = section("manage", "Launch")
        for command in (
            "agent-pm-crew worker",
            "agent-pm-crew role",
            "agent-pm-crew issue",
        ):
            self.assertIn(command, launch)
        self.assertNotIn("agent-pm-crew run", launch)
        self.assertIn("before", launch)
        self.assertIn("exit 2", launch)
        self.assertIn("--mode headless", launch)
        self.assertIn("the tui `<name>` and its harness `<name>-drive`", launch)
        self.assertIn("--display D", launch)
        self.assertIn("`--display none`", launch)
        self.assertIn("agent-pm-session --help", launch)

    def test_start_attaches_reads_its_output_and_arms_one_listener(self):
        start = section("manage", "Start")
        for phrase in (
            "`ctmux <name>`",
            "agent-pm-crew hold --manager",
            "TaskStop",
            "`agent-pm-crew attach`",
            "lease",
            "`name kind sid state view note cwd attach`",
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
        direct = section("manage", "Direct")
        for phrase in (
            "`agent-pm-session send <name> '<text>'`",
            "`agent-pm-session read <name> [--lines <n>]`",
            "`agent-pm-session reply <name>`",
            "`agent-pm-session restart <name>`",
            "`agent-pm-crew hide <name>…`",
            "`agent-pm-crew show <name>…`",
            "`--all`",
            "`agent-pm-session show <name>-drive`",
            "`agent-pm-crew note <name> '<text>'`",
            "`''` clears",
            "`agent-pm-crew status [<name>…]`",
            "`agent-pm-crew report`",
            "PR's URL",
            "`agent-pm-crew close <name>`",
            "`agent-pm-crew close-all`",
            "`agent-pm-crew forget <name>`",
            "`--force`",
            "`agent-pm-crew release`",
        ):
            self.assertIn(phrase, direct)
        for phrase in ("agent-pm-delegate hide", "agent-pm-linear hide", "agent-pm-session hide"):
            self.assertNotIn(phrase, direct)

    def test_color_is_left_to_the_session_but_a_start_dialog(self):
        (worker,) = [
            line for line in section("manage", "Launch").splitlines() if "**Worker**" in line
        ]
        self.assertEqual(body("manage").count("/color"), 1)
        self.assertIn("/color", worker)
        self.assertIn('"color not set"', worker)
        self.assertNotIn("Color", section("manage", "Direct"))

    def test_events_handles_every_word(self):
        events = section("manage", "Events")
        for word in ("done", "blocked", "dead", "exited", "outcome", "Monitor expired"):
            self.assertRegex(events, rf"\*\*{word}\b[^*]*\*\*", word)

    def test_an_events_content_is_its_read_and_only_numbered_lines_are_events(self):
        events = section("manage", "Events")
        self.assertNotIn("confirm each by reading", events)
        for phrase in (
            "content",
            "`content unavailable",
            "untrusted",
            "greater than the previous event's",
        ):
            self.assertIn(phrase, events)
        self.assertIn("agent-pm-crew report", rules("manage"))
        self.assertIn("agent-pm-crew report *", rules("manage"))

    def test_a_dialog_is_read_then_answered_with_keys_never_with_text(self):
        events = section("manage", "Events")
        blocked = re.search(r"\*\*blocked\*\*(.*?)(?=^- \*\*)", events, re.M | re.S)[1]
        self.assertIn("agent-pm-session read <name>", blocked)
        for keys in ("--key Down Enter", "--key 2", "--key Escape", "--key C-c"):
            self.assertIn(keys, blocked)
        self.assertRegex(blocked, r"send no text")
        self.assertRegex(blocked, r"read (it )?again")
        self.assertRegex(blocked, r"not by the last event")
        self.assertIn("ask the user", blocked)

    def test_recovery_is_per_entry_and_a_waiting_role_uses_recorded_run_settings(self):
        recover = section("manage", "Recover")
        self.assertIn("agent-pm-crew resume <name>", recover)
        self.assertIn("agent-pm-crew resume <name> --input '<answers>'", recover)
        self.assertNotIn("agent-pm-delegate resume", recover)
        self.assertNotIn("--resume --sid", recover)
        self.assertRegex(recover, r"no resume-all")

    def test_grid_names_its_keys_and_the_public_retile(self):
        grid = section("manage", "Grid")
        for word in ("workers_per_column", "grid_retile", "agent-pm-session tile <window>"):
            self.assertIn(word, grid)

    def test_sections(self):
        self.assertEqual(
            sections("manage")[:6], ["Start", "Events", "Launch", "Grid", "Direct", "Recover"]
        )
