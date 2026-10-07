"""Paths, orchestrator/config.toml (LOCAL on top), core config with its [core] overlay, and TASKS; imports no
other orchestrator module. Needs Python 3.11+ (tomllib).
"""
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(ROOT, "orchestrator", "config.toml")
LOCAL = "~/.agent-pm/orchestrator.local.toml"
CORE = os.path.join(ROOT, "core")
PROJECTS = os.path.expanduser("~/.claude/projects")
# launchd starts jobs with /usr/bin:/bin:/usr/sbin:/sbin; tmux and claude live elsewhere.
PATH = f"/opt/homebrew/bin:{os.path.expanduser('~/.local/bin')}:/usr/local/bin:/usr/bin:/bin"

# No orchestrator module may be named clients, compose, drive, repo, report or tui_claude: these come from core/src.
sys.path.insert(0, os.path.join(CORE, "src"))
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402
import repo  # noqa: E402


def _overlap(a, b):
    return os.path.commonpath([a, b]) in (a, b)


def work_dir(cfg, root=ROOT, clones=None):
    """work_dir of a config table, a real path: its absolute or ~ value, else ~/.agent-pm. One that lies in or contains root
    or a clone of `clones` (repo → real path) stops the caller."""
    real = os.path.realpath(root)
    v = cfg.get("work_dir", "~/.agent-pm")
    path = os.path.expanduser(v) if isinstance(v, str) and v.isprintable() else ""
    if not os.path.isabs(path):
        raise SystemExit(f"orchestrator/config.toml: work_dir must be a printable absolute or ~ path: {v!r}")
    path = os.path.realpath(path)
    if _overlap(path, real):
        raise SystemExit(f"orchestrator/config.toml: work_dir must neither lie in nor contain the repo {real}: {v!r}")
    for k, clone in (clones or {}).items():
        if _overlap(path, clone):
            raise SystemExit(f"orchestrator/config.toml: work_dir must neither lie in nor contain local_clones.{k} {clone}: {v!r}")
    return path


WORK_DIR = work_dir(repo.read_config(CONFIG, LOCAL))
RUNS_DIR, LOGS_DIR = os.path.join(WORK_DIR, "work"), os.path.join(WORK_DIR, "logs")
RUNS_LOG = os.path.join(LOGS_DIR, "runs.log")


def session(role, issue):
    """The tmux session of a role's agent run on an issue."""
    return f"agent-pm-{role}-{issue}"


def sh_run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          env={**os.environ, "PATH": PATH})


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def project_log(name, logs=LOGS_DIR):
    d = os.path.join(logs, "projects")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{slug(name)}.log")


def run_dir(issue):
    return os.path.join(RUNS_DIR, issue)


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")


def repo_slug(value):
    """(owner, name) of a bare `owner/name`, else None."""
    m = re.fullmatch(rf"({OWNER})/({NAME})", value) if isinstance(value, str) else None
    return m.groups() if m and m.group(2) not in (".", "..") else None


def transcript(issue, sid, projects=PROJECTS):
    """Session file for an agent run, under the cwd its record names (drive.session; none: the workdir); sid must be a
    UUID (untrusted input) or this returns None."""
    if not UUID_RE.fullmatch(sid):
        return None
    rd = run_dir(issue)
    entry = drive.session(rd, sid)
    return clients.claude.transcript(entry["cwd"] if entry else rd, sid, projects)


TOP_KEYS = {"team", "states", "human_members", "harness_key", "task_label_group", "task_labels", "roles", "project_repos", "local_clones", "core",
            "work_dir"}
# Logical workflow states the code uses -> the name the docs use (a label; Linear is always queried by id).
STATES = {"todo": "Todo", "in_progress": "In Progress", "in_review": "In Review",
          "handoff": "Handoff", "done": "Done", "canceled": "Canceled"}
ROLE_KEYS = {"account", "key", "next", "require_instructions", "max_runs"}


@dataclass(frozen=True)
class Task:
    """Per-task data. Readers: kind → inputs/run; prefix → promote, writeback retitle; the rest → writeback."""
    kind: Literal["research", "design", "build"]
    prefix: str = ""                    # issue title prefix: promote child titles, retitle
    start: str = ""                     # start comment lead
    done: str = ""                      # done comment lead
    question: str = ""                  # needs_input comment lead
    failed: str = ""                    # failed comment lead
    failed_new: str = "in_review"       # state after failed on a new agent run (resume: in_review)
    retitle: bool = False               # done → title "<prefix>: <outcome title>"
    approve: bool = False               # done → approve line
    files: bool = False                 # done/failed → outcome files as Spec/Plan comments
    hint: str = ""                      # appended to the needs_input footer


