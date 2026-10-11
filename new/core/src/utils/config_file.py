"""Pure TOML file loading and table merging; the core config's pair: config.toml (CONFIG) with this machine's
~/.agent-pm/core-new.local.toml (LOCAL) merged on top. Each reader reads only its own keys (config.toml's header)."""
import os
import tomllib

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"


def merge(base: dict, over: dict) -> dict:
    """`base` with `over` on top: tables merge key by key; any other value of `over` replaces base's."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config(path: str = CONFIG, local: str = LOCAL) -> dict:
    """TOML file `path`, with TOML file `local` (~ expanded) merged on top when it exists (merge())."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg
