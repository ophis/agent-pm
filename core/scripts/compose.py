"""Composer: resolves a role + task's run config and compiles principles + role + task + templates + output into its
prompt. A module for the driver (drive.py); needs Python 3.11+.

Principles get {{role}}, {{task}} and their anchors; role and task text get {{scripts}} (this dir, or the client's path
to it); a destination gets its output values.
"""
import json
import os
import re
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, get_args

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT = "crew"   # principles.md, roles/, tasks/, templates/ and output/ live here
CONFIG = os.path.join("config", "config.toml")
SCHEMA = os.path.join(TEXT, "output", "outcome.schema.json")

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS = get_args(Effort)
RUN_KEYS = frozenset({"tier", "effort", "read", "write", "commands", "templates", "output"})
GLOBAL_KEYS = RUN_KEYS | {"roles", "users"}
ROLE_KEYS = RUN_KEYS | {"default_task", "tasks"}
PLACEHOLDER = re.compile(r"\{\{(\w+)(?:\|([^{}]*))?\}\}")   # {{name}} or {{name|default}}
RESUME = "Resumed run after an interruption. These rules are current; they may have changed since this session started.\n\n"


class ConfigError(Exception):
    pass


class Vehicle(Protocol):
    """What render() needs from whatever carries the prompt (a client)."""
    def scripts_path(self, root: str) -> str: ...   # how the prompt names core's scripts/ dir
    def handover(self) -> str: ...                  # how the run returns its outcome and progress (Output › Return)


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    """A role + task's resolved config; checked on creation and on dataclasses.replace."""
    role: str
    task: str
    tier: int
    effort: Effort
    output: dict
    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    role_title: str = ""
    task_title: str = ""
    task_summary: str = ""

    def __post_init__(self):
        if type(self.tier) is not int or not 1 <= self.tier <= 4:
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {self.effort!r}")
        if "type" not in self.output:
            raise ConfigError("output.type is not set")


@dataclass(frozen=True, kw_only=True)
class RunParams:
    """What the caller passes for one run: its input, where its deliverable is saved, its workdir and Claude session."""
    input: str
    out: str
    workdir: str
    sid: str | None = None   # None → a new session id
    resume: bool = False

    def __post_init__(self):
        if self.resume and not self.sid:
            raise ConfigError("--resume needs --sid")
        if not self.sid:
            object.__setattr__(self, "sid", str(uuid.uuid4()))


def lookup(layer: Mapping, role: str, task: str, key: str):
    """`key` in one config layer: roles.<role>.tasks.<task> > roles.<role> > the top; None if unset."""
    r = layer.get("roles", {}).get(role, {})
    for table in (r.get("tasks", {}).get(task, {}), r, layer):
        if key in table:
            return table[key]
    return None


def load_run(root: str, role: str, task: str | None = None, *, layers: Sequence[Mapping] = ()) -> RunConfig:
    """The run config for role/task from config.toml, each later layer (same layout) replacing the run keys it sets.
    config.toml must be valid on its own; each layer is checked again once applied."""
    with open(os.path.join(root, CONFIG), "rb") as f:
        cfg = tomllib.load(f)
    task = _check(cfg, role, task)
    values = _run_keys(cfg, role, task)
    run = RunConfig(role=role, task=task, tier=values.pop("tier", None), effort=values.pop("effort", None),
                    output=values.pop("output", {}), **values)
    for layer in layers:
        run = replace(run, **_run_keys(layer, role, task))
    role_md, task_md = (_read(os.path.join(root, TEXT), rel) for rel in _rule_files(role, task))
    return replace(run, role_title=_title(role_md, f"roles/{role}.md"), task_title=_title(task_md, f"tasks/{task}.md"),
                   task_summary=_summary(task_md))


