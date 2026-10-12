import contextlib
import json
import os
import re
import shlex
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol, get_args

import repo
import tmux_host

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT = "team"
OUTPUT = os.path.join(TEXT, "output")
CONFIG = "config.toml"
SCHEMA = os.path.join(OUTPUT, "outcome.schema.json")

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS = get_args(Effort)
RUN_KEYS = frozenset(
    {
        "tier",
        "effort",
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
GLOBAL_KEYS = RUN_KEYS | {
    "roles",
    "users",
    "clients",
    "trusted_dirs",
    "status_line",
    "workers_per_column",
    "grid_retile",
    "team_dirs",
}
OLD_KEYS = frozenset({"default_task", "tasks"})
PLACEHOLDER = re.compile(r"\{\{(\w+)(?:\|([^{}]*))?\}\}")
INDEX = re.compile(r"^## Tasks\n(.*?)(?=^#|\Z)", re.M | re.S)
INDEX_LINE = re.compile(r"^- `([\w-]+)`(?: \(`([^`]*)`\))?: (.+)$", re.M)
INDEX_DEFAULT = re.compile(r"unsure → `([\w-]+)` \(`([^`]*)`\)")
METHOD = re.compile(r"<methods>/([\w-]+)\.md")
REPORT = "report"
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,6}) (.+)$")
BOLD_ITEM = re.compile(r"^\s*(?:(?:[-*]|\d+\.) )?\*\*(.+?)\*\*")
ANCHOR_LINE = re.compile(r'<a id="([^"]+)"></a>')
PARAMETERS = {
    "<Workdir>": ("<Workdir>", "workdir"),
    "<scripts>": ("<scripts>", "scripts"),
    "<tasks>": ("<tasks>", "tasks"),
    "<methods>": ("<methods>", "methods"),
    "<gate>": ("<gate>", "gate"),
    "<out-repo>": ("<out-repo>", "output.repo"),
    "<out-branch>": ("<out-branch>", "output.branch"),
    "<out-dir>": ("<out-dir>", "output.dir"),
    "<out-host>": ("<out-host>", "output.host"),
    REPORT: (r"`report[` ]", REPORT),
}  # prose says "report" too
RULE = (
    "Each name below stands for its value. Run every command with each name, nested ones included, replaced by its "
    "value and written out in full, no name or shell variable left"
)
PROGRESS = "agent-pm-progress"
CHANNEL = "run.jsonl"
START = re.compile(rf"^\s*\[{PROGRESS}:start\] \S", re.M)
KINDS = ("roles", "tasks", "templates")
TEAM_NAME = re.compile(r"[a-z0-9-]+")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
FILE_REF = re.compile(r"`<(tasks|methods)>/(<task>|[\w-]+)\.md`")
PAREN = re.compile(r"\(([^()]*)\)")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)
RESUME = (
    "Resumed agent run after an interruption. These rules are current and may have changed since this session "
    "started. The input below is current: it adds to this session's earlier input. Continue the task this "
    "session already picked or was given, following your task file's `## Resume` section; never pick it "
    "again.\n\n"
)


class ConfigError(Exception):
    pass


class PromptClient(Protocol):
    # compose cannot import Client (clients/base.py imports compose)
    inline_workdir: str
    inline_input: str

    def scripts_path(self, root: str) -> str: ...
    def methods_path(self, root: str) -> str: ...
    def tasks_path(self, root: str) -> str: ...
    def handover(self) -> str: ...
    def approval(self) -> str: ...
    def report_interpreter(self) -> str: ...


class ConfigClient(Protocol):
    config: Mapping


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    role: str
    task: str = ""
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
    cwd: str = ""
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
        if self.cwd and (
            not isinstance(self.cwd, str)
            or not self.cwd.isprintable()
            or not os.path.isabs(os.path.expanduser(self.cwd))
        ):
            raise ConfigError(f"cwd must be a printable absolute or ~ path, got {self.cwd!r:.80}")


