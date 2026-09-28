# TASK-38 Engineering skeleton — plan doc

Requirement: `/autopilot:build TASK-38` — Linear TASK-38 "ENG: Engineering 骨架", from the TASK-36 PRD
(/Users/francis/playground/private_docs/Product Design/2026-09-27-2317-TASK-36-engineering-skeleton.md).
Spec: docs/specs/2026-09-27-task-38-engineering-skeleton-design.md

RESUME: phase=S9 worktree=/Users/francis/playground/agent-pm/.claude/worktrees/TASK-38-engineering-skeleton branch=TASK-38-engineering-skeleton base_ref=19a01f9ca62d46a79f6ce03a13b3ff9df9105998 review_round=3 spec_file=/Users/francis/playground/agent-pm/.claude/worktrees/TASK-38-engineering-skeleton/docs/specs/2026-09-27-task-38-engineering-skeleton-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Engineering stage runnable: every run works in `work/<ID>/`; the launcher resolves and clones the target repo; the stage has `autopilot:build` build the PRD in `work/<ID>/worktrees/<branch>`, then pushes and opens a PR.

**Architecture:** `pipeline.py` gains per-issue run dirs, transcript paths and `allowed_tools` validation; `eng.py` = `resolve()` (Ok / Invalid / Transient) plus a `status`/`comments` CLI; `router.py` finds transcripts by issue and session; `launch.py` runs every stage in its run dir with locked-down flags and does the Engineering repo step; `stages/engineering.md` drives the build.

**Tech Stack:** Python 3.11 stdlib (`subprocess`, `tomllib`, `json`, `re`, `string`), `git`, `gh`, `unittest`.

**Spec:** `docs/specs/2026-09-27-task-38-engineering-skeleton-design.md` (read it; this plan implements it section by section).

### Global Constraints

- Python stdlib only; Python 3.11+ (`tomllib`).
- Every subprocess: argv list, never `shell=True`; `capture_output=True, text=True`; timeout 60 s for git/gh, 600 s for `gh repo clone`; `subprocess.TimeoutExpired` and `FileNotFoundError` → `Transient`.
- Values from outside (Repo value, `default_branch`, issue titles, existing branch names) are validated by the spec's patterns before use.
- No `--allowedTools` unless the project's `allowed_tools` is non-empty; templates never contain `*`.
- Existing behavior of Deep Research / Product Design launches and all existing tests stay unchanged.
- Code style: match the repo (short functions, no comments except for a non-obvious reason, ≤ 3 lines).
- Verify with `python3 -m unittest discover -s scripts/tests` from the worktree root; it must pass after every task.
- Commit after each task on branch `TASK-38-engineering-skeleton`; never touch `main`.

### Review Focus

1. A Linear description with `\r\n` line endings or a `Repo:` line indented inside an `## Instructions` quote → still parsed (pinned in Task 1).
2. `~/playground/<name>` exists but is a plain directory, not a git repo → `Invalid`, not a crash (Task 1).
3. `gh` or `git` missing from `PATH` (`FileNotFoundError`) → `Transient`, not a crash (Task 1).
4. An all-Chinese issue title → slug empty → branch `<ID>-build` (Task 1).
5. The worktree directory exists but is on another branch → `push` refuses, `setup` does not re-create (Task 2).

---

### Task 1: `eng.py` repo resolution library

**Files:**
- Create: `scripts/eng.py`
- Test: `scripts/tests/test_eng.py`

**Interfaces:**
- Produces:
  - `Ok(issue, title, owner, name, clone, default, branch, worktree)`, `Invalid(reason)`, `Transient(reason)` — frozen dataclasses.
  - `parse_repo(description: str) -> tuple[str, str] | Invalid`
  - `norm_url(url: str) -> str | None` — `"github.com/<owner>/<name>"` lower-cased, or None.
  - `slug(title: str) -> str` — strips one leading `^[A-Z][A-Z0-9]*: ` prefix, lower-cases, keeps `[a-z0-9]` runs joined by `-`, ≤ 40 chars without a trailing `-`; empty → `"build"`.
  - `resolve(issue_id: str, gql, run, playground: str = PLAYGROUND) -> Ok | Invalid | Transient` where `run(argv, timeout)` returns an object with `.returncode`, `.stdout`, `.stderr` (tests pass a fake; production uses `sh_run`).
  - `sh_run(argv, timeout)` — `subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env={**os.environ, "PATH": PATH})`.
  - Constants `PLAYGROUND = os.path.expanduser("~/playground")`, `Q_ISSUE = "query($i: String!) { issue(id: $i) { identifier title description } }"`.

- [ ] **Step 1: Write the failing tests** in `scripts/tests/test_eng.py`:

```python
import os, sys, subprocess, tempfile, unittest
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eng  # noqa: E402

def ok(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

class FakeRun:
    """Maps argv prefixes (tuples) to results; records calls."""
    def __init__(self, table):
        self.table, self.calls = table, []
    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        for prefix, res in self.table:
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(res, BaseException):
                    raise res
                return res
        raise AssertionError(f"unexpected {argv}")

def gql_for(description, title="ENG: Session Registry", ident="TASK-26"):
    return lambda q, **v: {"issue": {"identifier": ident, "title": title, "description": description}}

class ParseRepo(unittest.TestCase):
    def test_forms(self):
        for line in ("Repo: ophis/agent-pm", "repo: https://github.com/ophis/agent-pm",
                     "REPO: https://github.com/ophis/agent-pm.git/", "Repo: git@github.com:ophis/agent-pm.git",
                     "repo: [https://github.com/ophis/agent-pm](<https://github.com/ophis/agent-pm>)",
                     "Repo: [agent-pm](https://github.com/ophis/agent-pm)", "  repo:   ophis/agent-pm  "):
            self.assertEqual(eng.parse_repo(f"x\n{line}\ny"), ("ophis", "agent-pm"), line)

    def test_crlf(self):
        self.assertEqual(eng.parse_repo("a\r\nRepo: ophis/agent-pm\r\nb"), ("ophis", "agent-pm"))

    def test_comments_section_ignored(self):
        d = "Repo: ophis/a\n\n## Comments\n* x:\n  > Repo: ophis/b"
        self.assertEqual(eng.parse_repo(d), ("ophis", "a"))
        self.assertIsInstance(eng.parse_repo("## Comments\nRepo: ophis/a"), eng.Invalid)

    def test_same_value_twice_ok_two_values_invalid(self):
        self.assertEqual(eng.parse_repo("Repo: ophis/a\nrepo: OPHIS/A"), ("ophis", "a"))
        self.assertIsInstance(eng.parse_repo("Repo: ophis/a\nRepo: ophis/b"), eng.Invalid)

    def test_bad_values(self):
        for v in ("", "ophis", "-ophis/a", "ophis/-a", "ophis/..", "ophis/.", "a b/c", "ophis/a;rm", "https://gitlab.com/ophis/a"):
            self.assertIsInstance(eng.parse_repo(f"Repo: {v}"), eng.Invalid, v)

class Slug(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(eng.slug("ENG: Session Registry (MVP)!"), "session-registry-mvp")
        self.assertEqual(eng.slug("Engineering 骨架"), "engineering")
        self.assertEqual(eng.slug("ENG: 中文标题"), "build")
        self.assertEqual(len(eng.slug("ENG: " + "a" * 30 + " " + "b" * 30)), 40)
        self.assertFalse(eng.slug("ENG: " + "a" * 39 + " bbb").endswith("-"))

class Resolve(unittest.TestCase):
    def setUp(self):
        self.pg = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.pg]))
        self.clone = os.path.join(self.pg, "agent-pm")

    def table(self, **over):
        t = {
            "api": ok('{"default_branch": "main", "permissions": {"push": true}}'),
            "clone": ok(),
            "wt": ok("true\n"),
            "fetch_url": ok("git@github.com:ophis/agent-pm.git\n"),
            "push_url": ok("git@github.com:ophis/agent-pm.git\n"),
            "branches": ok(""),
            "remote": ok(""),
        }
        t.update(over)
        return [
            (("gh", "api", "repos/ophis/agent-pm"), t["api"]),
            (("gh", "repo", "clone"), t["clone"]),
            (("git", "-C", self.clone, "rev-parse", "--is-inside-work-tree"), t["wt"]),
            (("git", "-C", self.clone, "remote", "get-url", "--push", "origin"), t["push_url"]),
            (("git", "-C", self.clone, "remote", "get-url", "origin"), t["fetch_url"]),
            (("git", "-C", self.clone, "branch", "--list"), t["branches"]),
            (("git", "-C", self.clone, "ls-remote", "--heads", "origin"), t["remote"]),
        ]

    def resolve(self, desc="Repo: ophis/agent-pm", **over):
        self.run_ = FakeRun(self.table(**over))
        return eng.resolve("TASK-26", gql_for(desc), self.run_, playground=self.pg)

    def test_ok_existing_clone(self):
        os.makedirs(self.clone)
        r = self.resolve()
        self.assertEqual(r, eng.Ok("TASK-26", "ENG: Session Registry", "ophis", "agent-pm", self.clone, "main",
                                   "TASK-26-session-registry",
                                   os.path.join(self.clone, ".claude", "worktrees", "TASK-26-session-registry")))

    def test_absent_clone_is_cloned(self):
        r = self.resolve()
        self.assertIsInstance(r, eng.Ok)
        self.assertIn((("gh", "repo", "clone", "ophis/agent-pm", self.clone), 600), self.run_.calls)

    def test_linear_error_transient(self):
        def boom(q, **v):
            raise SystemExit("linear api error: boom")
        self.assertIsInstance(eng.resolve("TASK-26", boom, FakeRun([]), playground=self.pg), eng.Transient)

    def test_access(self):
        os.makedirs(self.clone)
        for res, kind in ((ok(code=1, stderr="gh: Not Found (HTTP 404)"), eng.Invalid),
                          (ok(code=1, stderr="gh: Forbidden (HTTP 403)"), eng.Invalid),
                          (ok('{"default_branch": "main", "permissions": {"push": false}}'), eng.Invalid),
                          (ok(code=1, stderr="gh: Bad Gateway (HTTP 502)"), eng.Transient),
                          (ok(code=1, stderr="error connecting to api.github.com"), eng.Transient),
                          (subprocess.TimeoutExpired("gh", 60), eng.Transient),
                          (FileNotFoundError("gh"), eng.Transient),
                          (ok('{"default_branch": "-x", "permissions": {"push": true}}'), eng.Invalid),
                          (ok('{"default_branch": "a..b", "permissions": {"push": true}}'), eng.Invalid),
                          (ok('{"default_branch": "ma$(x)", "permissions": {"push": true}}'), eng.Invalid)):
            self.assertIsInstance(self.resolve(api=res), kind, res)

    def test_clone_checks(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", other]))
        os.symlink(other, self.clone)
        self.assertIsInstance(self.resolve(), eng.Invalid)
        os.remove(self.clone)
        os.makedirs(self.clone)
        self.assertIsInstance(self.resolve(wt=ok(code=128, stderr="not a git repository")), eng.Invalid)
        self.assertIsInstance(self.resolve(fetch_url=ok("git@github.com:other/agent-pm.git\n")), eng.Invalid)
        self.assertIsInstance(self.resolve(push_url=ok("https://evil.example/x.git\n")), eng.Invalid)
        self.assertIsInstance(self.resolve(fetch_url=ok("https://github.com/OPHIS/Agent-PM\n")), eng.Ok)

    def test_clone_failure_transient(self):
        self.assertIsInstance(self.resolve(clone=ok(code=1, stderr="network")), eng.Transient)
        self.assertIsInstance(self.resolve(clone=subprocess.TimeoutExpired("gh", 600)), eng.Transient)

    def test_existing_branches(self):
        os.makedirs(self.clone)
        self.assertEqual(self.resolve(branches=ok("TASK-26-old-name\n")).branch, "TASK-26-old-name")
        self.assertEqual(self.resolve(remote=ok("abc\trefs/heads/TASK-26-remote\n")).branch, "TASK-26-remote")
        self.assertIsInstance(self.resolve(branches=ok("TASK-26-a\nTASK-26-b\n")), eng.Invalid)
        self.assertIsInstance(self.resolve(branches=ok("TASK-26-Bad$(x)\n")), eng.Invalid)

    def test_invalid_repo_line(self):
        self.assertIsInstance(self.resolve(desc="no repo here"), eng.Invalid)
```

- [ ] **Step 2: Run to verify failure:** `python3 -m unittest scripts.tests.test_eng -v` → FAIL (`No module named 'eng'`).

- [ ] **Step 3: Implement `scripts/eng.py`** (library part):