REPO_HINT = "To change the target repo, edit the description's `Repo:` line."
TASKS = {
    "deep-research":  Task("research", start="Research started:", failed_new="todo", hint=REPO_HINT),
    "light-research": Task("research", start="Research started:", failed_new="todo", hint=REPO_HINT),
    "product-design": Task("design", prefix="PRD", start="PRD started:", retitle=True, approve=True),
    "build":          Task("build", prefix="ENG", start="Build started:", done="Build ready:",
                           question="Question:", failed="Build failed:", files=True),
    "light-build":    Task("build", prefix="ENG", start="Build started:", done="Build ready:",
                           question="Question:", failed="Build failed:"),
}


@dataclass(frozen=True)
class Role:
    """A orchestrator/config.toml role checked by runnable(); key is a Keychain service name, never the secret."""
    account: str        # Linear email
    key: str            # Keychain service of its API key
    tasks: tuple        # core config tasks, default_task first, then core order
    max_runs: int = 1   # agent runs of the role at once

    @property
    def default(self):
        return self.tasks[0]


@dataclass(frozen=True)
class Docs:
    """Where core's document tasks publish: one github.com repo and branch, a dir per task."""
    repo: str           # owner/name
    branch: str
    dirs: dict          # task → output dir


# Core's checkout dirs under an agent run's workdir: config commands' `--dir {{workdir}}/src`, the github destination's
# `<Workdir>/publish`.
CLONES = ("src", "publish")


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(cfg):
    if not _uuid(cfg.get("team")):
        raise SystemExit(f"orchestrator/config.toml: team must be a Linear team id (UUID): {cfg.get('team')!r}")
    states = cfg["states"]
    if extra := sorted(set(states) - set(STATES)):
        raise SystemExit(f"orchestrator/config.toml: [states] has unknown keys: {', '.join(extra)}")
    for k in STATES:
        if not _uuid(states[k]):
            raise SystemExit(f"orchestrator/config.toml: states.{k} must be a Linear workflow state id (UUID): {states[k]!r}")
    if not _uuid(cfg.get("task_label_group")):
        raise SystemExit(f"orchestrator/config.toml: task_label_group must be a Linear label group id (UUID): {cfg.get('task_label_group')!r}")


def _missing(cfg):
    """The required deployment keys cfg lacks, dotted."""
    missing = [k for k in ("team", "harness_key", "task_label_group") if k not in cfg]
    states = cfg.get("states")
    missing += [f"states.{k}" for k in STATES if not isinstance(states, dict) or k not in states]
    roles = cfg.get("roles")
    for name, p in roles.items() if isinstance(roles, dict) else ():
        missing += [f"roles.{name}.{k}" for k in ("account", "key") if isinstance(p, dict) and k not in p]
    return missing


