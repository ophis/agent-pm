"""Packaged orchestrator settings, registered Delegates and resolved role runs."""

import os
import re
import subprocess
import sys
from types import MappingProxyType

import agent_pm.utils as utils
from agent_pm import delegate
from agent_pm.orchestrator.engine import workflow
from agent_pm.orchestrator.trackers.linear import types
from agent_pm.orchestrator.trackers.linear.log import config_error
from agent_pm.orchestrator.trackers.linear.types import (
    UUID_RE,
    Config,
    ConfigError,
    Docs,
    Role,
    State,
)
from agent_pm.utils import GUARD, STATE, temp_dirs, under

LOCAL = utils.LOCAL

REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")
_ROLE_ISSUE = r"([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)"

TOP_KEYS = {
    "team",
    "states",
    "human_members",
    "harness_key",
    "task_label_group",
    "task_labels",
    "task_tiers",
    "agent_label_group",
    "agent_labels",
    "client_max_runs",
    "roles",
    "project_repos",
    "local_clones",
    "core",
    "work_dir",
    "delegates",
}
ROLE_KEYS = {"account", "key", "next", "require_instructions", "max_runs"}
REQUIRED = (
    "team",
    "harness_key",
    "task_label_group",
    *(f"states.{k}" for k in State),
    "roles.*.account",
    "roles.*.key",
)


TASKS = workflow.DEFAULT


def report_error(src: str, error: ConfigError, *, dry: bool = False) -> None:
    if not error.logs_dir:
        print(error.msg, file=sys.stderr)
    else:
        config_error(src, error, dry=dry)


def load(*, path: str | None = None) -> Config:
    try:
        cfg = utils.read_resources(
            (("agent_pm.orchestrator", "config.toml"),),
            paths={"agent_pm.orchestrator": path} if path is not None else None,
        )
    except (OSError, ValueError) as e:
        raise ConfigError(f"orchestrator/config.toml: {e}") from None
    _flow(cfg, default=path is None)
    work = _work_dir(cfg)
    try:
        _load_config(cfg, LOCAL)
        return _load(cfg, work)
    except ConfigError as e:
        e.logs_dir = os.path.join(work, "logs")
        raise


def _flow(cfg, *, default):
    """Read the workflow; with the packaged config.toml, its next and require_instructions are each
    role's defaults. A replaced config.toml (path=) keeps them unset, as before the workflow file."""
    try:
        tasks = TASKS.load()
    except ValueError as e:
        raise ConfigError(str(e)) from None
    roles = cfg.get("roles")
    if not default or not isinstance(roles, dict):
        return
    for role, p in roles.items():
        if role in tasks and isinstance(p, dict):
            p.setdefault("next", tasks[role].next)
            p.setdefault("require_instructions", tasks[role].require_instructions)


def tui_prefix(role, issue):
    name = f"{role}-{issue}"
    if not re.fullmatch(_ROLE_ISSUE, name):
        raise ValueError(f"TUI session prefix {name!r}: want {_ROLE_ISSUE}")
    return name + "-"


def tui_name(role, issue, sid):
    return tui_prefix(role, issue) + sid[:8]


def path():
    """The PATH the router, promote and their tools and runs get, read at call: AGENT_PM_PATH (if
    set), this installation's bin/ (agent-pm-linear, the harness's write-back callback), then the
    default dirs.
    """
    [root] = utils.module_roots(types)
    default = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"
    found = os.path.join(os.path.dirname(os.path.dirname(root)), "bin")
    return ":".join(p for p in (os.environ.get("AGENT_PM_PATH"), found, default) if p)


def shell(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return utils.sh(argv, timeout, env={**os.environ, "PATH": path()})


def role_for(roles, email):
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


def _work_dir(cfg, clones=None):
    v = cfg.get("work_dir", STATE)
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
                    f"orchestrator/config.toml: work_dir must neither lie in nor contain local_clones.{k} {clone}: {v!r}"
                )
    except OSError as error:
        raise ConfigError(f"orchestrator/config.toml: work_dir: {error}") from None
    return path