```python
#!/usr/bin/env python3
"""Engineering runs: resolve an issue's target repo, and a CLI for the stage (spec docs/specs/2026-09-27-task-38-…-design.md)."""
import json, os, re, subprocess, sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import PATH, ROOT  # noqa: E402

PLAYGROUND = os.path.expanduser("~/playground")
Q_ISSUE = "query($i: String!) { issue(id: $i) { identifier title description } }"
OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
NAME = r"[A-Za-z0-9._][A-Za-z0-9._-]{0,99}"
REF = re.compile(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+")
SHORT, LONG = 60, 600

@dataclass(frozen=True)
class Ok:
    issue: str; title: str; owner: str; name: str; clone: str; default: str; branch: str; worktree: str

@dataclass(frozen=True)
class Invalid:
    reason: str

@dataclass(frozen=True)
class Transient:
    reason: str

def sh_run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env={**os.environ, "PATH": PATH})

def _one(value):
    v = value.strip()
    m = re.fullmatch(r"\[[^\]]*\]\(<?([^)>]+)>?\)", v)
    if m:
        v = m.group(1).strip()
    for pat in (rf"https://github\.com/({OWNER})/({NAME}?)(?:\.git)?/?", rf"git@github\.com:({OWNER})/({NAME}?)(?:\.git)?",
                rf"({OWNER})/({NAME})"):
        m = re.fullmatch(pat, v)
        if m and m.group(2) not in (".", ".."):
            return m.group(1), m.group(2)
    return None

def parse_repo(description):
    found = []
    for line in (description or "").splitlines():
        if line.rstrip("\r") == "## Comments":
            break
        m = re.match(r"^\s*repo:\s*(.+?)\s*$", line.rstrip("\r"), re.I)
        if m:
            one = _one(m.group(1))
            if not one:
                return Invalid(f"unreadable Repo line: {m.group(1)[:80]!r}")
            found.append(one)
    distinct = {(o.lower(), n.lower()) for o, n in found}
    if not found:
        return Invalid("no Repo: line in the description or its ## Instructions")
    if len(distinct) > 1:
        return Invalid("several different Repo: values")
    return found[0]

def norm_url(url):
    m = re.fullmatch(r"(?:https://|ssh://git@|git@)github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?", url.strip(), re.I)
    return f"github.com/{m.group(1)}/{m.group(2)}".lower() if m else None

def slug(title):
    t = re.sub(r"^[A-Z][A-Z0-9]*: ", "", title, count=1).lower()
    s = "-".join(re.findall(r"[a-z0-9]+", t))[:40].rstrip("-")
    return s or "build"

def _call(run, argv, timeout=SHORT):
    try:
        return run(argv, timeout), None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return None, Transient(f"{argv[0]} {argv[1]}: {type(e).__name__}")

def resolve(issue_id, gql, run, playground=PLAYGROUND):
    try:
        issue = gql(Q_ISSUE, i=issue_id)["issue"]
    except (SystemExit, Exception) as e:
        return Transient(f"Linear: {e}")
    repo = parse_repo(issue.get("description"))
    if isinstance(repo, Invalid):
        return repo
    owner, name = repo
    res, err = _call(run, ["gh", "api", f"repos/{owner}/{name}"])
    if err:
        return err
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            return Invalid(f"{owner}/{name}: not found or no access (HTTP {code.group(1)})")
        return Transient(f"gh api repos/{owner}/{name}: {(res.stderr or '').strip()[:200]}")
    data = json.loads(res.stdout)
    if not data.get("permissions", {}).get("push"):
        return Invalid(f"{owner}/{name}: no push permission")
    default = data.get("default_branch", "")
    if not REF.fullmatch(default):
        return Invalid(f"{owner}/{name}: unsafe default branch name")
    clone = os.path.join(playground, name)
    if os.path.islink(clone) or not os.path.realpath(clone).startswith(os.path.realpath(playground) + os.sep):
        return Invalid(f"{clone} is a symlink or outside {playground}")
    want = f"github.com/{owner}/{name}".lower()
    if os.path.exists(clone):
        res, err = _call(run, ["git", "-C", clone, "rev-parse", "--is-inside-work-tree"])
        if err:
            return err
        if res.returncode != 0 or res.stdout.strip() != "true":
            return Invalid(f"{clone} exists but is not a git repo")
        for extra in ([], ["--push"]):
            res, err = _call(run, ["git", "-C", clone, "remote", "get-url", *extra, "origin"])
            if err:
                return err
            if res.returncode != 0 or norm_url(res.stdout) != want:
                return Invalid(f"{clone} is not a clone of {owner}/{name} (origin {'push ' if extra else ''}URL differs)")
    else:
        res, err = _call(run, ["gh", "repo", "clone", f"{owner}/{name}", clone], LONG)
        if err:
            return err
        if res.returncode != 0:
            return Transient(f"gh repo clone {owner}/{name}: {(res.stderr or '').strip()[:200]}")
    ident = issue["identifier"]
    res, err = _call(run, ["git", "-C", clone, "branch", "--list", f"{ident}-*", "--format=%(refname:short)"])
    if err:
        return err
    names = res.stdout.split()
    if not names:
        res, err = _call(run, ["git", "-C", clone, "ls-remote", "--heads", "origin", f"{ident}-*"])
        if err:
            return err
        names = [line.split("refs/heads/", 1)[1] for line in res.stdout.splitlines() if "refs/heads/" in line]
    if len(names) > 1:
        return Invalid(f"several {ident}-* branches: {', '.join(names)[:200]}")
    if names and not re.fullmatch(rf"{re.escape(ident)}-[a-z0-9-]{{1,40}}", names[0]):
        return Invalid(f"existing branch name {names[0][:80]!r} is not {ident}-<lowercase slug>")
    branch = names[0] if names else f"{ident}-{slug(issue['title'])}"
    return Ok(ident, issue["title"], owner, name, clone, default, branch, os.path.join(clone, ".claude", "worktrees", branch))
```

Note on the FakeRun table: `get-url --push` is listed before `get-url` so the longer prefix matches first; `branch --list` prefix matches regardless of the pattern/format args.

- [ ] **Step 4: Run** `python3 -m unittest scripts.tests.test_eng -v` → PASS; then the full suite → PASS.
- [ ] **Step 5: Commit** `git add scripts/eng.py scripts/tests/test_eng.py && git commit -m "eng.py: resolve an Engineering issue's target repo"`.

---

### Task 2: `eng.py` CLI

**Files:**
- Modify: `scripts/eng.py` (append CLI)
- Test: `scripts/tests/test_eng.py` (append `Cli` tests)

**Interfaces:**
- Consumes: `resolve`, `Ok`, `Invalid`, `Transient`, `sh_run` (Task 1).
- Produces:
  - `push_argv(ok: Ok) -> list[str]` = `["git", "-c", "core.hooksPath=/dev/null", "-C", ok.worktree, "push", "-u", f"git@github.com:{ok.owner}/{ok.name}.git", f"{ok.branch}:{ok.branch}"]`.
  - `PUSH_RULE = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch}:{branch})"` — the recommended `allowed_tools` template (docs and test only).
  - `main(argv, env=os.environ, gql=None, run=sh_run, root=ROOT, out=sys.stdout, err=sys.stderr) -> int`; commands `status`, `setup`, `push [--print]`, `pr --body-file F`, `comments --since ISO`. Exit 2 on refusal (reason to `err`), the git/gh return code on a failed subprocess, 0 on success. `gql=None` → `pipeline.linear_gql`.
  - `PR_TITLE(ok) = f"{ok.issue}: " + re.sub(r"^[A-Z][A-Z0-9]*: ", "", ok.title, count=1)`.

- [ ] **Step 1: Write the failing tests** (append to `test_eng.py`):

