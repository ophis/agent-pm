"""A role's run config (resolve: the core config, then the caller's overlays), one run's params and
its runtime-neutral access intent (access), whose env is the session env its commands and gate need.
An overlay can widen read, write, commands and gate, so a caller passes only operator-controlled
config as one.
"""

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from agent_pm.delegate.composer.base import fill, title_of
from agent_pm.delegate.composer.teams import (
    charter,
    config_of,
    index,
    misnamed_error,
    no_charter,
    role_tables,
    tasks_path,
    team_or_default,
)
from agent_pm.delegate.composer.types import ConfigError, Intent, RunConfig, Team
from agent_pm.delegate.engine import config as delegate_config
from agent_pm.delegate.engine.config import CONFIG, PYTHONPATH
from agent_pm.utils import LOCAL

RUN_KEYS = frozenset(
    {
        "tier",
        "effort",
        "runtime",
        "read",
        "write",
        "commands",
        "templates",
        "output",
        "gate",
        "language",
        "show",
        "cwd",
    }
)
OUTPUT_KEYS = frozenset({"type", "repo", "branch", "dir", "host"})
OLD_KEYS = frozenset({"default_task", "tasks"})


def run_key(layer: Mapping, role: str, key: str):
    for table in (layer.get("roles", {}).get(role, {}), layer):
        if key in table:
            return table[key]
    return None


def check_old_keys(table: Mapping, role: str, prefix: str = "") -> None:
    if old := sorted(OLD_KEYS & set(table)):
        raise ConfigError.obsolete_task_keys(prefix, role, old[0])


def resolve(
    role: str,
    task: str | None = None,
    *,
    overlays: Sequence[Mapping] = (),
    label: tuple[str, str] = ("run overlay", "overlay"),
    team: Team | None = None,
    config: str = CONFIG,
    local: str = LOCAL,
    cfg: Mapping | None = None,
) -> RunConfig:
    overlays, team = tuple(overlays), team_or_default(team)
    known = set(role_tables(config, local, cfg)) | {n for kind, n in team.files if kind == "roles"}
    if role not in known:
        raise no_charter(role, {}, team)
    source, where = label
    for overlay in overlays:
        try:
            _check_overlay(overlay, known, where)
        except ConfigError as e:
            raise ConfigError.overlay(source, e) from None
    cfg = config_of(config, local, cfg)
    _check(cfg)
    values = _run_keys(cfg, role)
    run = RunConfig(
        role=role,
        task=task or "",
        tier=values.pop("tier", None),
        effort=values.pop("effort", None),
        output=values.pop("output", {}),
        **values,
    )
    for overlay in overlays:
        run = replace(run, **_run_keys(overlay, role))
    if not (found := charter(team, role)):
        raise no_charter(role, cfg, team)
    tasks = index(role, team, config, local, cfg=cfg)
    if task and task not in tasks:
        raise misnamed_error(team, "tasks", task) or ConfigError.task(task, role, tasks)
    return replace(run, role_title=title_of(*found))


def trusted_dirs(
    config: str = CONFIG, local: str = LOCAL, *, cfg: Mapping | None = None
) -> frozenset[str]:
    try:
        return delegate_config.trusted_dirs(config_of(config, local, cfg))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def bind(entry: str, repo_dir: str | None) -> str | None:
    if entry == "repo":
        return os.path.abspath(repo_dir) if repo_dir else None
    return os.path.abspath(os.path.expanduser(entry))


def access(
    run: RunConfig,
    workdir: str,
    *,
    cwd: str,
    project: bool,
    repo_dir: str | None = None,
    team: Team | None = None,
) -> Intent:
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ConfigError.absolute_cwd(cwd)
    team, workdir = team_or_default(team), os.path.abspath(workdir)
    paths = {"read": [], "write": []}
    for key, entries in (("read", run.read), ("write", run.write)):
        for entry in entries:
            p = bind(fill(entry, {"team": team.root}, key), repo_dir)
            if p and p not in paths[key]:
                paths[key].append(p)
    dirs = [workdir] if os.path.realpath(cwd) != os.path.realpath(workdir) else []
    dirs += [p for p in paths["write"] if p not in dirs]
    seen, reads = set(map(os.path.realpath, dirs)), []
    for p in (tasks_path(run.role, team), *paths["read"]):
        if (real := os.path.realpath(p)) not in seen:
            seen.add(real)
            reads.append(p)
    commands = tuple(
        fill(c, {"workdir": workdir, "team": team.root}, "commands") for c in run.commands
    )
    return Intent(
        dirs=tuple(dirs),
        reads=tuple(reads),
        writes=tuple(paths["write"]),
        commands=commands,
        gate=run.gate,
        env={"PYTHONPATH": PYTHONPATH} if commands or run.gate else {},
        cwd=cwd,
        project=project,
    )


def _check(cfg: Mapping) -> None:
    _check_output(cfg, "output", str)
    for r, table in cfg.get("roles", {}).items():
        _check_role(table, r, "", str)


def _check_overlay(overlay: Mapping, roles: set[str], where: str) -> None:
    """An overlay's errors name its tables in brackets."""
    if not isinstance(overlay, Mapping):
        raise ConfigError.overlay_table(where)
    _check_keys(overlay, RUN_KEYS | {"roles"}, f"[{where}]")
    _check_output(overlay, f"{where}.output", _boxed)
    if "roles" not in overlay:
        return
    if not isinstance(overlay["roles"], Mapping):
        raise ConfigError.overlay_roles_table(where)
    for role, table in overlay["roles"].items():
        if role not in roles:
            raise ConfigError.overlay_role(role, where)
        if not isinstance(table, Mapping):
            raise ConfigError.overlay_role_table(where, role)
        _check_role(table, role, f"{where}.", _boxed)


def _boxed(where: str) -> str:
    return f"[{where}]"


def _check_role(table: Mapping, role: str, prefix: str, shown: Callable[[str], str]) -> None:
    check_old_keys(table, role, prefix)
    _check_keys(table, RUN_KEYS, shown(f"{prefix}roles.{role}"))
    _check_output(table, f"{prefix}roles.{role}.output", shown)


def _check_output(table: Mapping, where: str, shown: Callable[[str], str]) -> None:
    if "output" not in table:
        return
    if not isinstance(table["output"], Mapping):
        raise ConfigError.table(shown(where))
    _check_keys(table["output"], OUTPUT_KEYS, shown(where))


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed, key=str):
        raise ConfigError.key(extra[0], where)


def _run_keys(layer: Mapping, role: str) -> dict:
    """A layer's run keys for `role`; an [output] key "" is unset."""
    values = {k: v for k in RUN_KEYS if (v := run_key(layer, role, k)) is not None}
    if isinstance(output := values.get("output"), Mapping):
        values["output"] = {k: v for k, v in output.items() if v != ""}
    return values