@dataclass(frozen=True, kw_only=True)
class RunParams:
    input: str
    out: str
    workdir: str
    sid: str | None = None
    resume: bool = False
    prefix: str | None = None

    def __post_init__(self):
        if self.resume and not self.sid:
            raise ConfigError("--resume needs --sid")
        if self.prefix is not None and not tmux_host.NAME.fullmatch(self.prefix):
            raise ConfigError(f"prefix {self.prefix!r}: want {tmux_host.NAME.pattern}")
        if not self.sid:
            object.__setattr__(self, "sid", str(uuid.uuid4()))

    @property
    def channel(self) -> str:
        return os.path.join(os.path.abspath(self.workdir), CHANNEL)

    @property
    def deliverable(self) -> str:
        out = os.path.abspath(self.out)
        if Path(os.path.realpath(out)).is_relative_to(os.path.realpath(self.workdir)):
            return out
        return os.path.join(os.path.abspath(self.workdir), "tmp", "deliverable.md")


def tui_session(role: str, sid: str, prefix: str | None = None) -> str:
    return f"{prefix or role}-{sid[:8]}"


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


@dataclass(frozen=True)
class Team:
    root: str
    dirs: tuple[str, ...] = ()
    files: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    misnamed: tuple[str, ...] = ()

    def custom(self, path: str) -> bool:
        return not Path(path).is_relative_to(os.path.join(self.root, TEXT))

    def label(self, kind: str, name: str, path: str) -> str:
        return path if self.custom(path) else f"{kind}/{name}.md"

    def source(self, path: str) -> str:
        return path if self.custom(path) else "built-in"

    def lookup(self, kind: str, name: str) -> str | None:
        paths = self.files.get((kind, name), ())
        if len(paths) > 1:
            raise ConfigError(
                f"{paths[1]}: {kind[:-1]} {name!r} is also defined by {paths[0]}; team dir files only add"
            )
        if paths and self.custom(paths[0]):
            self.check_file(kind, paths[0])
        return paths[0] if paths else None

    def read(self, kind: str, name: str) -> tuple[str, str]:
        if not (path := self.lookup(kind, name)):
            raise self.misnamed_error(kind, name) or ConfigError(f"missing file {kind}/{name}.md")
        return _text(path), self.label(kind, name, path)

    def check_file(self, kind: str, path: str) -> None:
        text = _text(path)
        if text.split("\n", 1)[0].strip() == "---":
            raise ConfigError(
                f"{path}: front matter (---) is not read: team dir files hold text only; run keys go in "
                f"{repo.LOCAL}"
            )
        if m := PLACEHOLDER.search(text):
            raise ConfigError(f"{path}: unfilled placeholder {m[0]}")
        for m in METHOD.findall(text):
            if not os.path.isfile(os.path.join(self.root, TEXT, "methods", f"{m}.md")):
                raise ConfigError(f"{path}: names <methods>/{m}.md, which core has no file for")
        if kind == "roles":
            _title(text, path)
        elif kind == "tasks":
            if not text.startswith("# "):
                raise ConfigError(f"{path}: must start with a '# ' heading")
            if (n := len(START.findall(text))) != 1:
                raise ConfigError(f"{path}: has {n} [{PROGRESS}:start] lines, not 1")
            if not any(line.rstrip() == "## Resume" for _, line in unfenced(text)):
                raise ConfigError(f"{path}: has no ## Resume section")

    def charter(self, role: str) -> tuple[str, str] | None:
        if not (path := self.lookup("roles", role)):
            return None
        return _text(path), self.label("roles", role, path)

    def tasks_dir(self, role: str) -> str | None:
        path = self.lookup("roles", role)
        return (
            os.path.join(os.path.dirname(os.path.dirname(path)), "tasks")
            if path and self.custom(path)
            else None
        )

    def used(self, role: str, templates: Sequence[str] = ()) -> list[str]:
        paths = [self.lookup("roles", role)]
        paths += [self.lookup("tasks", t) for t in index(self.root, role, self)]
        paths += [self.lookup("templates", name) for name in templates]
        return list(dict.fromkeys(p for p in paths if p and self.custom(p)))

    def misnamed_error(
        self, kind: str | None = None, name: str | None = None
    ) -> ConfigError | None:
        for path in self.misnamed:
            stem = os.path.basename(path).removesuffix(".md")
            if kind is None or (os.path.basename(os.path.dirname(path)), stem) == (kind, name):
                return ConfigError(f"{path}: name {stem!r}: want {TEAM_NAME.pattern}")
        return None

    def check_all(self, cfg: Mapping) -> None:
        if e := self.misnamed_error():
            raise e
        for kind, name in self.files:
            self.lookup(kind, name)
        roles(self.root, self, cfg)


