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
GLOBAL_KEYS = RUN_KEYS | {"roles", "users", "clients", "trusted_dirs", "status_line", "workers_per_column",
                          "grid_retile", "team_dirs"}
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
START = re.compile(rf"^\s*\[{PROGRESS}:start\] \S", re.M)   # a task's start mark line (the root CLAUDE.md › Gotchas)
KINDS = ("roles", "tasks", "templates")   # a team dir's subdirs, in scan order
TEAM_NAME = re.compile(r"[a-z0-9-]+")   # a team dir file's stem
# Section references (reference_errors).
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
FILE_REF = re.compile(r"`<(tasks|methods)>/(<task>|[\w-]+)\.md`")
PAREN = re.compile(r"\(([^()]*)\)")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)
RESUME = ("Resumed agent run after an interruption. These rules are current and may have changed since this session "
          "started. The input below is current: it adds to this session's earlier input. Continue the task this "
          "session already picked or was given, following your task file's `## Resume` section; never pick it "
          "again.\n\n")


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


@dataclass(frozen=True)
class View:
    """A task view: its dir and {task: the source path of its copy}."""
    dir: str
    files: Mapping[str, str]


@dataclass(frozen=True)
class Team:
    """The roles, tasks and templates a run can name: core's team/ (built-in) and the team dirs (custom), found by
    name. `files`: (kind, name) → its paths, built-in first, then the team dirs in order (two or more: a conflict);
    `extensions`: role → its extensions' paths; `misnamed`: custom files whose stem fails TEAM_NAME."""
    root: str
    dirs: tuple[str, ...] = ()
    files: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    extensions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    misnamed: tuple[str, ...] = ()

    def custom(self, path: str) -> bool:
        return not Path(path).is_relative_to(os.path.join(self.root, TEXT))

    def label(self, kind: str, name: str, path: str) -> str:
        return path if self.custom(path) else f"{kind}/{name}.md"

    def source(self, path: str) -> str:
        return path if self.custom(path) else "built-in"

    def lookup(self, kind: str, name: str) -> str | None:
        """The one path of that name, a custom one checked (check_file); None when none."""
        paths = self.files.get((kind, name), ())
        if len(paths) > 1:
            raise ConfigError(f"{paths[1]}: {kind[:-1]} {name!r} is also defined by {paths[0]}; team dir files only add")
        if paths and self.custom(paths[0]):
            self.check_file(kind, paths[0])
        return paths[0] if paths else None

    def read(self, kind: str, name: str) -> tuple[str, str]:
        if not (path := self.lookup(kind, name)):
            raise ConfigError(f"missing file {kind}/{name}.md")
        return _text(path), self.label(kind, name, path)

    def check_file(self, kind: str, path: str) -> None:
        """Raises ConfigError on the first defect of custom file `path`, naming it."""
        text = _text(path)
        if text.split("\n", 1)[0].strip() == "---":
            raise ConfigError(f"{path}: front matter (---) is not read: team dir files hold text only; run keys go in "
                              f"{repo.LOCAL}")
        if m := PLACEHOLDER.search(text):
            raise ConfigError(f"{path}: unfilled placeholder {m[0]}")
        for m in METHOD.findall(text):
            if not os.path.isfile(os.path.join(self.root, TEXT, "methods", f"{m}.md")):
                raise ConfigError(f"{path}: names <methods>/{m}.md, which core has no file for")
        if kind == "roles" and any(path in paths for paths in self.extensions.values()):
            _extension_lines(path, text)
        elif kind == "roles":
            _title(text, path)
        elif kind == "tasks":
            if not text.startswith("# "):
                raise ConfigError(f"{path}: must start with a '# ' heading")
            if (n := len(START.findall(text))) != 1:
                raise ConfigError(f"{path}: has {n} [{PROGRESS}:start] lines, not 1")
            if not any(line.rstrip() == "## Resume" for _, line in unfenced(text)):
                raise ConfigError(f"{path}: has no ## Resume section")

    def parts(self, role: str) -> tuple[str, str, list[tuple[str, str]]] | None:
        """(the charter's text, its label, [(extension path, its task lines)]); None without a charter."""
        if not (path := self.lookup("roles", role)):
            return None
        extensions = []
        for ext in self.extensions.get(role, ()):
            self.check_file("roles", ext)
            extensions.append((ext, _extension_lines(ext, _text(ext))))
        return _text(path), self.label("roles", role, path), extensions

    def charter(self, role: str) -> tuple[str, str] | None:
        """(the charter's text with each extension's task lines appended to its ## Tasks section, its label); None
        without a charter."""
        if not (parts := self.parts(role)):
            return None
        text, label, extensions = parts
        if extensions and (m := INDEX.search(text)):
            at = m.start(1) + len(m.group(1).rstrip())
            text = text[:at] + "".join("\n" + lines for _, lines in extensions) + text[at:]
        return text, label

    def used(self, role: str, templates: Sequence[str] = ()) -> list[str]:
        """The custom paths a run of `role` with `templates` uses: charter, extensions, index tasks, templates."""
        paths = [self.lookup("roles", role), *self.extensions.get(role, ())]
        paths += [self.lookup("tasks", t) for t in index(self.root, role, self)]
        paths += [self.lookup("templates", name) for name in templates]
        return list(dict.fromkeys(p for p in paths if p and self.custom(p)))

    def check_all(self, cfg: Mapping) -> None:
        """Raises ConfigError on the first defect of any team dir file or `[roles.<role>]` without a charter."""
        for path in self.misnamed:
            stem = os.path.basename(path).removesuffix(".md")
            raise ConfigError(f"{path}: name {stem!r}: want {TEAM_NAME.pattern}")
        for kind, name in self.files:
            self.lookup(kind, name)
        for role, paths in self.extensions.items():
            if not self.lookup("roles", role):
                raise ConfigError(f"{paths[0]}: extends role {role!r}, which neither core nor a team dir defines")
            for path in paths:
                self.check_file("roles", path)
        roles(self.root, self, cfg)


