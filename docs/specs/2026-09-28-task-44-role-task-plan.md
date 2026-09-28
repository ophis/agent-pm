# TASK-44: Role + task restructure (phase 1) — plan

RESUME: phase=S5 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=db150ef8c9febd7f265d45d43f10ee2e7c31ce8f review_round=1 spec_file=docs/specs/2026-09-28-task-44-role-task-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `stages/` + per-project `instructions` with roles (`roles/<role>.md`, `[roles.*]`) and tasks (`tasks/<task>.md`, `[tasks.*]`); the launcher resolves both from the project, with unchanged behavior for the three existing runs.

**Architecture:** `pipeline.runnable()` validates the registry and returns `{project_id: Run}` with every path the launcher needs already resolved; `launch.py` builds prompt, `--add-dir`, Edit denies and log name only from `Run`. The router keeps using only the keys of `runnable()`. Promote only calls `load_config()`.

**Tech Stack:** Python 3.11+ standard library (`tomllib`, `dataclasses`, `unittest`), TOML, Markdown.

**Spec:** `docs/specs/2026-09-28-task-44-role-task-design.md` (read it first; it is authoritative where this plan is terse).

## Global Constraints

- Worktree `/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task`, branch `TASK-44-role-task`; before any write assert `git -C <worktree> branch --show-current` prints `TASK-44-role-task`. Absolute paths only. Never touch `/Users/francis/playground/agent-pm` itself (the live install), never `main`, never force-push, never merge.
- After each task's commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task`.
- Standard library only (NFR-3). Python code style: match the files (terse docstrings, `SystemExit("pipeline.toml: …")` for config errors, no comments unless a gotcha).
- Test command (from the worktree root): `python3 -m unittest discover -s scripts/tests` — all green at the end of every task.
- NFR-1: the three existing runs' `claude` argv differ from today only in prompt text (charter path, principles path, resume wording "stage's" → "task's") and `stages/` → `roles/` + `tasks/` in `--add-dir` / `--disallowedTools`.
- Names: roles `researcher`, `pm`, `engineer`; tasks `deep-research`, `product-design`, `engineering`.

## Review Focus

- `read_only = ["~/playground/private_docs/"]` (trailing slash) must yield the same deny rule as without it → Task 1 test `test_read_only_normalized`.
- `memory` given as a symlink whose target is inside the repo root must be rejected (compare by `realpath`) → Task 1 test `test_memory_symlink_into_root_rejected`.
- A project still carrying `instructions` (forgotten migration) must fail loudly in `runnable()`, but not in `load_config()` (promote keeps working) → Task 1 tests `test_moved_keys_rejected` / `test_load_config_ignores_moved_keys`.
- A session started before the migration and resumed after it gets the new absolute paths and the same transcript location → Task 2 test `test_real_config_resume_prompt`.
- `{repo}` read-only role when the repo check is Invalid (no clone) → only the worktrees deny rule → Task 2 test `test_repo_read_only_invalid`.

---

### Task 1: Layout, registry and validation

**Files:**
- Move: `git mv stages/principles.md roles/principles.md`; `git mv stages/deep-research.md tasks/deep-research.md`; `git mv stages/product-design.md tasks/product-design.md`; `git mv stages/engineering.md tasks/engineering.md`
- Create: `roles/researcher.md`, `roles/pm.md`, `roles/engineer.md`
- Modify: `roles/principles.md` (title + intro + first bullet), each `tasks/*.md` (the one "`principles.md` beside this file" sentence)
- Modify: `pipeline.toml`, `scripts/pipeline.py`, `scripts/launch.py` (wiring only), `scripts/tests/test_pipeline.py`, `scripts/tests/test_router.py` (fixture), `scripts/tests/test_launch.py` (fixture + expectations for the new paths)

**Interfaces:**
- Produces (in `scripts/pipeline.py`):
  - `REPO = "{repo}"`
  - `@dataclass(frozen=True) class Run: project: dict; role_name: str; role: dict; task_name: str; task: dict; charter: str; instructions: str; memory: str | None; read_only: tuple`
  - `runnable(cfg, root=ROOT) -> dict[str, Run]`
  - `load_config(path=CONFIG)` now guarantees `cfg["roles"]`, `cfg["tasks"]`, `cfg["projects"]` exist (setdefault) and runs `check_allowed_tools(name, task)` for each `[tasks.*]`.
- Produces (in `scripts/launch.py`): `PRINCIPLES = os.path.join(ROOT, "roles", "principles.md")`; `prompt(a, run)`, `command(a, run, tail="", allowed=())` taking a `Run`.

- [ ] **Step 1: Move and write the Markdown files**

```bash
W=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task
git -C $W branch --show-current   # must print TASK-44-role-task
mkdir -p $W/roles $W/tasks
git -C $W mv stages/principles.md roles/principles.md
git -C $W mv stages/deep-research.md tasks/deep-research.md
git -C $W mv stages/product-design.md tasks/product-design.md
git -C $W mv stages/engineering.md tasks/engineering.md
```

`roles/principles.md`: replace the first three lines (title, blank, intro) and the first bullet so the file starts:

```markdown
# Principles (every role and task)

The prompt names this file, your role charter (who you are: responsibilities, boundaries, memory) and your task (the steps for this ticket). These rules hold for every role and task. On conflict, this file outranks the charter's boundaries, which outrank the task's steps.

- Team `Frank's Agents`; the run's project id is the prompt's `Project:` value (the name may change).
```

All other bullets stay byte-identical.

In each of `tasks/deep-research.md`, `tasks/product-design.md`, `tasks/engineering.md` replace exactly
`` `principles.md` beside this file holds the rules every stage shares.`` with
`the principles file named in the prompt holds the rules every role and task shares.` Nothing else changes in task files.

`roles/researcher.md`:

```markdown
# Researcher

Turns Deep Research issues into verified Markdown reports pushed to the user's `private_docs` GitHub repo and linked from the issue.

