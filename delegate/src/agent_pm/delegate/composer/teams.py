"""The Team: the roles, tasks and templates a run can name, from the team root and the team dirs;
each role's task index; the checks that keep a team dir out of reach of the agent run.
"""

import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_pm.delegate.composer.base import METHOD, PLACEHOLDER, TEAM, read_text, title_of
from agent_pm.delegate.composer.types import ConfigError, Team
from agent_pm.delegate.engine.config import CONFIG
from agent_pm.delegate.event import PROGRESS
from agent_pm.utils import LOCAL, read_project, temp_dirs, under, unfenced
from agent_pm.utils import load_config as load

START = re.compile(rf"^\s*\[{PROGRESS}:start\] \S", re.M)
KINDS = ("roles", "tasks", "templates")
TEAM_NAME = re.compile(r"[a-z0-9-]+")
INDEX = re.compile(r"^## Tasks\n(.*?)(?=^#|\Z)", re.M | re.S)
INDEX_LINE = re.compile(r"^- `([\w-]+)`(?: \(`([^`]*)`\))?: (.+)$", re.M)
INDEX_DEFAULT = re.compile(r"unsure → `([\w-]+)` \(`([^`]*)`\)")


def lookup(team: Team, kind: str, name: str) -> str | None:
    paths = team.files.get((kind, name), ())
    if len(paths) > 1:
        raise ConfigError.duplicate_file(paths[1], kind[:-1], name, paths[0])
    if paths and team.custom(paths[0]):
        check_file(team, kind, paths[0])
    return paths[0] if paths else None


def path_for(team: Team, kind: str, name: str) -> str:
    if path := lookup(team, kind, name):
        return path
    raise misnamed_error(team, kind, name) or ConfigError.missing_file(kind, name)


def read(team: Team, kind: str, name: str) -> tuple[str, str]:
    path = path_for(team, kind, name)
    return read_text(path), team.label(kind, name, path)


def check_file(team: Team, kind: str, path: str) -> None:
    text = read_text(path)
    if text.split("\n", 1)[0].strip() == "---":
        raise ConfigError.front_matter(path, LOCAL)
    if m := PLACEHOLDER.search(text):
        raise ConfigError.file_placeholder(path, m[0])
    for m in METHOD.findall(text):
        if not os.path.isfile(os.path.join(team.root, "methods", f"{m}.md")):
            raise ConfigError.missing_method(path, m)
    if kind == "roles":
        title_of(text, path)
    elif kind == "tasks":
        if not text.startswith("# "):
            raise ConfigError.heading(path)
        if (n := len(START.findall(text))) != 1:
            raise ConfigError.start_marks(path, n, PROGRESS)
        if not any(line.rstrip() == "## Resume" for _, line in unfenced(text)):
            raise ConfigError.resume_section(path)


def charter(team: Team, role: str) -> tuple[str, str] | None:
    if not (path := lookup(team, "roles", role)):
        return None
    return read_text(path), team.label("roles", role, path)


def tasks_dir(team: Team, role: str) -> str | None:
    path = lookup(team, "roles", role)
    return (
        os.path.join(os.path.dirname(os.path.dirname(path)), "tasks")
        if path and team.custom(path)
        else None
    )


def used(team: Team, role: str, templates: Sequence[str] = ()) -> list[str]:
    paths = [lookup(team, "roles", role)]
    paths += [lookup(team, "tasks", t) for t in index(role, team)]
    paths += [lookup(team, "templates", name) for name in templates]
    return list(dict.fromkeys(p for p in paths if p and team.custom(p)))


def misnamed_error(
    team: Team, kind: str | None = None, name: str | None = None
) -> ConfigError | None:
    for path in team.misnamed:
        stem = os.path.basename(path).removesuffix(".md")
        if kind is None or (os.path.basename(os.path.dirname(path)), stem) == (kind, name):
            return ConfigError.team_filename(path, stem, TEAM_NAME.pattern)
    return None


def check_all(team: Team, cfg: Mapping) -> None:
    if e := misnamed_error(team):
        raise e
    for kind, name in team.files:
        lookup(team, kind, name)
    roles(team, cfg)


def team_dirs(config: str = CONFIG, local: str = LOCAL) -> tuple[str, ...]:
    try:
        own, value = (
            load(config).get("team_dirs"),
            read_project("delegate", path=config, local=local).get("team_dirs"),
        )
    except (OSError, ValueError) as e:
        raise ConfigError.configuration(e) from None
    if own:
        raise ConfigError.team_dirs_override(local, os.path.basename(config))
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError.team_dirs_list(value)
    dirs = []
    for p in value:
        if (
            not isinstance(p, str)
            or not p.startswith(("/", "~"))
            or not os.path.isabs(os.path.expanduser(p))
        ):
            raise ConfigError.team_dir_absolute(p)
        real = os.path.realpath(os.path.expanduser(p))
        if not os.path.isdir(real):
            raise ConfigError.team_dir_missing(p)
        if real in dirs:
            raise ConfigError.team_dir_duplicate(p)
        dirs.append(real)
    return tuple(dirs)


def team(root: str = TEAM, dirs: Sequence[str] = ()) -> Team:
    files, misnamed = {}, []
    for base in (root, *dirs):
        for kind in KINDS:
            sub = os.path.join(base, kind)
            try:
                names = sorted(os.listdir(sub)) if os.path.isdir(sub) else []
            except OSError as e:
                raise ConfigError.file_io(sub, e.strerror) from None
            for name in names:
                path, stem = os.path.join(sub, name), name.removesuffix(".md")
                if not name.endswith(".md") or not os.path.isfile(path):
                    continue
                if base != root and not TEAM_NAME.fullmatch(stem):
                    misnamed.append(path)
                else:
                    files.setdefault((kind, stem), []).append(path)
    return Team(
        root=root,
        dirs=tuple(dirs),
        files={k: tuple(v) for k, v in files.items()},
        misnamed=tuple(misnamed),
    )


