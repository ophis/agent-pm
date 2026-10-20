"""Packaged orchestrator settings, registered Delegates and resolved role runs."""

import os
import re
import subprocess
import sys
from collections.abc import Mapping
from types import MappingProxyType

import agent_pm.utils as utils
from agent_pm import delegate
from agent_pm.orchestrator.engine import types
from agent_pm.orchestrator.engine.flow import workflow
from agent_pm.orchestrator.engine.log import config_error
from agent_pm.orchestrator.engine.types import (
    UUID_RE,
    Config,
    ConfigError,
    Docs,
    LinearRole,
    LinearSettings,
    Role,
    State,
    TrackerKind,
)
from agent_pm.utils import GUARD, STATE, temp_dirs, under

LOCAL = utils.LOCAL

REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")
_ROLE_ISSUE = r"([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)"

BOARD_ENV = "AGENT_PM_BOARD_DIR"

TOP_KEYS = {
    "tracker",
    "workflow",
    "task_tiers",
    "client_max_runs",
    "roles",
    "local_clones",
    "core",
    "work_dir",
    "delegates",
    "linear",
}
LINEAR_KEYS = {
    "team",
    "states",
    "human_members",
    "harness_key",
    "task_label_group",
    "task_labels",
    "agent_label_group",
    "agent_labels",
    "roles",
    "project_repos",
}
ROLE_KEYS = {"max_runs"}
LINEAR_ROLE_KEYS = {"account", "key"}
REQUIRED = (
    "linear.team",
    "linear.harness_key",
    "linear.task_label_group",
    *(f"linear.states.{k}" for k in State),
    "linear.roles.*.account",
    "linear.roles.*.key",
)


TASKS = workflow.DEFAULT


def report_error(src: str, error: ConfigError, *, dry: bool = False) -> None:
    if not error.logs_dir:
        print(error.msg, file=sys.stderr)
    else:
        config_error(src, error, dry=dry)


def linear_settings(cfg: Config, command: str) -> LinearSettings:
    """cfg's Linear settings; raises ConfigError, command's refusal, on another tracker."""
    if cfg.linear is None:
        raise ConfigError(f'{command} needs [orchestrator] tracker = "linear"')
    return cfg.linear


def load(*, path: str | None = None) -> Config:
    try:
        cfg = utils.read_resources(
            (("agent_pm.orchestrator", "config.toml"),),
            paths={"agent_pm.orchestrator": path} if path is not None else None,
        )
    except (OSError, ValueError) as e:
        raise ConfigError(f"orchestrator/config.toml: {e}") from None
    cfg["tracker"] = _tracker(cfg)
    board = _board() if cfg["tracker"] is TrackerKind.LOCAL else None
    work = _work_dir(cfg, board=board)
    try:
        _workflow(cfg)
        kind, delegates = _delegates(cfg)
        index = _index(delegates[kind])
        _load_config(cfg, LOCAL, board)
        return _load(cfg, work, kind, delegates, index, board=board)
    except ConfigError as e:
        e.logs_dir = os.path.join(work, "logs")
        raise