def team_dirs(root: str) -> tuple[str, ...]:
    """The real paths of the global `team_dirs` of <root>'s config with repo.LOCAL on top, in order; () when unset.
    ConfigError when config.toml itself sets it or it is malformed."""
    path = os.path.join(root, CONFIG)
    if "team_dirs" in repo.read_config(path, os.devnull):
        raise ConfigError(f"team_dirs: set it in {repo.LOCAL}, not {CONFIG}")
    if (value := repo.read_config(path).get("team_dirs")) is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"team_dirs: want a list of absolute or ~ paths, got {value!r:.80}")
    dirs = []
    for p in value:
        if not isinstance(p, str) or not p.startswith(("/", "~")) or not os.path.isabs(os.path.expanduser(p)):
            raise ConfigError(f"team_dirs: {p!r:.80} is not an absolute or ~ path")
        real = os.path.realpath(os.path.expanduser(p))
        if not os.path.isdir(real):
            raise ConfigError(f"team_dirs: {p} is not a directory")
        if real in dirs:
            raise ConfigError(f"team_dirs: {p} is listed twice")
        dirs.append(real)
    return tuple(dirs)


def team(root: str, dirs: Sequence[str] = ()) -> Team:
    """The Team of core's team/ and `dirs` (real paths, in order): each <kind>/*.md regular file, symlinks followed.
    A custom role file opening with ## Tasks is an extension; a custom file whose stem fails TEAM_NAME is misnamed."""
    files, extensions, misnamed = {}, {}, []
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
                elif base != builtin and kind == "roles" and _opens_index(_text(path)):
                    extensions.setdefault(stem, []).append(path)
                else:
                    files.setdefault((kind, stem), []).append(path)
    return Team(root=root, dirs=tuple(dirs), files={k: tuple(v) for k, v in files.items()},
                extensions={k: tuple(v) for k, v in extensions.items()}, misnamed=tuple(misnamed))


