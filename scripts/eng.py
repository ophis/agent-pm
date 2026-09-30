#!/usr/bin/env python3
"""Engineering runs: resolve an issue's target repo, and a CLI for the stage."""
import json, os, re, subprocess, sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import CONFIG, NAME, OWNER, PATH, REF, load_config, parse_time, repo_slug, run_dir  # noqa: E402

PLAYGROUND = os.path.expanduser("~/playground")
Q_ISSUE = "query($i: String!) { issue(id: $i) { identifier title description project { id } } }"
Q_COMMENTS = "query($i: String!) { issue(id: $i) { createdAt comments(first: 250) { nodes { body createdAt user { email } } } } }"
MAPPED = "project mapping "
BUILD_STARTED = re.compile(r"Build started\b")
SHORT, LONG = 60, 600

@dataclass(frozen=True)
class Ok:
    issue: str; title: str; owner: str; name: str; clone: str; default: str; branch: str; worktree: str; mapped: bool = False; branch_exists: str | None = None

@dataclass(frozen=True)
class Invalid:
    reason: str

@dataclass(frozen=True)
class Transient:
    reason: str

class TransientError(Exception):
    """A gh/git/Linear call of a CLI command failed or returned something unusable."""

def sh_run(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env={**os.environ, "PATH": PATH})

def _one(value):
    v = value.strip()
    m = re.fullmatch(r"\[[^\]]*\]\(<?([^)>]+)>?\)", v)
    if m:
        v = m.group(1).strip()
    for pat in (rf"https://github\.com/({OWNER})/({NAME}?)(?:\.git)?/?", rf"git@github\.com:({OWNER})/({NAME}?)(?:\.git)?"):
        m = re.fullmatch(pat, v)
        if m and m.group(2) not in (".", ".."):
            return m.group(1), m.group(2)
    return repo_slug(v)

def parse_repo(description):
    found = []
    for line in (description or "").splitlines():
        if line.rstrip("\r") == "## Comments":
            break
        # Markdown a pasted line may carry: a list or quote marker, `code`, **bold**.
        m = re.match(r"^\s*(?:[-*>]\s+)?(?:\*\*|`)?repo:(?:\*\*)?\s*(.+?)\s*$", line.rstrip("\r"), re.I)
        if m:
            one = _one(m.group(1).strip("`*").strip())
            if not one:
                return Invalid(f"unreadable Repo line: {m.group(1)[:80]!r}")
            found.append(one)
    distinct = {(o.lower(), n.lower()) for o, n in found}
    if not found:
        return Invalid("no Repo: line in the description or its ## Instructions")
    if len(distinct) > 1:
        return Invalid("several different Repo: values")
    return found[0]

NO_LINE = parse_repo("")

def norm_url(url):
    m = re.fullmatch(r"(?:https://|ssh://git@|git@)github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?", url.strip(), re.I)
    return f"github.com/{m.group(1)}/{m.group(2)}".lower() if m else None

def slug(title):
    t = re.sub(r"^[A-Z][A-Z0-9]*: ", "", title, count=1).lower()
    s = "-".join(re.findall(r"[a-z0-9]+", t))[:40].rstrip("-")
    return s or "build"

def _stderr(res):
    return (res.stderr or "").strip()[:200]

def _repo_info(owner, name, run):
    """(push permission, default branch) from `gh api repos/…`, or an Invalid/Transient."""
    res = run(["gh", "api", f"repos/{owner}/{name}"], SHORT)
    if res.returncode != 0:
        code = re.search(r"HTTP (\d{3})", res.stderr or "")
        if code and code.group(1) in ("403", "404"):
            return Invalid(f"{owner}/{name}: not found or no access (HTTP {code.group(1)})")
        return Transient(f"gh api repos/{owner}/{name}: {_stderr(res)}")
    data = json.loads(res.stdout)
    return data["permissions"]["push"] is True, data["default_branch"]

def _existing_branches(ident, clone, run):
    """(names of the <ident>-* branches, "local" / "remote" / None where they were found), local else on origin, or a Transient."""
    res = run(["git", "-C", clone, "branch", "--list", f"{ident}-*", "--format=%(refname:short)"], SHORT)
    if res.returncode != 0:
        return Transient(f"git branch --list: {_stderr(res)}")
    if local := res.stdout.split():
        return local, "local"
    res = run(["git", "-C", clone, "ls-remote", "--heads", "origin", f"{ident}-*"], SHORT)
    if res.returncode != 0:
        return Transient(f"git ls-remote: {_stderr(res)}")
    remote = [line.split("refs/heads/", 1)[1] for line in res.stdout.splitlines() if "refs/heads/" in line]
    return remote, "remote" if remote else None

def in_playground(clone, playground=PLAYGROUND):
    """True if clone is a real (non-symlink) entry directly in playground, where the repo step keeps clones."""
    return not os.path.islink(clone) and os.path.dirname(os.path.realpath(clone)) == os.path.realpath(playground)

