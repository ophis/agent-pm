"""Adapters record pure preparation and reject before any commit or runtime execution."""

import sys

from agent_pm.session.extension import (
    Adapter,
    ConfigError,
    InProcess,
    JsonHeadless,
    Launch,
    Spec,
    Tui,
)
from agent_pm.utils import overlaps


class PreflightAdapter(Adapter):
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
        return Launch(argv, env=env, cwd=spec.access.cwd or spec.workdir)


class PreflightHeadless(JsonHeadless):
    def __init__(self, adapter):
        self.adapter = adapter

    def launch(self, spec):
        return self.adapter.launch(spec)

    def commit(self):
        self.adapter.committed.append("headless")

    def parse(self, record):
        return ()


class SandboxedPreflightAdapter(PreflightAdapter):
    def launch(self, spec):
        for path in (spec.access.cwd or spec.workdir, *spec.access.dirs):
            for protected in spec.protect:
                if overlaps(path, protected):
                    raise ConfigError(f"writable path {path} overlaps protected path {protected}")
        return super().launch(spec)


class PreflightTui(Tui):
    def __init__(self, adapter):
        self.adapter = adapter

    def launch(self, spec, hook, *, source_roots):
        return self.adapter.launch(spec)

    def commit(self):
        self.adapter.committed.append("tui")

    def reply(self, payload):
        return ""


class PreflightInProcess(InProcess):
    def __init__(self, adapter):
        self.adapter = adapter

    def check(self, spec):
        self.adapter.prepared.append(spec)
        if spec.tier == 4:
            raise ConfigError("tier 4 is unmapped")

    def run(self, spec):
        self.adapter.ran.append(spec)
        raise AssertionError("preflight executed an in_process runtime")