def roles(root: str, team: Team, cfg: Mapping) -> list[str]:
    """Every role: core's charters in config order, then core's others sorted, then each team dir's, sorted.
    ConfigError for a `[roles.<role>]` without a charter."""
    for role in cfg.get("roles", {}):
        if not team.lookup("roles", role):
            raise _no_charter(role)
    charters = {name: paths for (kind, name), paths in team.files.items() if kind == "roles"}
    core = [name for name, paths in charters.items() if not team.custom(paths[0])]
    out = [r for r in cfg.get("roles", {}) if r in core] + sorted(r for r in core if r not in cfg.get("roles", {}))
    for d in team.dirs:
        out += sorted(name for name, paths in charters.items()
                      if name not in out and any(os.path.dirname(os.path.dirname(p)) == d for p in paths))
    return out


def view_files(team: Team, role: str) -> dict[str, str]:
    """{task: its path} of each task of the role's merged index, in order, when its charter or one of them is custom;
    else {} (no task view)."""
    files = {t: team.lookup("tasks", t) for t in index(team.root, role, team)}
    charter = team.lookup("roles", role)
    return files if any(team.custom(p) for p in [charter, *files.values()]) else {}


def index(root: str, role: str, team: Team | None = None) -> dict[str, str]:
    """{task: its description} of the role's task index, its charter's ## Tasks with each extension's lines after it
    (Team.charter), in order: the only list of its tasks. ConfigError unless the charter's opening sentence names a
    listed default, each task's path is <tasks>/<task>.md and the Team has that task's file."""
    team = _team(root, team)
    if not (parts := team.parts(role)):
        raise _no_charter(role, repo.read_config(os.path.join(root, CONFIG)))
    text, rel, extensions = parts
    m = INDEX.search(text)
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
    owner = dict.fromkeys(tasks, rel)
    for ext, ext_lines in extensions:
        for t, _, what in INDEX_LINE.findall(ext_lines):
            if t in tasks:
                raise ConfigError(f"{ext}: lists {t!r}, already in {role}'s index")
            tasks[t], owner[t] = what, ext
    for t in tasks:
        if not team.lookup("tasks", t):
            raise ConfigError(f"{owner[t]}: lists {t!r} without tasks/{t}.md")
    return tasks


def load_run(root: str, role: str, task: str | None = None, *, layers: Sequence[Mapping] = (),
             team: Team | None = None) -> RunConfig:
    """The run config for `role` from config.toml with repo.LOCAL on top, each later layer (same layout) replacing the
    run keys it sets; `task`, when named, must be in the role's index. That config must be valid on its own; each layer
    is checked again once applied. `team`: where its charter and tasks are found (None: core's team/ alone)."""
    cfg = repo.read_config(os.path.join(root, CONFIG))
    _check(cfg)
    team = _team(root, team)
    if role not in cfg.get("roles", {}) and not team.lookup("roles", role):
        raise ConfigError(f"unknown role {role!r}")
    values = _run_keys(cfg, role)
    run = RunConfig(role=role, task=task or "", tier=values.pop("tier", None), effort=values.pop("effort", None),
                    output=values.pop("output", {}), **values)
    for layer in layers:
        run = replace(run, **_run_keys(layer, role))
    if not (charter := team.charter(role)):
        raise _no_charter(role, cfg)
    tasks = index(root, role, team)
    if task and task not in tasks:
        raise ConfigError(f"task {task!r} is not one of {role}'s tasks ({', '.join(tasks)})")
    return replace(run, role_title=_title(*charter))