## Memory
```

`roles/pm.md`:

```markdown
# PM

Turns Product Design issues into PRDs pushed to the user's `private_docs` GitHub repo and linked from the issue; after the user's review, a Handoff creates the Engineering issue from each PRD.

## Memory
```

`roles/engineer.md`:

```markdown
# Engineer

Turns Engineering issues into pull requests on their target repos, built from the issue's PRD and linked from the issue.

## Memory
```

(Each file ends with `## Memory` and a newline; the section stays empty.)

- [ ] **Step 2: Rewrite `pipeline.toml`** (keep the top comments and the per-project name comments)

```toml
team = "Frank's Agents"
human_members = ["ophis.w@outlook.com"]   # the human users; the first is assigned issues that need human review (In Review)

# Roles: identity. Charter roles/<role>.md; read_only = paths the role's runs must not edit ({repo} = the issue's clone and worktrees);
# memory = the role's writable memory dir (unset = off).
[roles.researcher]

[roles.pm]

[roles.engineer]
read_only = ["~/playground/private_docs"]   # the PRDs

# Tasks: procedure. Instructions tasks/<task>.md; the log is logs/projects/<task>.log.
[tasks.deep-research]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]

[tasks.product-design]
model = "opus"
effort = "high"
add_dirs = ["~/playground/private_docs"]

[tasks.engineering]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
repo_from_issue = true                    # the launcher resolves and clones the issue's target repo

# Projects are keyed by their Linear project id (a name or URL slug changes with a rename); role + task make one runnable.
[projects."03495382-48f7-4280-a11c-4375df80a561"]  # 1-Research
next = "ba0738ba-ade7-4525-8d79-1b9944334e74"  # 2-Product Design
role = "researcher"
task = "deep-research"

[projects."ba0738ba-ade7-4525-8d79-1b9944334e74"]  # 2-Product Design
prefix = "PRD"                            # title prefix for issues created in this project
next = "ddbff8bf-b633-4b8c-9272-d1d5ee923747"  # 3-Engineering
require_instructions = false              # a PRD carries enough context; comments are optional
role = "pm"
task = "product-design"

[projects."ddbff8bf-b633-4b8c-9272-d1d5ee923747"]  # 3-Engineering
prefix = "ENG"
role = "engineer"
task = "engineering"
```

- [ ] **Step 3: Write the failing pipeline tests** (replace `test_runnable`, `test_runnable_needs_model_and_effort`, `test_missing_stage_file_does_not_break_load_config`, `test_load_config_rejects_bad_allowed_tools`, `test_load_config_accepts_valid_allowed_tools`, and `RealConfig` in `scripts/tests/test_pipeline.py`; keep `Paths`, `AllowedTools`, `test_stage_order`, `test_cycle_rejected`, `test_next_without_prefix_rejected`)

Add `from unittest import mock` to the imports. New helpers and tests:

```python
REGISTRY = """team = "T"
[roles.researcher]
[roles.engineer]
read_only = ["~/playground/private_docs"]
[tasks.deep-research]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
[tasks.engineering]
model = "opus"
effort = "high"
repo_from_issue = true
[projects.dr]
next = "eng"
role = "researcher"
task = "deep-research"
[projects.eng]
prefix = "ENG"
role = "engineer"
task = "engineering"
[projects.idle]
prefix = "I"
"""


class Runnable(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "root")
        self.outside = os.path.join(tmp.name, "outside")
        os.makedirs(self.outside)
        for d, names in (("roles", ("principles", "researcher", "engineer")), ("tasks", ("deep-research", "engineering"))):
            os.makedirs(os.path.join(self.root, d))
            for n in names:
                open(os.path.join(self.root, d, f"{n}.md"), "w").close()
        os.makedirs(os.path.join(self.root, "templates"))

    def load(self, text):
        path = os.path.join(self.outside, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def runs(self, text):
        return pipeline.runnable(self.load(text), root=self.root)

    def rejects(self, text, fragment):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        self.assertTrue(str(cm.exception.code).startswith("pipeline.toml: "), cm.exception.code)
        self.assertIn(fragment, str(cm.exception.code))

    def test_runs(self):
        runs = self.runs(REGISTRY)
        self.assertEqual(sorted(runs), ["dr", "eng"])
        dr, eng = runs["dr"], runs["eng"]
        self.assertEqual((dr.role_name, dr.task_name, dr.task["effort"], dr.memory, dr.read_only),
                         ("researcher", "deep-research", "xhigh", None, ()))
        self.assertEqual(dr.charter, os.path.join(self.root, "roles", "researcher.md"))
        self.assertEqual(dr.instructions, os.path.join(self.root, "tasks", "deep-research.md"))
        self.assertEqual(eng.read_only, (os.path.expanduser("~/playground/private_docs"),))
        self.assertEqual(eng.project["prefix"], "ENG")

    def test_read_only_normalized(self):
        runs = self.runs(REGISTRY.replace('"~/playground/private_docs"]\n[tasks', '"~/playground/private_docs/"]\n[tasks'))
        self.assertEqual(runs["eng"].read_only, (os.path.expanduser("~/playground/private_docs"),))

    def test_moved_keys_rejected(self):
        for key, value in (("instructions", '"tasks/x.md"'), ("model", '"opus"'), ("effort", '"high"'),
                           ("add_dirs", "[]"), ("repo_from_issue", "true"), ("allowed_tools", "[]")):
            self.rejects(REGISTRY.replace('[projects.idle]\nprefix = "I"\n', f'[projects.idle]\nprefix = "I"\n{key} = {value}\n'), key)

    def test_load_config_ignores_moved_keys(self):
        cfg = self.load(REGISTRY + 'instructions = "stages/gone.md"\n')
        self.assertIn("idle", cfg["projects"])

    def test_role_and_task_together(self):
        self.rejects(REGISTRY.replace('role = "engineer"\n', ""), "both role and task")
        self.rejects(REGISTRY.replace('task = "engineering"\n[projects.idle]', "[projects.idle]"), "both role and task")

    def test_unknown_role_or_task(self):
        self.rejects(REGISTRY.replace('role = "engineer"\n', 'role = "em"\n'), "em")
        self.rejects(REGISTRY.replace('task = "engineering"\n[projects.idle]', 'task = "work-breakdown"\n[projects.idle]'), "work-breakdown")

    def test_missing_files(self):
        os.remove(os.path.join(self.root, "roles", "engineer.md"))
        self.rejects(REGISTRY, "engineer.md")
        open(os.path.join(self.root, "roles", "engineer.md"), "w").close()
        os.remove(os.path.join(self.root, "tasks", "engineering.md"))
        self.rejects(REGISTRY, "engineering.md")

    def test_unused_entries_checked(self):
        self.rejects(REGISTRY + "[tasks.light-research]\nmodel = \"opus\"\neffort = \"high\"\n", "light-research")

    def test_bad_names(self):
        open(os.path.join(self.root, "roles", "Researcher.md"), "w").close()
        self.rejects(REGISTRY.replace("[roles.researcher]", "[roles.Researcher]").replace('role = "researcher"', 'role = "Researcher"'), "Researcher")
        self.rejects(REGISTRY + "[roles.principles]\n", "principles")
        self.rejects(REGISTRY + '[tasks.deep_research]\nmodel = "opus"\neffort = "high"\n', "deep_research")

    def test_task_needs_model_and_effort(self):
        self.rejects(REGISTRY.replace('effort = "xhigh"\n', ""), "effort")
        self.rejects(REGISTRY.replace('model = "opus"\neffort = "xhigh"', 'effort = "xhigh"'), "model")

    def test_unknown_keys(self):
        self.rejects(REGISTRY.replace('read_only = ["~/playground/private_docs"]', 'readonly = ["~/playground/private_docs"]'), "readonly")
        self.rejects(REGISTRY.replace('repo_from_issue = true\n', 'repo_from_issue = true\ninstructions = "x"\n'), "instructions")

    def test_read_only_paths(self):
        for bad in ('"playground/private_docs"', '"~/playground/../private_docs"', '"{repo}/x"'):
            self.rejects(REGISTRY.replace('read_only = ["~/playground/private_docs"]', f"read_only = [{bad}]"), "read_only")
        self.rejects(REGISTRY.replace('read_only = ["~/playground/private_docs"]', 'read_only = "/"'), "read_only")

    def test_repo_read_only(self):
        repo = REGISTRY.replace('read_only = ["~/playground/private_docs"]', 'read_only = ["{repo}"]')
        self.assertEqual(self.runs(repo)["eng"].read_only, ("{repo}",))
        self.rejects(repo.replace('role = "researcher"', 'role = "engineer"'), "{repo}")
        self.rejects(repo.replace("repo_from_issue = true\n", "repo_from_issue = true\nallowed_tools = []\n"), "{repo}")

    def memory(self, path, read_only="~/playground/private_docs"):
        return REGISTRY.replace('read_only = ["~/playground/private_docs"]',
                                f'read_only = ["{read_only}"]\nmemory = "{path}"')

    def test_memory_ok(self):
        mem = os.path.join(self.outside, "engineer")
        os.makedirs(mem)
        self.assertEqual(self.runs(self.memory(mem))["eng"].memory, mem)

    def test_memory_rejected(self):
        ro = os.path.join(self.outside, "docs")
        for d in (ro, os.path.join(ro, "sub"), os.path.join(self.root, "notes"), os.path.join(self.root, "roles")):
            os.makedirs(d, exist_ok=True)
        cases = [("relative", "mem"), ("missing", os.path.join(self.outside, "nope")),
                 ("under root", os.path.join(self.root, "notes")), ("root itself", self.root),
                 ("roles", os.path.join(self.root, "roles")), ("ancestor of root", os.path.dirname(self.root)),
                 ("at read_only", ro), ("under read_only", os.path.join(ro, "sub")), ("ancestor of read_only", self.outside)]
        for label, path in cases:
            with self.subTest(label):
                self.rejects(self.memory(path, read_only=ro), "memory")

    def test_memory_symlink_into_root_rejected(self):
        link = os.path.join(self.outside, "link")
        os.symlink(os.path.join(self.root, "templates"), link)
        self.rejects(self.memory(link), "memory")

    def test_memory_under_claude_config_rejected(self):
        home = os.path.join(self.outside, "home")
        for d in (".claude/mem", "Library/LaunchAgents/mem"):
            os.makedirs(os.path.join(home, d))
        with mock.patch.dict(os.environ, {"HOME": home}):
            for d in (".claude/mem", "Library/LaunchAgents/mem"):
                self.rejects(self.memory(os.path.join(home, d)), "memory")

    def test_load_config_checks_task_allowed_tools(self):
        with self.assertRaises(SystemExit):
            self.load(REGISTRY.replace("repo_from_issue = true\n", 'repo_from_issue = true\nallowed_tools = ["Bash(git push origin *)"]\n'))
        cfg = self.load(REGISTRY.replace("repo_from_issue = true\n", "repo_from_issue = true\nallowed_tools = "
                                         '["Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"]\n'))
        self.assertIn("allowed_tools", cfg["tasks"]["engineering"])

    def test_load_config_does_not_need_files(self):
        os.remove(os.path.join(self.root, "tasks", "engineering.md"))
        self.assertIn("eng", self.load(REGISTRY)["projects"])


class RealConfig(unittest.TestCase):
    def test_three_runs(self):
        runs = pipeline.runnable(pipeline.load_config())
        got = {k: (r.role_name, r.task_name, r.task["model"], r.task["effort"], bool(r.task.get("repo_from_issue")), r.read_only, r.memory)
               for k, r in runs.items()}
        private = os.path.expanduser("~/playground/private_docs")
        self.assertEqual(got, {
            "03495382-48f7-4280-a11c-4375df80a561": ("researcher", "deep-research", "opus", "xhigh", False, (), None),
            "ba0738ba-ade7-4525-8d79-1b9944334e74": ("pm", "product-design", "opus", "high", False, (), None),
            "ddbff8bf-b633-4b8c-9272-d1d5ee923747": ("engineer", "engineering", "opus", "xhigh", True, (private,), None),
        })
        self.assertNotIn("allowed_tools", runs["ddbff8bf-b633-4b8c-9272-d1d5ee923747"].task)
```