def team_dirs(root: str) -> tuple[str, ...]:
    path = os.path.join(root, CONFIG)
    try:
        own, value = (
            "team_dirs" in repo.read_config(path, os.devnull),
            repo.read_config(path).get("team_dirs"),
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"config: {e}") from None
    if own:
        raise ConfigError(f"team_dirs: set it in {repo.LOCAL}, not {CONFIG}")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"team_dirs: want a list of absolute or ~ paths, got {value!r:.80}")
    dirs = []
    for p in value:
        if (
            not isinstance(p, str)
            or not p.startswith(("/", "~"))
            or not os.path.isabs(os.path.expanduser(p))
        ):
            raise ConfigError(f"team_dirs: {p!r:.80} is not an absolute or ~ path")
        real = os.path.realpath(os.path.expanduser(p))
        if not os.path.isdir(real):
            raise ConfigError(f"team_dirs: {p} is not a directory")
        if real in dirs:
            raise ConfigError(f"team_dirs: {p} is listed twice")
        dirs.append(real)
    return tuple(dirs)


def team(root: str, dirs: Sequence[str] = ()) -> Team:
    files, misnamed = {}, []
    builtin = os.path.join(root, TEXT)
    for base in (builtin, *dirs):
        for kind in KINDS:
            sub = os.path.join(base, kind)
            try:
                names = sorted(os.listdir(sub)) if os.path.isdir(sub) else []
            except OSError as e:
                raise ConfigError(f"{sub}: {e.strerror}") from None
            for name in names:
                path, stem = os.path.join(sub, name), name.removesuffix(".md")
                if not name.endswith(".md") or not os.path.isfile(path):
                    continue
                if base != builtin and not TEAM_NAME.fullmatch(stem):
                    misnamed.append(path)
                else:
                    files.setdefault((kind, stem), []).append(path)
    return Team(
        root=root,
        dirs=tuple(dirs),
        files={k: tuple(v) for k, v in files.items()},
        misnamed=tuple(misnamed),
    )


def roles(root: str, team: Team, cfg: Mapping) -> list[str]:
    for role in cfg.get("roles", {}):
        if not team.lookup("roles", role):
            raise _no_charter(role)
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


def index(root: str, role: str, team: Team | None = None) -> dict[str, str]:
    team = _team(root, team)
    if not (charter := team.charter(role)):
        raise _no_charter(role, repo.read_config(os.path.join(root, CONFIG)), team)
    text, rel = charter
    m = INDEX.search(text)
    section = m.group(1).strip() if m else ""
    if not (lines := INDEX_LINE.findall(section)):
        raise ConfigError(
            f"{rel}: no task index (a ## Tasks section of - `<task>` (`<tasks>/<task>.md`): … lines)"
        )
    tasks = {t: what for t, _, what in lines}
    if not (default := INDEX_DEFAULT.search(section.split("\n", 1)[0])):
        raise ConfigError(
            f"{rel}: ## Tasks opens without its default (unsure → `<task>` (`<tasks>/<task>.md`))"
        )
    if default.group(1) not in tasks:
        raise ConfigError(f"{rel}: the default {default.group(1)!r} is not a listed task")
    for t, path in [default.groups(), *((t, path) for t, path, _ in lines)]:
        if path != f"<tasks>/{t}.md":
            raise ConfigError(f"{rel}: {t!r} has path {path!r}, not '<tasks>/{t}.md'")
    own = team.tasks_dir(role) or os.path.join(root, TEXT, "tasks")
    for t in tasks:
        if not (path := team.lookup("tasks", t)):
            raise team.misnamed_error("tasks", t) or ConfigError(
                f"{rel}: lists {t!r} without tasks/{t}.md"
            )
        if os.path.dirname(path) != own:
            raise ConfigError(
                f"{rel}: lists {t!r}, whose file {team.label('tasks', t, path)} is not in "
                f"{team.tasks_dir(role) or 'core team/tasks'}: a role runs only its own tasks"
            )
    return tasks