def render(root: str, run: RunConfig, params: RunParams | None = None, *, client: PromptClient,
           team: Team | None = None, tasks: str | None = None) -> str:
    """The agent run's prompt for `client`: the guide, then # Parameters (_parameters); its Output section ends with the
    client's handover as Output › Return; last, # Input: the input when params are given, else the client's
    inline_input ("" → no section). A client that runs needs params: <Workdir> and `report` are otherwise undefined.
    A repeated heading in the text before the input gets an `<a id>` line (_unique); the input text is verbatim.
    `team` finds the charter, tasks and templates (None: core's team/ alone); `tasks`: <tasks>'s value (None: the
    client's tasks_path). A run using a team dir file has its prompt's references checked (_check_prompt)."""
    text, team = os.path.join(root, TEXT), _team(root, team)
    names = {"role": run.role_title, "role_anchor": anchor(run.role_title), "language": run.language}
    names |= {"task": f"`{run.task}` (`<tasks>/{run.task}.md`)"} if run.task else {}
    if not (charter := team.charter(run.role)):
        raise ConfigError(f"missing file roles/{run.role}.md")
    role, rel = charter
    guide, principles = _read(text, "guide.md"), _read(text, "principles.md")
    if not run.language:
        principles = _without(principles, "{{language}}")
    texts = {"guide.md": fill(guide, names, "guide.md"), "principles.md": fill(principles, names, "principles.md"),
             rel: fill(role, {}, rel)}
    parts = list(texts.items())   # (label, text) of each part joined, # Parameters aside
    for name in run.templates:
        body, label = team.read("templates", name)
        f = _fence(body)
        parts.append((label, f"# Template: `templates/{name}.md`\n\n{f}markdown\n{body}{f}\n"))
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
    parts.append(("output.md", texts["output.md"] + "\n" + texts[dest]))
    if handover := client.handover():
        texts["handover"] = fill(handover, {}, "handover")
        parts.append(("handover", f"## Return\n\n{texts['handover'].strip()}\n"))
    joined = [part for _, part in parts]
    joined.insert(1, _parameters(root, run, params, client, texts, team, tasks))
    prompt = (RESUME if params and params.resume else "") + "\n".join(joined)
    given = params.input.strip() if params else client.inline_input
    prompt = _unique(prompt + "\n# Input\n") + f"\n{given}\n" if params or given else _unique(prompt)
    if team.used(run.role, run.templates):
        _check_prompt(team, run, prompt.split("\n# Input\n", 1)[0] + ("\n# Input\n" if params or given else ""),
                      dict(parts))
    return prompt


