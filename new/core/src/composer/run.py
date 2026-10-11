"""A role's run config (resolve: the core config, then the caller's overlays; `runtime` an opaque run key, no runtime
layer), one run's params and its runtime-neutral access intent (access)."""
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, get_args

from composer.base import CONFIG, SRC, ConfigError, _title, fill
from composer.teams import Team, _no_charter, _roles, _team, index, tasks_path
from utils.config_file import LOCAL, read_config
from utils.paths import real_dirs

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS = get_args(Effort)
RUN_KEYS = frozenset({"tier", "effort", "runtime", "read", "write", "commands", "templates", "output", "gate",
                      "language", "show", "cwd"})
OUTPUT_KEYS = frozenset({"type", "repo", "branch", "dir", "host"})
OLD_KEYS = frozenset({"default_task", "tasks"})


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    """A role's resolved config; checked on creation and on dataclasses.replace."""
    role: str
    task: str = ""   # the named task; "" → the agent run picks one from the role's index
    tier: int
    effort: Effort
    runtime: str = ""   # opaque: never compared with an Adapter's name
    output: dict
    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    gate: str = ""
    language: str = ""
    show: str | None = None
    cwd: str = ""   # "" → the caller's default
    role_title: str = ""

    def __post_init__(self):
        if type(self.tier) is not int or not 1 <= self.tier <= 4:
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {self.effort!r}")
        if not isinstance(self.runtime, str) or not self.runtime.strip() or not self.runtime.isprintable():
            raise ConfigError(f"runtime must be a non-empty one-line name, got {self.runtime!r:.80}")
        if "type" not in self.output:
            raise ConfigError("output.type is not set")
        if not isinstance(self.gate, str) or "\n" in self.gate or "`" in self.gate:
            raise ConfigError("gate must be one line of shell command without backticks")
        if not isinstance(self.language, str) or "\n" in self.language:
            raise ConfigError("language must be one line of text")
        if self.show is not None and (not isinstance(self.show, str) or "\n" in self.show):
            raise ConfigError("show must be one line of shell command")
        if self.cwd and (not isinstance(self.cwd, str) or not self.cwd.isprintable()
                         or not os.path.isabs(os.path.expanduser(self.cwd))):
            raise ConfigError(f"cwd must be a printable absolute or ~ path, got {self.cwd!r:.80}")


@dataclass(frozen=True, kw_only=True)
class RunParams:
    """What the caller passes for one agent run: its input, where its deliverable ends up and its workdir."""
    input: str
    out: str
    workdir: str
    resume: bool = False

    @property
    def deliverable(self) -> str:
        """The file the agent run writes its deliverable to: `out` when it lies under the real workdir, else
        <workdir>/tmp/deliverable.md."""
        out = os.path.abspath(self.out)
        if Path(os.path.realpath(out)).is_relative_to(os.path.realpath(self.workdir)):
            return out
        return os.path.join(os.path.abspath(self.workdir), "tmp", "deliverable.md")


@dataclass(frozen=True)
class Intent:
    """One agent run's access intent: the dirs it needs beyond its cwd, the commands it requests, its gate, cwd and
    whether the cwd's project settings load; the Adapter decides the grants."""
    dirs: tuple[str, ...]
    commands: tuple[str, ...]
    gate: str
    cwd: str
    project: bool


def lookup(layer: Mapping, role: str, key: str):
    """`key` in one config layer: roles.<role> > the top; None if unset."""
    for table in (layer.get("roles", {}).get(role, {}), layer):
        if key in table:
            return table[key]
    return None


def check_old_keys(table: Mapping, role: str, prefix: str = "") -> None:
    """Raises ConfigError when role table [<prefix>roles.<role>] holds a key of the task-level config this one replaced,
    naming the table its run keys now go in."""
    if old := sorted(OLD_KEYS & set(table)):
        raise ConfigError(f"[{prefix}roles.{role}] has {old[0]!r}: task-level config and default_task are gone; set "
                          f"run keys in [{prefix}roles.{role}] (a run without a task picks one)")