def load_run(
    root: str,
    role: str,
    task: str | None = None,
    *,
    layers: Sequence[Mapping] = (),
    team: Team | None = None,
) -> RunConfig:
    cfg = repo.read_config(os.path.join(root, CONFIG))
    _check(cfg)
    team = _team(root, team)
    if not team.lookup("roles", role) and role not in cfg.get("roles", {}):
        raise _no_charter(role, cfg, team)
    values = _run_keys(cfg, role)
    run = RunConfig(
        role=role,
        task=task or "",
        tier=values.pop("tier", None),
        effort=values.pop("effort", None),
        output=values.pop("output", {}),
        **values,
    )
    for layer in layers:
        run = replace(run, **_run_keys(layer, role))
    if not (charter := team.charter(role)):
        raise _no_charter(role, cfg, team)
    tasks = index(root, role, team)
    if task and task not in tasks:
        raise team.misnamed_error("tasks", task) or ConfigError(
            f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
        )
    return replace(run, role_title=_title(*charter))


def resolve(
    root: str,
    client: ConfigClient,
    role: str,
    task: str | None = None,
    *,
    overlays: Sequence[Mapping] = (),
    trusted: bool = False,
    label: tuple[str, str] = ("run overlay", "overlay"),
    team: Team | None = None,
) -> RunConfig:
    """Resolve base (including local config), selected client, then caller overlays: later layers win, role values beat
    global values within a layer, and output tables replace rather than merge. The caller must set `trusted` only for
    operator-controlled local configuration: an in-process caller can opt in, so this is an API contract rather than a
    sandbox."""
    overlays = tuple(overlays)
    if not trusted and any(overlay for overlay in overlays):
        raise ConfigError("nonempty run overlays require a trusted caller")
    team = _team(root, team)
    roles = _known(root, team)
    if role not in roles:
        raise _no_charter(role, {}, team)
    client_roles = client.config.get("roles", {})
    if not isinstance(client_roles, Mapping):
        raise ConfigError("selected client roles must be a table")
    named = roles | _team_dir_roles(root)
    for name, table in client_roles.items():
        if name not in named:
            raise ConfigError(f"unknown role {name!r} in selected client")
        if not isinstance(table, Mapping):
            raise ConfigError(f"selected client roles.{name} must be a table")
    source, where = label
    for overlay in overlays:
        try:
            _check_overlay(overlay, roles, where)
        except ConfigError as e:
            raise ConfigError(f"{source}: {e}") from None
    return load_run(root, role, task, layers=(client.config, *overlays), team=team)


def catalog(root: str, team: Team | None = None) -> dict[str, dict[str, str]]:
    team = _team(root, team)
    _roles(root)
    return {
        role: index(root, role, team)
        for role in roles(root, team, repo.read_config(os.path.join(root, CONFIG)))
    }


def _roles(root: str) -> Mapping:
    path = os.path.join(root, CONFIG)
    if not os.path.isfile(path):
        raise ConfigError(f"no core config {path}")
    roles = repo.read_config(path).get("roles")
    if not isinstance(roles, Mapping):
        raise ConfigError(f"malformed [roles] table in {path}")
    for role, table in roles.items():
        if not isinstance(role, str) or not isinstance(table, Mapping):
            raise ConfigError(f"malformed [roles.{role}] table in {path}")
    return roles


def _known(root: str, team: Team) -> set[str]:
    return set(_roles(root)) | {name for kind, name in team.files if kind == "roles"}