def reference_errors(own: str, tasks: Mapping[str, str], methods: Mapping[str, str], *,
                     texts: Mapping[str, str] | None = None, labels: Mapping[str, str] | None = None) -> list[str]:
    """What is wrong with the section references of a prompt's own text `own`, the role's task files `tasks` {task:
    text} and the method files `methods` {label: text} its texts name: each error is `<label>: <what>: <offender>`, the
    label `prompt`, `tasks/<task>.md` or the methods' own. Each unfenced text is checked for (1) every link `[t](#a)`:
    `a` an anchor of `own`, and no relative target (`<scheme>://` is no reference); (2) a link text `A › B`: a path
    of `own` whose anchor is `a`, any one of the entries of that path; (3) a task or method file reference, the code
    span `<tasks>/<task>.md` (a task file of `tasks`, `<task>` every one) or `<methods>/<m>.md` (`methods/<m>.md` of
    `methods`) then ` › ` and a path of each of them, their `# ` title dropped, the longest that matches; (4) no `›`
    left once those, code spans and links are removed; (5) no `(name)` left, `name` the last title of a path of
    `own` (a section named in parentheses, not linked). `texts` {label: text}: the texts checked in place of `own`
    (its anchors and paths still `own`'s); `labels` {task: label}: a task's label in place of `tasks/<task>.md`."""
    outline_ = outline(own)
    anchors = {a for _, _, a, _ in outline_ if a}
    entries, titles = {}, {path[-1] for _, path, _, _ in outline_}
    for _, path, a, _ in outline_:
        entries.setdefault(path, set()).add(a)
    inside = {(kind, name): {path[1:] for _, path, _, _ in outline(text) if len(path) > 1}
              for kind, files in (("tasks", tasks), ("methods", methods)) for name, text in files.items()}
    named = {"tasks": list(tasks), "methods": []}
    labels = labels or {}
    checked = (dict(texts) if texts is not None else {"prompt": own}) | {
        labels.get(t, f"tasks/{t}.md"): x for t, x in tasks.items()} | dict(methods)
    errors = []
    for label, text in checked.items():
        for _, line in unfenced(text):
            kept, pos = [], 0
            for m in CODE_SPAN.finditer(line):
                kept.append(line[pos:m.start()] + " ")
                pos = m.end()
                if not (ref := FILE_REF.fullmatch(m[0])) or not line.startswith(" › ", pos):
                    continue
                kind = ref[1]
                names = [(kind, n) for n in named[kind]] if ref[2] == "<task>" else [
                    (kind, ref[2] if kind == "tasks" else f"methods/{ref[2]}.md")]
                found = names and all(n in inside for n in names)
                best = _task_path(line[pos:], set.intersection(*(inside[n] for n in names))) if found else None
                if not best or line[pos + len(best):].startswith(" › "):
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
                        errors.append(f"{label}: not a path of the prompt at that anchor: [{t}]({target})")
                elif not SCHEME.match(target):
                    errors.append(f"{label}: relative link target: [{t}]({target})")
            plain = LINK.sub(" ", left)
            if "›" in plain or any(name in titles for name in PAREN.findall(plain)):
                errors.append(f"{label}: bare reference: {line.strip()}")
    return errors


def _task_path(after: str, paths) -> str | None:
    """The longest of `paths` (tuples of titles) that `after`, the text following a file's code span, opens with
    ` › `, as that text; a path ending inside a word doesn't count. None when there is none."""
    best = None
    for path in paths:
        want = " › " + " › ".join(path)
        if after.startswith(want) and not re.match(r"\w", after[len(want):]) and (not best or len(want) > len(best)):
            best = want
    return best


def _check_prompt(team: Team, run: RunConfig, own: str, parts: Mapping[str, str]) -> None:
    """Raises ConfigError on the first reference error of a run using team dir files (Team.used): one in a custom text
    as it is, one in core's text naming the custom file that broke it."""
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
    custom = [label for label in [*parts, *labels.values()] if os.path.isabs(label)]   # built-in ones are relative
    if first := next((e for e in errors if any(e.startswith(f"{c}: ") for c in custom)), None):
        raise ConfigError(first)
    error, blame = errors[0], used[0]
    line = error.split(": ", 2)[-1]
    if ": no such task file path: " in error and "`<tasks>/<task>.md`" in line:
        blame = next((paths[t] for t in paths if team.custom(paths[t])
                      and reference_errors(own, {t: tasks[t]}, {}, texts={"x": line})), blame)
    elif ": bare reference: " in error:
        want = set(PAREN.findall(line))
        texts = {label: text for label, text in parts.items() if label in custom}
        texts |= {paths[t]: tasks[t] for t in paths if team.custom(paths[t])}
        blame = next((label for label, text in texts.items()
                      if any(heading and path[-1] in want for _, path, _, heading in outline(text))), blame)
    raise ConfigError(f"{blame}: breaks a reference in {error}")


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


def _check(cfg: Mapping) -> None:
    """Raises ConfigError unless the config's keys, its client's role tables included, are valid."""
    _check_keys(cfg, GLOBAL_KEYS, "the global table")
    for name, client in cfg.get("clients", {}).items():
        for r, table in client.get("roles", {}).items():
            check_old_keys(table, r, f"clients.{name}.")
    for r, table in cfg.get("roles", {}).items():
        check_old_keys(table, r)
        _check_keys(table, RUN_KEYS, f"roles.{r}")


