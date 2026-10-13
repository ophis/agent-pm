"""The Linear orchestrator's config: load() reads its own keys once (linear-
orchestrator/config.toml with the keys of ~/.agent-pm/config.local.toml it owns: TOP_KEYS, and
ROLE_KEYS in a [roles.<role>], a table the delegate's config shares) and the delegate's,
constructs a caller-owned Driver and returns a frozen Config with a resolved RunConfig per
role, or raises ConfigError; plus TUI names and shared helpers."""

import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from agent_pm import delegate, utils
from agent_pm.linear_orchestrator.engine.types import ConfigError, TuiError
from agent_pm.session import Driver
from agent_pm.utils import GUARD, NAME, OWNER, STATE, import_roots, temp_dirs, under

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 5))
CONFIG = os.path.join(ROOT, "linear-orchestrator", "config.toml")
LOCAL = utils.LOCAL

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
REQUIRED = (
    "team",
    "harness_key",
    "task_label_group",
    *(f"states.{k}" for k in STATES),
    "roles.*.account",
    "roles.*.key",
)


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

# Core's checkout dirs under an agent run's workdir: config commands' `--dir {{workdir}}/src`, the
# github destination's
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
    run: delegate.RunConfig


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
    driver: Driver | None = None

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


def load(
    root: str = ROOT,
    *,
    path: str = CONFIG,
    local: str = LOCAL,
    core: str = delegate.CONFIG,
) -> Config:
    try:
        cfg = utils.read_project("linear-orchestrator", path=path, local=local, own=True)
    except (OSError, ValueError) as e:
        raise ConfigError.read_failed(e) from None
    work = _work_dir(cfg, root)
    try:
        found, core_cfg = _load(cfg, root, work, local, core)
        try:
            driver = delegate.configure(local=local, cfg=core_cfg)
        except delegate.ConfigError as e:
            raise ConfigError(str(e)) from None
        return replace(found, driver=driver)
    except ConfigError as e:
        e.logs_dir = os.path.join(work, "logs")
        raise


def session(role, issue):
    return f"agent-pm-{role}-{issue}"


def tui_prefix(role, issue):
    name = f"{role}-{issue}"
    if not re.fullmatch(_ROLE_ISSUE, name):
        raise TuiError.tui_session_prefix_want(name, _ROLE_ISSUE)
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
        found, _ = utils.resolve(
            path, run=lambda argv, timeout: run(["git", *GUARD, *argv[1:]], timeout)
        )
    except utils.Invalid as e:
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
    v = cfg.get("work_dir", STATE)
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if not os.path.isabs(path):
        raise ConfigError.invalid_work_path(v)
    path = os.path.realpath(path)
    if _overlap(path, real):
        raise ConfigError.work_overlaps_repo(real, v)
    for k, clone in (clones or {}).items():
        if _overlap(path, clone):
            raise ConfigError(
                f"linear-orchestrator/config.toml: work_dir must neither lie in nor contain local_clones.{k} {clone}: {v!r}"
            )
    return path