```python
class Cli(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.root]))
        os.makedirs(os.path.join(self.root, "work"))
        self.wt = os.path.join(self.root, "clone", ".claude", "worktrees", "TASK-26-x")
        self.ok = eng.Ok("TASK-26", "ENG: X", "ophis", "agent-pm", os.path.join(self.root, "clone"), "main", "TASK-26-x", self.wt)

    def cli(self, *argv, env=None, table=(), resolved=None):
        self.run_ = FakeRun(list(table))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(eng, "resolve", return_value=resolved or self.ok):
            rc = eng.main(list(argv), env={"AGENT_PM_ISSUE": "TASK-26"} if env is None else env,
                          gql=lambda *a, **k: None, run=self.run_, root=self.root, out=out, err=err)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def test_requires_issue_env(self):
        self.assertEqual(self.cli("status", env={}), 2)
        self.assertIn("AGENT_PM_ISSUE", self.err)

    def test_refuses_when_not_ok(self):
        self.assertEqual(self.cli("push", resolved=eng.Invalid("no Repo")), 2)
        self.assertIn("no Repo", self.err)

    def test_push_argv_and_print(self):
        os.makedirs(self.wt)
        branch = ok("TASK-26-x\n")
        self.assertEqual(self.cli("push", "--print", table=[(("git", "-C", self.wt, "rev-parse"), branch)]), 0)
        want = "git -c core.hooksPath=/dev/null -C " + self.wt + " push -u git@github.com:ophis/agent-pm.git TASK-26-x:TASK-26-x"
        self.assertEqual(self.out.strip(), want)
        self.assertEqual(eng.PUSH_RULE.format(worktree=self.wt, owner="ophis", name="agent-pm", branch="TASK-26-x"), f"Bash({want})")
        self.cli("push", table=[(("git", "-C", self.wt, "rev-parse"), branch), (tuple(eng.push_argv(self.ok)), ok())])
        self.assertEqual(self.run_.calls[-1][0], tuple(eng.push_argv(self.ok)))

    def test_push_refuses_other_branch_or_missing_worktree(self):
        self.assertEqual(self.cli("push"), 2)
        os.makedirs(self.wt)
        self.assertEqual(self.cli("push", table=[(("git", "-C", self.wt, "rev-parse"), ok("main\n"))]), 2)

    def test_setup_new_branch(self):
        table = [(("git", "-C", self.ok.clone, "fetch"), ok()), (("git", "-C", self.ok.clone, "rev-parse", "--git-common-dir"), ok(".git\n")),
                 (("git", "-C", self.ok.clone, "show-ref"), ok(code=1)), (("git", "-C", self.ok.clone, "ls-remote"), ok("")),
                 (("git", "-C", self.ok.clone, "worktree", "add"), ok())]
        os.makedirs(os.path.join(self.ok.clone, ".git", "info"))
        self.assertEqual(self.cli("setup", table=table), 0)
        self.assertIn((("git", "-C", self.ok.clone, "worktree", "add", "-b", "TASK-26-x", self.wt, "origin/main"), 60), self.run_.calls)
        with open(os.path.join(self.ok.clone, ".git", "info", "exclude")) as f:
            self.assertIn(".claude/worktrees/\n", f.read())
        self.cli("setup", table=table)
        with open(os.path.join(self.ok.clone, ".git", "info", "exclude")) as f:
            self.assertEqual(f.read().count(".claude/worktrees/"), 1)

    def test_setup_existing_worktree_is_left_alone(self):
        os.makedirs(self.wt)
        os.makedirs(os.path.join(self.ok.clone, ".git", "info"))
        table = [(("git", "-C", self.ok.clone, "fetch"), ok()), (("git", "-C", self.ok.clone, "rev-parse", "--git-common-dir"), ok(".git\n"))]
        self.assertEqual(self.cli("setup", table=table), 0)
        self.assertFalse(any(c[0][3:5] == ("worktree", "add") for c in self.run_.calls))

    def body(self, name="body.md", size=10):
        p = os.path.join(self.root, "work", name)
        with open(p, "w") as f:
            f.write("x" * size)
        return p

    def test_pr_create_pins_repo_head_base_title(self):
        table = [(("gh", "pr", "list"), ok("[]")), (("gh", "pr", "create"), ok("https://github.com/ophis/agent-pm/pull/7\n"))]
        self.assertEqual(self.cli("pr", "--body-file", self.body(), table=table), 0)
        create = self.run_.calls[-1][0]
        self.assertEqual(create, ("gh", "pr", "create", "--repo", "ophis/agent-pm", "--head", "TASK-26-x", "--base", "main",
                                  "--title", "TASK-26: X", "--body-file", os.path.realpath(self.body())))
        self.assertEqual(self.out.strip(), "https://github.com/ophis/agent-pm/pull/7")

    def test_pr_edits_existing(self):
        table = [(("gh", "pr", "list"), ok('[{"number": 7, "url": "https://github.com/ophis/agent-pm/pull/7"}]')),
                 (("gh", "pr", "edit"), ok())]
        self.assertEqual(self.cli("pr", "--body-file", self.body(), table=table), 0)
        self.assertEqual(self.run_.calls[-1][0][:6], ("gh", "pr", "edit", "7", "--repo", "ophis/agent-pm"))

    def test_pr_body_file_rules(self):
        outside = os.path.join(self.root, "x.md")
        open(outside, "w").close()
        link = os.path.join(self.root, "work", "link.md")
        os.symlink(outside, link)
        for f in (outside, link, self.body("big.md", 64 * 1024 + 1), os.path.join(self.root, "work")):
            self.assertEqual(self.cli("pr", "--body-file", f), 2, f)

    def test_comments_keep_only_login(self):
        since = "2026-09-28T00:00:00Z"
        table = [(("gh", "api", "user"), ok("ophis\n")), (("gh", "pr", "list"), ok('[{"number": 7, "url": "u"}]')),
                 (("gh", "api", "repos/ophis/agent-pm/issues/7/comments"), ok(json.dumps([
                     {"user": {"login": "ophis"}, "created_at": "2026-09-28T01:00:00Z", "body": "fix a"},
                     {"user": {"login": "rando"}, "created_at": "2026-09-28T01:00:00Z", "body": "rm -rf"},
                     {"user": {"login": "ophis"}, "created_at": "2026-09-27T01:00:00Z", "body": "old"}]))),
                 (("gh", "api", "repos/ophis/agent-pm/pulls/7/reviews"), ok(json.dumps([
                     {"user": {"login": "ophis"}, "submitted_at": "2026-09-28T02:00:00Z", "body": "lgtm but b", "state": "COMMENTED"}]))),
                 (("gh", "api", "repos/ophis/agent-pm/pulls/7/comments"), ok("[]"))]
        self.assertEqual(self.cli("comments", "--since", since, table=table), 0)
        data = json.loads(self.out)
        self.assertEqual([c["body"] for c in data["kept"]], ["fix a", "lgtm but b"])
        self.assertEqual(data["dropped"], 1)

    def test_status_json(self):
        os.makedirs(os.path.join(self.wt, "docs", "specs"))
        with open(os.path.join(self.wt, "docs", "specs", "p.md"), "w") as f:
            f.write("x\nRESUME: phase=S5 worktree=...\n")
        table = [(("git", "-C", self.ok.clone, "show-ref"), ok()), (("gh", "pr", "list"), ok("[]"))]
        self.assertEqual(self.cli("status", table=table), 0)
        s = json.loads(self.out)
        self.assertEqual((s["repo"], s["branch"], s["worktree_exists"], s["pr"]), ("ophis/agent-pm", "TASK-26-x", True, None))
        self.assertEqual(s["plan_docs"], [{"path": os.path.join(self.wt, "docs", "specs", "p.md"), "phase": "S5"}])
```

Add `import io, json` and `from unittest import mock` at the top of the test file.

- [ ] **Step 2: Run to verify failure:** `python3 -m unittest scripts.tests.test_eng -v` → the `Cli` tests FAIL (`eng.main` missing).