def render(root: str, run: RunConfig, params: RunParams | None = None, *, vehicle: Vehicle) -> str:
    """The run's prompt for `vehicle`: its Output section ends with the vehicle's handover as Output › Return, then the
    Workdir/Input tail when params are given."""
    text = os.path.join(root, TEXT)
    names = {"role": run.role_title, "task": run.task_title}
    names |= {"role_anchor": anchor(run.role_title), "task_anchor": anchor(run.task_title)}
    paths = {"scripts": vehicle.scripts_path(root)}
    parts = [fill(_read(text, "principles.md"), names, "principles.md")]
    parts += [fill(_read(text, rel), paths, rel) for rel in _rule_files(run.role, run.task)]
    for name in run.templates:
        rel = f"templates/{name}.md"
        body = _read(text, rel)
        f = _fence(body)
        parts.append(f"# Template: `{rel}`\n\n{f}markdown\n{body}{f}\n")
    dest = f"output/destinations/{run.output['type']}.md"
    if not os.path.isfile(os.path.join(text, dest)):
        raise ConfigError(f"no destination {run.output['type']!r} ({dest})")
    parts.append(fill(_read(text, "output/output.md"), {}, "output/output.md") + "\n" + fill(_read(text, dest), run.output, dest))
    if handover := vehicle.handover():
        parts.append(f"## Return\n\n{handover.strip()}\n")
    prompt = (RESUME if params and params.resume else "") + "\n".join(parts)
    if params:
        tail = f"Workdir: {os.path.abspath(params.workdir)}"
        if os.path.isfile(params.input):
            tail = f"Input: {os.path.abspath(params.input)}\n{tail}"
        else:
            tail = f"{tail}\nInput:\n\n{params.input.strip()}"
        prompt += f"\n---\n\n{tail}\n"
    return prompt


def outcome_schema(root: str) -> dict:
    with open(os.path.join(root, SCHEMA)) as f:
        return json.load(f)


def fill(text: str, values: Mapping, where: str) -> str:
    def value(m):
        if m.group(1) in values:
            return str(values[m.group(1)])
        return m.group(2) if m.group(2) is not None else m.group(0)
    out = PLACEHOLDER.sub(value, text)
    if m := PLACEHOLDER.search(out):
        raise ConfigError(f"unfilled placeholder {m.group(0)} in {where}")
    return out


def anchor(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _check(cfg: Mapping, role: str, task: str | None) -> str:
    """The task to run (the role's default when None), once config.toml's keys, role and task are valid."""
    _check_keys(cfg, GLOBAL_KEYS, "the global table")
    roles = cfg.get("roles", {})
    if role not in roles:
        raise ConfigError(f"unknown role {role!r}")
    r = roles[role]
    _check_keys(r, ROLE_KEYS, f"roles.{role}")
    tasks = r.get("tasks", {})
    default = r.get("default_task")
    if default is not None and default not in tasks:
        raise ConfigError(f"default_task {default!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    task = task or default
    if task not in tasks:
        raise ConfigError(f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    _check_keys(tasks[task], RUN_KEYS, f"roles.{role}.tasks.{task}")
    return task


def _run_keys(layer: Mapping, role: str, task: str) -> dict:
    return {k: v for k in RUN_KEYS if (v := lookup(layer, role, task, k)) is not None}


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}")


def _rule_files(role: str, task: str) -> tuple[str, str]:
    return f"roles/{role}.md", f"tasks/{task}.md"


def _read(root: str, rel: str) -> str:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise ConfigError(f"missing file {rel}")
    with open(path) as f:
        return f.read().strip() + "\n"


def _title(text: str, rel: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    if not first.startswith("# "):
        raise ConfigError(f"{rel} must start with a '# ' heading")
    return first[2:].strip()


def _summary(text: str) -> str:
    """The first paragraph line after a file's heading."""
    return next((line.strip() for line in text.splitlines()[1:] if line.strip() and not line.startswith("#")), "")


def _fence(text: str) -> str:
    ticks = max([len(r) for r in re.findall(r"`+", text)] + [2]) + 1
    return "`" * ticks