def resolve(issue_id, gql, run, playground=PLAYGROUND, repos=None):
    try:
        issue = gql(Q_ISSUE, i=issue_id)["issue"]
    except (SystemExit, Exception) as e:
        return Transient(f"Linear: {e}")
    if issue is None:
        return Invalid(f"{issue_id}: issue not found")
    if issue.get("identifier") != issue_id:
        return Invalid(f"Linear returned {str(issue.get('identifier'))[:40]!r} for {issue_id}")
    base = run_dir(issue_id)
    if not os.path.realpath(os.path.join(base, "worktrees")).startswith(os.path.realpath(base) + os.sep):
        return Invalid(f"{os.path.join(base, 'worktrees')} resolves outside {base}")
    repo = parse_repo(issue.get("description"))
    pid = (issue.get("project") or {}).get("id")
    mapped = repo == NO_LINE and pid in (repos or {})
    tag = MAPPED if mapped else ""
    if mapped:
        repo = repo_slug(repos[pid]) or Invalid(f"{MAPPED}{str(repos[pid])[:80]!r}: not <owner>/<name>")
    if isinstance(repo, Invalid):
        return repo
    owner, name = repo
    info = _repo_info(owner, name, run)
    if isinstance(info, Invalid):
        return Invalid(tag + info.reason)
    if isinstance(info, Transient):
        return info
    push, default = info
    if not push:
        return Invalid(f"{tag}{owner}/{name}: no push permission")
    if not isinstance(default, str) or not REF.fullmatch(default):
        return Invalid(f"{tag}{owner}/{name}: unsafe default branch name")
    clone = os.path.join(playground, name)
    if not in_playground(clone, playground):
        return Invalid(f"{clone} is a symlink or outside {playground}")
    want = f"github.com/{owner}/{name}".lower()
    if os.path.exists(clone):
        for extra in ([], ["--push"]):
            res = run(["git", "-C", clone, "remote", "get-url", *extra, "origin"], SHORT)
            if res.returncode != 0 or norm_url(res.stdout) != want:
                return Invalid(f"{clone} is not a clone of {owner}/{name} (origin {'push ' if extra else ''}URL differs)")
    else:
        res = run(["gh", "repo", "clone", f"{owner}/{name}", clone], LONG)
        if res.returncode != 0:
            return Transient(f"gh repo clone {owner}/{name}: {_stderr(res)}")
    found = _existing_branches(issue_id, clone, run)
    if isinstance(found, Transient):
        return found
    names, where = found
    if len(names) > 1:
        return Invalid(f"several {issue_id}-* branches: {', '.join(names)[:200]}")
    if names and not re.fullmatch(rf"{re.escape(issue_id)}-[a-z0-9-]{{1,40}}", names[0]):
        return Invalid(f"existing branch name {names[0][:80]!r} is not {issue_id}-<lowercase slug>")
    branch = names[0] if names else f"{issue_id}-{slug(issue['title'])}"
    return Ok(issue_id, issue["title"], owner, name, clone, default, branch,
              os.path.join(base, "worktrees", branch), mapped=mapped, branch_exists=where)

def pr_title(ok):
    """`<ID>: <title without prefix>` reduced to Unicode letters/digits, spaces and .,:()_/- (safe in single quotes)."""
    t = f"{ok.issue}: " + re.sub(r"^[A-Z][A-Z0-9]*: ", "", ok.title, count=1)
    return " ".join(re.sub(r"[^\w .,:()/-]", " ", t).split())

def _out(run, argv):
    res = run(argv, SHORT)
    if res.returncode != 0:
        raise TransientError(f"{' '.join(argv)[:150]}: {_stderr(res)}")
    return res.stdout

def _json(run, argv):
    try:
        return json.loads(_out(run, argv))
    except ValueError as e:
        raise TransientError(f"{' '.join(argv)[:150]}: {e}") from None

def _rows(data, what):
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise TransientError(f"{what}: not a JSON list of objects")
    return data

def _author(row, key):
    who = row.get(key)
    return who.get("login") if isinstance(who, dict) else None

def _login(run):
    data = _json(run, ["gh", "api", "user"])
    login = data.get("login") if isinstance(data, dict) else None
    if not isinstance(login, str) or not login:
        raise TransientError("gh api user: no login")
    return login

def _pr(ok, run, login):
    """The newest PR for the branch from the repo itself by the gh login; fork PRs and other authors are ignored."""
    rows = _rows(_json(run, ["gh", "pr", "list", "--repo", f"{ok.owner}/{ok.name}", "--head", ok.branch, "--state", "all",
                             "--json", "number,url,state,isCrossRepository,author", "--limit", "100"]), "gh pr list")
    mine = [r for r in rows if r.get("isCrossRepository") is False and _author(r, "author") == login]
    if not mine:
        return None
    if not isinstance(mine[0].get("number"), int):
        raise TransientError("gh pr list: a PR without a number")
    return {k: mine[0].get(k) for k in ("number", "url", "state")}

def _plan_docs(worktree, branch):
    found = []
    for base, dirs, files in os.walk(worktree):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".claude")]
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(base, f)
                try:
                    with open(p, errors="replace") as fh:
                        m = re.search(r"RESUME: phase=(S\d)([^\n]*)", fh.read())
                except OSError:
                    continue
                if m and f"branch={branch}" in m.group(2).split():
                    found.append({"path": p, "phase": m.group(1)})
    return sorted(found, key=lambda d: d["path"])