def _team(root: str, given: Team | None) -> Team:
    """`given`, else core's team/ alone."""
    return given if given is not None else team(root)


def _no_charter(role: str, cfg: Mapping | None = None) -> ConfigError:
    """The error for a role without a charter: `[roles.<role>]` set (or `cfg` None) → no charter, else unknown."""
    if cfg is not None and role not in cfg.get("roles", {}):
        return ConfigError(f"unknown role {role!r}")
    return ConfigError(f"roles.{role}: no charter roles/{role}.md in core or team_dirs")


def _text(path: str) -> str:
    """File `path`'s text, stripped, plus a newline; ConfigError naming it when it can't be read."""
    try:
        with open(path) as f:
            return f.read().strip() + "\n"
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None


def _opens_index(text: str) -> bool:
    """Whether `text`, less any front matter, opens with ## Tasks (an extension)."""
    lines = text.strip().split("\n")
    if lines[0].strip() == "---":
        lines = lines[next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), len(lines)) + 1:]
        lines = "\n".join(lines).strip().split("\n")
    return lines[0].rstrip() == "## Tasks"


def _extension_lines(path: str, text: str) -> str:
    """An extension's task lines, joined; ConfigError unless it holds only ## Tasks, blank lines, then task lines."""
    head, _, rest = text.strip().partition("\n")
    lines = rest.strip().split("\n") if rest.strip() else []
    if head.rstrip() != "## Tasks" or not lines or not all(
            (m := INDEX_LINE.fullmatch(line)) and m[2] == f"<tasks>/{m[1]}.md" for line in lines):
        raise ConfigError(f"{path}: an extension holds only a ## Tasks section of - `<task>` (`<tasks>/<task>.md`): … "
                          "lines")
    return "\n".join(lines)


def _run_keys(layer: Mapping, role: str) -> dict:
    return {k: v for k in RUN_KEYS if (v := lookup(layer, role, k)) is not None}


def _check_keys(table: Mapping, allowed: frozenset, where: str) -> None:
    if extra := sorted(set(table) - allowed):
        raise ConfigError(f"unknown key {extra[0]!r} in {where}")


def _parameters(root: str, run: RunConfig, params: RunParams | None, client: PromptClient,
                texts: Mapping[str, str], team: Team, tasks: str | None = None) -> str:
    """The # Parameters section: RULE, then each PARAMETERS name that `texts` ({label: text}), the role's task files or
    the method files these name use, plus <Workdir>, <scripts> and <tasks> even unused, each with its value as inline
    code (<Workdir> without params: the client's inline_workdir, prose). A used name without a value raises ConfigError
    naming the first text using it. `tasks`: <tasks>'s value (None: the client's tasks_path)."""
    text = os.path.join(root, TEXT)
    texts = dict(texts) | {label: body for body, label in (team.read("tasks", t) for t in index(root, run.role, team))}
    named = dict.fromkeys(f"methods/{m}.md" for t in texts.values() for m in METHOD.findall(t))
    texts |= {rel: _read(text, rel) for rel in named if os.path.isfile(os.path.join(text, rel))}
    values = {"workdir": _code(os.path.abspath(params.workdir)) if params else client.inline_workdir,
              "scripts": _code(client.scripts_path(root)), "tasks": _code(tasks or client.tasks_path(root)),
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
        raise ConfigError(f"{rel}: must start with a '# ' heading")
    return first[2:].strip()


def _fence(text: str, least: int = 3) -> str:
    ticks = max([len(r) + 1 for r in re.findall(r"`+", text)] + [least])
    return "`" * ticks


def _code(value: str) -> str:
    """`value` as Markdown inline code."""
    f, pad = _fence(value, 1), " " if "`" in value else ""
    return f"{f}{pad}{value}{pad}{f}"
