"""Clients record pure preparation and reject before any commit or runtime execution."""

import sys

from agent_pm.session.extension import (
    Client,
    ConfigError,
    InProcess,
    JsonHeadless,
    Launch,
    Spec,
    Tui,
)


class PreflightClient(Client):
    keys = frozenset({"unsupported", "empty_argv", "bad_env", "writable"})
    forbidden_selection = ("--resume",)
    prepared: list[Spec] = []
    committed: list[str] = []
    ran: list[Spec] = []

    def headless(self):
        return None if self.config.get("unsupported") == "headless" else PreflightHeadless(self)

    def tui(self):
        return None if self.config.get("unsupported") == "tui" else PreflightTui(self)

    def in_process(self):
        return None if self.config.get("unsupported") == "in_process" else PreflightInProcess(self)

    def writable_dirs(self, spec):
        return tuple(self.config.get("writable", ()))

    def launch(self, spec):
        self.prepared.append(spec)
        env = {**spec.access.env, **spec.env}
        if self.config.get("bad_env"):
            env["BAD"] = "nul\0value"
        argv = () if self.config.get("empty_argv") else (sys.executable, spec.prompt)
        return Launch(argv, env=env, cwd=spec.access.cwd)


class PreflightHeadless(JsonHeadless):
    def __init__(self, client):
        self.owner = client

    def launch(self, spec):
        return self.owner.launch(spec)

    def commit(self):
        self.owner.committed.append("headless")

    def parse(self, record):
        return ()


class PreflightTui(Tui):
    def __init__(self, client):
        self.owner = client

    def launch(self, spec, hook):
        return self.owner.launch(spec)

    def commit(self):
        self.owner.committed.append("tui")

    def reply(self, payload):
        return ""


class PreflightInProcess(InProcess):
    def __init__(self, client):
        self.owner = client

    def check(self, spec):
        self.owner.prepared.append(spec)
        if spec.tier == 4:
            raise ConfigError("tier 4 is unmapped")

    def run(self, spec):
        self.owner.ran.append(spec)
        raise AssertionError("preflight executed an in_process client")