def load_config(path=CONFIG):
    """`path` with LOCAL on top (repo.read_config), with the checks every consumer needs; the checks against core config
    are in runnable(), which every consumer calls."""
    cfg = repo.read_config(path, LOCAL)
    if missing := _missing(cfg):
        raise SystemExit(f"orchestrator/config.toml: missing {', '.join(missing)}: set them in {LOCAL} "
                         "(orchestrator/config.toml's `# local:` lines)")
    _check_ids(cfg)
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not hk:
        raise SystemExit(f"orchestrator/config.toml: harness_key must be a Keychain service name: {hk!r}")
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise SystemExit(f"orchestrator/config.toml: unknown keys: {', '.join(extra)}")
    roles = cfg.setdefault("roles", {})
    for name, p in roles.items():
        if extra := sorted(set(p) - ROLE_KEYS):
            raise SystemExit(f"orchestrator/config.toml: [roles.{name}] has unknown keys: {', '.join(extra)}")
        for k, what in (("account", "the role's Linear email"), ("key", "a Keychain service name")):
            if not isinstance(p.get(k), str) or not p[k]:
                raise SystemExit(f"orchestrator/config.toml: [roles.{name}] {k} must be {what}: {p.get(k)!r}")
        n = p.get("max_runs", 1)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise SystemExit(f"orchestrator/config.toml: [roles.{name}] max_runs must be a whole number >= 1: {n!r}")
    for name in roles:
        nxt, seen = roles[name].get("next"), {name}
        while nxt:
            if nxt in seen:
                raise SystemExit(f"orchestrator/config.toml: the next chain from {name!r} has a cycle")
            seen.add(nxt)
            nxt = roles.get(nxt, {}).get("next")
    repos = cfg.setdefault("project_repos", {})
    if not isinstance(repos, dict):
        raise SystemExit('orchestrator/config.toml: project_repos must be a table of "<Linear project id>" = "<owner>/<name>"')
    for k, v in repos.items():
        if not _uuid(k) or not repo_slug(v):
            raise SystemExit(f"orchestrator/config.toml: project_repos.{k} must map a Linear project id (UUID) to <owner>/<name>: {v!r}")
    clones = cfg.setdefault("local_clones", {})
    if not isinstance(clones, dict):
        raise SystemExit('orchestrator/config.toml: local_clones must be a table of "<owner>/<name>" = "<path>"')
    seen = {}
    for k, v in clones.items():
        if not repo_slug(k) or not isinstance(v, str) or not v.startswith(("/", "~")) or not v.isprintable():
            raise SystemExit(f"orchestrator/config.toml: local_clones.{k} must map <owner>/<name> to a printable path starting with / or ~: {v!r}")
        if k.lower() in seen:
            raise SystemExit(f"orchestrator/config.toml: local_clones.{seen[k.lower()]} and local_clones.{k} are the same repo")
        seen[k.lower()] = k
        clones[k] = os.path.realpath(os.path.expanduser(v))
    work_dir(cfg, clones=clones)
    labels = cfg.setdefault("task_labels", {})
    if not isinstance(labels, dict):
        raise SystemExit('orchestrator/config.toml: task_labels must be a table of <task> = "<Linear label id>"')
    seen = {}
    for k, v in labels.items():
        if not _uuid(v):
            raise SystemExit(f"orchestrator/config.toml: task_labels.{k} must be a Linear label id (UUID): {v!r}")
        if v in seen:
            raise SystemExit(f"orchestrator/config.toml: task_labels.{seen[v]} and task_labels.{k} have the same label id {v}")
        seen[v] = k
    return cfg


