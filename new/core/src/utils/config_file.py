"""The core config's files: config.toml (CONFIG) with this machine's ~/.agent-pm/core-new.local.toml (LOCAL) merged on
top (read). Each reader reads only its own keys (config.toml's header); trusted_dirs reads the global `trusted_dirs`."""
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


def read(path: str = CONFIG, local: str = LOCAL) -> dict:
    """TOML file `path`, with TOML file `local` (~ expanded) merged on top when it exists (merge())."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg


def trusted_dirs(cfg: dict) -> frozenset[str]:
    """Realpaths of the config's `trusted_dirs`; ValueError unless it is a list of absolute or expandable ~ paths."""
    paths = cfg.get("trusted_dirs", [])
    if not isinstance(paths, list):
        raise ValueError("trusted_dirs: want a list of local paths")
    for p in paths:
        if not isinstance(p, str) or not p.startswith(("/", "~")) or os.path.expanduser(p).startswith("~"):
            raise ValueError(f"trusted_dirs: {p!r:.80} is not an absolute or expandable ~ path")
    return frozenset(os.path.realpath(os.path.expanduser(p)) for p in paths)
