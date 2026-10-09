"""Composer: resolves a role's run config and compiles guide + parameters + principles + role (with its task index) +
templates + output + input into its prompt; the agent run reads its task's file itself. A module for the driver
(drive.py).

Guide and principles get {{role}}, its anchor, {{task}} (the named task and its file; none → the guide's default
text) and {{language}} (unset → each line holding it is dropped); a destination gets {{deliverable}}, <Workdir>/<its
path in the workdir> (no params → each line holding it is dropped). Every other run value is a PARAMETERS name, its
value given once in # Parameters.
"""
import json
import os
import re
import shlex
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
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
INDEX = re.compile(r"^## Tasks\n(.*?)(?=^#|\Z)", re.M | re.S)   # a role's task index (core/CLAUDE.md › Rules)
INDEX_LINE = re.compile(r"^- `([\w-]+)`(?: \(`([^`]*)`\))?: (.+)$", re.M)   # task, path (none → ""), description
INDEX_DEFAULT = re.compile(r"unsure → `([\w-]+)` \(`([^`]*)`\)")   # in the index's opening sentence
METHOD = re.compile(r"<methods>/([\w-]+)\.md")   # a method file named in a text
REPORT = "report"   # the report command's Parameters name
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,6}) (.+)$")
BOLD_ITEM = re.compile(r"^\s*(?:(?:[-*]|\d+\.) )?\*\*(.+?)\*\*")
ANCHOR_LINE = re.compile(r'<a id="([^"]+)"></a>')
# The # Parameters section's names, in order: name → (the regex a text uses it by, its value's source in _parameters).
PARAMETERS = {"<Workdir>": ("<Workdir>", "workdir"), "<scripts>": ("<scripts>", "scripts"),
              "<tasks>": ("<tasks>", "tasks"), "<methods>": ("<methods>", "methods"), "<gate>": ("<gate>", "gate"),
              "<out-repo>": ("<out-repo>", "output.repo"), "<out-branch>": ("<out-branch>", "output.branch"),
              "<out-dir>": ("<out-dir>", "output.dir"), "<out-host>": ("<out-host>", "output.host"),
              REPORT: (r"`report[` ]", REPORT)}   # only a code span starting with it: prose says "report" too
RULE = ("Each name below stands for its value. Run every command with each name, nested ones included, replaced by its "
        "value and written out in full, no name or shell variable left: pre-approved commands match on their exact "
        "text and each shell call starts fresh, so anything else waits on a permission prompt.")
# The progress mark: core/CLAUDE.md › Rules.
PROGRESS = "agent-pm-progress"
CHANNEL = "run.jsonl"   # in the workdir: the agent run's record, its reports and the driver's events (drive.start)
RESUME = ("Resumed agent run after an interruption. These rules are current and may have changed since this session "
          "started. The input below is current: it adds to this session's earlier input. Continue the task this "
          "session already picked or was given; never pick it again.\n\n")


class ConfigError(Exception):
    pass