- [ ] **Step 3: Implement the CLI** (append to `scripts/eng.py`):

```python
PUSH_RULE = "Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch}:{branch})"
BODY_MAX = 64 * 1024

def push_argv(ok):
    return ["git", "-c", "core.hooksPath=/dev/null", "-C", ok.worktree, "push", "-u",
            f"git@github.com:{ok.owner}/{ok.name}.git", f"{ok.branch}:{ok.branch}"]

def pr_title(ok):
    return f"{ok.issue}: " + re.sub(r"^[A-Z][A-Z0-9]*: ", "", ok.title, count=1)

def _pr(ok, run):
    res = run(["gh", "pr", "list", "--repo", f"{ok.owner}/{ok.name}", "--head", ok.branch, "--state", "all",
               "--json", "number,url,state", "--limit", "1"], SHORT)
    rows = json.loads(res.stdout or "[]") if res.returncode == 0 else []
    return rows[0] if rows else None

def _plan_docs(worktree):
    found = []
    for base, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".claude")]
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(base, f)
                with open(p, errors="replace") as fh:
                    m = re.search(r"RESUME: phase=(S\d)", fh.read())
                if m:
                    found.append({"path": p, "phase": m.group(1)})
    return sorted(found, key=lambda d: d["path"])

def _branch_exists(ok, run):
    local = run(["git", "-C", ok.clone, "show-ref", "--verify", "--quiet", f"refs/heads/{ok.branch}"], SHORT)
    if local.returncode == 0:
        return "local"
    remote = run(["git", "-C", ok.clone, "ls-remote", "--heads", "origin", ok.branch], SHORT)
    return "remote" if remote.stdout.strip() else None

def cmd_status(ok, run, out):
    json.dump({"repo": f"{ok.owner}/{ok.name}", "clone": ok.clone, "default": ok.default, "branch": ok.branch,
               "worktree": ok.worktree, "branch_exists": _branch_exists(ok, run), "worktree_exists": os.path.isdir(ok.worktree),
               "pr": _pr(ok, run), "plan_docs": _plan_docs(ok.worktree) if os.path.isdir(ok.worktree) else []}, out)
    out.write("\n")
    return 0

def cmd_setup(ok, run, err):
    for argv in (["git", "-C", ok.clone, "fetch", "origin"],):
        res = run(argv, SHORT)
        if res.returncode:
            err.write(res.stderr)
            return res.returncode
    common = run(["git", "-C", ok.clone, "rev-parse", "--git-common-dir"], SHORT).stdout.strip()
    exclude = os.path.join(ok.clone, common, "info", "exclude") if not os.path.isabs(common) else os.path.join(common, "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    text = open(exclude).read() if os.path.exists(exclude) else ""
    if ".claude/worktrees/" not in text.splitlines():
        with open(exclude, "a") as f:
            f.write(("" if text.endswith("\n") or not text else "\n") + ".claude/worktrees/\n")
    if os.path.isdir(ok.worktree):
        return 0
    where = _branch_exists(ok, run)
    if where == "local":
        argv = ["git", "-C", ok.clone, "worktree", "add", ok.worktree, ok.branch]
    elif where == "remote":
        argv = ["git", "-C", ok.clone, "worktree", "add", "--track", "-b", ok.branch, ok.worktree, f"origin/{ok.branch}"]
    else:
        argv = ["git", "-C", ok.clone, "worktree", "add", "-b", ok.branch, ok.worktree, f"origin/{ok.default}"]
    res = run(argv, SHORT)
    if res.returncode:
        err.write(res.stderr)
    return res.returncode

def cmd_push(ok, run, out, err, only_print):
    if not os.path.isdir(ok.worktree):
        err.write(f"no worktree at {ok.worktree}; run setup\n")
        return 2
    head = run(["git", "-C", ok.worktree, "rev-parse", "--abbrev-ref", "HEAD"], SHORT).stdout.strip()
    if head != ok.branch:
        err.write(f"worktree is on {head!r}, not {ok.branch}\n")
        return 2
    argv = push_argv(ok)
    out.write(" ".join(argv) + "\n")
    if only_print:
        return 0
    res = run(argv, SHORT)
    err.write(res.stderr or "")
    return res.returncode

def cmd_pr(ok, run, out, err, body_file, root):
    work = os.path.realpath(os.path.join(root, "work")) + os.sep
    real = os.path.realpath(body_file)
    if os.path.islink(body_file) or not real.startswith(work) or not os.path.isfile(real) or os.path.getsize(real) > BODY_MAX:
        err.write(f"body file must be a regular file under {work}, at most {BODY_MAX} bytes, not a symlink\n")
        return 2
    pr = _pr(ok, run)
    repo = f"{ok.owner}/{ok.name}"
    if pr:
        res = run(["gh", "pr", "edit", str(pr["number"]), "--repo", repo, "--body-file", real], SHORT)
        url = pr["url"]
    else:
        res = run(["gh", "pr", "create", "--repo", repo, "--head", ok.branch, "--base", ok.default,
                   "--title", pr_title(ok), "--body-file", real], SHORT)
        url = res.stdout.strip()
    if res.returncode:
        err.write(res.stderr or "")
        return res.returncode
    out.write(url + "\n")
    return 0

def cmd_comments(ok, run, out, since):
    login = run(["gh", "api", "user", "--jq", ".login"], SHORT).stdout.strip()
    pr = _pr(ok, run)
    kept, dropped = [], 0
    if pr:
        base = f"repos/{ok.owner}/{ok.name}"
        for path, key in ((f"{base}/issues/{pr['number']}/comments", "created_at"), (f"{base}/pulls/{pr['number']}/reviews", "submitted_at"),
                          (f"{base}/pulls/{pr['number']}/comments", "created_at")):
            for c in json.loads(run(["gh", "api", path], SHORT).stdout or "[]"):
                if (c.get(key) or "") <= since:
                    continue
                if (c.get("user") or {}).get("login") != login:
                    dropped += 1
                    continue
                kept.append({"at": c[key], "body": c.get("body", ""), "kind": path.rsplit("/", 1)[1]})
    json.dump({"kept": sorted(kept, key=lambda c: c["at"]), "dropped": dropped}, out)
    out.write("\n")
    return 0

def main(argv, env=os.environ, gql=None, run=sh_run, root=ROOT, out=sys.stdout, err=sys.stderr):
    import argparse
    ap = argparse.ArgumentParser(prog="eng.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); sub.add_parser("setup")
    sub.add_parser("push").add_argument("--print", action="store_true")
    sub.add_parser("pr").add_argument("--body-file", required=True)
    sub.add_parser("comments").add_argument("--since", required=True)
    a = ap.parse_args(argv)
    issue = env.get("AGENT_PM_ISSUE", "")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", issue):
        err.write("eng.py: AGENT_PM_ISSUE is not set to an issue id\n")
        return 2
    if gql is None:
        from pipeline import linear_gql as gql
    ok = resolve(issue, gql, run)
    if not isinstance(ok, Ok):
        err.write(f"eng.py: {type(ok).__name__}: {ok.reason}\n")
        return 2
    if a.cmd == "status":
        return cmd_status(ok, run, out)
    if a.cmd == "setup":
        return cmd_setup(ok, run, err)
    if a.cmd == "push":
        return cmd_push(ok, run, out, err, a.print)
    if a.cmd == "pr":
        return cmd_pr(ok, run, out, err, a.body_file, root)
    return cmd_comments(ok, run, out, a.since)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Adjust the Task 2 test tables if the implementation's exact argv prefixes differ (the FakeRun matches prefixes); keep the pinned assertions on `push_argv`, the `gh pr create` argv, and `worktree add` argv exactly as written.

- [ ] **Step 4: Run** `python3 -m unittest scripts.tests.test_eng -v` → PASS; full suite → PASS.
- [ ] **Step 5: Commit** `git add scripts/eng.py scripts/tests/test_eng.py && git commit -m "eng.py: status/setup/push/pr/comments CLI for Engineering runs"`.

---

### Task 3: `pipeline.py` helpers and `allowed_tools` validation

(Tasks 1–2 are done — commits c6b6c99, a6493d2, e19e205. The spec was then redesigned; Task 4 reworks their code.)

**Files:**
- Modify: `scripts/pipeline.py`
- Test: `scripts/tests/test_pipeline.py`

**Interfaces:**
- Produces (in `pipeline.py`):
  - `PROJECTS = os.path.expanduser("~/.claude/projects")`
  - `escape(path: str) -> str` = `re.sub(r"[^A-Za-z0-9]", "-", path)`
  - `run_dir(issue: str) -> str` = `os.path.join(WORK, issue)`
  - `transcript(issue: str, sid: str, projects: str = PROJECTS) -> str | None` = `os.path.join(projects, escape(run_dir(issue)), f"{sid}.jsonl")` when `sid` matches `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`, else None.
  - `PLACEHOLDERS = {"worktree", "branch", "owner", "name", "default", "clone"}`; `check_allowed_tools(name, p, root=ROOT)` called from `load_config` for every project: raise `SystemExit("pipeline.toml: …")` for `allowed_tools` without `repo_from_issue`, a template containing `*`, a placeholder outside `PLACEHOLDERS` (via `string.Formatter().parse`), the string `root`, or `\b(?:python3?|bash|sh|zsh|node|ruby|perl)\s+\S*[/.]`.
  - Keep `TRANSCRIPTS` only if something still imports it after Task 5; delete it in Task 5.

- [ ] **Step 1: Failing tests** in `test_pipeline.py`: `escape("/Users/a_b/x.y")` == `"-Users-a-b-x-y"`; `run_dir("TASK-9")` ends with `/work/TASK-9`; `transcript("TASK-9", "0f0f0f0f-1111-2222-3333-444444444444", projects="/p")` == `"/p/" + escape(run_dir("TASK-9")) + "/0f0f0f0f-1111-2222-3333-444444444444.jsonl"`; `transcript("TASK-9", "../x")` is None; `allowed_tools`: accepts `Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})` on a project with `repo_from_issue = true`; rejects `Bash(git push origin *)`, `Bash(git push {remote})`, a rule containing `pipeline.ROOT`, `Bash(python3 /tmp/x.py)`, `Bash(bash ./do.sh)`, `Bash(node x.js)`, and any `allowed_tools` on a project without `repo_from_issue`.
- [ ] **Step 2:** run `python3 -m unittest scripts.tests.test_pipeline -v` → new tests FAIL.
- [ ] **Step 3:** implement as in Interfaces.
- [ ] **Step 4:** full suite passes.
- [ ] **Step 5:** commit `pipeline: per-issue run dirs, transcript paths, allowed_tools validation`.

---

### Task 4: `eng.py` rework for the new layout

**Files:**
- Modify: `scripts/eng.py`, `scripts/tests/test_eng.py`

**Interfaces:**
- Consumes: `pipeline.run_dir` (Task 3).
- Changes:
  - `resolve`: `worktree = os.path.join(run_dir(ident), "worktrees", branch)` (was under the clone). Everything else unchanged.
  - Remove the `setup`, `push`, `pr` commands and the code only they use (`cmd_setup`, `cmd_push`, `cmd_pr`, `push_argv`, `PUSH_RULE`, `BODY_MAX`); keep `status`, `comments`.
  - `status` JSON gains `pr_title` = `pr_title(ok)` with every character outside `[A-Za-z0-9 .,:()_/-]` replaced by a space, and `worktrees_dir_ok` = `os.path.realpath(os.path.join(run_dir(ok.issue), "worktrees"))` starts with `os.path.realpath(run_dir(ok.issue)) + os.sep` (true when the dir does not exist yet and no component is a symlink leaving the run dir). `plan_docs` scans the worktree as before.
- Tests (update/add): `Resolve.test_ok_existing_clone` expects the new worktree path; a local-only branch (`branch --list` returns it, `ls-remote` not needed) with its worktree dir present resolves to that branch and path; `Cli`: `setup`/`push`/`pr` are rejected by argparse (exit 2); `status` includes `pr_title` sanitized (a title `ENG: a $(rm) \`x\` "q"` → no `$`, backtick or quote) and `worktrees_dir_ok` false when `work/<ID>/worktrees` is a symlink to a directory outside `work/<ID>/`; comments and transient-exit tests unchanged. Tests that call `eng.main` pass a temp `root` and patch `pipeline.WORK`/`eng.run_dir` so `run_dir` points into the temp dir.

- [ ] **Step 1:** update/add the tests → FAIL. **Step 2:** implement. **Step 3:** full suite passes. **Step 4:** commit `eng.py: worktree under work/<ID>/worktrees; status/comments only`.

---

### Task 5: router finds transcripts by issue and session

**Files:**
- Modify: `scripts/router.py`, `scripts/tests/test_router.py`, and remove `TRANSCRIPTS` from `scripts/pipeline.py` if unused.

**Interfaces:**
- Consumes: `pipeline.transcript`, `pipeline.PROJECTS`.
- Changes: `tdir` defaults to `PROJECTS` (argument names may stay). `is_live(tdir, issue, sid, now)`: checks `transcript(issue, sid, tdir)` and the files under `<its folder>/<sid>/`. Recover's "has a transcript" check uses `transcript(issue, sid, tdir)`; the `start` line's `transcript=` value is `transcript(issue, sid, tdir)`. Every call site already knows the issue identifier.
- Tests: the fixtures' `touch()` helper writes under `tdir/<escape(run_dir(issue))>/`; all existing router tests keep passing after the fixture change; add one test that a transcript for the same sid in another project's folder is ignored.

- [ ] Steps: tests first (FAIL), implement, full suite, commit `router: locate transcripts under each issue's run dir`.

---

### Task 6: launcher — every run in `work/<ID>/`, Engineering repo step

**Files:**
- Modify: `scripts/launch.py`, `scripts/tests/test_launch.py`

