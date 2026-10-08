"""Clients: one class per agent client, turning a composed agent run into the command that starts it.

Add a client:
1. Class: <name>.py, a base.Client subclass (it satisfies compose.PromptClient) setting `keys` (its own config keys;
   "roles" too for per-role/task entries), `needs_config` (whether it reads [clients.<name>] in config.toml) and `runs`
   (True: launch() starts an agent run; False: inline(prompt) returns the prompt drive.py prints for the calling
   conversation to follow, as skill does); scripts_path(root) and methods_path(root) only when prompts must name
   core's src/ or team/methods/ other than by their absolute path.
2. Launch (runs): launch(prompt, run, *, params, access) -> Launch(argv, env, cwd, interactive=…).
   - Map run.tier and run.effort through its config; a value with no mapping is a ConfigError.
   - Turn access.dirs and access.commands into its own permission flags; whatever it can't enforce stays a prompt
     request (docs/adr/0003).
   - New session vs resume from params.sid and params.resume; cwd = access.cwd; access.project → load that cwd's
     project settings and instructions, else user settings only. drive.place decides both (the `cwd` run key,
     trusted_dirs, a resume's run.json entry); access.dirs then starts with the workdir when the cwd is another.
   - transcript and resume: where the client saves the session and the shell command a human resumes it with;
     drive.Record writes them to <workdir>/run.json.
   - interactive: the same agent run as the client's interactive command, for the tui runner; empty when it has none.
3. Outcome and progress (docs/adr/0006): override handover(), the Output › Return text telling the agent run to report
   both with {{report}} (compose.report_command: src/report.py --to <workdir>/.report.jsonl, pre-approved for every
   run): `progress <name> <text>`, and `outcome --status … --title … --summary …` as its last action. drive.start
   creates that channel before launch, tails it during the run (only lines appended after it began) and once more
   after the runner ends, keeps the last outcome and validates it (drive.validate, the only check). events(lines)
   turns a headless run's stdout into Event("text"). Have interactive append `stop` to the channel at each turn end
   (compose.report_command + " stop"; claude: a Stop hook in --settings): stop lines go to the runner, never the sinks,
   and tui nudges a run that stopped without an outcome. Add --pending KEY when the hook's stdin JSON holds the run's
   in-flight background work as a list at top-level KEY (claude: background_tasks; another input shape needs another
   report.py option): the line then carries `pending`, and tui neither nudges nor counts a stop with pending work.
   Without stop lines tui never nudges a run; it gives up only after drive.WAIT_LIMIT with no progress report or
   outcome.
4. Register it in REGISTRY; add [clients.<name>] to config.toml when needs_config.
5. Test in src/tests/drive_test.py: its argv and interactive for a role/task, resume, an unmapped tier, its events
   from a sample of its output.
Done when `drive.py --client <name> --dry-run` (and --runner tui, when it has interactive) prints the expected command
for every role/task in config.toml and the suite passes.
"""
from compose import ConfigError

from .base import PROGRESS, Access, Client, Event, Launch, load_config  # noqa: F401
from .claude import ClaudeClient
from .skill import SkillClient

REGISTRY = {"claude": ClaudeClient, "skill": SkillClient}


def get(name: str, root: str) -> Client:
    """The client named `name`, built from config.toml's [clients.<name>]."""
    if name not in REGISTRY:
        raise ConfigError(f"unknown client {name!r} (known: {', '.join(sorted(REGISTRY))})")
    cls = REGISTRY[name]
    return cls(load_config(name, root) if cls.needs_config else {})