def _team_dir_roles(root: str) -> set[str]:
    # by file name only: a core-only resolve (the orchestrator's) still takes the selected client's table for one
    try:
        dirs = team_dirs(root)
    except ConfigError:
        return set()
    names = set()
    for d in dirs:
        with contextlib.suppress(OSError):
            names |= {
                n.removesuffix(".md")
                for n in os.listdir(os.path.join(d, "roles"))
                if n.endswith(".md")
            }
    return names


def _check_overlay(overlay: Mapping, roles: Mapping, where: str) -> None:
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


def render(
    root: str,
    run: RunConfig,
    params: RunParams | None = None,
    *,
    client: PromptClient,
    team: Team | None = None,
    tasks: str | None = None,
) -> str:
    text, team = os.path.join(root, TEXT), _team(root, team)
    names = {
        "role": run.role_title,
        "role_anchor": anchor(run.role_title),
        "language": run.language,
    }
    names |= {"task": f"`{run.task}` (`<tasks>/{run.task}.md`)"} if run.task else {}
    if not (charter := team.charter(run.role)):
        raise ConfigError(f"missing file roles/{run.role}.md")
    role, rel = charter
    guide, principles = _read(text, "guide.md"), _read(text, "principles.md")
    if not run.language:
        principles = _without(principles, "{{language}}")
    texts = {
        "guide.md": fill(guide, names, "guide.md"),
        "principles.md": fill(principles, names, "principles.md"),
        rel: fill(role, {}, rel),
    }
    parts = list(texts.items())
    for name in run.templates:
        body, label = team.read("templates", name)
        f = _fence(body)
        parts.append((label, f"# Template: `templates/{name}.md`\n\n{f}markdown\n{body}{f}\n"))
    output, dest = os.path.join(root, OUTPUT), f"destinations/{run.output['type']}.md"
    if not os.path.isfile(os.path.join(output, dest)):
        raise ConfigError(f"no destination {run.output['type']!r} ({OUTPUT}/{dest})")
    destination, values = _read(output, dest), {}
    if params:
        inside = os.path.relpath(
            os.path.realpath(params.deliverable), os.path.realpath(params.workdir)
        )
        values = {"deliverable": f"<Workdir>/{inside}"}
    else:
        destination = _without(destination, "{{deliverable}}")
    texts |= {
        "output.md": fill(_read(output, "output.md"), {}, "output.md"),
        dest: fill(destination, values, dest),
    }
    parts.append(("output.md", texts["output.md"] + "\n" + texts[dest]))
    if handover := client.handover():
        texts["handover"] = fill(handover, {}, "handover")
        parts.append(("handover", f"## Return\n\n{texts['handover'].strip()}\n"))
    joined = [part for _, part in parts]
    joined.insert(
        1, _parameters(root, run, params, client, texts, team, tasks or team.tasks_dir(run.role))
    )
    prompt = (RESUME if params and params.resume else "") + "\n".join(joined)
    given = params.input.strip() if params else client.inline_input
    own = _unique(prompt + "\n# Input\n") if params or given else _unique(prompt)
    if team.used(run.role, run.templates):
        _check_prompt(team, run, own, dict(parts))
    return own + f"\n{given}\n" if params or given else own