def cmd_status(ok, run, out):
    json.dump({"repo": f"{ok.owner}/{ok.name}", "clone": ok.clone, "default": ok.default, "branch": ok.branch,
               "worktree": ok.worktree, "branch_exists": ok.branch_exists, "worktree_exists": os.path.isdir(ok.worktree),
               "pr": _pr(ok, run, _login(run)), "pr_title": pr_title(ok),
               "plan_docs": _plan_docs(ok.worktree, ok.branch) if os.path.isdir(ok.worktree) else []}, out)
    out.write("\n")
    return 0

def _pages(run, path):
    """Every row of a paginated list endpoint; `--slurp` wraps the pages in one array."""
    pages = _json(run, ["gh", "api", "--paginate", "--slurp", path])
    if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
        raise TransientError(f"{path}: not a JSON array of pages")
    return _rows([r for p in pages for r in p], path)

def _linear(gql, issue):
    """(the issue's createdAt, [(instant, comment)]) of its first 250 comments; a failure or a missing field is Transient."""
    try:
        page = gql(Q_COMMENTS, i=issue)["issue"]
        return parse_time(page["createdAt"]), [(parse_time(n["createdAt"]), n) for n in page["comments"]["nodes"]]
    except (SystemExit, Exception) as e:
        raise TransientError(f"Linear: {e!r}") from None

def _pr_entries(ok, run, pr, since):
    """[(instant, entry)] of the PR's comments, reviews and inline comments after since; a pending review (no submitted_at) is skipped."""
    base, found = f"repos/{ok.owner}/{ok.name}", []
    for path, key, kind in ((f"{base}/issues/{pr['number']}/comments", "created_at", "comment"), (f"{base}/pulls/{pr['number']}/reviews", "submitted_at", "review"),
                            (f"{base}/pulls/{pr['number']}/comments", "created_at", "review_comment")):
        for c in _pages(run, path):
            if c.get(key) is None:
                continue
            try:
                at = parse_time(c[key])
            except (AttributeError, ValueError):
                raise TransientError(f"{path}: bad {key} {str(c[key])[:40]!r}") from None
            if at > since:
                e = {"at": c[key], "source": "pr", "kind": kind, "author": _author(c, "user"), "body": c.get("body") or ""}
                if kind == "review":
                    e["state"] = c.get("state")
                if kind == "review_comment":
                    e["path"], e["line"] = c.get("path"), c.get("line") or c.get("original_line")
                found.append((at, e))
    return found

def cmd_comments(ok, run, out, gql, humans):
    """The comments after the latest non-human `Build started` (else the issue's creation), the user's apart from the others'."""
    created, notes = _linear(gql, ok.issue)
    email = lambda n: (n["user"] or {}).get("email")
    who = lambda n: (email(n) or "").lower()
    since = max((at for at, n in notes if who(n) not in humans and BUILD_STARTED.match(n["body"].strip())), default=created)
    login = _login(run)
    pr = _pr(ok, run, login)
    user = [(at, {"at": n["createdAt"], "source": "linear", "kind": "comment", "author": email(n), "body": n["body"]})
            for at, n in notes if who(n) in humans and at > since]
    others = []
    for at, e in _pr_entries(ok, run, pr, since) if pr else []:
        (user if e["author"] == login else others).append((at, e))
    oldest_first = lambda rows: [e for _, e in sorted(rows, key=lambda r: r[0])]
    json.dump({"since": since.isoformat(), "user": oldest_first(user), "others": oldest_first(others)}, out)
    out.write("\n")
    return 0

def _command(a, issue, repos, humans, gql, run, out):
    try:
        ok = resolve(issue, gql, run, repos=repos)
    except Exception as e:
        raise TransientError(f"resolve: {e!r}") from None
    if isinstance(ok, (Invalid, Transient)):
        raise TransientError(ok.reason)
    return cmd_status(ok, run, out) if a.cmd == "status" else cmd_comments(ok, run, out, gql, humans)

def main(argv, env=os.environ, gql=None, run=sh_run, out=sys.stdout, err=sys.stderr, config=CONFIG):
    import argparse
    ap = argparse.ArgumentParser(prog="eng.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("comments")
    a = ap.parse_args(argv)
    issue = env.get("AGENT_PM_ISSUE", "")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", issue):
        err.write("eng.py: AGENT_PM_ISSUE is not set to an issue id\n")
        return 1
    try:
        cfg = load_config(config)
    except SystemExit as e:
        err.write(f"eng.py: {e.code}\n")
        return 1
    humans = {e.lower() for e in cfg.get("human_members") or []}
    if gql is None:
        from pipeline import linear_gql as gql
    try:
        return _command(a, issue, cfg["project_repos"], humans, gql, run, out)
    except (TransientError, subprocess.TimeoutExpired, OSError) as e:
        err.write(f"eng.py: {e}\n")
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