Note for `test_role_and_task_together` second case: the replace removes the engineering project's `task` line (the `task = "engineering"` directly before `[projects.idle]`). Note for `test_memory_ok`: `self.outside` is a temp dir; on macOS `/var` is a symlink to `/private/var`, so compare against the configured (non-realpath) value, which `Run.memory` keeps (normpath only).

- [ ] **Step 4: Run to verify failure**

Run: `python3 -m unittest discover -s scripts/tests -k Runnable -k RealConfig`
Expected: errors (`Run` / new validation missing; `runnable` raises on the old keys or returns dicts).

- [ ] **Step 5: Implement in `scripts/pipeline.py`**

Add `from dataclasses import dataclass` to the imports. Update the module docstring's last sentence only if needed. Replace `load_config` and `runnable` and add the helpers:

```python
MOVED = ("instructions", "model", "effort", "add_dirs", "repo_from_issue", "allowed_tools")
ROLE_KEYS = {"read_only", "memory"}
TASK_KEYS = {"model", "effort", "add_dirs", "repo_from_issue", "allowed_tools"}
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REPO = "{repo}"
# Config the runs (--setting-sources user) and launchd trust; a writable memory dir must stay out of them.
PROTECTED = ("~/.claude", "~/Library/LaunchAgents")


@dataclass(frozen=True)
class Run:
    """A runnable project resolved against [roles] and [tasks]; every path is absolute."""
    project: dict
    role_name: str
    role: dict
    task_name: str
    task: dict
    charter: str
    instructions: str
    memory: str | None
    read_only: tuple


def load_config(path=CONFIG):
    """Checks every consumer needs; runnable-project checks are in runnable()."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    projects = cfg.setdefault("projects", {})
    cfg.setdefault("roles", {})
    for name, t in cfg.setdefault("tasks", {}).items():
        check_allowed_tools(name, t)
    for name, p in projects.items():
        nxt = p.get("next")
        ...  # the existing next/cycle checks, unchanged
    return cfg


def _under(path, base):
    return os.path.commonpath([path, base]) == base


def _role(name, r, root):
    """(read_only, memory) of a [roles] entry, normalized; a broken entry stops the caller."""
    where = f"pipeline.toml: role {name!r}"
    if not NAME_RE.fullmatch(name) or name == "principles":
        raise SystemExit(f"{where}: names are lowercase-kebab and not 'principles'")
    if extra := sorted(set(r) - ROLE_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if not os.path.isfile(os.path.join(root, "roles", f"{name}.md")):
        raise SystemExit(f"{where}: charter not found: roles/{name}.md")
    if not isinstance(r.get("read_only", []), list):
        raise SystemExit(f"{where}: read_only must be a list")
    read_only = []
    for entry in r.get("read_only", []):
        path = os.path.expanduser(entry)
        if entry != REPO and (not os.path.isabs(path) or ".." in path.split(os.sep) or "{" in path or "}" in path):
            raise SystemExit(f"{where}: read_only entries are absolute paths or {REPO}: {entry!r}")
        read_only.append(entry if entry == REPO else os.path.normpath(path))
    memory = r.get("memory")
    if memory is None:
        return tuple(read_only), None
    memory = os.path.normpath(os.path.expanduser(memory))
    if not os.path.isabs(memory) or not os.path.isdir(memory):
        raise SystemExit(f"{where}: memory must be an existing absolute directory: {r['memory']!r}")
    real = os.path.realpath(memory)
    near = [os.path.realpath(root)] + [os.path.realpath(p) for p in read_only if p != REPO]
    if any(_under(real, b) or _under(b, real) for b in near) or \
            any(_under(real, os.path.realpath(os.path.expanduser(p))) for p in PROTECTED):
        raise SystemExit(f"{where}: memory must not overlap the repo root, a read_only path or {', '.join(PROTECTED)}: {r['memory']!r}")
    return tuple(read_only), memory


def _task(name, t, root):
    where = f"pipeline.toml: task {name!r}"
    if not NAME_RE.fullmatch(name):
        raise SystemExit(f"{where}: names are lowercase-kebab")
    if extra := sorted(set(t) - TASK_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if missing := [k for k in ("model", "effort") if not t.get(k)]:
        raise SystemExit(f"{where} has no {', '.join(missing)}")
    if not os.path.isfile(os.path.join(root, "tasks", f"{name}.md")):
        raise SystemExit(f"{where}: instructions not found: tasks/{name}.md")


def runnable(cfg, root=ROOT):
    """{name: Run} of projects with role + task; a broken entry anywhere in the registry stops the caller (fail loud)."""
    roles = {name: _role(name, r, root) for name, r in cfg["roles"].items()}
    for name, t in cfg["tasks"].items():
        _task(name, t, root)
    out = {}
    for name, p in cfg["projects"].items():
        if moved := [k for k in MOVED if k in p]:
            raise SystemExit(f"pipeline.toml: {name!r} has {', '.join(moved)}: these keys moved to [tasks]; a project names a role and a task")
        if "role" not in p and "task" not in p:
            continue
        if "role" not in p or "task" not in p:
            raise SystemExit(f"pipeline.toml: {name!r} needs both role and task")
        role, task = p["role"], p["task"]
        if role not in roles:
            raise SystemExit(f"pipeline.toml: {name!r} role {role!r} has no [roles.{role}] entry")
        if task not in cfg["tasks"]:
            raise SystemExit(f"pipeline.toml: {name!r} task {task!r} has no [tasks.{task}] entry")
        read_only, memory = roles[role]
        t = cfg["tasks"][task]
        if REPO in read_only and (not t.get("repo_from_issue") or "allowed_tools" in t):
            raise SystemExit(f"pipeline.toml: {name!r}: role {role!r} has read_only {REPO}, so task {task!r} needs repo_from_issue and no allowed_tools")
        out[name] = Run(p, role, cfg["roles"][role], task, t, os.path.join(root, "roles", f"{role}.md"),
                        os.path.join(root, "tasks", f"{task}.md"), memory, read_only)
    return out
```

