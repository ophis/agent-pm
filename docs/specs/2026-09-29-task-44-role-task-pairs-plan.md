# TASK-44: Role and task file pairs (change request) — plan

RESUME: phase=S7 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=124a7b7802c1e68922489d61d478398870853632 review_round=0 spec_file=docs/specs/2026-09-29-task-44-role-task-pairs-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Role and task settings move from `pipeline.toml` into `roles/<role>.toml` / `tasks/<task>.toml` pairs beside their `.md`; tighter memory checks; the router refuses to tick with an uncommitted registry.

**Architecture:** `pipeline.registry(root)` scans and validates the pairs; `pipeline.runnable(cfg, root)` validates `pipeline.toml` and joins projects to them, returning a slimmer `Run`. `launch.py` adds one config error (memory vs the issue's repo). `router.tick` runs a git-status guard before anything else touches Linear.

**Tech Stack:** Python 3.11+ standard library (`tomllib`, `dataclasses`, `unittest`), git, TOML, Markdown.

**Spec:** `docs/specs/2026-09-29-task-44-role-task-pairs-design.md` (authoritative where this plan is terse). It amends `docs/specs/2026-09-28-task-44-role-task-design.md`.

## Global Constraints

- Worktree `/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task`, branch `TASK-44-role-task`; before any write assert `git -C <worktree> branch --show-current` prints `TASK-44-role-task`. Absolute paths only. Never touch `/Users/francis/playground/agent-pm` itself (the live install), never `main`, never force-push, never merge.
- After each task's commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task`.
- Standard library only. Match the files' style: terse docstrings, `SystemExit("<file>: …")` for config errors, no comments unless a gotcha.
- Tests: `python3 -m unittest discover -s scripts/tests` from the worktree root, all green at the end of every task.
- NFR-1: the real config's three `claude` argv (test_launch `RealConfig`) must not change.
- `router.py --now --dry-run` reads the worktree's own `roles/` / `tasks/`: run it only after committing (the guard stops it otherwise).

## Review Focus

- A `.DS_Store` or editor swap file in `roles/` must not stop the router or the loader → Task 1 `test_other_files_ignored`, Task 3 `test_ds_store_alone_ticks`.
- A path with spaces or a rename in `git status -z` output must be parsed right → Task 3 `test_uncommitted_parse`.
- memory = `$HOME` (ancestor of `~/.claude`) must fail, while a sibling like `$HOME/notes` still passes → Task 1 `test_memory_protected_both_ways`.
- The memory-vs-repo check must not fire for a non-`repo_from_issue` task → Task 2 `test_memory_check_skipped_without_repo_step`.
- Promote keeps working with old `[roles]` / `[tasks]` tables still in `pipeline.toml` (half-migrated live clone) → Task 1 `test_load_config_ignores_old_tables`.

---

### Task 1: Role/task file pairs, loader, `Run`, config migration

**Files:**
- Create: `roles/researcher.toml`, `roles/pm.toml`, `roles/engineer.toml`, `tasks/deep-research.toml`, `tasks/product-design.toml`, `tasks/engineering.toml`
- Modify: `pipeline.toml`, `scripts/pipeline.py:86-230`, `scripts/launch.py` (`main` signature only), `scripts/tests/test_pipeline.py`, `scripts/tests/test_launch.py`, `scripts/tests/test_router.py:24-41`

**Interfaces:**
- Produces: `pipeline.registry(root=ROOT) -> (roles, tasks)` where `roles = {name: (read_only: tuple, memory: str | None)}`, `tasks = {name: dict}`; `pipeline.runnable(cfg, root=ROOT) -> {project_id: Run}`; `Run(task_name, task, charter, instructions, memory, read_only)`; `pipeline.overlaps(a, b) -> bool`; `pipeline.check_allowed_tools(where, p, root=ROOT)`; `launch.main(..., root=ROOT)` (passed to `runnable`).

- [ ] **Step 1: Write the registry files and migrate `pipeline.toml`**

`roles/researcher.toml`, `roles/pm.toml`: empty files.

`roles/engineer.toml`:
```toml
read_only = ["~/playground/private_docs"]   # the PRDs
```
`tasks/deep-research.toml`:
```toml
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
```
`tasks/product-design.toml`:
```toml
model = "opus"
effort = "high"
add_dirs = ["~/playground/private_docs"]
```
`tasks/engineering.toml`:
```toml
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
repo_from_issue = true                    # the launcher resolves and clones the issue's target repo
```
In `pipeline.toml` delete the `# Roles: …` and `# Tasks: …` comment blocks and every `[roles.*]` / `[tasks.*]` table; replace the projects comment with:
```toml
# Projects are keyed by their Linear project id (a name or URL slug changes with a rename). role + task make one runnable:
# the pairs roles/<role>.md + roles/<role>.toml and tasks/<task>.md + tasks/<task>.toml.
```
The three `[projects."…"]` tables are unchanged.

- [ ] **Step 2: Rewrite the `Runnable` tests in `scripts/tests/test_pipeline.py`**

Add `import dataclasses`. Replace `REGISTRY` and the whole `class Runnable` with:

```python
PROJECTS = """team = "T"
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
FILES = {
    "roles/principles.md": "",
    "roles/researcher.md": "", "roles/researcher.toml": "",
    "roles/engineer.md": "", "roles/engineer.toml": 'read_only = ["~/playground/private_docs"]\n',
    "tasks/deep-research.md": "",
    "tasks/deep-research.toml": 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/playground/private_docs"]\n',
    "tasks/engineering.md": "", "tasks/engineering.toml": 'model = "opus"\neffort = "high"\nrepo_from_issue = true\n',
}
ENGINEERING = FILES["tasks/engineering.toml"]


class Runnable(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "root")
        self.outside = os.path.join(tmp.name, "outside")
        os.makedirs(self.outside)
        os.makedirs(os.path.join(self.root, "templates"))
        for rel, text in FILES.items():
            self.write(rel, text)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def remove(self, rel):
        os.remove(os.path.join(self.root, rel))

    def load(self, text=PROJECTS):
        path = os.path.join(self.outside, "pipeline.toml")
        with open(path, "w") as f:
            f.write(text)
        return pipeline.load_config(path)

    def runs(self, text=PROJECTS):
        return pipeline.runnable(self.load(text), root=self.root)

    def rejects(self, fragment, prefix="pipeline.toml", text=PROJECTS):
        with self.assertRaises(SystemExit) as cm:
            self.runs(text)
        msg = str(cm.exception.code)
        self.assertTrue(msg.startswith(prefix + ":") or msg.startswith(prefix + " "), msg)
        self.assertIn(fragment, msg)

    def test_runs(self):
        runs = self.runs()
        self.assertEqual(sorted(runs), ["dr", "eng"])
        dr, eng = runs["dr"], runs["eng"]
        self.assertEqual([f.name for f in dataclasses.fields(pipeline.Run)],
                         ["task_name", "task", "charter", "instructions", "memory", "read_only"])
        self.assertEqual((dr.task_name, dr.task["effort"], dr.memory, dr.read_only), ("deep-research", "xhigh", None, ()))
        self.assertEqual(dr.charter, os.path.join(self.root, "roles", "researcher.md"))
        self.assertEqual(dr.instructions, os.path.join(self.root, "tasks", "deep-research.md"))
        self.assertEqual(eng.read_only, (os.path.expanduser("~/playground/private_docs"),))
        self.assertTrue(eng.task["repo_from_issue"])

    def test_read_only_normalized(self):
        self.write("roles/engineer.toml", 'read_only = ["~/playground/private_docs/"]\n')
        self.assertEqual(self.runs()["eng"].read_only, (os.path.expanduser("~/playground/private_docs"),))

    def test_old_tables_rejected(self):
        self.rejects("unknown keys: roles", text=PROJECTS + "[roles.researcher]\n")
        self.rejects("unknown keys: tasks", text=PROJECTS + '[tasks.x]\nmodel = "opus"\n')

    def test_project_keys_rejected(self):
        for key, value in (("instructions", '"tasks/x.md"'), ("model", '"opus"'), ("effort", '"high"'), ("add_dirs", "[]"),
                           ("repo_from_issue", "true"), ("allowed_tools", "[]"), ("prefx", '"I"')):
            with self.subTest(key):
                self.rejects(key, text=PROJECTS.replace('prefix = "I"\n', f'prefix = "I"\n{key} = {value}\n'))

    def test_load_config_ignores_old_tables(self):
        cfg = self.load(PROJECTS + '[roles.researcher]\n[projects.old]\ninstructions = "stages/gone.md"\n')
        self.assertIn("old", cfg["projects"])

    def test_role_and_task_together(self):
        self.rejects("both role and task", text=PROJECTS.replace('role = "engineer"\n', ""))
        self.rejects("both role and task", text=PROJECTS.replace('task = "engineering"\n', ""))

    def test_unknown_role_or_task(self):
        self.rejects("'em'", text=PROJECTS.replace('role = "engineer"', 'role = "em"'))
        self.rejects("'principles'", text=PROJECTS.replace('role = "engineer"', 'role = "principles"'))
        self.rejects("'work-breakdown'", text=PROJECTS.replace('task = "engineering"', 'task = "work-breakdown"'))

    def test_orphans(self):
        for rel, other in (("roles/engineer.toml", "roles/engineer.md"), ("roles/engineer.md", "roles/engineer.toml"),
                           ("tasks/engineering.toml", "tasks/engineering.md"), ("tasks/engineering.md", "tasks/engineering.toml")):
            with self.subTest(rel):
                self.remove(rel)
                self.rejects(rel, prefix=other)
                self.write(rel, FILES[rel])

    def test_unreferenced_orphan_rejected(self):
        self.write("tasks/light-research.toml", 'model = "opus"\neffort = "high"\n')
        self.rejects("tasks/light-research.md", prefix="tasks/light-research.toml")

    def test_principles_has_no_toml(self):
        self.write("roles/principles.toml", "")
        self.rejects("principles", prefix="roles/principles.toml")

    def test_other_files_ignored(self):
        self.write("roles/.DS_Store", "x")
        self.write("roles/notes.txt", "x")
        self.write("tasks/.engineering.toml.swp", "x")
        self.write("roles/drafts/x.md", "")
        self.assertEqual(sorted(self.runs()), ["dr", "eng"])

    def test_bad_names(self):
        self.write("roles/Researcher.md", "")
        self.write("roles/Researcher.toml", "")
        self.rejects("kebab", prefix="roles/Researcher.md")
        self.remove("roles/Researcher.md")
        self.remove("roles/Researcher.toml")
        self.write("tasks/deep_research.md", "")
        self.write("tasks/deep_research.toml", 'model = "opus"\neffort = "high"\n')
        self.rejects("kebab", prefix="tasks/deep_research.md")

    def test_bad_toml(self):
        self.write("tasks/engineering.toml", "model = \n")
        self.rejects("", prefix="tasks/engineering.toml")

    def test_task_needs_model_and_effort(self):
        self.write("tasks/deep-research.toml", 'model = "opus"\n')
        self.rejects("effort", prefix="tasks/deep-research.toml")
        self.write("tasks/deep-research.toml", 'effort = "high"\n')
        self.rejects("model", prefix="tasks/deep-research.toml")

    def test_unknown_keys(self):
        self.write("roles/engineer.toml", 'readonly = ["~/playground/private_docs"]\n')
        self.rejects("readonly", prefix="roles/engineer.toml")
        self.write("roles/engineer.toml", FILES["roles/engineer.toml"])
        self.write("tasks/engineering.toml", ENGINEERING + 'instructions = "x"\n')
        self.rejects("instructions", prefix="tasks/engineering.toml")

    def test_read_only_paths(self):
        for bad in ('["playground/private_docs"]', '["~/playground/../private_docs"]', '["{repo}/x"]', '"/"'):
            with self.subTest(bad):
                self.write("roles/engineer.toml", f"read_only = {bad}\n")
                self.rejects("read_only", prefix="roles/engineer.toml")

    def test_repo_read_only(self):
        self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n')
        self.assertEqual(self.runs()["eng"].read_only, ("{repo}",))
        self.rejects("{repo}", text=PROJECTS.replace('role = "researcher"', 'role = "engineer"'))
        self.write("tasks/engineering.toml", ENGINEERING + "allowed_tools = []\n")
        self.rejects("{repo}")

    def memory(self, path, read_only="~/playground/private_docs"):
        self.write("roles/engineer.toml", f'read_only = ["{read_only}"]\nmemory = "{path}"\n')

    def test_memory_ok(self):
        mem = os.path.join(self.outside, "engineer")
        os.makedirs(mem)
        self.memory(mem)
        self.assertEqual(self.runs()["eng"].memory, mem)

    def test_memory_rejected(self):
        ro = os.path.join(self.outside, "docs")
        for d in (ro, os.path.join(ro, "sub"), os.path.join(self.root, "notes")):
            os.makedirs(d, exist_ok=True)
        cases = [("relative", "mem"), ("missing", os.path.join(self.outside, "nope")),
                 ("under root", os.path.join(self.root, "notes")), ("root itself", self.root),
                 ("roles", os.path.join(self.root, "roles")), ("ancestor of root", os.path.dirname(self.root)),
                 ("at read_only", ro), ("under read_only", os.path.join(ro, "sub")), ("ancestor of read_only", self.outside)]
        for label, path in cases:
            with self.subTest(label):
                self.memory(path, read_only=ro)
                self.rejects("memory", prefix="roles/engineer.toml")

    def test_memory_symlink_into_root_rejected(self):
        link = os.path.join(self.outside, "link")
        os.symlink(os.path.join(self.root, "templates"), link)
        self.memory(link)
        self.rejects("memory", prefix="roles/engineer.toml")

    def test_memory_protected_both_ways(self):
        home = os.path.join(self.outside, "home")
        for d in (".claude/mem", "Library/LaunchAgents/mem", "notes"):
            os.makedirs(os.path.join(home, d))
        ro = os.path.join(self.outside, "docs")
        os.makedirs(ro)
        with mock.patch.dict(os.environ, {"HOME": home}):
            for d in (".claude", ".claude/mem", "Library/LaunchAgents", "Library/LaunchAgents/mem", "Library", ""):
                with self.subTest(d or "home"):
                    self.memory(os.path.join(home, d).rstrip("/"), read_only=ro)
                    self.rejects("memory", prefix="roles/engineer.toml")
            self.memory(os.path.join(home, "notes"), read_only=ro)
            self.assertEqual(self.runs()["eng"].memory, os.path.join(home, "notes"))

    def test_allowed_tools_checked_in_task_file(self):
        self.write("tasks/engineering.toml", ENGINEERING + 'allowed_tools = ["Bash(git push origin *)"]\n')
        self.rejects("wildcard", prefix="tasks/engineering.toml")
        rule = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})"
        self.write("tasks/engineering.toml", ENGINEERING + f'allowed_tools = ["{rule}"]\n')
        self.assertEqual(self.runs()["eng"].task["allowed_tools"], [rule])

    def test_load_config_does_not_need_files(self):
        self.remove("tasks/engineering.md")
        self.assertIn("eng", self.load()["projects"])
```

In `class RealConfig.test_three_runs` build `got` from `(r.task_name, r.task["model"], r.task["effort"], bool(r.task.get("repo_from_issue")), r.read_only, r.memory, os.path.basename(r.charter))` with expected tuples `("deep-research", "opus", "xhigh", False, (), None, "researcher.md")`, `("product-design", "opus", "high", False, (), None, "pm.md")`, `("engineering", "opus", "xhigh", True, (private,), None, "engineer.md")`.

In the `allowed_tools` test class: `check()` calls `pipeline.check_allowed_tools("tasks/engineering.toml", p)`; `test_rejects_root_in_home_forms` passes `"tasks/e.toml"`; `test_malformed_template_is_config_error` asserts `startswith("tasks/engineering.toml: ")`.

- [ ] **Step 3: Run the pipeline tests to see them fail**

Run: `python3 -m unittest discover -s scripts/tests -k test_pipeline -v 2>&1 | tail -30`
Expected: FAIL/ERROR (e.g. `Run` fields, `roles/…` prefixes, `registry` missing).

- [ ] **Step 4: Implement in `scripts/pipeline.py`**

Replace `check_allowed_tools`'s `name` parameter with `where` (the file, e.g. `tasks/engineering.toml`); every message becomes `f"{where}: …"` (e.g. `f"{where}: allowed_tools rule has a wildcard: {rule!r}"`, `f"{where}: allowed_tools without repo_from_issue"`); `_fields(where, rule)` likewise. Its docstring and the `_root_forms(root)` comparison are unchanged (default `root=ROOT`; `_task` never passes a root).

Replace everything from `MOVED = (…)` through the end of `runnable` with:

```python
TOP_KEYS = {"team", "human_members", "projects"}
PROJECT_KEYS = {"next", "prefix", "require_instructions", "role", "task"}
ROLE_KEYS = {"read_only", "memory"}
TASK_KEYS = {"model", "effort", "add_dirs", "repo_from_issue", "allowed_tools"}
SETTINGS = "role and task settings live in roles/<role>.toml and tasks/<task>.toml"
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REPO = "{repo}"
# Config the runs (--setting-sources user) and launchd trust; a writable memory dir must stay out of them.
PROTECTED = ("~/.claude", "~/Library/LaunchAgents")


@dataclass(frozen=True)
class Run:
    """A runnable project's task and role, resolved from roles/ and tasks/; every path is absolute."""
    task_name: str
    task: dict
    charter: str
    instructions: str
    memory: str | None
    read_only: tuple
```

Keep `load_config` but delete its `cfg.setdefault("roles", {})` line and the `for name, t in cfg.setdefault("tasks", {}).items(): check_allowed_tools(...)` loop (it keeps `projects` and the `next` checks). Then:

```python
def _under(path, base):
    return os.path.commonpath([path, base]) == base


def overlaps(a, b):
    """True when one path is at or under the other, compared by realpath."""
    a, b = os.path.realpath(a), os.path.realpath(b)
    return _under(a, b) or _under(b, a)


def _pairs(root, kind):
    """{name: parsed <kind>/<name>.toml} for every .md + .toml pair in root/<kind>; other files are ignored."""
    d = os.path.join(root, kind)
    found = {}
    for f in sorted(os.listdir(d)):
        stem, ext = os.path.splitext(f)
        if ext in (".md", ".toml") and os.path.isfile(os.path.join(d, f)):
            found.setdefault(stem, set()).add(ext)
    if kind == "roles" and ".toml" in found.pop("principles", set()):
        raise SystemExit("roles/principles.toml: principles.md is not a role and has no .toml")
    out = {}
    for name, exts in found.items():
        where = f"{kind}/{name}{min(exts)}"
        if not NAME_RE.fullmatch(name):
            raise SystemExit(f"{where}: names are lowercase-kebab")
        if len(exts) == 1:
            (ext,) = exts
            raise SystemExit(f"{kind}/{name}{ext}: has no {kind}/{name}{'.toml' if ext == '.md' else '.md'}")
        try:
            with open(os.path.join(d, f"{name}.toml"), "rb") as f:
                out[name] = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"{kind}/{name}.toml: {e}") from None
    return out


def _role(name, r, root):
    """(read_only, memory) of roles/<name>.toml, normalized; a broken file stops the caller."""
    where = f"roles/{name}.toml"
    if extra := sorted(set(r) - ROLE_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
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
    near = [root] + [p for p in read_only if p != REPO] + [os.path.expanduser(p) for p in PROTECTED]
    if any(overlaps(memory, b) for b in near):
        raise SystemExit(f"{where}: memory must not overlap the repo root, a read_only path or {', '.join(PROTECTED)}: {r['memory']!r}")
    return tuple(read_only), memory


def _task(name, t):
    where = f"tasks/{name}.toml"
    if extra := sorted(set(t) - TASK_KEYS):
        raise SystemExit(f"{where} has unknown keys: {', '.join(extra)}")
    if missing := [k for k in ("model", "effort") if not t.get(k)]:
        raise SystemExit(f"{where} has no {', '.join(missing)}")
    check_allowed_tools(where, t)


def registry(root=ROOT):
    """(roles, tasks) from root/roles and root/tasks, every pair validated: {name: (read_only, memory)}, {name: task table}."""
    roles = {name: _role(name, r, root) for name, r in _pairs(root, "roles").items()}
    tasks = _pairs(root, "tasks")
    for name, t in tasks.items():
        _task(name, t)
    return roles, tasks


def runnable(cfg, root=ROOT):
    """{project id: Run} of projects with role + task; a broken pipeline.toml, role or task stops the caller (fail loud)."""
    if extra := sorted(set(cfg) - TOP_KEYS):
        raise SystemExit(f"pipeline.toml has unknown keys: {', '.join(extra)}; {SETTINGS}")
    roles, tasks = registry(root)
    out = {}
    for name, p in cfg["projects"].items():
        if extra := sorted(set(p) - PROJECT_KEYS):
            raise SystemExit(f"pipeline.toml: {name!r} has unknown keys: {', '.join(extra)}; {SETTINGS}")
        if "role" not in p and "task" not in p:
            continue
        if "role" not in p or "task" not in p:
            raise SystemExit(f"pipeline.toml: {name!r} needs both role and task")
        role, task = p["role"], p["task"]
        if role not in roles:
            raise SystemExit(f"pipeline.toml: {name!r} role {role!r} has no roles/<role>.md + .toml pair")
        if task not in tasks:
            raise SystemExit(f"pipeline.toml: {name!r} task {task!r} has no tasks/<task>.md + .toml pair")
        read_only, memory = roles[role]
        t = tasks[task]
        if REPO in read_only and (not t.get("repo_from_issue") or "allowed_tools" in t):
            raise SystemExit(f"pipeline.toml: {name!r}: role {role!r} has read_only {REPO}, so task {task!r} needs repo_from_issue and no allowed_tools")
        out[name] = Run(task, t, os.path.join(root, "roles", f"{role}.md"), os.path.join(root, "tasks", f"{task}.md"), memory, read_only)
    return out
```
Note `_under` moves above `overlaps` (it was below `load_config`; keep one definition).

- [ ] **Step 5: Run the pipeline tests**

Run: `python3 -m unittest discover -s scripts/tests -k test_pipeline 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 6: `launch.main` takes the registry root; migrate the launcher and router fixtures**

`scripts/launch.py`: import `ROOT` is already there; change the signature to `def main(argv, sh=subprocess.run, config=None, runs=RUNS_LOG, logs=None, gql=None, run=eng.sh_run, projects=PROJECTS, root=ROOT):` and `jobs = runnable(cfg, root)`.

`scripts/tests/test_launch.py`: `CONFIG` keeps only `team`, `human_members` and the three `[projects.*]` tables (drop `[roles.*]` / `[tasks.*]`). Add:
```python
REGISTRY = {
    "roles/principles.md": "", "roles/researcher.md": "", "roles/researcher.toml": "",
    "roles/engineer.md": "", "roles/engineer.toml": 'read_only = ["~/playground/private_docs"]\n',
    "tasks/deep-research.md": "",
    "tasks/deep-research.toml": 'model = "opus"\neffort = "xhigh"\nadd_dirs = ["~/playground/private_docs"]\n',
    "tasks/engineering.md": "",
    "tasks/engineering.toml": 'model = "opus"\neffort = "high"\nadd_dirs = ["~/playground/private_docs"]\nrepo_from_issue = true\n',
}
```
In `Launch.setUp` set `self.root = os.path.join(self.tmp, "registry")` and write every `REGISTRY` file via a new helper:
```python
    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
```
`run_launch` passes `root=self.root`. In `test_new_prompt_and_flags` and `test_resume_prompt`, `charter` / `instructions` become `os.path.join(self.root, "roles/researcher.md")` / `os.path.join(self.root, "tasks/deep-research.md")` (principles, `--add-dir` and deny rules stay on `pipeline.ROOT`). `with_memory` writes `self.write("roles/researcher.toml", f'memory = "{mem}"\n')`; `repo_config` writes `self.write("roles/engineer.toml", 'read_only = ["{repo}"]\n')`; `test_engineering_ok_fills_allowed_tools` writes `self.write("tasks/engineering.toml", REGISTRY["tasks/engineering.toml"] + f'allowed_tools = ["{PUSH_RULE}"]\n')`. `RealConfig` is unchanged.

`scripts/tests/test_router.py`: delete the `[roles.researcher]`, `[tasks.deep-research]` and `[tasks.product-design]` tables (and their keys) from `CONFIG`; the comment "Role and task files must exist under the repo root." stays.

- [ ] **Step 7: Run all tests**

Run: `python3 -m unittest discover -s scripts/tests 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 8: Commit and push**

```bash
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task add -A roles tasks pipeline.toml scripts
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task commit -m "TASK-44: roles and tasks are .md + .toml pairs; pipeline.toml keeps team, human_members, projects"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

### Task 2: Launcher config error — role memory vs the issue's repo

**Files:**
- Modify: `scripts/launch.py` (`transient`, `main`), `scripts/tests/test_launch.py`

**Interfaces:**
- Consumes: `pipeline.overlaps(a, b)`, `Run.memory`, `launch.main(..., root=)` from Task 1.
- Produces: `launch.fail(plog, issue, kind, reason, rc) -> int` (replaces `transient`).

- [ ] **Step 1: Write the failing tests** (in `class Launch`, after `test_log_named_after_task`; add `import re` at the top)

```python
    def eng_memory(self, mem):
        os.makedirs(mem, exist_ok=True)
        self.write("roles/engineer.toml", f'read_only = ["~/playground/private_docs"]\nmemory = "{mem}"\n')

    def ok_at(self, clone):
        wt = os.path.join(self.work, "TASK-1", "worktrees", "TASK-1-demo")
        return eng.Ok("TASK-1", "ENG: Demo", "ophis", "demo", clone, "main", "TASK-1-demo", wt)

    def assert_config_error(self, mem):
        self.assertEqual(self.calls, [])
        last = self.plog("engineering").splitlines()[-1]
        self.assertRegex(last, rf"^\S+ \S+ config-error TASK-1: role memory {re.escape(mem)} overlaps the issue's repo \S+$")
        self.assertIn("config-error TASK-1: role memory", self.err)

    def test_memory_overlapping_repo_is_config_error(self):
        playground = os.path.join(self.tmp, "playground")
        clone = os.path.join(playground, "demo")
        os.makedirs(clone)
        worktrees = os.path.join(self.work, "TASK-1", "worktrees")
        cases = [("at clone", clone, self.ok_at(clone)), ("under clone", os.path.join(clone, "mem"), self.ok_at(clone)),
                 ("ancestor of clone", playground, self.ok_at(clone)),
                 ("under worktrees", os.path.join(worktrees, "mem"), self.ok_at(clone)),
                 ("under worktrees, invalid repo", os.path.join(worktrees, "mem"), eng.Invalid("no Repo: line"))]
        for label, mem, result in cases:
            with self.subTest(label):
                self.calls = []
                self.eng_memory(mem)
                self.assertEqual(self.launch_eng(result), 2)
                self.assert_config_error(mem)

    def test_memory_overlapping_repo_on_resume(self):
        clone = os.path.join(self.tmp, "playground", "demo")
        os.makedirs(clone)
        self.eng_memory(clone)
        self.make_transcript()
        self.assertEqual(self.launch_eng(self.ok_at(clone), mode="resume"), 2)
        self.assert_config_error(clone)

    def test_memory_apart_from_repo_starts(self):
        clone = os.path.join(self.tmp, "playground", "demo")
        os.makedirs(clone)
        mem = os.path.join(self.tmp, "engmem")
        self.eng_memory(mem)
        self.assertEqual(self.launch_eng(self.ok_at(clone)), 0)
        self.assertIn(f"Your role memory: {mem};", self.claude()[2])

    def test_memory_check_skipped_without_repo_step(self):
        mem = os.path.join(self.work, "TASK-1", "worktrees", "mem")
        os.makedirs(mem)
        self.write("roles/researcher.toml", f'memory = "{mem}"\n')
        self.assertEqual(self.run_launch(*self.args()), 0)
        self.assertEqual(len(self.calls), 1)
```

- [ ] **Step 2: Run them to see them fail**

Run: `python3 -m unittest discover -s scripts/tests -k memory -v 2>&1 | tail -20`
Expected: the three config-error tests FAIL (rc 0 instead of 2); the other two pass.

- [ ] **Step 3: Implement in `scripts/launch.py`**

Import `overlaps` from `pipeline`. Replace `transient` with:
```python
def fail(plog, issue, kind, reason, rc):
    """A run that does not start: one `<kind>` line in the project log and on stderr; returns the exit code."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {kind} {issue}: {reason}"
    with open(plog, "a") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr)
    return rc
```
Both existing calls become `return fail(plog, a.issue, "transient", <reason>, 3)`. In `main`, after `tail += extra`:
```python
        if job.memory:
            paths = [os.path.join(run_dir(a.issue), "worktrees")] + ([repo.clone] if isinstance(repo, eng.Ok) else [])
            if hit := next((p for p in paths if overlaps(job.memory, p)), None):
                return fail(plog, a.issue, "config-error", f"role memory {job.memory} overlaps the issue's repo {hit}", 2)
```
Update the module docstring's exit-code sentence to: "Exits 2 for an unknown or non-runnable project or a config error (a role memory overlapping the issue's repo; logged), 3 when the run cannot start yet (…unchanged…)."

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s scripts/tests 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 5: Commit and push**

```bash
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task add scripts/launch.py scripts/tests/test_launch.py
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task commit -m "TASK-44: launcher rejects a role memory overlapping the issue's repo (exit 2)"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

### Task 3: Router stops on an uncommitted registry

**Files:**
- Modify: `scripts/pipeline.py` (add `uncommitted`), `scripts/router.py` (`check_registry`, `tick`), `scripts/tests/test_pipeline.py`, `scripts/tests/test_router.py`

**Interfaces:**
- Produces: `pipeline.uncommitted(porcelain: str) -> list[str]`; `router.check_registry(sh, root=ROOT) -> None` (raises `SystemExit`).

- [ ] **Step 1: Write the failing tests**

`scripts/tests/test_pipeline.py`:
```python
class Uncommitted(unittest.TestCase):
    def test_uncommitted_parse(self):
        out = "\0".join([" M roles/engineer.md", "?? tasks/new.toml", "!! roles/hidden.md", "?? roles/.DS_Store",
                         "!! tasks/.x.toml.swp", "R  roles/b.toml", "roles/a.toml", "?? roles/with space.toml",
                         "D  tasks/old.md", "?? roles/notes.txt"]) + "\0"
        self.assertEqual(pipeline.uncommitted(out), ["roles/engineer.md", "tasks/new.toml", "roles/hidden.md",
                                                     "roles/b.toml", "roles/with space.toml", "tasks/old.md"])

    def test_clean(self):
        self.assertEqual(pipeline.uncommitted(""), [])
```

`scripts/tests/test_router.py`: extend `FakeShell`:
```python
    def __init__(self, active=False, probe_five=0.2, prune_fails=False, registry="", git_rc=0):
        self.calls, self.active, self.five = [], active, probe_five
        self.registry, self.git_rc = registry, git_rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, self.git_rc, stdout=self.registry, stderr="fatal: not a git repository")
        ...unchanged...
```
Add to `class Tick`:
```python
    def test_dirty_registry_stops_before_anything(self):
        for argv in ((), ("--dry-run",)):
            with self.subTest(argv):
                fake = FakeLinear([issue("TASK-1", "Todo")])
                with self.assertRaises(SystemExit) as cm:
                    self.tick(fake, *argv, shell=FakeShell(registry=" M roles/engineer.toml\0?? tasks/x.toml\0"))
                self.assertIn("roles/ or tasks/ has uncommitted changes: roles/engineer.toml, tasks/x.toml", str(cm.exception.code))
                self.assertEqual(fake.mutations, [])
                self.assertEqual([c[0] for c in self.sh.calls], ["tmux", "git"])

    def test_registry_git_failure_stops(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        with self.assertRaises(SystemExit) as cm:
            self.tick(fake, shell=FakeShell(git_rc=128))
        self.assertIn("git status", str(cm.exception.code))
        self.assertEqual(self.sh.launches(), [])

    def test_ds_store_alone_ticks(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.tick(fake, shell=FakeShell(registry="?? roles/.DS_Store\0"))
        self.assertEqual(len(self.sh.launches()), 1)

    def test_registry_git_call(self):
        self.tick(FakeLinear([]))
        self.assertIn(["git", "-C", router.ROOT, "status", "--porcelain", "-z", "--ignored", "-uall", "--", "roles", "tasks"],
                      self.sh.calls)

    def test_check_registry_real_git(self):
        repo = os.path.join(self.tmp.name, "repo")
        for rel in ("roles/engineer.md", "tasks/x.md", ".gitignore"):
            os.makedirs(os.path.dirname(os.path.join(repo, rel)), exist_ok=True)
            with open(os.path.join(repo, rel), "w") as f:
                f.write("roles/hidden.md\n.DS_Store\n" if rel == ".gitignore" else "x\n")
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        router.check_registry(subprocess.run, repo)
        for rel, text in (("roles/hidden.md", "x"), ("roles/.DS_Store", "x"), ("tasks/x.md", "changed\n")):
            with open(os.path.join(repo, rel), "w") as f:
                f.write(text)
        with self.assertRaises(SystemExit) as cm:
            router.check_registry(subprocess.run, repo)
        self.assertIn("roles/hidden.md", str(cm.exception.code))
        self.assertIn("tasks/x.md", str(cm.exception.code))
        self.assertNotIn(".DS_Store", str(cm.exception.code))
```
If any existing `Tick` test asserts an exact `self.sh.calls` list that now gains the git call, add the git call to its expectation (do not drop assertions).

- [ ] **Step 2: Run them to see them fail**

Run: `python3 -m unittest discover -s scripts/tests -k registry -k uncommitted -v 2>&1 | tail -20`
Expected: FAIL/ERROR (`uncommitted` / `check_registry` missing; no git call).

- [ ] **Step 3: Implement**

`scripts/pipeline.py`, after `runnable`:
```python
def uncommitted(porcelain):
    """Registry paths in `git status --porcelain -z --ignored -uall` output: every tracked change,
    and untracked or ignored files only when they are .md or .toml (what registry() scans)."""
    out, entries = [], iter(porcelain.split("\0"))
    for entry in entries:
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            next(entries, None)  # -z puts a rename's or copy's source path in the next entry
        if code in ("??", "!!") and not path.endswith((".md", ".toml")):
            continue
        out.append(path)
    return out
```
`scripts/router.py`: import `uncommitted` (and `ROOT` if not already imported) from `pipeline`; add above `tick`:
```python
def check_registry(sh, root=ROOT):
    """Runs trust roles/ and tasks/ but can reach them by Bash, so a tick never starts on uncommitted registry changes."""
    res = sh(["git", "-C", root, "status", "--porcelain", "-z", "--ignored", "-uall", "--", "roles", "tasks"],
             capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"git status of roles/ and tasks/ failed: {res.stderr.strip()}")
    if paths := uncommitted(res.stdout):
        raise SystemExit(f"roles/ or tasks/ has uncommitted changes: {', '.join(paths)}")
```
In `tick`, directly after the tmux `has-session` block and before `if not dry: try: prune(...)`:
```python
    check_registry(sh)
```
Add `registry guard` to the module docstring's one-tick sequence (e.g. "hours, lock, registry guard, prune, Recover, …").

- [ ] **Step 4: Run all tests**

Run: `python3 -m unittest discover -s scripts/tests 2>&1 | tail -5`
Expected: OK.

- [ ] **Step 5: Commit and push**

```bash
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task add scripts
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task commit -m "TASK-44: router stops a tick while roles/ or tasks/ has uncommitted changes"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

### Task 4: Docs

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/specs/2026-09-28-task-44-role-task-design.md` (one line)

- [ ] **Step 1: README.md**

- `roles/` bullet → "`roles/` — `principles.md`, the rules shared by every role and task; per role a pair: `<role>.md`, the charter, and `<role>.toml`, its settings (may be empty): `read_only` (absolute paths or `{repo}`) and `memory` (an existing dir that overlaps neither the repo, the role's `read_only`, `~/.claude` nor `~/Library/LaunchAgents`, in either direction)."
- `tasks/` bullet → "`tasks/` — per task a pair: `<task>.md`, the procedure Claude follows (each must define a resume rule), and `<task>.toml`: `model`, `effort`, `add_dirs`, `repo_from_issue` (the launcher's repo step) and `allowed_tools` (move the existing parenthetical from the `pipeline.toml` bullet here verbatim). A file without its pair, an unknown key or a bad name stops the router and launcher. The launcher passes the principles, charter and task paths in the prompt, plus the role's memory dir when set."
- router bullet: after "tmux lock (`agent-pm`)," insert "a registry guard (the tick stops, dry-run too, while `roles/` or `tasks/` has a tracked change or an untracked or ignored `.md`/`.toml` file; Edit deny rules don't stop a run's Bash writes there),".
- launcher bullet: "starts one run from the project's role and task in `pipeline.toml`" → "starts one run from the project's role and task (their `roles/` and `tasks/` pairs)"; append to the repo-step sub-bullet: "A role `memory` overlapping the issue's clone or `work/<ID>/worktrees` is a config error: a `config-error` line in the project log, exit 2, no run."
- `pipeline.toml` bullet → keep `team`, `human_members` and the projects text (`next`, `prefix`, `require_instructions`, `role` + `task`); remove the roles and tasks text (now in the `roles/` / `tasks/` bullets); add "nothing else (other keys stop the router and launcher)".
- Design list: append `docs/specs/2026-09-29-task-44-role-task-pairs-design.md`.

- [ ] **Step 2: CLAUDE.md**

- Router bullet: "hours → tmux lock (session `agent-pm`) → prune" → "hours → tmux lock (session `agent-pm`) → registry guard → prune".
- Launcher bullet: "(`runnable()` resolves its `role` and `task`)" → "(`runnable()` resolves its `role` and `task` from their `roles/` and `tasks/` pairs)".
- In "Runner vs role and task", replace the sentence starting "The registry:" with: "The registry: `roles/<role>.md` + `roles/<role>.toml` (`read_only`, `memory`), `tasks/<task>.md` + `tasks/<task>.toml` (`model`, `effort`, `add_dirs`, `repo_from_issue`, `allowed_tools`), and `pipeline.toml` with only `team`, `human_members` and `[projects."<id>"]` (`role` + `task` = runnable; `next`, `prefix`, `require_instructions`); `runnable()` rejects an unpaired file, a bad name, unknown keys and a `memory` overlapping the repo, a `read_only` path, `~/.claude` or `~/Library/LaunchAgents` (either direction); the launcher exits 2 (`config-error` in the project log) for a `memory` overlapping the issue's clone or worktrees."
- Gotchas, new bullet after the `test_launch.py` one: "- The router stops every tick while `roles/` or `tasks/` in the live clone has uncommitted `.md`/`.toml` changes (runs can write there by Bash; Edit denies don't cover that): commit or revert local edits there."

- [ ] **Step 3: Old spec note**

Under the title of `docs/specs/2026-09-28-task-44-role-task-design.md` add one line: "Amended by `2026-09-29-task-44-role-task-pairs-design.md`: role and task settings live in `roles/<role>.toml` / `tasks/<task>.toml`, not `pipeline.toml`."

- [ ] **Step 4: Check no stale registry text remains**

Run: `grep -n '\[roles\|\[tasks\|MOVED\|role_name' README.md CLAUDE.md pipeline.toml scripts/*.py`
Expected: no output.

- [ ] **Step 5: Commit and push**

```bash
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task add README.md CLAUDE.md docs/specs/2026-09-28-task-44-role-task-design.md
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task commit -m "TASK-44: docs for role and task pairs, registry guard, memory config error"
git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task
```

## Verification (S6)

From the worktree, with everything committed: `python3 -m unittest discover -s scripts/tests`; `python3 scripts/router.py --now --dry-run` (rc 0; it passes the registry guard); `python3 scripts/promote.py --dry-run` (rc 0).

## Progress

- S1: reusing the existing worktree on TASK-44-role-task (base 124a7b7, the converged phase-1 build).
- decision(pair scope): validate every .md/.toml pair found in roles/ and tasks/, not only referenced ones - orphans must fail at deploy; dissent: none
- decision(check location): role/task checks stay in runnable(); promote (load_config only) keeps working; dissent: none
- decision(launcher memory-vs-repo scope): applies to every role with memory on a repo_from_issue task, not only read_only {repo} - memory in the target clone would leak into commits; dissent: none
- S2: spec written (docs/specs/2026-09-29-task-44-role-task-pairs-design.md).
- S3 panel: core=[architecture,spec-fitness] +optional=[security] (run permissions: memory dir vs deny rules)
- S3 reviewers: architecture=a30de7fbba7481ada spec-fitness=a94117bc5e79e89d1 security=ae2ba65ff1ccbdfa8
- S3 r0: architecture=PASS spec-fitness=PASS security=FAIL -> security#1 'deny rules cover the .toml files' is false: Bash writes bypass Edit denies and roles/ tasks/ are add-dirs, so run-permission settings become run-writable; fix: launcher refuses (exit 2) while git status shows changes under roles/ or tasks/, claim corrected, residual stated
- S3 r1: architecture=FAIL spec-fitness=PASS security=PASS -> architecture#6 registry guard in the launcher runs after the claim, so a dirty roles/ (e.g. .DS_Store) burns every queued issue's attempts; fix: guard moves to the router tick before Recover/claim, counts tracked changes + untracked/ignored .md/.toml only
- S3 r2: architecture=PASS spec-fitness=PASS security=PASS -> converged. Deferred NBs: architecture#5 hardcoded error prefixes (test-only); security#3 ~/.local/bin not protected; security#4 task key types unchecked (as phase 1). Plan-level NBs: guard scoped to tick; filter applies to untracked/ignored only; run router dry-run after committing.
- S4: plan written (4 tasks: pairs+loader+fixtures, launcher memory-vs-repo, router registry guard, docs); execution: subagent-driven-development, per-task reviews kept, final review skipped (S7).
- S5: tasks 1-4 done (315394c pairs+loader, 7050a49 launcher memory-vs-repo, b26e99d router registry guard, c073c4b docs); per-task reviews approved; minors in the SDD ledger. Ruling: test_bad_names fixture renamed roles/Reviewer.* (case-insensitive APFS clobbered roles/researcher.*).
- S6: unittest 236 OK; router.py --now --dry-run rc 0 (guard passed; plan: new, usage blocked at 94%); promote.py --dry-run rc 0; untracked tasks/zz-probe.toml -> router dry-run rc 1 "roles/ or tasks/ has uncommitted changes: tasks/zz-probe.toml".
