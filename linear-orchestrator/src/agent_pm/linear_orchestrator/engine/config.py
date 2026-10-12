"""The Linear orchestrator's config: load() reads linear-orchestrator/config.toml (its local file merged) and the core
config once and returns a frozen Config with a resolved RunConfig per role, or raises ConfigError; plus TUI names and
shared helpers."""

import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

import agent_pm.config
from agent_pm.delegate import composer
from agent_pm.utils import config_file, repos
from agent_pm.utils.paths import temp_dirs, under
from agent_pm.utils.repos import NAME, OWNER
from agent_pm.utils.repos.git import GUARD

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 5))
CONFIG = os.path.join(ROOT, "linear-orchestrator", "config.toml")
LOCAL = "~/.agent-pm/linear-new.local.toml"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")
_ROLE_ISSUE = r"([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)"
TUI = re.compile(_ROLE_ISSUE + r"-[0-9a-f]{8}")

TOP_KEYS = {
    "team",
    "states",
    "human_members",
    "harness_key",
    "task_label_group",
    "task_labels",
    "roles",
    "project_repos",
    "local_clones",
    "core",
    "work_dir",
}
STATES = {
    "todo": "Todo",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "handoff": "Handoff",
    "done": "Done",
    "canceled": "Canceled",
}
ROLE_KEYS = {"account", "key", "next", "require_instructions", "max_runs"}


@dataclass(frozen=True)
class Task:
    kind: Literal["research", "design", "build"]
    prefix: str = ""
    start: str = ""
    done: str = ""
    question: str = ""
    failed: str = ""
    failed_new: str = "in_review"
    retitle: bool = False
    approve: bool = False
    files: bool = False
    hint: str = ""


REPO_HINT = "To change the target repo, edit the description's `Repo:` line."
TASKS = {
    "researcher": Task("research", start="Research started:", failed_new="todo", hint=REPO_HINT),
    "pm": Task("design", prefix="PRD", start="PRD started:", retitle=True, approve=True),
    "engineer": Task(
        "build",
        prefix="ENG",
        start="Build started:",
        done="Build ready:",
        question="Question:",
        failed="Build failed:",
        files=True,
    ),
}

# Core's checkout dirs under an agent run's workdir: config commands' `--dir {{workdir}}/src`, the github destination's
# `<Workdir>/publish`.
CLONES = ("src", "publish")


@dataclass(frozen=True)
class Role:
    account: str
    key: str
    tasks: tuple[str, ...]
    max_runs: int
    next: str | None
    require_instructions: bool
    run: composer.RunConfig


@dataclass(frozen=True)
class Docs:
    repo: str
    branch: str
    dirs: Mapping[str, str]


@dataclass(frozen=True)
class Config:
    team: str
    states: Mapping[str, str]
    human_members: tuple[str, ...]
    harness_key: str
    task_label_group: str
    task_labels: Mapping[str, str]
    roles: Mapping[str, Role]
    project_repos: Mapping[str, str]
    local_clones: Mapping[str, str]
    work_dir: str
    docs: Docs
    stages: Mapping[str, int]

    @property
    def runs_dir(self) -> str:
        return os.path.join(self.work_dir, "work")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.work_dir, "logs")

    @property
    def runs_log(self) -> str:
        return os.path.join(self.logs_dir, "runs.jsonl")

    def run_dir(self, issue: str) -> str:
        return os.path.join(self.runs_dir, issue)


class ConfigError(SystemExit):
    def __init__(self, msg: str, logs_dir: str | None = None):
        super().__init__(msg)
        self.msg, self.logs_dir = msg, logs_dir


def load(
    root: str = ROOT,
    *,
    path: str = CONFIG,
    local: str = LOCAL,
    core: str = agent_pm.config.CONFIG,
    core_local: str = agent_pm.config.LOCAL,
) -> Config:
    try:
        cfg = config_file.read_config(path, local)
    except (OSError, ValueError) as e:
        raise ConfigError(f"linear-orchestrator/config.toml: {e}") from None
    work = _work_dir(cfg, root)
    try:
        return _load(cfg, root, work, local, core, core_local)
    except ConfigError as e:
        e.logs_dir = os.path.join(work, "logs")
        raise


def session(role, issue):
    return f"agent-pm-{role}-{issue}"


def tui_prefix(role, issue):
    name = f"{role}-{issue}"
    if not re.fullmatch(_ROLE_ISSUE, name):
        raise ValueError(f"TUI session prefix {name!r}: want {_ROLE_ISSUE}")
    return name + "-"


def tui_name(role, issue, sid):
    return tui_prefix(role, issue) + sid[:8]


def tui_issue(name):
    m = TUI.fullmatch(name)
    return m.groups() if m else None


def path():
    """The PATH run, promote and their tools get, read at call: AGENT_PM_PATH (if set) before the default dirs."""
    default = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"
    front = os.environ.get("AGENT_PM_PATH")
    return f"{front}:{default}" if front else default


def sh_run(argv, timeout):
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PATH": path()},
    )


def repo_slug(value):
    m = re.fullmatch(rf"({OWNER})/({NAME})", value) if isinstance(value, str) else None
    return m.groups() if m and m.group(2) not in (".", "..") else None


def role_for(roles, email):
    return next(
        (r for r, role in roles.items() if email and role.account.lower() == email.lower()), None
    )


def writable(work):
    return (work, *temp_dirs())


def clone_error(path, slug, *, run=sh_run):
    if not path.isprintable():
        return f"{path!r} has a non-printable character"
    if not os.path.exists(path):
        return f"{path} does not exist"
    try:
        found, _ = repos.resolve(
            path, run=lambda argv, timeout: run(["git", *GUARD, *argv[1:]], timeout)
        )
    except repos.Invalid as e:
        return str(e)
    except (
        OSError,
        ValueError,
        subprocess.TimeoutExpired,
    ) as e:  # UnicodeDecodeError, from non-UTF-8 git output, is a ValueError
        return f"git: {e}"
    if found.host != "github.com" or f"{found.owner}/{found.name}".lower() != slug.lower():
        return f"origin is {found.slug}, not github.com/{slug}"
    return None


def _overlap(a, b):
    return os.path.commonpath([a, b]) in (a, b)


def _work_dir(cfg, root, clones=None):
    real = os.path.realpath(root)
    v = cfg.get("work_dir", "~/.agent-pm")
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if not os.path.isabs(path):
        raise ConfigError(
            f"linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: {v!r}"
        )
    path = os.path.realpath(path)
    if _overlap(path, real):
        raise ConfigError(
            f"linear-orchestrator/config.toml: work_dir must neither lie in nor contain the repo {real}: {v!r}"
        )
    for k, clone in (clones or {}).items():
        if _overlap(path, clone):
            raise ConfigError(
                f"linear-orchestrator/config.toml: work_dir must neither lie in nor contain local_clones.{k} {clone}: {v!r}"
            )
    return path


def _load(cfg, root, work, local, core, core_local):
    _load_config(cfg, root, local)
    roles = _runnable(cfg, root, work, core, core_local)
    docs = _docs(roles, core_local)
    frozen = {
        k: MappingProxyType(dict(cfg[k]))
        for k in ("states", "task_labels", "project_repos", "local_clones")
    }
    return Config(
        team=cfg["team"],
        human_members=tuple(cfg.get("human_members") or ()),
        harness_key=cfg["harness_key"],
        task_label_group=cfg["task_label_group"],
        roles=MappingProxyType(roles),
        work_dir=work,
        docs=docs,
        stages=MappingProxyType(_stage_order(cfg["roles"])),
        **frozen,
    )


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(cfg):
    if not _uuid(cfg.get("team")):
        raise ConfigError(
            f"linear-orchestrator/config.toml: team must be a Linear team id (UUID): {cfg.get('team')!r}"
        )
    states = cfg["states"]
    if extra := sorted(set(states) - set(STATES)):
        raise ConfigError(
            f"linear-orchestrator/config.toml: [states] has unknown keys: {', '.join(extra)}"
        )
    for k in STATES:
        if not _uuid(states[k]):
            raise ConfigError(
                f"linear-orchestrator/config.toml: states.{k} must be a Linear workflow state id (UUID): {states[k]!r}"
            )
    if not _uuid(cfg.get("task_label_group")):
        raise ConfigError(
            f"linear-orchestrator/config.toml: task_label_group must be a Linear label group id (UUID): {cfg.get('task_label_group')!r}"
        )