def tasks_path(role: str, team: Team) -> str:
    return tasks_dir(team, role) or os.path.join(team.root, "tasks")


def roles(team: Team, cfg: Mapping) -> list[str]:
    for role in cfg.get("roles", {}):
        if not lookup(team, "roles", role):
            raise no_charter(role)
    charters = {name: paths for (kind, name), paths in team.files.items() if kind == "roles"}
    core = [name for name, paths in charters.items() if not team.custom(paths[0])]
    out = [r for r in cfg.get("roles", {}) if r in core] + sorted(
        r for r in core if r not in cfg.get("roles", {})
    )
    for d in team.dirs:
        out += sorted(
            name
            for name, paths in charters.items()
            if name not in out and any(os.path.dirname(os.path.dirname(p)) == d for p in paths)
        )
    return out


def index(
    role: str,
    team: Team | None = None,
    config: str = CONFIG,
    local: str = LOCAL,
    *,
    cfg: Mapping | None = None,
) -> dict[str, str]:
    team = team_or_default(team)
    if not (found := charter(team, role)):
        raise no_charter(role, config_of(config, local, cfg), team)
    text, rel = found
    m = INDEX.search(text)
    section = m.group(1).strip() if m else ""
    if not (lines := INDEX_LINE.findall(section)):
        raise ConfigError.task_index(rel)
    tasks = {t: what for t, _, what in lines}
    if not (default := INDEX_DEFAULT.search(section.split("\n", 1)[0])):
        raise ConfigError.task_default(rel)
    if default.group(1) not in tasks:
        raise ConfigError.task_default_unlisted(rel, default.group(1))
    for t, path in [default.groups(), *((t, path) for t, path, _ in lines)]:
        if path != f"<tasks>/{t}.md":
            raise ConfigError.task_path(rel, t, path)
    own = tasks_path(role, team)
    for t in tasks:
        if not (path := lookup(team, "tasks", t)):
            raise misnamed_error(team, "tasks", t) or ConfigError.missing_task_file(rel, t)
        if os.path.dirname(path) != own:
            raise ConfigError.task_ownership(
                rel, t, team.label("tasks", t, path), tasks_dir(team, role) or "core team/tasks"
            )
    return tasks


def catalog(
    team: Team | None = None, config: str = CONFIG, local: str = LOCAL
) -> dict[str, dict[str, str]]:
    team = team_or_default(team)
    role_tables(config, local)
    return {
        role: index(role, team, config, local) for role in roles(team, config_of(config, local))
    }


def config_of(config: str, local: str, cfg: Mapping | None = None) -> Mapping:
    """`cfg`, the caller's already-read delegate config, else `config` with `local` read."""
    if cfg is not None:
        return cfg
    try:
        return read_project("delegate", path=config, local=local)
    except (OSError, ValueError) as e:
        raise ConfigError.configuration(e) from None


def role_tables(config: str, local: str, cfg: Mapping | None = None) -> Mapping:
    if cfg is None and not os.path.isfile(config):
        raise ConfigError.core_config(config)
    roles = config_of(config, local, cfg).get("roles")
    if not isinstance(roles, Mapping):
        raise ConfigError.roles_table(config)
    for role, table in roles.items():
        if not isinstance(role, str) or not isinstance(table, Mapping):
            raise ConfigError.role_table(role, config)
    return roles


def team_or_default(given: Team | None) -> Team:
    return given if given is not None else team()


def no_charter(role: str, cfg: Mapping | None = None, team: Team | None = None) -> ConfigError:
    if team is not None and (e := misnamed_error(team, "roles", role)):
        return e
    if cfg is not None and role not in cfg.get("roles", {}):
        return ConfigError.unknown_role(role)
    return ConfigError.no_charter(role)


def check_team_dirs(
    dirs: Sequence[str], *, writes: Sequence[tuple[str, str]], temp: Sequence[str] | None = None
) -> None:
    if not dirs:
        return
    temps, me = temp_dirs() if temp is None else temp, os.getuid()
    for d in map(os.path.realpath, dirs):
        for path in team_paths(d):
            real = os.path.realpath(path)
            shown = path if real == path else f"{path} ({real})"
            if t := under(real, temps):
                raise ConfigError.team_dir_temp(shown, t)
            check_overlap(shown, real, writes)
            try:
                st = os.stat(path)
            except OSError as e:
                raise ConfigError.file_io(path, e.strerror) from None
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise ConfigError.team_dir_writable(shown)
            if st.st_uid != me:
                raise ConfigError.team_dir_owner(shown, st.st_uid, me)
        for parent in Path(d).parents:
            try:
                st = os.stat(parent)
            except OSError as e:
                raise ConfigError.file_io(parent, e.strerror) from None
            if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
                raise ConfigError.team_parent_writable(d, parent)
            if st.st_uid not in (me, 0):
                raise ConfigError.team_parent_owner(d, parent, st.st_uid)


def team_paths(d: str) -> list[str]:
    paths = [d]
    for kind in KINDS:
        if os.path.isdir(sub := os.path.join(d, kind)):
            try:
                names = sorted(os.listdir(sub))
            except OSError as e:
                raise ConfigError.file_io(sub, e.strerror) from None
            paths += [sub, *(os.path.join(sub, n) for n in names if n.endswith(".md"))]
    return paths


def check_overlap(shown: str, real: str, writes: Sequence[tuple[str, str]]) -> None:
    for what, p in writes:
        p = os.path.realpath(p)
        if under(real, [p]) or under(p, [real]):
            raise ConfigError.team_overlap(shown, what, p)