**Interfaces:**
- Consumes: `pipeline.run_dir`, `pipeline.transcript`, `pipeline.linear_gql`, `eng.resolve`, `eng.Ok/Invalid/Transient`, `eng.sh_run`.
- Behavior (spec §5), for every project:
  - cwd `run_dir(issue)` (`os.makedirs(..., exist_ok=True)`), used for tmux `-c`.
  - claude flags: `--setting-sources user --strict-mcp-config`; `--add-dir <ROOT>/stages --add-dir <ROOT>/templates` plus each `add_dirs` entry (no `--add-dir <ROOT>`); `--disallowedTools` with `Edit(//P)` and `Write(//P)` for `P` in `<ROOT>/stages/**`, `<ROOT>/templates/**`, `<run_dir>/worktrees/*/.git`, and for `repo_from_issue` projects each expanded `add_dirs` path + `/**`. One helper `deny(path)` returns the two rules; `//` + the absolute path without its leading `/`.
  - prompt gains ` Reviewer: <cfg["human_members"][0] or "none">. Project: <project id>.`
  - resume: if `transcript(issue, sid)` is None or does not exist → print a reason, append `transient` to the project log, return 3 (no tmux).
- For `repo_from_issue`: `resolve(issue, gql or linear_gql, run)`:
  - `Ok` → env `AGENT_PM_ISSUE=<issue>` in the tmux `export`; prompt gains ` Repo check: OK <owner>/<name>, clone <clone>, default branch <default>, branch <branch>, worktree <worktree>.`; `--allowedTools` + each `allowed_tools` template `.format(worktree=…, branch=…, owner=…, name=…, default=…, clone=…)` when non-empty.
  - `Invalid` → new run: prompt gains ` Repo check failed: <reason>.`; resume: treated as `Transient`.
  - `Transient` → append `<%F %T> transient <issue>: <reason>` to the project log, print it to stderr, return 3.
- `main(argv, sh=…, config=None, runs=RUNS_LOG, logs=None, gql=None, run=eng.sh_run, projects=PROJECTS)` (tests pass a temp `projects` and patch `pipeline.WORK`).
- Tests: update the existing launch tests for the new cwd/flags (the tmux `-c` is `run_dir`, no `--add-dir <ROOT>`); add: flags and deny rules present for every project and in `//` form; Reviewer/Project line; resume without a transcript → 3 and no tmux; Engineering Ok (env, prompt line, no `--allowedTools` when empty, filled rule when set, `private_docs` deny rules), Invalid (reason line; resume → 3), Transient (3, log line); a non-Engineering project never calls `resolve`.

- [ ] Steps: tests first (FAIL), implement, full suite, commit `launch: per-issue run dirs, locked-down flags, Engineering repo step`.

---

### Task 7: stages, registry, docs

**Files:**
- Create: `stages/engineering.md`
- Modify: `stages/deep-research.md`, `stages/product-design.md`, `pipeline.toml`, `README.md`, `CLAUDE.md`, `scripts/tests/test_pipeline.py` (real-config test)

- [ ] **Step 1: Failing test** in `test_pipeline.py`: the real `pipeline.toml` loads; the Engineering entry (`ddbff8bf-b633-4b8c-9272-d1d5ee923747`) is runnable with `instructions == "stages/engineering.md"`, `prefix == "ENG"`, `repo_from_issue` true, no `allowed_tools`.
- [ ] **Step 2:** implement:
  - `pipeline.toml`: Engineering gets `instructions = "stages/engineering.md"`, `model = "opus"`, `effort = "xhigh"`, `add_dirs = ["~/playground/private_docs"]`, `repo_from_issue = true`.
  - `stages/engineering.md`: spec §6 in the style of the other stage files (title, one-line purpose, `## Board`, `## Inputs`, `## Steps` 1–8, `## Resume rule`), carrying every exact command from spec §6 verbatim (the three `git worktree add` forms, the push command, `gh pr create/edit` with `--title '<pr_title>'`), the `Build started` / `Build docs:` / `Build ready:` / `Build failed:` comment prefixes, the fallbacks, the docs-location rule, and "the reviewer and project come from the prompt's `Reviewer:`/`Project:` line"; `eng.py` invoked as `python3 ../scripts/eng.py <cmd>` relative to this file.
  - `stages/deep-research.md`, `stages/product-design.md`: the Board bullets that read `../pipeline.toml` (project id, reviewer) now say the prompt's `Reviewer: … Project: …` line gives them; nothing else changes.
  - `README.md`: layout (`work/<ID>/`, `work/<ID>/worktrees/<branch>`), flags every run gets, Engineering runnable, `repo_from_issue` / `allowed_tools` (exact templates only, empty by default), the repo step's three outcomes, `eng.py status`/`comments`, the `Repo:` line in the Handoff comment (URL and Linear link forms accepted), autopilot docs in the target repo (else `autopilot_docs/`), the `Build docs:` comment, deploy only when no DR/PD issue is In Progress.
  - `CLAUDE.md`: 2–4 lines: per-issue run dirs and transcript lookup; the flags and write scope; Engineering + `eng.py` (correctness aid, not a security boundary).
- [ ] **Step 3:** full suite passes. **Step 4:** commit `Engineering stage: runnable end to end`.

### Verification (S6)