def _load(cfg, work):
    kind, delegates = _delegates(cfg)
    roles = _runnable(cfg, work, delegates[kind])
    docs = _docs(roles, LOCAL)
    frozen = {
        k: MappingProxyType(dict(cfg[k]))
        for k in (
            "states",
            "task_labels",
            "task_tiers",
            "agent_labels",
            "client_max_runs",
            "project_repos",
            "local_clones",
        )
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
        states=frozen["states"],
        task_labels=frozen["task_labels"],
        task_tiers=frozen["task_tiers"],
        agent_label_group=cfg["agent_label_group"],
        agent_labels=frozen["agent_labels"],
        client_max_runs=frozen["client_max_runs"],
        project_repos=frozen["project_repos"],
        local_clones=frozen["local_clones"],
        delegate_kind=kind,
        delegates=MappingProxyType(delegates),
        core=utils.freeze(cfg.get("core", {})),
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


def _check_ids(cfg):
    if not _uuid(cfg.get("team")):
        raise ConfigError(
            f"orchestrator/config.toml: team must be a Linear team id (UUID): {cfg.get('team')!r}"
        )
    states = cfg["states"]
    if extra := sorted(set(states) - set(State)):
        raise ConfigError(
            f"orchestrator/config.toml: [states] has unknown keys: {', '.join(extra)}"
        )
    for k in State:
        if not _uuid(states[k]):
            raise ConfigError(
                f"orchestrator/config.toml: states.{k} must be a Linear workflow state id (UUID): {states[k]!r}"
            )
    if not _uuid(cfg.get("task_label_group")):
        raise ConfigError(
            f"orchestrator/config.toml: task_label_group must be a Linear label group id (UUID): {cfg.get('task_label_group')!r}"
        )


def _load_config(cfg, local):
    if missing := utils.unset(cfg, REQUIRED):
        raise ConfigError(
            f"orchestrator/config.toml: required, unset: {', '.join(missing)}: set them in {local} (orchestrator/config.toml's `# local:` lines)"
        )
    _check_ids(cfg)
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise ConfigError(
            f"orchestrator/config.toml: harness_key must be a Keychain service name: {hk!r}"
        )
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise ConfigError(f"orchestrator/config.toml: unknown keys: {', '.join(extra)}")
    _role_fields(cfg)
    _next_chains(cfg["roles"])
    _project_repos(cfg)
    _local_clones(cfg)
    _work_dir(cfg, cfg["local_clones"])
    _task_labels(cfg)
    _task_tiers(cfg)
    _agent_labels(cfg)
    _client_max_runs(cfg)


def _role_fields(cfg):
    roles = cfg.setdefault("roles", {})
    for name, p in roles.items():
        if extra := sorted(set(p) - ROLE_KEYS):
            raise ConfigError(
                f"orchestrator/config.toml: [roles.{name}] has unknown keys: {', '.join(extra)}"
            )
        for k, what in (("account", "the role's Linear email"), ("key", "a Keychain service name")):
            if not isinstance(p.get(k), str) or not p[k]:
                raise ConfigError(
                    f"orchestrator/config.toml: [roles.{name}] {k} must be {what}: {p.get(k)!r}"
                )
        n = p.get("max_runs", 1)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ConfigError(
                f"orchestrator/config.toml: [roles.{name}] max_runs must be a whole number >= 1: {n!r}"
            )


def _next_chains(roles):
    for name in roles:
        nxt, seen = roles[name].get("next"), {name}
        while nxt:
            if nxt in seen:
                raise ConfigError(
                    f"orchestrator/config.toml: the next chain from {name!r} has a cycle"
                )
            seen.add(nxt)
            nxt = roles.get(nxt, {}).get("next")


def _project_repos(cfg):
    projects = cfg.setdefault("project_repos", {})
    if not isinstance(projects, dict):
        raise ConfigError(
            'orchestrator/config.toml: project_repos must be a table of "<Linear project id>" = "<owner>/<name>"'
        )
    for k, v in projects.items():
        if not _uuid(k) or not utils.repo_slug(v):
            raise ConfigError(
                f"orchestrator/config.toml: project_repos.{k} must map a Linear project id (UUID) to <owner>/<name>: {v!r}"
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


def _task_labels(cfg):
    labels = cfg.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise ConfigError(
            'orchestrator/config.toml: task_labels must be a table of <task> = "<Linear label id>"'
        )
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise ConfigError(
                f"orchestrator/config.toml: task_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"orchestrator/config.toml: task_labels.{seen[v]} and task_labels.{k} have the same label id {v}"
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


def _agent_labels(cfg):
    group = cfg.setdefault("agent_label_group", "")
    labels = cfg.setdefault("agent_labels", {})
    if group != "" and not _uuid(group):
        raise ConfigError(
            f"orchestrator/config.toml: agent_label_group must be a Linear label group id (UUID) or empty: {group!r}"
        )
    if not isinstance(labels, dict):
        raise ConfigError(
            'orchestrator/config.toml: agent_labels must be a table of <client> = "<Linear label id>"'
        )
    if bool(group) != bool(labels):
        raise ConfigError(
            "orchestrator/config.toml: agent_label_group and [agent_labels] must both be set or both be empty"
        )
    if group == cfg["task_label_group"]:
        raise ConfigError(
            "orchestrator/config.toml: agent_label_group and task_label_group must be different"
        )
    tasks = {v: k for k, v in cfg["task_labels"].items()}
    seen = {}
    for k, v in labels.items():
        if not k:
            raise ConfigError(
                "orchestrator/config.toml: [agent_labels] client names must not be empty"
            )
        if not _uuid(v):
            raise ConfigError(
                f"orchestrator/config.toml: agent_labels.{k} must be a Linear label id (UUID): {v!r}"
            )
        if v in seen:
            raise ConfigError(
                f"orchestrator/config.toml: agent_labels.{seen[v]} and agent_labels.{k} have the same label id {v}"
            )
        if v in tasks:
            raise ConfigError(
                f"orchestrator/config.toml: agent_labels.{k} and task_labels.{tasks[v]} have the same label id {v}"
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


def _runnable(cfg, work, delegate_cls):
    try:
        trusted = delegate_cls.trusted_dirs()
        index = delegate_cls.roles()
    except (OSError, ValueError, delegate.ConfigError) as e:
        raise ConfigError(f"core: {e}") from None
    for d in sorted(trusted):
        if under(work, [d]):
            raise ConfigError(f"core: trusted_dirs: {d} is or contains work_dir {work}")
    out = _resolve_roles(cfg, cfg.get("core", {}), delegate_cls, index)
    _role_links(cfg, out)
    _runnable_inputs(cfg, out)
    return out


def _resolve_roles(cfg, overlay, delegate_cls, index):
    out = {}
    for name, p in cfg["roles"].items():
        if name not in index:
            raise ConfigError(
                f"orchestrator/config.toml: role {name!r} is not in delegate/config.toml"
            )
        if name not in TASKS:
            raise ConfigError(
                f"orchestrator/config.toml: role {name!r} has no entry in config.TASKS"
            )
        try:
            run = delegate_cls.resolve(name, overlays=(overlay,))
            tasks = tuple(index[name])
        except delegate.ConfigError as e:
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
    return out


def _role_links(cfg, out):
    owner = {}
    for name, r in out.items():
        if r.key == cfg["harness_key"]:
            raise ConfigError(
                f"orchestrator/config.toml: [roles.{name}] key {r.key!r} is harness_key"
            )
        if r.key in owner:
            raise ConfigError(
                f"orchestrator/config.toml: [roles.{name}] key {r.key!r} is also [roles.{owner[r.key]}]'s"
            )
        owner[r.key] = name
    for name, r in out.items():
        if not r.next:
            continue
        if r.next not in out:
            raise ConfigError(
                f"orchestrator/config.toml: next of {name!r} names undefined role {r.next!r}"
            )
        if not TASKS[r.next].prefix:
            raise ConfigError(
                f"orchestrator/config.toml: next of {name!r} is role {r.next!r}, whose config.TASKS entry has no prefix"
            )


def _runnable_inputs(cfg, roles):
    tasks = {t for r in roles.values() for t in r.tasks}
    for table in ("task_labels", "task_tiers"):
        for task in cfg[table]:
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
