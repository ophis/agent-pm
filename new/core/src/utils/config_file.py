"""Pure TOML file loading and table merging."""
import os
import tomllib

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"


def merge(base: dict, over: dict) -> dict:
    """`over` on `base`: tables merge key by key; any other value replaces base's."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config(path: str = CONFIG, local: str = LOCAL) -> dict:
    """`path`, with `local` (~ expanded) merged on top when it exists."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg
