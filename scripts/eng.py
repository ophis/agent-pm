#!/usr/bin/env python3
"""Engineering runs: resolve an issue's target repo, and a CLI for the stage (spec docs/specs/2026-09-27-task-38-…-design.md)."""
import json, os, re, subprocess, sys
from dataclasses import dataclass
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import PATH, parse_time, run_dir  # noqa: E402

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

class TransientError(Exception):
    """A gh/git call of a CLI command failed: exit 3."""

class Malformed(Exception):
    """gh printed something other than the expected JSON: exit 2."""

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

def _stderr(res):
    return (res.stderr or "").strip()[:200]

def _repo_info(owner, name, run):
    """(push permission, default branch) from `gh api repos/…`, or an Invalid/Transient."""
    res, err = _call(run, ["gh", "api", f"repos/{owner}/{name}"])
    if err:
        return err
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            return Invalid(f"{owner}/{name}: not found or no access (HTTP {code.group(1)})")
        return Transient(f"gh api repos/{owner}/{name}: {_stderr(res)}")
    try:
        data = json.loads(res.stdout)
    except ValueError:
        data = None
    perms = (data.get("permissions") or {}) if isinstance(data, dict) else None
    if not isinstance(perms, dict):
        return Transient(f"gh api repos/{owner}/{name}: malformed JSON")
    return perms.get("push") is True, data.get("default_branch")

def _existing_branches(ident, clone, run):
    """Names of the <ident>-* branches, local else on origin, or a Transient."""
    res, err = _call(run, ["git", "-C", clone, "branch", "--list", f"{ident}-*", "--format=%(refname:short)"])
    if err:
        return err
    if res.returncode != 0:
        return Transient(f"git branch --list: {_stderr(res)}")
    if local := res.stdout.split():
        return local
    res, err = _call(run, ["git", "-C", clone, "ls-remote", "--heads", "origin", f"{ident}-*"])
    if err:
        return err
    if res.returncode != 0:
        return Transient(f"git ls-remote: {_stderr(res)}")
    return [line.split("refs/heads/", 1)[1] for line in res.stdout.splitlines() if "refs/heads/" in line]

def resolve(issue_id, gql, run, playground=PLAYGROUND):
    try:
        issue = gql(Q_ISSUE, i=issue_id)["issue"]
    except (SystemExit, Exception) as e:
        return Transient(f"Linear: {e}")
    if issue is None:
        return Invalid(f"{issue_id}: issue not found")
    if issue.get("identifier") != issue_id:
        return Invalid(f"Linear returned {str(issue.get('identifier'))[:40]!r} for {issue_id}")
    repo = parse_repo(issue.get("description"))
    if isinstance(repo, Invalid):
        return repo
    owner, name = repo
    info = _repo_info(owner, name, run)
    if isinstance(info, (Invalid, Transient)):
        return info
    push, default = info
    if not push:
        return Invalid(f"{owner}/{name}: no push permission")
    if not isinstance(default, str) or not REF.fullmatch(default):
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
            return Transient(f"gh repo clone {owner}/{name}: {_stderr(res)}")
    names = _existing_branches(issue_id, clone, run)
    if isinstance(names, Transient):
        return names
    if len(names) > 1:
        return Invalid(f"several {issue_id}-* branches: {', '.join(names)[:200]}")
    if names and not re.fullmatch(rf"{re.escape(issue_id)}-[a-z0-9-]{{1,40}}", names[0]):
        return Invalid(f"existing branch name {names[0][:80]!r} is not {issue_id}-<lowercase slug>")
    branch = names[0] if names else f"{issue_id}-{slug(issue['title'])}"
    return Ok(issue_id, issue["title"], owner, name, clone, default, branch,
              os.path.join(run_dir(issue_id), "worktrees", branch))

def pr_title(ok):
    """`<ID>: <title without prefix>` reduced to Unicode letters/digits, spaces and .,:()_/- (safe in single quotes)."""
    t = f"{ok.issue}: " + re.sub(r"^[A-Z][A-Z0-9]*: ", "", ok.title, count=1)
    return " ".join(re.sub(r"[^\w .,:()/-]", " ", t).split())

def iso(s):
    t = parse_time(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

def _out(run, argv):
    res = run(argv, SHORT)
    if res.returncode != 0:
        raise TransientError(f"{' '.join(argv)[:150]}: {_stderr(res)}")
    return res.stdout

def _json(run, argv):
    try:
        return json.loads(_out(run, argv))
    except ValueError as e:
        raise Malformed(f"{' '.join(argv)[:150]}: {e}") from None

def _rows(data, what):
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise Malformed(f"{what}: not a JSON list of objects")
    return data

def _author(row, key):
    who = row.get(key)
    return who.get("login") if isinstance(who, dict) else None

def _login(run):
    data = _json(run, ["gh", "api", "user"])
    login = data.get("login") if isinstance(data, dict) else None
    if not isinstance(login, str) or not login:
        raise Malformed("gh api user: no login")
    return login

def _pr(ok, run, login):
    """The newest PR for the branch from the repo itself by the gh login; fork PRs and other authors are ignored."""
    rows = _rows(_json(run, ["gh", "pr", "list", "--repo", f"{ok.owner}/{ok.name}", "--head", ok.branch, "--state", "all",
                             "--json", "number,url,state,isCrossRepository,author", "--limit", "100"]), "gh pr list")
    mine = [r for r in rows if r.get("isCrossRepository") is False and _author(r, "author") == login]
    if not mine:
        return None
    if not isinstance(mine[0].get("number"), int):
        raise Malformed("gh pr list: a PR without a number")
    return {k: mine[0].get(k) for k in ("number", "url", "state")}

def _plan_docs(worktree):
    found = []
    for base, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".claude")]
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(base, f)
                try:
                    with open(p, errors="replace") as fh:
                        m = re.search(r"RESUME: phase=(S\d)", fh.read())
                except OSError:
                    continue
                if m:
                    found.append({"path": p, "phase": m.group(1)})
    return sorted(found, key=lambda d: d["path"])

def _branch_exists(ok, run):
    local = run(["git", "-C", ok.clone, "show-ref", "--verify", "--quiet", f"refs/heads/{ok.branch}"], SHORT)
    if local.returncode == 0:
        return "local"
    if local.returncode != 1:
        raise TransientError(f"git show-ref: {_stderr(local)}")
    return "remote" if _out(run, ["git", "-C", ok.clone, "ls-remote", "--heads", "origin", ok.branch]).strip() else None

def worktrees_dir_ok(issue):
    base = run_dir(issue)
    return os.path.realpath(os.path.join(base, "worktrees")).startswith(os.path.realpath(base) + os.sep)

def cmd_status(ok, run, out):
    json.dump({"repo": f"{ok.owner}/{ok.name}", "clone": ok.clone, "default": ok.default, "branch": ok.branch,
               "worktree": ok.worktree, "branch_exists": _branch_exists(ok, run), "worktree_exists": os.path.isdir(ok.worktree),
               "pr": _pr(ok, run, _login(run)), "pr_title": pr_title(ok), "worktrees_dir_ok": worktrees_dir_ok(ok.issue),
               "plan_docs": _plan_docs(ok.worktree) if os.path.isdir(ok.worktree) else []}, out)
    out.write("\n")
    return 0

def _pages(run, path):
    """Every row of a paginated list endpoint; `--slurp` wraps the pages in one array."""
    pages = _json(run, ["gh", "api", "--paginate", "--slurp", path])
    if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
        raise Malformed(f"{path}: not a JSON array of pages")
    return _rows([r for p in pages for r in p], path)

def cmd_comments(ok, run, out, since):
    login = _login(run)
    pr = _pr(ok, run, login)
    kept, dropped = [], 0
    if pr:
        base = f"repos/{ok.owner}/{ok.name}"
        for path, key in ((f"{base}/issues/{pr['number']}/comments", "created_at"), (f"{base}/pulls/{pr['number']}/reviews", "submitted_at"),
                          (f"{base}/pulls/{pr['number']}/comments", "created_at")):
            for c in _pages(run, path):
                if c.get(key) is None:  # a pending review has no submitted_at
                    continue
                try:
                    at = iso(c[key])
                except (AttributeError, ValueError):
                    raise Malformed(f"{path}: bad {key} {str(c[key])[:40]!r}") from None
                if at <= since:
                    continue
                if _author(c, "user") != login:
                    dropped += 1
                    continue
                kept.append((at, {"at": c[key], "body": c.get("body", ""), "kind": path.rsplit("/", 1)[1]}))
    json.dump({"kept": [c for _, c in sorted(kept, key=lambda k: k[0])], "dropped": dropped}, out)
    out.write("\n")
    return 0

def _command(a, issue, gql, run, out, err):
    try:
        ok = resolve(issue, gql, run)
    except Exception as e:
        raise TransientError(f"resolve: {e!r}") from None
    if isinstance(ok, Transient):
        raise TransientError(ok.reason)
    if isinstance(ok, Invalid):
        err.write(f"eng.py: Invalid: {ok.reason}\n")
        return 2
    return cmd_status(ok, run, out) if a.cmd == "status" else cmd_comments(ok, run, out, a.since)

def main(argv, env=os.environ, gql=None, run=sh_run, out=sys.stdout, err=sys.stderr):
    import argparse
    ap = argparse.ArgumentParser(prog="eng.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("comments").add_argument("--since", required=True, type=iso)
    a = ap.parse_args(argv)
    issue = env.get("AGENT_PM_ISSUE", "")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", issue):
        err.write("eng.py: AGENT_PM_ISSUE is not set to an issue id\n")
        return 2
    if gql is None:
        from pipeline import linear_gql as gql
    try:
        return _command(a, issue, gql, run, out, err)
    except Malformed as e:
        err.write(f"eng.py: malformed gh output: {e}\n")
        return 2
    except (TransientError, subprocess.TimeoutExpired, OSError) as e:
        err.write(f"eng.py: transient: {e}\n")
        return 3

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