class PromptClient(Protocol):
    """The part of a Client (clients/base.py) that render() uses; compose cannot import Client (base.py imports compose)."""
    inline_workdir: str
    inline_input: str

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
    """What the caller passes for one agent run: its input, where its deliverable ends up, its workdir and Claude
    session."""
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

    @property
    def deliverable(self) -> str:
        """The file the agent run writes its deliverable to: `out` when it lies under the real workdir, else
        <workdir>/tmp/deliverable.md."""
        out = os.path.abspath(self.out)
        if Path(os.path.realpath(out)).is_relative_to(os.path.realpath(self.workdir)):
            return out
        return os.path.join(os.path.abspath(self.workdir), "tmp", "deliverable.md")


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
    """{task: its description} of the role's task index, team/roles/<role>.md › Tasks, in order: the only list of its
    tasks. ConfigError unless its opening sentence names a listed default and each task's path is <tasks>/<task>.md,
    with that file in team/tasks/."""
    rel = f"roles/{role}.md"
    m = INDEX.search(_read(os.path.join(root, TEXT), rel))
    section = m.group(1).strip() if m else ""
    if not (lines := INDEX_LINE.findall(section)):
        raise ConfigError(f"{rel}: no task index (a ## Tasks section of - `<task>` (`<tasks>/<task>.md`): … lines)")
    tasks = {t: what for t, _, what in lines}
    if not (default := INDEX_DEFAULT.search(section.split("\n", 1)[0])):
        raise ConfigError(f"{rel}: ## Tasks opens without its default (unsure → `<task>` (`<tasks>/<task>.md`))")
    if default.group(1) not in tasks:
        raise ConfigError(f"{rel}: the default {default.group(1)!r} is not a listed task")
    for t, path in [default.groups(), *((t, path) for t, path, _ in lines)]:
        if path != f"<tasks>/{t}.md":
            raise ConfigError(f"{rel}: {t!r} has path {path!r}, not '<tasks>/{t}.md'")
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
    """The agent run's prompt for `client`: the guide, then # Parameters (_parameters); its Output section ends with the
    client's handover as Output › Return; last, # Input: the input when params are given, else the client's
    inline_input ("" → no section). A client that runs needs params: <Workdir> and `report` are otherwise undefined.
    A repeated heading in the text before the input gets an `<a id>` line (_unique); the input text is verbatim."""
    text = os.path.join(root, TEXT)
    names = {"role": run.role_title, "role_anchor": anchor(run.role_title), "language": run.language}
    names |= {"task": f"`{run.task}` (`<tasks>/{run.task}.md`)"} if run.task else {}
    rel = f"roles/{run.role}.md"
    guide, principles, role = _read(text, "guide.md"), _read(text, "principles.md"), _read(text, rel)
    if not run.language:
        principles = _without(principles, "{{language}}")
    texts = {"guide.md": fill(guide, names, "guide.md"), "principles.md": fill(principles, names, "principles.md"),
             rel: fill(role, {}, rel)}
    parts = list(texts.values())
    for name in run.templates:
        rel = f"templates/{name}.md"
        body = _read(text, rel)
        f = _fence(body)
        parts.append(f"# Template: `{rel}`\n\n{f}markdown\n{body}{f}\n")
    output, dest = os.path.join(root, OUTPUT), f"destinations/{run.output['type']}.md"
    if not os.path.isfile(os.path.join(output, dest)):
        raise ConfigError(f"no destination {run.output['type']!r} ({OUTPUT}/{dest})")
    destination, values = _read(output, dest), {}
    if params:
        inside = os.path.relpath(os.path.realpath(params.deliverable), os.path.realpath(params.workdir))
        values = {"deliverable": f"<Workdir>/{inside}"}
    else:
        destination = _without(destination, "{{deliverable}}")
    texts |= {"output.md": fill(_read(output, "output.md"), {}, "output.md"), dest: fill(destination, values, dest)}
    parts.append(texts["output.md"] + "\n" + texts[dest])
    if handover := client.handover():
        texts["handover"] = fill(handover, {}, "handover")
        parts.append(f"## Return\n\n{texts['handover'].strip()}\n")
    parts.insert(1, _parameters(root, run, params, client, texts))
    prompt = (RESUME if params and params.resume else "") + "\n".join(parts)
    given = params.input.strip() if params else client.inline_input
    if params or given:
        return _unique(prompt + "\n# Input\n") + f"\n{given}\n"
    return _unique(prompt)


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


def unfenced(text: str) -> list[tuple[int, str]]:
    """(line index, line) of each line of `text` outside fences, fence lines excluded. A fence opens at 3+ backticks or
    tildes and closes at a line of only that char, as many times or more; one never closed runs to the end."""
    out, fence = [], None
    for i, line in enumerate(text.split("\n")):
        shut = line.strip()
        if fence:
            if len(shut) >= fence[1] and shut == fence[0] * len(shut):
                fence = None
        elif m := FENCE.match(line):
            fence = (m[1][0], len(m[1]))
        else:
            out.append((i, line))
    return out