def reference_errors(
    own: str,
    tasks: Mapping[str, str],
    methods: Mapping[str, str],
    *,
    texts: Mapping[str, str] | None = None,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    outline_ = outline(own)
    anchors = {a for _, _, a, _ in outline_ if a}
    entries, titles = {}, {path[-1] for _, path, _, _ in outline_}
    for _, path, a, _ in outline_:
        entries.setdefault(path, set()).add(a)
    inside = {
        (kind, name): {path[1:] for _, path, _, _ in outline(text) if len(path) > 1}
        for kind, files in (("tasks", tasks), ("methods", methods))
        for name, text in files.items()
    }
    named = {"tasks": list(tasks), "methods": []}
    labels = labels or {}
    checked = (
        (dict(texts) if texts is not None else {"prompt": own})
        | {labels.get(t, f"tasks/{t}.md"): x for t, x in tasks.items()}
        | dict(methods)
    )
    errors = []
    for label, text in checked.items():
        for _, line in unfenced(text):
            kept, pos = [], 0
            for m in CODE_SPAN.finditer(line):
                kept.append(line[pos : m.start()] + " ")
                pos = m.end()
                if not (ref := FILE_REF.fullmatch(m[0])) or not line.startswith(" › ", pos):
                    continue
                kind = ref[1]
                names = (
                    [(kind, n) for n in named[kind]]
                    if ref[2] == "<task>"
                    else [(kind, ref[2] if kind == "tasks" else f"methods/{ref[2]}.md")]
                )
                found = names and all(n in inside for n in names)
                best = (
                    _task_path(line[pos:], set.intersection(*(inside[n] for n in names)))
                    if found
                    else None
                )
                if not best or line[pos + len(best) :].startswith(" › "):
                    errors.append(f"{label}: no such {kind[:-1]} file path: {line.strip()}")
                    pos = len(line)
                    break
                pos += len(best)
            left = "".join(kept) + line[pos:]
            for t, target in LINK.findall(left):
                if target.startswith("#"):
                    if target[1:] not in anchors:
                        errors.append(f"{label}: no such anchor: [{t}]({target})")
                    elif " › " in t and target[1:] not in entries.get(tuple(t.split(" › ")), ()):
                        errors.append(
                            f"{label}: not a path of the prompt at that anchor: [{t}]({target})"
                        )
                elif not SCHEME.match(target):
                    errors.append(f"{label}: relative link target: [{t}]({target})")
            plain = LINK.sub(" ", left)
            if "›" in plain or any(name in titles for name in PAREN.findall(plain)):
                errors.append(f"{label}: bare reference: {line.strip()}")
    return errors


def _task_path(after: str, paths) -> str | None:
    best = None
    for path in paths:
        want = " › " + " › ".join(path)
        if (
            after.startswith(want)
            and not re.match(r"\w", after[len(want) :])
            and (not best or len(want) > len(best))
        ):
            best = want
    return best


def _check_prompt(team: Team, run: RunConfig, own: str, parts: Mapping[str, str]) -> None:
    used = team.used(run.role, run.templates)
    paths = {t: team.lookup("tasks", t) for t in index(team.root, run.role, team)}
    tasks = {t: _text(p) for t, p in paths.items()}
    labels = {t: team.label("tasks", t, p) for t, p in paths.items()}
    named = dict.fromkeys(METHOD.findall(own + "".join(tasks.values())))
    methods = {}
    for m in named:
        if os.path.isfile(path := os.path.join(team.root, TEXT, "methods", f"{m}.md")):
            methods[f"methods/{m}.md"] = _text(path)
    errors = reference_errors(own, tasks, methods, texts=parts, labels=labels)
    if not errors:
        return
    custom = [
        label for label in [*parts, *labels.values()] if os.path.isabs(label)
    ]  # built-in ones are relative
    if first := next((e for e in errors if any(e.startswith(f"{c}: ") for c in custom)), None):
        raise ConfigError(first)
    error, blame = errors[0], used[0]
    line = error.split(": ", 2)[-1]
    if ": no such task file path: " in error and "`<tasks>/<task>.md`" in line:
        blame = next(
            (
                paths[t]
                for t in paths
                if team.custom(paths[t])
                and reference_errors(own, {t: tasks[t]}, {}, texts={"x": line})
            ),
            blame,
        )
    elif ": bare reference: " in error:
        want = set(PAREN.findall(line))
        texts = {label: text for label, text in parts.items() if label in custom}
        texts |= {paths[t]: tasks[t] for t in paths if team.custom(paths[t])}
        blame = next(
            (
                label
                for label, text in texts.items()
                if any(heading and path[-1] in want for _, path, _, heading in outline(text))
            ),
            blame,
        )
    raise ConfigError(f"{blame}: breaks a reference in {error}")


def report_command(scripts: str, params: RunParams, interpreter: str = "python3") -> str:
    return (
        f"{shlex.quote(interpreter)} {shlex.quote(os.path.join(scripts, 'report.py'))} "
        f"--to {shlex.quote(params.channel)}"
    )


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
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def unfenced(text: str) -> list[tuple[int, str]]:
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
            out.append(
                (i, tuple(t for _, t in stack) + (re.sub(r"[:.]$", "", m[1]),), section, False)
            )
        last = (i, line)
    return out


def _check(cfg: Mapping) -> None:
    _check_keys(cfg, GLOBAL_KEYS, "the global table")
    for name, client in cfg.get("clients", {}).items():
        for r, table in client.get("roles", {}).items():
            check_old_keys(table, r, f"clients.{name}.")
    for r, table in cfg.get("roles", {}).items():
        check_old_keys(table, r)
        _check_keys(table, RUN_KEYS, f"roles.{r}")


def _team(root: str, given: Team | None) -> Team:
    return given if given is not None else team(root)


def _no_charter(role: str, cfg: Mapping | None = None, team: Team | None = None) -> ConfigError:
    if team is not None and (e := team.misnamed_error("roles", role)):
        return e
    if cfg is not None and role not in cfg.get("roles", {}):
        return ConfigError(f"unknown role {role!r}")
    return ConfigError(f"roles.{role}: no charter roles/{role}.md in core or team_dirs")


def _text(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip() + "\n"
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None


def _run_keys(layer: Mapping, role: str) -> dict:
    return {k: v for k in RUN_KEYS if (v := lookup(layer, role, k)) is not None}


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}")


def _parameters(
    root: str,
    run: RunConfig,
    params: RunParams | None,
    client: PromptClient,
    texts: Mapping[str, str],
    team: Team,
    tasks: str | None = None,
) -> str:
    text = os.path.join(root, TEXT)
    texts = dict(texts) | {
        label: body for body, label in (team.read("tasks", t) for t in index(root, run.role, team))
    }
    named = dict.fromkeys(f"methods/{m}.md" for t in texts.values() for m in METHOD.findall(t))
    texts |= {rel: _read(text, rel) for rel in named if os.path.isfile(os.path.join(text, rel))}
    values = {
        "workdir": _code(os.path.abspath(params.workdir)) if params else client.inline_workdir,
        "scripts": _code(client.scripts_path(root)),
        "tasks": _code(tasks or client.tasks_path(root)),
        "methods": _code(client.methods_path(root)),
        "gate": _code(run.gate or "none"),
        REPORT: _code(
            f"{shlex.quote(client.report_interpreter())} <scripts>/report.py --to <Workdir>/{CHANNEL}"
        )
        if params
        else "",
    }
    values |= {
        f"output.{k}": _code(str(v)) for k, v in ({"host": "github.com"} | run.output).items()
    }
    unset = {
        "workdir": "no run params and the client has no inline_workdir",
        REPORT: "no run params",
    }
    lines = []
    for name, (use, source) in PARAMETERS.items():
        where = next((label for label, t in texts.items() if re.search(use, t)), None)
        if where and not values.get(source):
            raise ConfigError(
                f"{name} is used in {where} but Parameters doesn't define it "
                f"({unset.get(source, f'{source} is not set')})"
            )
        if values.get(source) and (where or name in ("<Workdir>", "<scripts>", "<tasks>")):
            lines.append(f"- `{name}`: {values[source]}\n")
    rule = RULE + (client.approval() or ".")
    return f"# Parameters\n\n{rule}\n\n" + "".join(lines)


def _unique(text: str) -> str:
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
    return "".join(line for line in text.splitlines(True) if placeholder not in line)


def _title(text: str, rel: str) -> str:
    first = text.splitlines()[0] if text.strip() else ""
    if not first.startswith("# "):
        raise ConfigError(f"{rel}: must start with a '# ' heading")
    return first[2:].strip()


def _fence(text: str, least: int = 3) -> str:
    ticks = max([len(r) + 1 for r in re.findall(r"`+", text)] + [least])
    return "`" * ticks


def _code(value: str) -> str:
    f, pad = _fence(value, 1), " " if "`" in value else ""
    return f"{f}{pad}{value}{pad}{f}"
