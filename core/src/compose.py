"""Composer: resolves a role's run config and compiles guide + principles + role (with its task index) + templates +
output into its prompt; the agent run reads its task's file itself. A module for the driver (drive.py).

Guide and principles get {{role}}, its anchor, {{task}} (the named task; none → the guide's default text) and
{{language}} (unset → each line holding it is dropped); guide, principles and role text get {{scripts}} (this dir, or
the client's path to it), {{methods}} (team/methods/, likewise), {{tasks}} (team/tasks/, likewise) and {{gate}}; a
destination gets its output values.
"""
import json
import os
import re
import shlex
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, get_args

import repo
import tui_claude

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT = "team"
OUTPUT = "output"
CONFIG = "config.toml"
SCHEMA = os.path.join(OUTPUT, "outcome.schema.json")

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS = get_args(Effort)
RUN_KEYS = frozenset({"tier", "effort", "read", "write", "commands", "templates", "output", "gate", "language", "show",
                      "cwd"})
GLOBAL_KEYS = RUN_KEYS | {"roles", "users", "clients", "trusted_dirs", "status_line", "workers_per_column"}
OLD_KEYS = frozenset({"default_task", "tasks"})
PLACEHOLDER = re.compile(r"\{\{(\w+)(?:\|([^{}]*))?\}\}")   # {{name}} or {{name|default}}
INDEX = re.compile(r"^## Tasks\n(.*?)(?=^#|\Z)", re.M | re.S)   # a role's task index: its lines - `<task>`: <text>
INDEX_LINE = re.compile(r"^- `([\w-]+)`: (.+)$", re.M)
# The progress mark: core/CLAUDE.md › Rules.
PROGRESS = "agent-pm-progress"
CHANNEL = ".report.jsonl"
RESUME = ("Resumed agent run after an interruption. These rules and the input are current; either may have changed since this "
          "session started, so re-read the input. Continue the task this session already picked or was given; never "
          "pick it again.\n\n")


class ConfigError(Exception):
    pass


class PromptClient(Protocol):
    """The part of a Client (clients/base.py) that render() uses; compose cannot import Client (base.py imports compose)."""
    def scripts_path(self, root: str) -> str: ...
    def methods_path(self, root: str) -> str: ...
    def tasks_path(self, root: str) -> str: ...
    def handover(self) -> str: ...


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    """A role's resolved config; checked on creation and on dataclasses.replace."""
    role: str
    task: str = ""   # the named task; "" → the agent run picks one from the role's index
    tier: int
    effort: Effort
    output: dict
    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    gate: str = ""
    language: str = ""
    show: str | None = None
    cwd: str = ""   # "" → the caller's default (drive.place)
    role_title: str = ""

    def __post_init__(self):
        if type(self.tier) is not int or not 1 <= self.tier <= 4:
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {self.effort!r}")
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
    """What the caller passes for one agent run: its input, where its deliverable is saved, its workdir and Claude session."""
    input: str
    out: str
    workdir: str
    sid: str | None = None   # None → a new session id
    resume: bool = False
    prefix: str | None = None   # tui runner only: its session's name before the sid (tui_session)

    def __post_init__(self):
        if self.resume and not self.sid:
            raise ConfigError("--resume needs --sid")
        if self.prefix is not None and not tui_claude.NAME.fullmatch(self.prefix):
            raise ConfigError(f"prefix {self.prefix!r}: want {tui_claude.NAME.pattern}")
        if not self.sid:
            object.__setattr__(self, "sid", str(uuid.uuid4()))

    @property
    def channel(self) -> str:
        return os.path.join(os.path.abspath(self.workdir), CHANNEL)


def tui_session(role: str, sid: str, prefix: str | None = None) -> str:
    """The name of the tui runner's tmux session for an agent run."""
    return f"{prefix or role}-{sid[:8]}"


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


def index(root: str, role: str) -> dict[str, str]:
    """{task: its line} of the role's task index, team/roles/<role>.md › Tasks, in order: the only list of its tasks."""
    rel = f"roles/{role}.md"
    m = INDEX.search(_read(os.path.join(root, TEXT), rel))
    if not (tasks := dict(INDEX_LINE.findall(m.group(1))) if m else {}):
        raise ConfigError(f"{rel}: no task index (a ## Tasks section of - `<task>`: … lines)")
    for t in tasks:
        if not os.path.isfile(os.path.join(root, TEXT, "tasks", f"{t}.md")):
            raise ConfigError(f"{rel} lists {t!r} without tasks/{t}.md")
    return tasks