- `python3 -m unittest discover -s scripts/tests` from the worktree root: all pass.
- `python3 scripts/router.py --now --dry-run` from the worktree: loads the new config without error (uses the worktree's `pipeline.toml`).
- `AGENT_PM_ISSUE=TASK-38 python3 scripts/eng.py status` (real Linear + gh): TASK-38's description has `repo: https://github.com/ophis/agent-pm` → prints JSON with `repo` `ophis/agent-pm` (read-only smoke).

## Progress

- decision(branch/worktree): branch `TASK-38-engineering-skeleton` in `.claude/worktrees/` (user rule: branch = `<issue ID>-<feature>`), ignored via `.git/info/exclude`, not `.gitignore`; dissent: none
- decision(S8): skip squash, keep commits; user squash-merges the PR (TASK-36 FR-6); dissent: none
- decision(owner of repo step): launcher resolves/validates Repo, clones, grants dir + allow rules (deterministic, testable); the stage owns branch/worktree/build/PR; dissent: none
- decision(Repo format): relax PRD FR-3's strict `Repo: owner/name` to case-insensitive key + owner/name | GitHub URL | Linear markdown link (user's real Handoff comment); dissent: none
- S3 panel: core=[architecture,spec-fitness] +optional=[security]
- S3 reviewers: architecture=a2a42575f505ff0d8 spec-fitness=a284d75b2a9cb20bb security=a73b1c9f1c347b47a
- S3 r0: architecture=FAIL spec-fitness=PASS security=FAIL -> architecture#1 transient infra errors treated as invalid Repo; security#1 wildcard push rule allows :main/--force/--delete; security#2 gh pr rules not pinned to repo; security#3 PR/issue comments from any author drive builds; security#4 default_branch unvalidated; security#5 no trust model for Repo source; spec fixed (snapshot design.r1.md)
- decision(push/PR enforcement): route all pushes and PRs through `scripts/eng.py` bound to AGENT_PM_ISSUE, one allow rule for that script, over exact raw-git allow rules or git hooks in the user's clone (hooks would also affect the user's own pushes); dissent: none
- decision(user): autopilot spec/plan docs go in the target repo per its convention, else `autopilot_docs/`; agent-pm: `docs/specs/` now tracked, `*.r<N>.md` snapshots ignored (e215a9d)
- S3 r1: architecture=PASS spec-fitness=PASS security=FAIL -> security#9/#11 eng.py allow rule defeatable by editing <ROOT>; security#12 pr --body-file reads any path; fix: no --allowedTools at all (auto classifier, as DR/PD pushes), body file confined to work/, push URL check, branch-name validation, title built by eng.py, Transient reason logged; snapshot design.r2.md
- S3 r2: architecture=PASS security=PASS spec-fitness=FAIL -> spec-fitness#4 FR-4a allow rules dropped with no remediation if the supervised run hits a stop; fix: exact-only `allowed_tools` templates in pipeline.toml (no `*`), `eng.py push --print`, compare-URL fallback for PR; snapshot design.r3.md
- S3 r3: architecture=PASS spec-fitness=PASS security=PASS -> converged. Folded in non-blockers: trust-model wording, stricter allowed_tools validation (placeholders, <ROOT>, interpreters, repo_from_issue), push via explicit SSH URL with hooks off, status reports plan docs, template==push --print test. Residual NB: architecture#5 repo re-derived on resume (known limitation); security#16 AGENT_PM_ISSUE override; security#17 default-branch protection via GitHub (user); security#18 docs public in public repos (D3)
- decision(user, during S5 after Tasks 1–2): Engineering runs start in the target worktree (launcher runs ensure_worktree, tmux -c <worktree>); router finds transcripts by session id under ~/.claude/projects/*/ (replaces TASK-20 FR-A5's cwd= in runs.log; survives runs.log's 7-day prune); eng.py loses `setup`. Recording runs on the Linear issue (attachment per run) is TASK-26, not this task. Spec snapshot design.r4.md; S3 delta review r4
- S3 delta r4: spec-fitness=PASS architecture=FAIL security=FAIL -> architecture#8 resume cwd from transcript ambiguous (records follow cd); security#21 target repo's .claude settings/hooks/.mcp.json load in its cwd; security#22 resume cwd unvalidated; fix: deterministic candidate match by escaped folder name else Transient, --setting-sources user --strict-mcp-config, transcript() in pipeline.py with UUID check and no symlinked folders, ensure_worktree ownership/realpath checks, resume Invalid → Transient; snapshot design.r5.md
- decision(user): worktrees live at <ROOT>/work/worktrees/<branch> (everything agent-pm runs sits under work/); accepted: agent-pm's CLAUDE.md is loaded as an ancestor; clones stay at ~/playground/<name> (D3)
- S3 delta r5: architecture=PASS security=FAIL -> security#26 project agents/skills/commands/CLAUDE.md under --setting-sources user unverified; security#27 --add-dir ROOT lets a run write other issues' worktrees; security#28 PR body from any file under work/. Probe (2026-09-28): --setting-sources user --strict-mcp-config loads no project CLAUDE.md/skills/agents; user plugins still load. Fix: stage reads target CLAUDE.md/AGENTS.md as a file; Engineering add-dirs = <ROOT>/stages + work/pr/<ID>/ (cwd = own worktree), not ROOT; body file under work/pr/<ID>/; escape() non-alnum; title-change limitation noted; snapshot design.r6.md
- decision(user): every run's cwd is a per-issue subdirectory of work/ (DR/PD work/<ID>/, Engineering work/worktrees/<branch> or work/<ID>/ when Invalid); resume uses the same rule and checks the transcript folder; no legacy fallback for sessions started in work/ (none pending)
- S3 delta r7/r8: architecture#9 reviewer/project unreachable → launcher prompt line; security#29–#34 fixed (deny rules, flags for all runs, .git/.claude)
- decision(user): launcher only resolves + clones + makes work/<ID>/; autopilot creates the worktree at work/<ID>/worktrees/<branch>; agent pushes/opens PRs itself (eng.py keeps status + comments only; setup/push/pr removed); every run's cwd is work/<ID>/; all runs lose --add-dir <ROOT> in favor of read-only stages/ and templates/, reviewer/project via the prompt. Spec rewritten clean (snapshot design.r9.md)
- S3 rewrite review: architecture=PASS security=PASS spec-fitness=PASS -> converged. Folded in: `//` absolute deny rules via one helper, second autopilot coupling named + checklist, local-only branch test, shell-safe pr_title, hardened remediation push rule, .git pointer deny, worktrees_dir_ok, private_docs read-only for Engineering. User add: `Build docs:` comment with spec/plan links after autopilot finishes (converged or failed). Plan Tasks 3–7 rewritten for the new layout
- S5 done (Tasks 1–7; user asked to skip per-task reviews from Task 5); side commit 879392f router window 02:00 (also on main as 08fd86a); Engineering effort xhigh (user)
- S6: 180 tests OK; router --now --dry-run loads the new config; `eng.py status` on TASK-38 works — found pr_title drops CJK characters (to fix in S7)
- S7 panel: core=[correctness,requirement-fidelity,doc] +optional=[architecture,test] +ad-hoc=[security (roster agent)]
- S7 reviewers: correctness=a0ae7dca8c34f051c requirement-fidelity=af761cf605dc03642 doc=a215e8bb58c346881 architecture=abb64bf5b92e24dce test=ada3c3716676a607d security=a499a14d6ecb18df6
- S7 r0: correctness=FAIL requirement-fidelity=FAIL doc=PASS architecture=FAIL test=PASS security=FAIL -> architecture#1 resolve breaks Ok/Invalid/Transient contract (json, null issue, git exit codes); architecture#2=correctness#1 status/comments hide gh failures; correctness#2 comments not paginated; correctness#3=requirement-fidelity#1 pr_title drops CJK; security#1 _pr accepts fork PRs with the same branch name; fix dispatched (pre-fix HEAD 2cd6d90, findings file tmp/s7-r0-findings.md)
- S7 r1: correctness=PASS requirement-fidelity=PASS doc=PASS architecture=PASS test=PASS security=PASS -> converged (fix b12160a, e11f5a7). Residual NON-BLOCKING: eng.main still tracebacks on unexpected resolve exceptions (launcher maps them to Transient); allowed_tools interpreter list misses pnpm/yarn/bun/cargo/go/pytest/uv and the $HOME root form; identifier mismatch bounces instead of following the new id; stage text mixes "<eng>" and "eng.py status"; no test for repo names starting with "."; work/<ID>/ cleanup is a follow-up
- Post-S9 (user): squashed to one commit; fixed residual NBs (resolve exception -> exit 3, allowed_tools build tools + $HOME root forms, <eng> wording, dot-name repo test). Moved-team identifier stays a bounce; noted on TASK-38.
- S8: skipped (decision: keep commits; user squash-merges)
- decision(allow rules): none by default, exact templated rules as the remediation path; eng.py is a correctness layer, not a security boundary; over snapshotting eng.py outside granted dirs or deny rules (Bash writes would bypass Edit denies); dissent: FR-4a asked for explicit allow rules, evidence says pushes already pass auto mode
- decision(Repo trust): keep D3 (any pushable repo) and the user's no-human-author-check stance; trust boundary = description body + ## Instructions, never ## Comments; GitHub comments only from the user's gh login; dissent: security may prefer an owner allowlist
