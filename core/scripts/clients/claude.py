"""Claude Code: starts the run with `claude -p`, its data from clients/claude.toml."""
from compose import ConfigError

from .base import Client, Launch


class ClaudeClient(Client):
    keys = frozenset({"flags", "tiers", "efforts", "env", "allow", "roles"})

    def launch(self, prompt, run, *, sid, resume, access, out):
        c = self.config
        model, effort = c.get("tiers", {}).get(str(run["tier"])), c.get("efforts", {}).get(run["effort"])
        if model is None:
            raise ConfigError(f"no model for tier {run['tier']} in clients/claude.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run['effort']!r} in clients/claude.toml")
        argv = ["claude", "-p", prompt, "--resume" if resume else "--session-id", sid,
                "--model", model, "--effort", effort, *c.get("flags", [])]
        for d in access.dirs:
            argv += ["--add-dir", d]
        allow = [f"Bash({cmd})" for cmd in access.commands] + (self.value(run, "allow") or [])
        if allow:
            argv += ["--allowedTools", *allow]
        return Launch(argv, dict(c.get("env", {})))
