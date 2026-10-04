"""The client interface: a Client turns a composed run into a Launch."""
import os
import tomllib
from dataclasses import dataclass, field

from compose import ConfigError


@dataclass
class Launch:
    argv: list                                # empty: nothing to start
    env: dict = field(default_factory=dict)   # added to the caller's environment
    cwd: str = ""                             # set by the driver
    files: dict = field(default_factory=dict)  # path → text, written by the driver


class Client:
    keys = frozenset()
    needs_config = True
    runs = True   # starts a run: needs --input and --workdir, and gets the Input/Output/Workdir tail

    def __init__(self, config):
        if extra := sorted(set(config) - self.keys):
            raise ConfigError(f"unknown key {extra[0]!r} in {type(self).__name__}'s config")
        self.config = config

    def value(self, run, key):
        """`key` for this run, the same layout as config.toml: roles.<role>.tasks.<task> > roles.<role> > global."""
        role = self.config.get("roles", {}).get(run["role"], {})
        for layer in (role.get("tasks", {}).get(run["task"], {}), role, self.config):
            if key in layer:
                return layer[key]
        return None

    def output(self, run):
        """The run's output destination: this client's `output` entry for it, else the task's own."""
        return self.value(run, "output") or run["output"]

    def launch(self, prompt, run, *, sid, resume, access, out):
        raise NotImplementedError


def load_config(name, root):
    path = os.path.join(root, "clients", f"{name}.toml")
    if not os.path.isfile(path):
        raise ConfigError(f"no client config {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)