def resolve(role: str, task: str | None = None, *, overlays: Sequence[Mapping] = (),
            label: tuple[str, str] = ("run overlay", "overlay"), team: Team | None = None,
            config: str = CONFIG) -> RunConfig:
    """The run config of `role`: `config` with LOCAL on top, then each overlay in order (the same layout), each
    replacing the run keys it sets; within a layer [roles.<role>] beats the global value and an output table replaces
    the whole. `config` must give a valid RunConfig on its own (so it sets runtime); each overlay is checked again once
    applied, its errors naming `label` (source, the overlays' table). A role is known by its [roles.<role>] table or
    its charter in `team` (None: the team root alone); `task`, when named, must be in the role's index.

    An overlay can widen read, write, commands and gate, so a caller passes only operator-controlled config as one."""
    overlays, team = tuple(overlays), _team(team)
    known = set(_roles(config)) | {name for kind, name in team.files if kind == "roles"}
    if role not in known:
        raise _no_charter(role, {}, team)
    source, where = label
    for overlay in overlays:
        try:
            _check_overlay(overlay, known, where)
        except ConfigError as e:
            raise ConfigError(f"{source}: {e}") from None
    cfg = read_config(config, LOCAL)
    _check(cfg)
    values = _run_keys(cfg, role)
    run = RunConfig(role=role, task=task or "", tier=values.pop("tier", None), effort=values.pop("effort", None),
                    output=values.pop("output", {}), **values)
    for overlay in overlays:
        run = replace(run, **_run_keys(overlay, role))
    if not (charter := team.charter(role)):
        raise _no_charter(role, cfg, team)
    tasks = index(role, team, config)
    if task and task not in tasks:
        raise team.misnamed_error("tasks", task) or ConfigError(
            f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    return replace(run, role_title=_title(*charter))


def trusted_dirs(config: str = CONFIG) -> frozenset[str]:
    """Real paths of the global `trusted_dirs` of `config` with LOCAL on top; ConfigError when it is malformed."""
    try:
        return real_dirs(read_config(config, LOCAL).get("trusted_dirs", []), "trusted_dirs")
    except ValueError as e:
        raise ConfigError(str(e)) from None


def bind(entry: str, repo_dir: str | None) -> str | None:
    """A read/write entry as an absolute dir: `repo` is `repo_dir`, the caller's repo dir (None without one); else a
    path."""
    if entry == "repo":
        return os.path.abspath(repo_dir) if repo_dir else None
    return os.path.abspath(os.path.expanduser(entry))


def access(run: RunConfig, workdir: str, *, cwd: str, project: bool, repo_dir: str | None = None,
           team: Team | None = None) -> Intent:
    """`run`'s Intent. `cwd` (an absolute path, else ConfigError) and `project` are the caller's, passed through. Dirs:
    the workdir when `cwd` is another real path, then <tasks>, then each read then write entry with {{methods}} filled
    and bound (bind), deduped; commands with {{scripts}} and {{workdir}} filled; the gate verbatim. `team` as
    resolve's."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ConfigError(f"cwd must be an absolute path, got {cwd!r:.80}")
    team, workdir = _team(team), os.path.abspath(workdir)
    dirs = ([workdir] if os.path.realpath(cwd) != os.path.realpath(workdir) else []) + [tasks_path(run.role, team)]
    methods = os.path.join(team.root, "methods")
    for key, entries in (("read", run.read), ("write", run.write)):
        for entry in entries:
            p = bind(fill(entry, {"methods": methods}, key), repo_dir)
            if p and p not in dirs:
                dirs.append(p)
    values = {"scripts": SRC, "workdir": workdir}
    return Intent(dirs=tuple(dirs), commands=tuple(fill(c, values, "commands") for c in run.commands), gate=run.gate,
                  cwd=cwd, project=project)


def _check(cfg: Mapping) -> None:
    """Raises ConfigError unless the config's own tables are valid: [output] and each [roles.<role>] (old keys first).
    Its other global keys are other layers' (the core config's header) and go unchecked."""
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
    return {k: v for k in RUN_KEYS if (v := lookup(layer, role, k)) is not None}