def _load(cfg, root, work, local, core):
    _load_config(cfg, root, local)
    try:
        core_cfg = utils.read_project("delegate", path=core, local=local)
    except (OSError, ValueError) as e:
        raise ConfigError.core(e) from None
    roles = _runnable(cfg, root, work, core, core_cfg, local)
    docs = _docs(roles, local)
    frozen = {
        k: MappingProxyType(dict(cfg[k]))
        for k in ("states", "task_labels", "project_repos", "local_clones")
    }
    found = Config(
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
    return found, core_cfg


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(cfg):
    if not _uuid(cfg.get("team")):
        raise ConfigError.invalid_team_id(cfg.get("team"))
    states = cfg["states"]
    if extra := sorted(set(states) - set(STATES)):
        raise ConfigError.unknown_states(", ".join(extra))
    for k in STATES:
        if not _uuid(states[k]):
            raise ConfigError.invalid_state_id(k, states[k])
    if not _uuid(cfg.get("task_label_group")):
        raise ConfigError.invalid_label_group_id(cfg.get("task_label_group"))


def _load_config(cfg, root, local):
    if missing := utils.unset(cfg, REQUIRED):
        raise ConfigError.missing_keys(", ".join(missing), local)
    _check_ids(cfg)
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise ConfigError.invalid_harness_key(hk)
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise ConfigError.unknown_keys(", ".join(extra))
    roles = cfg.setdefault("roles", {})
    for name, p in roles.items():
        if extra := sorted(set(p) - ROLE_KEYS):
            raise ConfigError.unknown_role_keys(name, ", ".join(extra))
        for k, what in (("account", "the role's Linear email"), ("key", "a Keychain service name")):
            if not isinstance(p.get(k), str) or not p[k]:
                raise ConfigError.invalid_role_field(name, k, what, p.get(k))
        n = p.get("max_runs", 1)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError.invalid_max_runs(name, n)
    for name in roles:
        nxt, seen = roles[name].get("next"), {name}
        while nxt:
            if nxt in seen:
                raise ConfigError.next_cycle(name)
            seen.add(nxt)
            nxt = roles.get(nxt, {}).get("next")
    projects = cfg.setdefault("project_repos", {})
    if not isinstance(projects, dict):
        raise ConfigError.invalid_projects_table()
    for k, v in projects.items():
        if not _uuid(k) or not repo_slug(v):
            raise ConfigError.invalid_project_repo(k, v)
    clones = cfg.setdefault("local_clones", {})
    if not isinstance(clones, dict):
        raise ConfigError.invalid_clones_table()
    seen = {}
    for k, v in clones.items():
        if (
            not repo_slug(k)
            or not isinstance(v, str)
            or not v.startswith(("/", "~"))
            or not v.isprintable()
        ):
            raise ConfigError.invalid_local_clone(k, v)
        if k.lower() in seen:
            raise ConfigError.duplicate_clone(seen[k.lower()], k)
        seen[k.lower()] = k
        clones[k] = os.path.realpath(os.path.expanduser(v))
    _work_dir(cfg, root, clones)
    labels = cfg.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise ConfigError.invalid_labels_table()
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise ConfigError.invalid_task_label(k, v)
        if v in seen:
            raise ConfigError.duplicate_label(seen[v], k, v)
        seen[v] = k


def _fill_root(value, root):
    if isinstance(value, str):
        value = value.replace("{{root}}", shlex.quote(root))
        if "{{pythonpath}}" in value:
            roots = shlex.quote(os.pathsep.join(import_roots(root)))
            value = value.replace("{{pythonpath}}", roots)
        return value
    if isinstance(value, dict):
        return {k: _fill_root(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_root(v, root) for v in value]
    return value


def _runnable(cfg, root, work, core, core_cfg, local):
    try:
        core_roles = core_cfg.get("roles", {})
        trusted = delegate.trusted_dirs(core, local, cfg=core_cfg)
    except (OSError, ValueError, delegate.ComposerConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for d in sorted(trusted):
        if under(work, [d]):
            raise ConfigError.work_trusted(d, work)
    overlay, out = _fill_root(cfg.get("core", {}), root), {}
    for name, p in cfg["roles"].items():
        if name not in core_roles:
            raise ConfigError.undefined_delegate_role(name)
        if name not in TASKS:
            raise ConfigError.undefined_task_role(name)
        try:
            run = delegate.resolve_run(
                name,
                None,
                overlays=[overlay],
                label=("linear-orchestrator/config.toml", "core"),
                config=core,
                local=local,
                cfg=core_cfg,
            )
            tasks = tuple(delegate.task_index(name, config=core, local=local, cfg=core_cfg))
        except delegate.ComposerConfigError as e:
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
            raise ConfigError.shared_harness_key(name, r.key)
        if r.key in owner:
            raise ConfigError.shared_role_key(name, r.key, owner[r.key])
        owner[r.key] = name
    for name, r in out.items():
        if not r.next:
            continue
        if r.next not in out:
            raise ConfigError.undefined_next_role(name, r.next)
        if not TASKS[r.next].prefix:
            raise ConfigError.next_without_prefix(name, r.next)
    tasks = {t for r in out.values() for t in r.tasks}
    for task in cfg["task_labels"]:
        if task not in tasks:
            raise ConfigError.undefined_task(task)
    for key, path in cfg["local_clones"].items():
        if reason := clone_error(path, key):
            raise ConfigError.local_clones(key, reason)
    return out


def _docs(roles, local):
    outs = {name: o for name, r in roles.items() if (o := r.run.output)["type"] == "github"}
    targets = {(o.get("repo"), o.get("branch"), o.get("host", "github.com")) for o in outs.values()}
    name, branch, host = targets.pop() if len(targets) == 1 else (None, None, None)
    if not name or not branch or host != "github.com":
        raise ConfigError.inconsistent_docs_destination(local)
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