def _workflow(cfg):
    """Load TASKS from workflow: "" → the packaged file; else that file, whole."""
    v = cfg.get("workflow", "")
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if v != "" and not os.path.isabs(path):
        raise ConfigError(
            f'orchestrator/config.toml: workflow must be "" (the packaged workflow) or a printable absolute or ~ path: {v!r}'
        )
    try:
        TASKS.load(path or None)
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _index(delegate_cls):
    """The Delegate's role → task index; every stage's role must be in it."""
    try:
        index = delegate_cls.roles()
    except (OSError, ValueError, delegate.ConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for name in TASKS:
        if name not in index:
            raise ConfigError(
                f"{TASKS.path}: stage {name}: not a role the Delegate knows (its charters: core and [delegate] team_dirs)"
            )
    return index


def tui_prefix(role, issue):
    name = f"{role}-{issue}"
    if not re.fullmatch(_ROLE_ISSUE, name):
        raise ValueError(f"TUI session prefix {name!r}: want {_ROLE_ISSUE}")
    return name + "-"


def tui_name(role, issue, sid):
    return tui_prefix(role, issue) + sid[:8]


def path():
    """The PATH the router, promote and their tools and runs get, read at call: AGENT_PM_PATH (if
    set), this installation's bin/ (agent-pm-orchestrator, the harness's write-back callback), then the
    default dirs.
    """
    [root] = utils.module_roots(types)
    default = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"
    found = os.path.join(os.path.dirname(os.path.dirname(root)), "bin")
    return ":".join(p for p in (os.environ.get("AGENT_PM_PATH"), found, default) if p)


def shell(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return utils.sh(argv, timeout, env={**os.environ, "PATH": path()})


def role_for(roles: Mapping[str, LinearRole], email):
    return next(
        (r for r, role in roles.items() if email and role.account.lower() == email.lower()), None
    )


def writable(work):
    return (work, *temp_dirs())


def clone_error(clone, slug, *, run=None):
    run = run or shell
    if not clone.isprintable():
        return f"{clone!r} has a non-printable character"
    if not os.path.exists(clone):
        return f"{clone} does not exist"
    try:
        found, _ = utils.resolve(
            clone, run=lambda argv, timeout: run(["git", *GUARD, *argv[1:]], timeout)
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


def _tracker(cfg):
    v = cfg.get("tracker", TrackerKind.LOCAL)
    if v not in tuple(TrackerKind):
        raise ConfigError(
            f"orchestrator/config.toml: tracker: want one of {', '.join(TrackerKind)}: {v!r}"
        )
    return TrackerKind(v)


def _board():
    v = os.environ.get(BOARD_ENV) or os.path.join(STATE, "board")
    path = os.path.expanduser(v) if v.isprintable() else ""
    if not os.path.isabs(path):
        raise ConfigError(f"{BOARD_ENV} must be a printable absolute or ~ path: {v!r}")
    return os.path.realpath(path)


def _work_dir(cfg, clones=None, *, board=None):
    """The state root: work_dir, or a local board's <board>/.state."""
    name, v = "work_dir", cfg.get("work_dir", STATE)
    if board is not None:
        name, v = "the board's .state", os.path.join(board, ".state")
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if not os.path.isabs(path):
        raise ConfigError(
            f"orchestrator/config.toml: work_dir must be a printable absolute or ~ path: {v!r}"
        )
    path = os.path.realpath(path)
    try:
        for k, clone in (clones or {}).items():
            if utils.overlaps(path, clone):
                raise ConfigError(
                    f"orchestrator/config.toml: {name} must neither lie in nor contain local_clones.{k} {clone}: {v!r}"
                )
    except OSError as error:
        raise ConfigError(f"orchestrator/config.toml: {name}: {error}") from None
    return path


def _load(cfg, work, kind, delegates, index, *, board):
    roles = _runnable(cfg, work, delegates[kind], index)
    docs = _docs(roles, LOCAL)
    frozen = {
        k: MappingProxyType(dict(cfg[k])) for k in ("task_tiers", "client_max_runs", "local_clones")
    }
    return Config(
        roles=MappingProxyType(roles),
        tracker=cfg["tracker"],
        linear=_linear_settings(cfg["linear"]) if cfg["tracker"] is TrackerKind.LINEAR else None,
        board=board,
        work_dir=work,
        docs=docs,
        stages=MappingProxyType(_stage_order()),
        task_tiers=frozen["task_tiers"],
        client_max_runs=frozen["client_max_runs"],
        local_clones=frozen["local_clones"],
        delegate_kind=kind,
        delegates=MappingProxyType(delegates),
        core=utils.freeze(cfg.get("core", {})),
    )


def _linear_settings(linear):
    frozen = {
        k: MappingProxyType(dict(linear[k]))
        for k in ("states", "task_labels", "agent_labels", "project_repos")
    }
    return LinearSettings(
        team=linear["team"],
        human_members=tuple(linear.get("human_members") or ()),
        harness_key=linear["harness_key"],
        task_label_group=linear["task_label_group"],
        agent_label_group=linear["agent_label_group"],
        roles=MappingProxyType(
            {name: LinearRole(p["account"], p["key"]) for name, p in linear["roles"].items()}
        ),
        **frozen,
    )


def _delegates(cfg) -> tuple[str, dict[str, type]]:
    registry = cfg.get("delegates")
    if not isinstance(registry, dict):
        raise ConfigError("orchestrator/config.toml: [delegates] must be a table")
    classes = {}
    for kind, entry in registry.items():
        if kind == "default":
            continue
        if not isinstance(entry, dict) or set(entry) != {"implementation"}:
            raise ConfigError(
                f"orchestrator/config.toml: [delegates.{kind}] takes only implementation"
            )
        try:
            classes[kind] = utils.load_class(entry["implementation"], delegate.Delegate)
        except ValueError as error:
            raise ConfigError(f"orchestrator/config.toml: [delegates.{kind}]: {error}") from None
    default = registry.get("default")
    if not isinstance(default, str) or default not in classes:
        raise ConfigError(
            f"orchestrator/config.toml: [delegates] default: want one of {', '.join(sorted(classes))}"
        )
    return default, classes


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(linear):
    if not _uuid(linear.get("team")):
        raise ConfigError(
            f"orchestrator/config.toml: linear.team must be a Linear team id (UUID): {linear.get('team')!r}"
        )
    states = linear["states"]
    if extra := sorted(set(states) - set(State)):
        raise ConfigError(
            f"orchestrator/config.toml: [linear.states] has unknown keys: {', '.join(extra)}"
        )
    for k in State:
        if not _uuid(states[k]):
            raise ConfigError(
                f"orchestrator/config.toml: linear.states.{k} must be a Linear workflow state id (UUID): {states[k]!r}"
            )
    if not _uuid(linear.get("task_label_group")):
        raise ConfigError(
            f"orchestrator/config.toml: linear.task_label_group must be a Linear label group id (UUID): {linear.get('task_label_group')!r}"
        )


def _load_config(cfg, local, board):
    linear = cfg.setdefault("linear", {})
    if not isinstance(linear, dict):
        raise ConfigError("orchestrator/config.toml: [linear] must be a table")
    accounts = linear.setdefault("roles", {})
    if not isinstance(accounts, dict):
        raise ConfigError("orchestrator/config.toml: [linear.roles] must be a table")
    roles = _stage_roles(cfg, linear)
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise ConfigError(f"orchestrator/config.toml: unknown keys: {', '.join(extra)}")
    if extra := sorted(set(linear) - LINEAR_KEYS):
        raise ConfigError(f"orchestrator/config.toml: unknown keys in [linear]: {', '.join(extra)}")
    if cfg["tracker"] is TrackerKind.LINEAR:
        _linear(cfg, local)
    _role_fields(roles)
    _local_clones(cfg)
    _work_dir(cfg, cfg["local_clones"], board=board)
    _task_tiers(cfg)
    _client_max_runs(cfg)


def _linear(cfg, local):
    if missing := utils.unset(cfg, REQUIRED):
        raise ConfigError(
            f"orchestrator/config.toml: required, unset: {', '.join(missing)}: set them in {local} (orchestrator/config.toml's `# local:` lines)"
        )
    linear = cfg["linear"]
    _check_ids(linear)
    hk = linear.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise ConfigError(
            f"orchestrator/config.toml: linear.harness_key must be a Keychain service name: {hk!r}"
        )
    _linear_roles(linear)
    _project_repos(linear)
    _task_labels(linear)
    _agent_labels(linear)


def _stage_roles(cfg, linear):
    """[roles] and [linear.roles]: one table per stage, in the workflow's order. A table for a
    packaged stage the workflow leaves out is dropped (the packaged config.toml has one); one for
    any other non-stage role is an error."""
    packaged = set(workflow.Workflow().load())
    out = {}
    for where, tables, keys in (
        ("roles", cfg.setdefault("roles", {}), ROLE_KEYS),
        ("linear.roles", linear["roles"], LINEAR_ROLE_KEYS),
    ):
        if not isinstance(tables, dict):
            raise ConfigError(f"orchestrator/config.toml: [{where}] must be a table")
        for name, p in tables.items():
            if not isinstance(p, dict):
                raise ConfigError(f"orchestrator/config.toml: [{where}.{name}] must be a table")
            if extra := sorted(set(p) - keys):
                raise ConfigError(
                    f"orchestrator/config.toml: [{where}.{name}] has unknown keys: {', '.join(extra)}"
                )
            if name not in TASKS and name not in packaged:
                raise ConfigError(
                    f"orchestrator/config.toml: [{where}.{name}] names no stage of the workflow"
                )
        out[where] = {name: tables.get(name, {}) for name in TASKS}
    cfg["roles"], linear["roles"] = out["roles"], out["linear.roles"]
    return cfg["roles"]


def _linear_roles(linear):
    """Each [linear.roles.<role>]: a stage's account and a key no other account or the harness
    uses."""
    owner = {}
    for name, p in linear["roles"].items():
        for k, what in (("account", "the role's Linear email"), ("key", "a Keychain service name")):
            if not isinstance(p.get(k), str) or not p[k]:
                raise ConfigError(
                    f"orchestrator/config.toml: [linear.roles.{name}] {k} must be {what}: {p.get(k)!r}"
                )
        key = p["key"]
        if key == linear["harness_key"]:
            raise ConfigError(
                f"orchestrator/config.toml: [linear.roles.{name}] key {key!r} is linear.harness_key"
            )
        if key in owner:
            raise ConfigError(
                f"orchestrator/config.toml: [linear.roles.{name}] key {key!r} is also [linear.roles.{owner[key]}]'s"
            )
        owner[key] = name


def _role_fields(roles):
    for name, p in roles.items():
        n = p.get("max_runs", 1)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(
                f"orchestrator/config.toml: [roles.{name}] max_runs must be a whole number >= 1: {n!r}"
            )


def _project_repos(linear):
    projects = linear.setdefault("project_repos", {})
    if not isinstance(projects, dict):
        raise ConfigError(
            'orchestrator/config.toml: linear.project_repos must be a table of "<Linear project id>" = "<owner>/<name>"'
        )
    for k, v in projects.items():
        if not _uuid(k) or not utils.repo_slug(v):
            raise ConfigError(
                f"orchestrator/config.toml: linear.project_repos.{k} must map a Linear project id (UUID) to <owner>/<name>: {v!r}"
            )


def _local_clones(cfg):
    clones = cfg.setdefault("local_clones", {})
    if not isinstance(clones, dict):
        raise ConfigError(
            'orchestrator/config.toml: local_clones must be a table of "<owner>/<name>" = "<path>"'
        )
    seen = {}
    for k, v in clones.items():
        if (
            not utils.repo_slug(k)
            or not isinstance(v, str)
            or not v.startswith(("/", "~"))
            or not v.isprintable()
        ):
            raise ConfigError(
                f"orchestrator/config.toml: local_clones.{k} must map <owner>/<name> to a printable path starting with / or ~: {v!r}"
            )
        if k.lower() in seen:
            raise ConfigError(
                f"orchestrator/config.toml: local_clones.{seen[k.lower()]} and local_clones.{k} are the same repo"
            )
        seen[k.lower()] = k
        clones[k] = os.path.realpath(os.path.expanduser(v))


def _task_labels(linear):
    labels = linear.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise ConfigError(
            'orchestrator/config.toml: linear.task_labels must be a table of <task> = "<Linear label id>"'
        )
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise ConfigError(
                f"orchestrator/config.toml: linear.task_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"orchestrator/config.toml: linear.task_labels.{seen[v]} and linear.task_labels.{k} have the same label id {v}"
            )
        seen[v] = k


def _task_tiers(cfg):
    tiers = cfg.setdefault("task_tiers", {})
    if not isinstance(tiers, dict):
        raise ConfigError(
            "orchestrator/config.toml: task_tiers must be a table of <task> = <tier 1-4>"
        )
    for k, v in tiers.items():
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 4:
            raise ConfigError(
                f"orchestrator/config.toml: task_tiers.{k} must be a tier, a whole number 1-4: {v!r}"
            )


def _agent_labels(linear):
    group = linear.setdefault("agent_label_group", "")
    labels = linear.setdefault("agent_labels", {})
    if group != "" and not _uuid(group):
        raise ConfigError(
            f"orchestrator/config.toml: linear.agent_label_group must be a Linear label group id (UUID) or empty: {group!r}"
        )
    if not isinstance(labels, dict):
        raise ConfigError(
            'orchestrator/config.toml: linear.agent_labels must be a table of <client> = "<Linear label id>"'
        )
    if bool(group) != bool(labels):
        raise ConfigError(
            "orchestrator/config.toml: linear.agent_label_group and [linear.agent_labels] must both be set or both be empty"
        )
    if group == linear["task_label_group"]:
        raise ConfigError(
            "orchestrator/config.toml: linear.agent_label_group and linear.task_label_group must be different"
        )
    tasks = {v: k for k, v in linear["task_labels"].items()}
    seen = {}
    for k, v in labels.items():
        if not k:
            raise ConfigError(
                "orchestrator/config.toml: [linear.agent_labels] client names must not be empty"
            )
        if not _uuid(v):
            raise ConfigError(
                f"orchestrator/config.toml: linear.agent_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"orchestrator/config.toml: linear.agent_labels.{seen[v]} and linear.agent_labels.{k} have the same label id {v}"
            )
        if v in tasks:
            raise ConfigError(
                f"orchestrator/config.toml: linear.agent_labels.{k} and linear.task_labels.{tasks[v]} have the same label id {v}"
            )
        seen[v] = k


def _client_max_runs(cfg):
    limits = cfg.setdefault("client_max_runs", {})
    if not isinstance(limits, dict):
        raise ConfigError(
            "orchestrator/config.toml: client_max_runs must be a table of <client> = <whole number >= 1>"
        )
    for k, n in limits.items():
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(
                f"orchestrator/config.toml: [client_max_runs] {k} must be a whole number >= 1: {n!r}"
            )


def _runnable(cfg, work, delegate_cls, index):
    try:
        trusted = delegate_cls.trusted_dirs()
    except (OSError, ValueError, delegate.ConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for d in sorted(trusted):
        if under(work, [d]):
            raise ConfigError(f"core: trusted_dirs: {d} is or contains work_dir {work}")
    out = _resolve_roles(cfg, cfg.get("core", {}), delegate_cls, index)
    _runnable_inputs(cfg, out)
    return out


def _resolve_roles(cfg, overlay, delegate_cls, index):
    out = {}
    for name, p in cfg["roles"].items():
        try:
            run = delegate_cls.resolve(name, overlays=(overlay,))
            tasks = tuple(index[name])
        except delegate.ConfigError as e:
            raise ConfigError(f"core: {e}") from None
        out[name] = Role(tasks, p.get("max_runs", 1), run)
    return out


def _runnable_inputs(cfg, roles):
    tasks = {t for r in roles.values() for t in r.tasks}
    tables = {"task_tiers": cfg["task_tiers"]}
    if cfg["tracker"] is TrackerKind.LINEAR:
        tables = {"linear.task_labels": cfg["linear"]["task_labels"], **tables}
    for table, entries in tables.items():
        for task in entries:
            if task not in tasks:
                raise ConfigError(
                    f"orchestrator/config.toml: {table}.{task} is not a task of a role in orchestrator/config.toml"
                )
    for key, path in cfg["local_clones"].items():
        if reason := clone_error(path, key):
            raise ConfigError(f"orchestrator/config.toml: local_clones.{key}: {reason}")


def _docs(roles, local):
    outs = {name: o for name, r in roles.items() if (o := r.run.output)["type"] == "github"}
    targets = {(o.get("repo"), o.get("branch"), o.get("host", "github.com")) for o in outs.values()}
    name, branch, host = targets.pop() if len(targets) == 1 else (None, None, None)
    if not name or not branch or host != "github.com":
        raise ConfigError(
            f"core: document roles must publish to one github.com repo and branch (their [output] in {local})"
        )
    return Docs(name, branch, MappingProxyType({r: o["dir"] for r, o in outs.items()}))


def _stage_order():
    targets = {s.next for s in TASKS.values() if s.next}
    order = dict.fromkeys(TASKS, 0)
    for start in TASKS:
        if start in targets:
            continue
        name, i = start, 0
        while name:
            order[name] = i
            name, i = TASKS[name].next, i + 1
    return order
