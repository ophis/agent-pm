"""A role's run config (resolve: the core config, then the caller's overlays), one run's params and its runtime-neutral
access intent (access), whose env is the session env its commands and gate need. An overlay can widen read, write,
commands and gate, so a caller passes only operator-controlled config as one."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from agent_pm.delegate.composer.base import CONFIG, fill, title_of
from agent_pm.delegate.composer.teams import (
    Team,
    index,
    no_charter,
    read,
    role_tables,
    tasks_path,
    team_or_default,
)
from agent_pm.delegate.composer.types import ConfigError, RunConfig
from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.utils.config_file import LOCAL
from agent_pm.utils.paths import real_dirs

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


@dataclass(frozen=True)
class Intent:
    dirs: tuple[str, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    commands: tuple[str, ...]
    gate: str
    env: Mapping[str, str]
    cwd: str
    project: bool


def lookup(layer: Mapping, role: str, key: str):
    for table in (layer.get("roles", {}).get(role, {}), layer):
        if key in table:
            return table[key]
    return None


def check_old_keys(table: Mapping, role: str, prefix: str = "") -> None:
    if old := sorted(OLD_KEYS & set(table)):
        raise ConfigError(
            f"[{prefix}roles.{role}] has {old[0]!r}: task-level config and default_task are gone; set "
            f"run keys in [{prefix}roles.{role}] (a run without a task picks one)"
        )


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
            raise ConfigError(f"{source}: {e}") from None
    cfg = read(config, local, cfg)
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
    if not (charter := team.charter(role)):
        raise no_charter(role, cfg, team)
    tasks = index(role, team, config, local, cfg=cfg)
    if task and task not in tasks:
        raise team.misnamed_error("tasks", task) or ConfigError(
            f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
        )
    return replace(run, role_title=title_of(*charter))


def trusted_dirs(
    config: str = CONFIG, local: str = LOCAL, *, cfg: Mapping | None = None
) -> frozenset[str]:
    try:
        return real_dirs(read(config, local, cfg).get("trusted_dirs", []), "trusted_dirs")
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
        raise ConfigError(f"cwd must be an absolute path, got {cwd!r:.80}")
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
    _check_output(cfg, "output")
    for r, table in cfg.get("roles", {}).items():
        check_old_keys(table, r)
        _check_keys(table, RUN_KEYS, f"roles.{r}")
        _check_output(table, f"roles.{r}.output")


def _check_output(table: Mapping, where: str) -> None:
    if "output" not in table:
        return
    if not isinstance(table["output"], Mapping):
        raise ConfigError(f"{where} must be a table")
    _check_keys(table["output"], OUTPUT_KEYS, where)


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}")


def _check_overlay(overlay: Mapping, roles: set[str], where: str) -> None:
    if not isinstance(overlay, Mapping):
        raise ConfigError(f"[{where}] must be a table")
    _check_overlay_keys(overlay, RUN_KEYS | {"roles"}, where)
    _check_overlay_output(overlay, where)
    if "roles" not in overlay:
        return
    if not isinstance(overlay["roles"], Mapping):
        raise ConfigError(f"[{where}.roles] must be a table")
    for role, table in overlay["roles"].items():
        if role not in roles:
            raise ConfigError(f"unknown role {role!r} in [{where}.roles]")
        if not isinstance(table, Mapping):
            raise ConfigError(f"[{where}.roles.{role}] must be a table")
        check_old_keys(table, role, f"{where}.")
        _check_overlay_keys(table, RUN_KEYS, f"{where}.roles.{role}")
        _check_overlay_output(table, f"{where}.roles.{role}")


def _check_overlay_output(table: Mapping, where: str) -> None:
    if "output" not in table:
        return
    if not isinstance(table["output"], Mapping):
        raise ConfigError(f"[{where}.output] must be a table")
    _check_overlay_keys(table["output"], OUTPUT_KEYS, f"{where}.output")


def _check_overlay_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed, key=str):
        raise ConfigError(f"unknown key {extra[0]!r} in [{where}]")


def _run_keys(layer: Mapping, role: str) -> dict:
    """A layer's run keys for `role`; an [output] key "" is unset."""
    values = {k: v for k in RUN_KEYS if (v := lookup(layer, role, k)) is not None}
    if isinstance(output := values.get("output"), Mapping):
        values["output"] = {k: v for k, v in output.items() if v != ""}
    return values