def _missing(cfg):
    missing = [k for k in ("team", "harness_key", "task_label_group") if k not in cfg]
    states = cfg.get("states")
    missing += [f"states.{k}" for k in STATES if not isinstance(states, dict) or k not in states]
    roles = cfg.get("roles")
    for name, p in roles.items() if isinstance(roles, dict) else ():
        missing += [
            f"roles.{name}.{k}" for k in ("account", "key") if isinstance(p, dict) and k not in p
        ]
    return missing


def _load_config(cfg, root, local):
    if missing := _missing(cfg):
        raise ConfigError(
            f"linear-orchestrator/config.toml: missing {', '.join(missing)}: set them in {local} "
            "(linear-orchestrator/config.toml's `# local:` lines)"
        )
    _check_ids(cfg)
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise ConfigError(
            f"linear-orchestrator/config.toml: harness_key must be a Keychain service name: {hk!r}"
        )
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise ConfigError(f"linear-orchestrator/config.toml: unknown keys: {', '.join(extra)}")
    roles = cfg.setdefault("roles", {})
    for name, p in roles.items():
        if extra := sorted(set(p) - ROLE_KEYS):
            raise ConfigError(
                f"linear-orchestrator/config.toml: [roles.{name}] has unknown keys: {', '.join(extra)}"
            )
        for k, what in (("account", "the role's Linear email"), ("key", "a Keychain service name")):
            if not isinstance(p.get(k), str) or not p[k]:
                raise ConfigError(
                    f"linear-orchestrator/config.toml: [roles.{name}] {k} must be {what}: {p.get(k)!r}"
                )
        n = p.get("max_runs", 1)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(
                f"linear-orchestrator/config.toml: [roles.{name}] max_runs must be a whole number >= 1: {n!r}"
            )
    for name in roles:
        nxt, seen = roles[name].get("next"), {name}
        while nxt:
            if nxt in seen:
                raise ConfigError(
                    f"linear-orchestrator/config.toml: the next chain from {name!r} has a cycle"
                )
            seen.add(nxt)
            nxt = roles.get(nxt, {}).get("next")
    projects = cfg.setdefault("project_repos", {})
    if not isinstance(projects, dict):
        raise ConfigError(
            'linear-orchestrator/config.toml: project_repos must be a table of "<Linear project id>" = "<owner>/<name>"'
        )
    for k, v in projects.items():
        if not _uuid(k) or not repo_slug(v):
            raise ConfigError(
                f"linear-orchestrator/config.toml: project_repos.{k} must map a Linear project id (UUID) to <owner>/<name>: {v!r}"
            )
    clones = cfg.setdefault("local_clones", {})
    if not isinstance(clones, dict):
        raise ConfigError(
            'linear-orchestrator/config.toml: local_clones must be a table of "<owner>/<name>" = "<path>"'
        )
    seen = {}
    for k, v in clones.items():
        if (
            not repo_slug(k)
            or not isinstance(v, str)
            or not v.startswith(("/", "~"))
            or not v.isprintable()
        ):
            raise ConfigError(
                f"linear-orchestrator/config.toml: local_clones.{k} must map <owner>/<name> to a printable path starting with / or ~: {v!r}"
            )
        if k.lower() in seen:
            raise ConfigError(
                f"linear-orchestrator/config.toml: local_clones.{seen[k.lower()]} and local_clones.{k} are the same repo"
            )
        seen[k.lower()] = k
        clones[k] = os.path.realpath(os.path.expanduser(v))
    _work_dir(cfg, root, clones)
    labels = cfg.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise ConfigError(
            'linear-orchestrator/config.toml: task_labels must be a table of <task> = "<Linear label id>"'
        )
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise ConfigError(
                f"linear-orchestrator/config.toml: task_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"linear-orchestrator/config.toml: task_labels.{seen[v]} and task_labels.{k} have the same label id {v}"
            )
        seen[v] = k