def outline(text: str) -> list[tuple[int, tuple[str, ...], str, bool]]:
    """(line index, path, anchor, is_heading) of each heading and bold item of `text` outside fences, in order. A
    heading's path is its ancestors' titles then its own, its anchor the `<a id>` line right before it, else
    `anchor(title)`; a bold item (`**name**` opening its line after a list marker, `name` less a trailing `:` or `.`)
    has its section's path plus its name and its section's anchor."""
    out, stack, section, last = [], [], "", (-2, "")
    for i, line in unfenced(text):
        if m := HEADING.match(line):
            level, title = len(m[1]), m[2].strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            explicit = ANCHOR_LINE.fullmatch(last[1]) if last[0] == i - 1 else None
            section = explicit[1] if explicit else anchor(title)
            out.append((i, tuple(t for _, t in stack), section, True))
        elif m := BOLD_ITEM.match(line):
            out.append((i, tuple(t for _, t in stack) + (re.sub(r"[:.]$", "", m[1]),), section, False))
        last = (i, line)
    return out


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


def _parameters(root: str, run: RunConfig, params: RunParams | None, client: PromptClient,
                texts: Mapping[str, str]) -> str:
    """The # Parameters section: RULE, then each PARAMETERS name that `texts` ({label: text}), the role's task files or
    the method files these name use, plus <Workdir>, <scripts> and <tasks> even unused, each with its value as inline
    code (<Workdir> without params: the client's inline_workdir, prose). A used name without a value raises ConfigError
    naming the first text using it."""
    text = os.path.join(root, TEXT)
    texts = dict(texts) | {f"tasks/{t}.md": _read(text, f"tasks/{t}.md") for t in index(root, run.role)}
    named = dict.fromkeys(f"methods/{m}.md" for t in texts.values() for m in METHOD.findall(t))
    texts |= {rel: _read(text, rel) for rel in named if os.path.isfile(os.path.join(text, rel))}
    values = {"workdir": _code(os.path.abspath(params.workdir)) if params else client.inline_workdir,
              "scripts": _code(client.scripts_path(root)), "tasks": _code(client.tasks_path(root)),
              "methods": _code(client.methods_path(root)), "gate": _code(run.gate or "none"),
              REPORT: _code(f"python3 <scripts>/report.py --to <Workdir>/{CHANNEL}") if params else ""}
    values |= {f"output.{k}": _code(str(v)) for k, v in ({"host": "github.com"} | run.output).items()}
    unset = {"workdir": "no run params and the client has no inline_workdir", REPORT: "no run params"}
    lines = []
    for name, (use, source) in PARAMETERS.items():
        where = next((label for label, t in texts.items() if re.search(use, t)), None)
        if where and not values.get(source):
            raise ConfigError(f"{name} is used in {where} but Parameters doesn't define it "
                              f"({unset.get(source, f'{source} is not set')})")
        if values.get(source) and (where or name in ("<Workdir>", "<scripts>", "<tasks>")):
            lines.append(f"- `{name}`: {values[source]}\n")
    return f"# Parameters\n\n{RULE}\n\n" + "".join(lines)


def _unique(text: str) -> str:
    """`text` with an `<a id>` line before each heading whose anchor an earlier heading has, its id the anchor of
    "<top-level title> <title>" (a top-level heading: of its title), then -2, -3, ... until free."""
    lines, taken, top, ids = text.split("\n"), set(), "", {}
    for i, path, own, heading in outline(text):
        if not heading:
            continue
        title, first = path[-1], lines[i].startswith("# ")
        top = title if first else top
        if own in taken:
            base = anchor(title if first else f"{top} {title}")
            own, n = base, 2
            while own in taken:
                own, n = f"{base}-{n}", n + 1
            ids[i] = own
        taken.add(own)
    for i, id_ in reversed(ids.items()):
        lines.insert(i, f'<a id="{id_}"></a>')
    return "\n".join(lines)


def _read(root: str, rel: str) -> str:
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise ConfigError(f"missing file {rel}")
    with open(path) as f:
        return f.read().strip() + "\n"


def _without(text: str, placeholder: str) -> str:
    """`text` less each line holding `placeholder`."""
    return "".join(line for line in text.splitlines(True) if placeholder not in line)


def _title(text: str, rel: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    if not first.startswith("# "):
        raise ConfigError(f"{rel} must start with a '# ' heading")
    return first[2:].strip()


def _fence(text: str, least: int = 3) -> str:
    ticks = max([len(r) + 1 for r in re.findall(r"`+", text)] + [least])
    return "`" * ticks


def _code(value: str) -> str:
    """`value` as Markdown inline code."""
    f, pad = _fence(value, 1), " " if "`" in value else ""
    return f"{f}{pad}{value}{pad}{f}"