def _fill_root(value, root):
    if isinstance(value, str):
        return value.replace("{{root}}", shlex.quote(root))
    if isinstance(value, dict):
        return {k: _fill_root(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_root(v, root) for v in value]
    return value


def _check_overlay_keys(table, allowed, where):
    if extra := sorted(set(table) - allowed):
        raise SystemExit(f"orchestrator/config.toml [core]: unknown key {extra[0]!r} in {where}")


def overlay(root=ROOT):
    """[core] of <root>/orchestrator/config.toml with LOCAL on top ({} without one), {{root}} filled; its keys are checked
    here, since core checks only its own config's, the rest by load_config, which every consumer calls first."""
    layer = _fill_root(repo.read_config(os.path.join(root, "orchestrator", "config.toml"), LOCAL).get("core", {}), root)
    _check_overlay_keys(layer, compose.RUN_KEYS | {"roles"}, "the global table")
    for r, role in layer.get("roles", {}).items():
        _check_overlay_keys(role, compose.RUN_KEYS | {"tasks"}, f"roles.{r}")
        for t, task in role.get("tasks", {}).items():
            _check_overlay_keys(task, compose.RUN_KEYS, f"roles.{r}.tasks.{t}")
    return layer


def layers(root=ROOT):
    """The orchestrator's config layers, applied after `core/config.toml`'s `[clients.claude]`; the one source for run_config and run.py."""
    return [overlay(root)]


def run_config(role, task, root=ROOT):
    """compose.RunConfig of role/task: core config, then its `[clients.claude]`, then layers(root)."""
    core = os.path.join(root, "core")
    return compose.load_run(core, role, task, layers=[clients.load_config("claude", core), *layers(root)])


def writable(work=RUNS_DIR):
    """The dirs an agent run can write; a clone there gets no git run by the harness."""
    return (work, *repo.temp_dirs())


def clone_error(path, slug, *, run=sh_run):
    """Why realpath `path` is not a local clone of github.com/<slug>, or None. The one git call is guarded: the clone's config
    is not trusted."""
    if not path.isprintable():
        return f"{path!r} has a non-printable character"
    if not os.path.exists(path):
        return f"{path} does not exist"
    try:
        found, _ = repo.resolve(path, run=lambda argv, timeout: run(["git", *repo.GUARD, *argv[1:]], timeout))
    except repo.Invalid as e:
        return str(e)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:   # UnicodeDecodeError, from non-UTF-8 git output, is a ValueError
        return f"git: {e}"
    if found.host != "github.com" or f"{found.owner}/{found.name}".lower() != slug.lower():
        return f"origin is {found.slug}, not github.com/{slug}"
    return None


def runnable(cfg, root=ROOT):
    """{role: Role} of orchestrator/config.toml's roles in its order, checked against core config with overlay(root) and TASKS; a
    broken one stops the caller (fail loud). Core roles absent from orchestrator/config.toml are not orchestrated."""
    core_roles = repo.read_config(os.path.join(root, "core", compose.CONFIG)).get("roles", {})
    try:
        trusted = drive.trusted_dirs(os.path.join(root, "core"))
    except compose.ConfigError as e:
        raise SystemExit(f"core: {e}") from None
    for d in sorted(trusted):
        if repo.under(WORK_DIR, [d]):
            raise SystemExit(f"core: trusted_dirs: {d} is or contains work_dir {WORK_DIR}")
    out = {}
    for name, p in cfg["roles"].items():
        if name not in core_roles:
            raise SystemExit(f"orchestrator/config.toml: role {name!r} is not in core/config.toml")
        default, tasks = core_roles[name].get("default_task"), list(core_roles[name].get("tasks", {}))
        tasks = tuple(dict.fromkeys([default, *tasks] if default else tasks))
        for t in tasks:
            if t not in TASKS:
                raise SystemExit(f"orchestrator/config.toml: {name}'s task {t!r} has no entry in config.TASKS")
            try:
                run_config(name, t, root)
            except compose.ConfigError as e:
                raise SystemExit(f"core: {e}") from None
        out[name] = Role(p["account"], p["key"], tasks, p.get("max_runs", 1))
    owner = {}
    for name, r in out.items():
        if r.key == cfg["harness_key"]:
            raise SystemExit(f"orchestrator/config.toml: [roles.{name}] key {r.key!r} is harness_key")
        if r.key in owner:
            raise SystemExit(f"orchestrator/config.toml: [roles.{name}] key {r.key!r} is also [roles.{owner[r.key]}]'s")
        owner[r.key] = name
    for name, p in cfg["roles"].items():
        nxt = p.get("next")
        if not nxt:
            continue
        if nxt not in out:
            raise SystemExit(f"orchestrator/config.toml: next of {name!r} names undefined role {nxt!r}")
        if not TASKS[out[nxt].default].prefix:
            raise SystemExit(f"orchestrator/config.toml: next of {name!r} is role {nxt!r}, whose default task {out[nxt].default!r} has no prefix")
    tasks = {t for r in out.values() for t in r.tasks}
    for task in cfg["task_labels"]:
        if task not in tasks:
            raise SystemExit(f"orchestrator/config.toml: task_labels.{task} is not a task of a role in orchestrator/config.toml")
    for key, path in cfg["local_clones"].items():
        if reason := clone_error(path, key):
            raise SystemExit(f"orchestrator/config.toml: local_clones.{key}: {reason}")
    docs(out, root)
    return out


def docs(roles, root=ROOT):
    """Docs of the github outputs among roles' tasks ({role: Role}); they must share one github.com repo and branch."""
    outs = {t: o for r, role in roles.items() for t in role.tasks if (o := run_config(r, t, root).output)["type"] == "github"}
    targets = {(o.get("repo"), o.get("branch"), o.get("host", "github.com")) for o in outs.values()}
    name, branch, host = targets.pop() if len(targets) == 1 else (None, None, None)
    if not name or not branch or host != "github.com":
        raise SystemExit(f"core: document tasks must publish to one github.com repo and branch (their [output] in {repo.LOCAL})")
    return Docs(name, branch, {t: o["dir"] for t, o in outs.items()})


def role_for(runs, email):
    """The role in runs ({role: Role}) whose account is email (case-insensitive), or None."""
    return next((r for r, run in runs.items() if email and run.account.lower() == email.lower()), None)


def stage_order(cfg):
    """{role: position in its next chain}; roles outside a chain are 0."""
    roles = cfg["roles"]
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