def _fill_root(value, root):
    if isinstance(value, str):
        return value.replace("{{root}}", shlex.quote(root))
    if isinstance(value, dict):
        return {k: _fill_root(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_root(v, root) for v in value]
    return value


def _runnable(cfg, root, work, core, core_local):
    try:
        core_roles = config_file.read_config(core, core_local).get("roles", {})
        trusted = composer.trusted_dirs(config=core, local=core_local)
    except (OSError, ValueError, composer.ConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for d in sorted(trusted):
        if under(work, [d]):
            raise ConfigError(f"core: trusted_dirs: {d} is or contains work_dir {work}")
    overlay, out = _fill_root(cfg.get("core", {}), root), {}
    for name, p in cfg["roles"].items():
        if name not in core_roles:
            raise ConfigError(
                f"linear-orchestrator/config.toml: role {name!r} is not in core/config.toml"
            )
        if name not in TASKS:
            raise ConfigError(
                f"linear-orchestrator/config.toml: role {name!r} has no entry in config.TASKS"
            )
        try:
            run = composer.resolve(
                name,
                None,
                overlays=[overlay],
                label=("linear-orchestrator/config.toml", "core"),
                config=core,
                local=core_local,
            )
            tasks = tuple(composer.index(name, config=core, local=core_local))
        except composer.ConfigError as e:
            raise ConfigError(f"core: {e}") from None
        out[name] = Role(
            p["account"],
            p["key"],
            tasks,
            p.get("max_runs", 1),
            p.get("next") or None,
            p.get("require_instructions", True),
            run,
        )
    owner = {}
    for name, r in out.items():
        if r.key == cfg["harness_key"]:
            raise ConfigError(
                f"linear-orchestrator/config.toml: [roles.{name}] key {r.key!r} is harness_key"
            )
        if r.key in owner:
            raise ConfigError(
                f"linear-orchestrator/config.toml: [roles.{name}] key {r.key!r} is also [roles.{owner[r.key]}]'s"
            )
        owner[r.key] = name
    for name, r in out.items():
        if not r.next:
            continue
        if r.next not in out:
            raise ConfigError(
                f"linear-orchestrator/config.toml: next of {name!r} names undefined role {r.next!r}"
            )
        if not TASKS[r.next].prefix:
            raise ConfigError(
                f"linear-orchestrator/config.toml: next of {name!r} is role {r.next!r}, whose config.TASKS entry has no prefix"
            )
    tasks = {t for r in out.values() for t in r.tasks}
    for task in cfg["task_labels"]:
        if task not in tasks:
            raise ConfigError(
                f"linear-orchestrator/config.toml: task_labels.{task} is not a task of a role in linear-orchestrator/config.toml"
            )
    for key, path in cfg["local_clones"].items():
        if reason := clone_error(path, key):
            raise ConfigError(f"linear-orchestrator/config.toml: local_clones.{key}: {reason}")
    return out


def _docs(roles, core_local):
    outs = {name: o for name, r in roles.items() if (o := r.run.output)["type"] == "github"}
    targets = {(o.get("repo"), o.get("branch"), o.get("host", "github.com")) for o in outs.values()}
    name, branch, host = targets.pop() if len(targets) == 1 else (None, None, None)
    if not name or not branch or host != "github.com":
        raise ConfigError(
            "core: document roles must publish to one github.com repo and branch (their [output] in "
            f"{core_local})"
        )
    return Docs(name, branch, MappingProxyType({r: o["dir"] for r, o in outs.items()}))


def _stage_order(roles):
    targets = {p["next"] for p in roles.values() if p.get("next")}
    order = {name: 0 for name in roles}
    for start in roles:
        if start in targets:
            continue
        name, i = start, 0
        while name:
            order[name] = i
            name, i = roles.get(name, {}).get("next"), i + 1
    return order