The existing `check_allowed_tools(name, p, root=ROOT)` stays as is (it now receives a task entry).

- [ ] **Step 6: Wire `scripts/launch.py` to `Run`** (behavior per NFR-1; prompt wording changes arrive in Task 2)

```python
from pipeline import (PATH, PLACEHOLDERS, PROJECTS, REPO, ROOT, RUNS_LOG, SESSION, linear_gql, load_config,  # noqa: E402
                      project_log, run_dir, runnable, transcript)

PRINCIPLES = os.path.join(ROOT, "roles", "principles.md")


def prompt(a, run):
    if a.mode == "resume":
        return (f"Resumed run {a.k} for {a.issue} ({a.url}) after an interruption. Re-read {PRINCIPLES} and {run.instructions} "
                "first (they may have changed since this session started) and follow the stage's resume rule.")
    return f"Follow {PRINCIPLES} and {run.instructions} to handle {a.issue} ({a.url}). The runner has already claimed it."


def command(a, run, tail="", allowed=()):
    t = run.task
    dirs = [os.path.expanduser(d) for d in t.get("add_dirs", [])]
    denied = [os.path.join(ROOT, d, "**") for d in ("roles", "tasks", "templates")]
    denied.append(os.path.join(run_dir(a.issue), "worktrees", "*", ".git"))
    denied += [os.path.join(p, "**") for p in run.read_only if p != REPO]
    cmd = ["claude", "-p", prompt(a, run) + tail,
           "--resume" if a.mode == "resume" else "--session-id", a.sid,
           "--model", t["model"], "--effort", t["effort"], "--permission-mode", "auto",
           "--setting-sources", "user", "--strict-mcp-config"]
    for d in [os.path.join(ROOT, d) for d in ("roles", "tasks", "templates")] + dirs:
        cmd += ["--add-dir", d]
    cmd += ["--disallowedTools", *map(deny, denied)]
    if allowed:
        cmd += ["--allowedTools", *allowed]
    return cmd
```

In `main`: `runs = runnable(cfg)`; `if a.project not in runs:` (same message, exit 2); `job = runs[a.project]`; `plog = project_log(job.task_name, logs) if logs else project_log(job.task_name)` (drop the `name = …splitext…` line and its comment); repo step keyed on `job.task.get("repo_from_issue")` with `repo_step(a, job.task, gql or linear_gql, run)`; `cmd = command(a, job, tail, allowed)`. The module docstring's "per pipeline.toml" stays.

- [ ] **Step 7: Migrate the test fixtures**

`scripts/tests/test_router.py` — replace the comment + `CONFIG` + `PD_RUNNABLE` (the `[roles]`/`[tasks]` tables must precede the projects so appended lines land in `[projects.p-pd]`):

```python
# Role and task files must exist under the repo root.
CONFIG = """team = "T"
[roles.researcher]
[tasks.deep-research]
model = "opus"
effort = "xhigh"
[tasks.product-design]
model = "opus"
effort = "high"
[projects.p-dr]
next = "p-pd"
role = "researcher"
task = "deep-research"
[projects.p-pd]
prefix = "PRD"
"""
PD_RUNNABLE = 'role = "researcher"\ntask = "product-design"\n'
```

`scripts/tests/test_launch.py`:

```python
CONFIG = """team = "T"
human_members = ["me@x.com"]
[roles.researcher]
[roles.engineer]
read_only = ["~/playground/private_docs"]
[tasks.deep-research]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
[tasks.engineering]
model = "opus"
effort = "high"
add_dirs = ["~/playground/private_docs"]
repo_from_issue = true
[projects.p-dr]
role = "researcher"
task = "deep-research"
[projects.p-pd]
prefix = "PRD"
[projects.p-eng]
prefix = "ENG"
role = "engineer"
task = "engineering"
"""
```

Then update: `plog(self, name="deep-research")` reads `logs/projects/<name>.log`, and the three Engineering tests that call `self.plog()` (`test_engineering_resolve_error_is_transient`, `test_engineering_invalid_on_resume_is_transient`, `test_engineering_transient`) call `self.plog("engineering")`; `test_engineering_ok_fills_allowed_tools` inserts the rule into the task: `self.write_config(CONFIG.replace("repo_from_issue = true\n", "repo_from_issue = true\n" + f"allowed_tools = [{PUSH_RULE!r}]\n".replace("'", '"')))`; `test_new_prompt_and_flags` and `test_resume_prompt` use `instructions = os.path.join(pipeline.ROOT, "tasks/deep-research.md")`, `--add-dir` `{root}/roles`, `{root}/tasks`, `{root}/templates`, PRIVATE, and deny `Edit({slashes(root)}/roles/**)`, `Edit({slashes(root)}/tasks/**)`, `Edit({slashes(root)}/templates/**)`, `Edit({slashes(rd)}/worktrees/*/.git)`; `test_every_project_locked_down` asserts `roles/**` and `tasks/**` rules instead of `stages/**`.

- [ ] **Step 8: Run all tests**

Run: `python3 -m unittest discover -s scripts/tests`
Expected: OK. Also `grep -rn "stages" scripts pipeline.toml` → no hits except `test_pipeline.py`'s `stages/gone.md` literal.

- [ ] **Step 9: Commit and push**

```bash
git -C $W add -A roles tasks stages pipeline.toml scripts
git -C $W commit -m "TASK-44: roles/ + tasks/ and the [roles]/[tasks] registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

---

### Task 2: Launcher — charter, memory, `{repo}` and NFR-1 pins

**Files:**
- Modify: `scripts/launch.py` (`prompt`, `command`, `repo_step`, `main`)
- Test: `scripts/tests/test_launch.py`

**Interfaces:**
- Consumes: `pipeline.Run`, `pipeline.REPO`, `pipeline.runnable` from Task 1.
- Produces: `prompt(a, run) -> str`; `command(a, run, tail="", allowed=(), repo=None) -> list[str]` where `repo` is the `eng.Ok`/`eng.Invalid` from the repo step or None; `repo_step(a, task, gql, run) -> (tail, env, allowed, result) | eng.Transient`.

- [ ] **Step 1: Write the failing tests** (in `scripts/tests/test_launch.py`)

Update the expected prompts in `test_new_prompt_and_flags` and `test_resume_prompt`:

```python
charter = os.path.join(pipeline.ROOT, "roles/researcher.md")
# new
f"Follow {launch.PRINCIPLES}, your role charter {charter} and the task {instructions} to handle TASK-1 (https://l/TASK-1). "
"The runner has already claimed it. Reviewer: me@x.com. Humans: me@x.com. Project: p-dr."
# resume
f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {launch.PRINCIPLES}, your role charter {charter} "
f"and the task {instructions} first (they may have changed since this session started) and follow the task's resume rule."
" Reviewer: me@x.com. Humans: me@x.com. Project: p-dr."
```

Add `self.assertEqual(launch.PRINCIPLES, os.path.join(pipeline.ROOT, "roles", "principles.md"))` to `test_new_prompt_and_flags`.

New tests:

```python
    def with_memory(self):
        mem = os.path.join(self.tmp, "mem")
        os.makedirs(mem)
        self.write_config(CONFIG.replace("[roles.researcher]\n", f'[roles.researcher]\nmemory = "{mem}"\n'))
        return mem

    def test_memory_prompt_and_dir(self):
        mem = self.with_memory()
        self.run_launch(*self.args())
        argv = self.claude()
        self.assertIn(f"The runner has already claimed it. Your role memory: {mem}; your role charter says how to use it."
                      " Reviewer: me@x.com.", argv[2])
        self.assertEqual([argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"][-1], mem)
        self.assertFalse(any(mem in r for r in self.after(argv, "--disallowedTools")))

    def test_memory_on_resume(self):
        mem = self.with_memory()
        self.make_transcript()
        self.run_launch(*self.args("resume"))
        self.assertIn(f"follow the task's resume rule. Your role memory: {mem}; your role charter says how to use it.", self.claude()[2])

    def repo_config(self):
        self.write_config(CONFIG.replace('read_only = ["~/playground/private_docs"]', 'read_only = ["{repo}"]'))

    def test_repo_read_only_ok(self):
        self.repo_config()
        self.launch_eng(self.ok())
        rules = self.after(self.claude(), "--disallowedTools")
        rd = os.path.join(self.work, "TASK-1")
        self.assertEqual(rules[-2:], [f"Edit({slashes(self.ok().clone)}/**)", f"Edit({slashes(rd)}/worktrees/**)"])
        self.assertNotIn(f"Edit({slashes(PRIVATE)}/**)", rules)

    def test_repo_read_only_invalid(self):
        self.repo_config()
        self.launch_eng(eng.Invalid("no Repo: line"))
        rules = self.after(self.claude(), "--disallowedTools")
        rd = os.path.join(self.work, "TASK-1")
        self.assertEqual(rules[-1], f"Edit({slashes(rd)}/worktrees/**)")
        self.assertFalse(any("/u/playground/demo" in r for r in rules))

    def test_log_named_after_task(self):
        self.launch_eng(eng.Transient("x"))
        self.assertIn("transient TASK-1: x", self.plog("engineering"))


class RealConfig(unittest.TestCase):
    """NFR-1: the three runs of the repo's pipeline.toml, command for command."""
    DR, PD, ENG = ("03495382-48f7-4280-a11c-4375df80a561", "ba0738ba-ade7-4525-8d79-1b9944334e74",
                   "ddbff8bf-b633-4b8c-9272-d1d5ee923747")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")
        patch = mock.patch.object(pipeline, "WORK", self.work)
        patch.start()
        self.addCleanup(patch.stop)
        self.projects = os.path.join(self.tmp, "projects")
        self.calls = []
        self.humans = pipeline.load_config()["human_members"]

    def launch(self, project, mode="new", repo=None):
        argv = ["--issue", "TASK-1", "--url", "https://l/TASK-1", "--project", project, "--sid", SID, "--mode", mode]
        if mode == "resume":
            argv += ["--k", "2"]
            path = pipeline.transcript("TASK-1", SID, self.projects)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        with redirect_stderr(io.StringIO()), mock.patch.dict(os.environ), \
                mock.patch.object(eng, "resolve", return_value=repo):
            rc = launch.main(argv, sh=lambda cmd, **kw: self.calls.append(cmd), runs=os.path.join(self.tmp, "runs.log"),
                             logs=os.path.join(self.tmp, "logs"), gql=object(), run=object(), projects=self.projects)
        self.assertEqual(rc, 0)
        (cmd,) = self.calls
        toks = shlex.split(cmd[9])
        i = toks.index("claude")
        return toks[i:toks.index("<", i)], cmd[9]

    def expected(self, project, role, task, effort, extra_deny=(), tail=""):
        root, rd = pipeline.ROOT, os.path.join(self.work, "TASK-1")
        humans = self.humans
        return [
            "claude", "-p",
            f"Follow {root}/roles/principles.md, your role charter {root}/roles/{role}.md and the task {root}/tasks/{task}.md"
            " to handle TASK-1 (https://l/TASK-1). The runner has already claimed it."
            f" Reviewer: {humans[0]}. Humans: {', '.join(humans)}. Project: {project}." + tail,
            "--session-id", SID, "--model", "opus", "--effort", effort, "--permission-mode", "auto",
            "--setting-sources", "user", "--strict-mcp-config",
            "--add-dir", f"{root}/roles", "--add-dir", f"{root}/tasks", "--add-dir", f"{root}/templates", "--add-dir", PRIVATE,
            "--disallowedTools", f"Edit({slashes(root)}/roles/**)", f"Edit({slashes(root)}/tasks/**)",
            f"Edit({slashes(root)}/templates/**)", f"Edit({slashes(rd)}/worktrees/*/.git)", *extra_deny]

    def test_deep_research(self):
        argv, script = self.launch(self.DR)
        self.assertEqual(argv, self.expected(self.DR, "researcher", "deep-research", "xhigh"))
        self.assertIn(os.path.join(self.tmp, "logs", "projects", "deep-research.log"), script)

    def test_product_design(self):
        argv, script = self.launch(self.PD)
        self.assertEqual(argv, self.expected(self.PD, "pm", "product-design", "high"))
        self.assertIn(os.path.join(self.tmp, "logs", "projects", "product-design.log"), script)

    def test_engineering(self):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        ok = eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", "/u/playground/demo", "main", "TASK-1-demo", wt)
        argv, script = self.launch(self.ENG, repo=ok)
        self.assertEqual(argv, self.expected(
            self.ENG, "engineer", "engineering", "xhigh", extra_deny=[f"Edit({slashes(PRIVATE)}/**)"],
            tail=" Repo check: OK ophis/demo, clone /u/playground/demo, default branch main,"
                 f" branch TASK-1-demo, worktree {wt}. eng.py: python3 {ENG_PY}."))
        self.assertIn(" AGENT_PM_ISSUE=TASK-1", script.split(";")[0])
        self.assertIn(os.path.join(self.tmp, "logs", "projects", "engineering.log"), script)

    def test_real_config_resume_prompt(self):
        argv, _ = self.launch(self.DR, mode="resume")
        root = pipeline.ROOT
        self.assertEqual(argv[2].split(" Reviewer:")[0],
                         f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {root}/roles/principles.md,"
                         f" your role charter {root}/roles/researcher.md and the task {root}/tasks/deep-research.md first"
                         " (they may have changed since this session started) and follow the task's resume rule.")
        self.assertEqual(argv[3:5], ["--resume", SID])
```

(`launch.main` without `config=` loads the repo's `pipeline.toml`.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest discover -s scripts/tests -k launch`
Expected: FAIL on the prompt strings, memory and `{repo}` tests.

- [ ] **Step 3: Implement in `scripts/launch.py`**

```python
def prompt(a, run):
    docs = f"{PRINCIPLES}, your role charter {run.charter} and the task {run.instructions}"
    if a.mode == "resume":
        text = (f"Resumed run {a.k} for {a.issue} ({a.url}) after an interruption. Re-read {docs} "
                "first (they may have changed since this session started) and follow the task's resume rule.")
    else:
        text = f"Follow {docs} to handle {a.issue} ({a.url}). The runner has already claimed it."
    if run.memory:
        text += f" Your role memory: {run.memory}; your role charter says how to use it."
    return text


def command(a, run, tail="", allowed=(), repo=None):
    t, rd = run.task, run_dir(a.issue)
    dirs = [os.path.join(ROOT, d) for d in ("roles", "tasks", "templates")]
    dirs += [os.path.expanduser(d) for d in t.get("add_dirs", [])]
    if run.memory:
        dirs.append(run.memory)
    denied = [os.path.join(ROOT, d, "**") for d in ("roles", "tasks", "templates")]
    denied.append(os.path.join(rd, "worktrees", "*", ".git"))
    for p in run.read_only:
        if p != REPO:
            denied.append(os.path.join(p, "**"))
            continue
        if isinstance(repo, eng.Ok):
            denied.append(os.path.join(repo.clone, "**"))
        denied.append(os.path.join(rd, "worktrees", "**"))
    cmd = ["claude", "-p", prompt(a, run) + tail,
           "--resume" if a.mode == "resume" else "--session-id", a.sid,
           "--model", t["model"], "--effort", t["effort"], "--permission-mode", "auto",
           "--setting-sources", "user", "--strict-mcp-config"]
    for d in dirs:
        cmd += ["--add-dir", d]
    cmd += ["--disallowedTools", *map(deny, denied)]
    if allowed:
        cmd += ["--allowedTools", *allowed]
    return cmd
```

`repo_step(a, task, gql, run)`: return `(f" Repo check failed: {r.reason}.{cli}", {}, [], r)` for Invalid and `(tail, {"AGENT_PM_ISSUE": a.issue}, [...], r)` for Ok (templates from `task.get("allowed_tools", [])`). In `main`: `extra, env, allowed, repo = step`, and `cmd = command(a, job, tail, allowed, repo)` (`repo = None` before the repo-step branch).

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s scripts/tests`
Expected: OK.

- [ ] **Step 5: Commit and push**

```bash
git -C $W add scripts/launch.py scripts/tests/test_launch.py
git -C $W commit -m "TASK-44: launcher loads the role charter and memory, applies role read_only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

---

### Task 3: Docs

**Files:**
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:** none (describes Tasks 1–2).

- [ ] **Step 1: `CLAUDE.md`**
  - Intro: "…a router runs the runnable projects in `pipeline.toml` (Deep Research, Product Design, Engineering), each by a role doing a task, and promote hands issues between projects."
  - Architecture › Launcher: "looks the project up in `pipeline.toml` (`runnable()` resolves its `role` and `task`) and starts `claude -p` in tmux; output and the `end` line go to `logs/projects/<task>.log`, the `end` line also to `runs.log`."
  - Replace "Runner vs stage" with "Runner vs role and task": `scripts/` decides what runs; `roles/<role>.md` is the role's charter (identity: responsibilities, boundaries, memory; no task steps), `tasks/<task>.md` what the agent does (filling a skeleton from `templates/` where it has one), `roles/principles.md` the rules every role and task shares (board, reviewer, agent account, whose comments count; precedence principles > charter boundaries > task steps). All three pass by absolute path in the prompt (not skills), plus the role's `memory` dir when set. Every task file must define a resume rule; resumed sessions re-read all three mid-flight. The registry: `[roles.<role>]` (`read_only`, `memory`), `[tasks.<task>]` (`model`, `effort`, `add_dirs`, `repo_from_issue`, `allowed_tools`), `[projects."<id>"]` (`role` + `task` = runnable; `next`, `prefix`, `require_instructions`); `runnable()` rejects moved keys, unknown keys, missing files and a `memory` overlapping the repo, a `read_only` path, `~/.claude` or `~/Library/LaunchAgents`.
  - Architecture › Engineering: `tasks/engineering.md` instead of `stages/engineering.md`; "for `repo_from_issue` tasks".
  - Gotchas first bullet: `--add-dir` for `roles/`, `tasks/`, `templates/`, the task's `add_dirs` and the role's `memory`; Edit deny rules for `roles/`, `tasks/`, `templates/`, worktree `.git` files and the role's `read_only` (`{repo}` = the issue's clone and `work/<ID>/worktrees/`); "any new path a task needs goes in its `add_dirs`".
- [ ] **Step 2: `README.md`**
  - Line 3: "Runs agent roles and tasks from the Linear board (team Frank's Agents): each runnable project has one role (who: charter, memory, read-only scope) doing one task (what: the procedure)." Keep the rest of the paragraph; "Promote hands reviewed issues to the next project."
  - Logs bullet: "`projects/<task>.log` (named after the task)".
  - Replace the `stages/<stage>.md` bullet with two bullets: `roles/` (`principles.md`, rules shared by every role and task; `<role>.md`, a role's charter) and `tasks/<task>.md` (the procedure; each must define a resume rule); the launcher passes all three paths in the prompt, plus the role's memory dir when set.
  - `templates/` bullet: "a task fills in".
  - `scripts/launch.py` bullet: "from the project's role and task in `pipeline.toml`"; `--add-dir` for `roles/`, `tasks/`, `templates/`, the task's `add_dirs` and the role's `memory`; Edit deny for `roles/`, `tasks/`, `templates/`, the worktrees' `.git` files and the role's `read_only`. Repo step "for `repo_from_issue` tasks"; "the command the task runs".
  - `eng.py` bullet: "for the task".
  - `pipeline.toml` bullet: roles (`read_only`: absolute paths or `{repo}`; `memory`: an existing dir outside the repo, the role's `read_only`, `~/.claude` and `~/Library/LaunchAgents`), tasks (`model`, `effort`, `add_dirs`, `repo_from_issue`, `allowed_tools` with the existing rule-template wording), projects (`next`, `prefix`, `require_instructions`, `role` + `task` for runnable ones). Keep the `allowed_tools` detail text.
  - Resuming bullet: "re-read `roles/principles.md`, the charter and `tasks/deep-research.md` and follow the task's resume rule".
  - Design line: append `docs/specs/2026-09-28-task-44-role-task-design.md`.
- [ ] **Step 3: Check**

Run: `grep -n "stages\|instructions file\|stage's" CLAUDE.md README.md` → no hits except "later stage" ordering wording in the router bullet (unchanged behavior) and `require_instructions`.

- [ ] **Step 4: Commit and push**

```bash
git -C $W add CLAUDE.md README.md
git -C $W commit -m "TASK-44: docs for roles and tasks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

---

## Verification (S6)

From the worktree root:
- `python3 -m unittest discover -s scripts/tests` → OK.
- `python3 scripts/router.py --now --dry-run` → loads the migrated registry; prints a plan; changes nothing (it may run the tiny usage probe).
- `python3 scripts/promote.py --dry-run` → changes nothing.
- `grep -rn "stages/" --include=*.py --include=*.toml --include=*.md . | grep -v docs/specs` → no hits.

## Progress

- S1: worktree created from origin/main (db150ef).
- decision(validation scope): runnable() validates every declared role/task, not only referenced ones - surfaces registry errors at deploy (NFR-2); dissent: none
- decision(allowed_tools check location): stays in load_config, now over [tasks] - keeps today's behavior for every consumer; dissent: none
- decision(charter memory section): heading present, body empty (FR-2a literal); dissent: none
- S2: spec written (docs/specs/2026-09-28-task-44-role-task-design.md).
- S3 panel: core=[architecture,spec-fitness] +optional=[security]
- S3 reviewers: architecture=abddcf6a9cc330698 spec-fitness=ae1ff96f3f1a26822 security=a86d36e6dafb2b314
- S3 r0: architecture=PASS spec-fitness=PASS security=FAIL -> security#1 memory may overlap ROOT/ancestors (writable runner); fix: memory not at/under ROOT nor ancestor of ROOT or read_only; also applied NBs: normalized read_only, unknown-key check, Run carries resolved paths, repo_step returns result, one-commit/NFR-3/post-deploy notes
- S3 r1: architecture=PASS spec-fitness=PASS security=PASS -> converged. Applied NBs: resume wording; memory also not at/under ~/.claude or ~/Library/LaunchAgents. Deferred NBs: security#3 symlinked read_only, #4 add_dirs overlap, #5 allowed_tools vs read_only (all unchanged from today), #6 memory as injection channel (TASK-33).
- S4: plan written (3 tasks: registry+layout, launcher, docs); execution: subagent-driven-development, per-task reviews kept, final review skipped (S7).