def load_run(root: str, role: str, task: str | None = None, *, layers: Sequence[Mapping] = ()) -> RunConfig:
    """The run config for `role` from config.toml with repo.LOCAL on top, each later layer (same layout) replacing the
    run keys it sets; `task`, when named, must be in the role's index. That config must be valid on its own; each layer
    is checked again once applied."""
    cfg = repo.read_config(os.path.join(root, CONFIG))
    _check(cfg, role)
    values = _run_keys(cfg, role)
    run = RunConfig(role=role, task=task or "", tier=values.pop("tier", None), effort=values.pop("effort", None),
                    output=values.pop("output", {}), **values)
    for layer in layers:
        run = replace(run, **_run_keys(layer, role))
    tasks = index(root, role)
    if task and task not in tasks:
        raise ConfigError(f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    rel = f"roles/{role}.md"
    return replace(run, role_title=_title(_read(os.path.join(root, TEXT), rel), rel))


def render(root: str, run: RunConfig, params: RunParams | None = None, *, client: PromptClient) -> str:
    """The agent run's prompt for `client`: its Output section ends with the client's handover as Output › Return
    ({{report}} filled when params are given), then the Workdir/Input tail when params are given."""
    text = os.path.join(root, TEXT)
    names = {"role": run.role_title, "role_anchor": anchor(run.role_title), "language": run.language}
    names |= {"task": f"`{run.task}`"} if run.task else {}
    paths = {"scripts": client.scripts_path(root), "methods": client.methods_path(root),
             "tasks": client.tasks_path(root), "gate": run.gate or "none"}
    principles = _read(text, "principles.md")
    if not run.language:
        principles = "".join(line for line in principles.splitlines(True) if "{{language}}" not in line)
    rel = f"roles/{run.role}.md"
    parts = [fill(_read(text, "guide.md"), names | paths, "guide.md"), fill(principles, names | paths, "principles.md"),
             fill(_read(text, rel), paths, rel)]
    for name in run.templates:
        rel = f"templates/{name}.md"
        body = _read(text, rel)
        f = _fence(body)
        parts.append(f"# Template: `{rel}`\n\n{f}markdown\n{body}{f}\n")
    output, dest = os.path.join(root, OUTPUT), f"destinations/{run.output['type']}.md"
    if not os.path.isfile(os.path.join(output, dest)):
        raise ConfigError(f"no destination {run.output['type']!r} ({OUTPUT}/{dest})")
    parts.append(fill(_read(output, "output.md"), {}, "output.md") + "\n" + fill(_read(output, dest), run.output, dest))
    if handover := client.handover():
        report = {"report": report_command(paths["scripts"], params)} if params else {}
        parts.append(f"## Return\n\n{fill(handover, report, 'handover').strip()}\n")
    prompt = (RESUME if params and params.resume else "") + "\n".join(parts)
    if params:
        tail = f"Workdir: {os.path.abspath(params.workdir)}"
        if os.path.isfile(params.input):
            tail = f"Input: {os.path.abspath(params.input)}\n{tail}"
        else:
            tail = f"{tail}\nInput:\n\n{params.input.strip()}"
        prompt += f"\n---\n\n{tail}\n"
    return prompt


def report_command(scripts: str, params: RunParams) -> str:
    """The shell command an agent run reports its progress and outcome with, up to its subcommand."""
    return f"python3 {shlex.quote(os.path.join(scripts, 'report.py'))} --to {shlex.quote(params.channel)}"


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
    """GitHub's heading anchor."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def _check(cfg: Mapping, role: str) -> None:
    """Raises ConfigError unless the config's keys, its client's role tables included, are valid and it has `role`."""
    _check_keys(cfg, GLOBAL_KEYS, "the global table")
    for name, client in cfg.get("clients", {}).items():
        for r, table in client.get("roles", {}).items():
            check_old_keys(table, r, f"clients.{name}.")
    for r, table in cfg.get("roles", {}).items():
        check_old_keys(table, r)
        _check_keys(table, RUN_KEYS, f"roles.{r}")
    if role not in cfg.get("roles", {}):
        raise ConfigError(f"unknown role {role!r}")


def _run_keys(layer: Mapping, role: str) -> dict:
    return {k: v for k in RUN_KEYS if (v := lookup(layer, role, k)) is not None}


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}")


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


def _fence(text: str) -> str:
    ticks = max([len(r) for r in re.findall(r"`+", text)] + [2]) + 1
    return "`" * ticks
