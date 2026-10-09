"""Write-back: a core agent run's start and progress marks and its outcome, posted to its Linear issue as the role account,
and the engineering bounce before an agent run."""
import fcntl
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import LOCAL, TASKS  # noqa: E402
from linear import append, call, comment, move, one_line, reraise_signal, subscribe  # noqa: E402
import drive  # noqa: E402
from issues import Issue  # noqa: E402
from target import MAPPED  # noqa: E402

M_TITLE = "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
M_ATTACH = "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
M_UNARCHIVE = "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
Q_ATTACHED = "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }"
Q_ID = "query($i: String!) { issue(id: $i) { id team { id } } }"

LEDGER = "writeback.json"
MAX_FILE = 1_000_000
PLAN_MARK = "RESUME: phase="
FOOTER = "Answer in a comment, then move this issue back to Todo."
ATTACHED = ("already been linked", "Duplicate attachment", "Unable to create issue attachment")
# Opens a file the agent run could replace. O_NONBLOCK: a FIFO planted there would block a plain open forever; regular-file
# reads ignore it.
NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class Context:
    """What write-back needs of an agent run."""
    ident: str
    issue_id: str
    role: str
    sid: str
    resume: bool
    project: str | None
    workdir: str
    plog: str
    gql: Callable[..., dict]          # role account (service=role.key)
    humans: tuple[str, ...]           # human_members, config order
    states: dict[str, str]            # logical → state id
    repos: dict[str, str]             # project_repos
    team: str                         # cfg team id (bounce's SRC check)
    target: tuple[str, str] | None    # engineering (owner, name) given to the agent run (finish's URL check); else None


def say(lead, text):
    if not lead:
        return text
    if not text:
        return lead.removesuffix(":")
    return f"{lead}\n{text}" if "\n" in text else f"{lead} {text}"


def approve_line(repo):
    if repo is None:
        return "**🔴 To approve, move this issue to Handoff with a comment `Repo: <owner>/<name>` naming the target repo.**"
    return (f"**🔴 Target repo:** `{repo}` **(from the project mapping). To approve, move this issue to Handoff; "
            "to use another repo, comment** `Repo: <owner>/<name>` **first.**")


def _log(ctx, line):
    try:
        append(ctx.plog, line)
    except OSError:
        pass


def _guarded_move(ctx, issue, ident, to, frm):
    """linear.move from frm to to (logical states); a skip, the issue in neither, is logged."""
    now = move(ctx.gql, issue, ctx.states[to], ctx.states[frm])
    if now not in (None, ctx.states[to]):
        _log(ctx, f"writeback-skip {ident}: move to {to}: issue is {now}")


def _ledger(ctx):
    """{sid: [step, …]}; a missing, symlinked or invalid file is {}."""
    try:
        with os.fdopen(os.open(os.path.join(ctx.workdir, LEDGER), NO_FOLLOW), "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    valid = isinstance(data, dict) and all(isinstance(v, list) and all(isinstance(s, str) for s in v)
                                           for v in data.values())
    return data if valid else {}


def _step(ctx, step, call):
    """Runs call unless step is ledgered for ctx.sid, then ledgers it; returns the error (logged), else None."""
    if step in _ledger(ctx).get(ctx.sid, []):
        return None
    try:
        call()
        ledger = _ledger(ctx)
        ledger.setdefault(ctx.sid, []).append(step)
        drive.save(os.path.join(ctx.workdir, LEDGER), json.dumps(ledger))
    except (Exception, SystemExit) as e:
        reraise_signal(e)
        _log(ctx, f"writeback-error {ctx.ident}: {step}: {one_line(e)}")
        return e
    _log(ctx, f"writeback {ctx.ident}: {step}")
    return None


def sink(ctx) -> drive.Sink:
    """Phases 1 and 2: the `start` mark as the role's start comment, once per sid; any other mark as
    `Progress (<name>): <text>`. Raises only the runner's signal SystemExit: a raising sink kills the agent run."""
    def handle(event):
        try:
            if event.kind != "progress":
                return
            task = TASKS[ctx.role]
            if event.name == "start":
                _step(ctx, "start", lambda: comment(ctx.gql, ctx.issue_id, say(task.start, event.text)))
            else:
                digest = hashlib.sha256(event.text.encode()).hexdigest()[:12]
                _step(ctx, f"progress:{event.name}:{digest}",
                      lambda: comment(ctx.gql, ctx.issue_id, f"Progress ({event.name}): {event.text}"))
        except (Exception, SystemExit) as e:
            reraise_signal(e)
            _log(ctx, f"writeback-error {ctx.ident}: sink: {one_line(e)}")
    return handle


def _read_file(path, workdir):
    """An outcome file's bytes, re-checked on the open fd: a process the agent run left behind could swap it after
    drive.validate."""
    fd = os.open(path, NO_FOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_nlink != 1:
            raise ValueError("not a single-link file")
        real = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
        if not real.endswith(".md"):
            raise ValueError("not a .md file")
        if not real.startswith(os.path.join(os.path.realpath(workdir), "")):
            raise ValueError("not under the workdir")
        data = f.read(MAX_FILE + 1) if st.st_size <= MAX_FILE else None
    if data is None or len(data) > MAX_FILE:  # len: the file may grow after fstat
        raise ValueError(f"over {MAX_FILE} bytes")
    return data


def _url(ctx, url):
    """url, or "" when an engineering url is not on ctx.target (every url without one)."""
    if not url or TASKS[ctx.role].kind != "build":
        return url
    if ctx.target and url.lower().startswith(f"https://github.com/{ctx.target[0]}/{ctx.target[1]}/".lower()):
        return url
    _log(ctx, f"writeback-skip {ctx.ident}: url not on {'/'.join(ctx.target) if ctx.target else 'a target repo'}")
    return ""


def finish(ctx, outcome: drive.Outcome) -> bool:
    """Phase 3: the outcome as comments, title, attachment and state; steps ledgered per sid, stopping at the first
    error. True once every step is done (a file post or subscribe failure is noted in the comment instead); logs,
    never raises: a signal SystemExit stops the steps and returns False, so the runner still posts its end lines."""
    try:
        return _finish(ctx, outcome)
    except (Exception, SystemExit) as e:
        _log(ctx, f"writeback-error {ctx.ident}: finish: {one_line(e)}")
        return False


def _finish(ctx, o):
    task, gql, issue = TASKS[ctx.role], ctx.gql, ctx.issue_id
    url = _url(ctx, o.url)
    if o.status == "done":
        lead, text, state = task.done, o.summary, "in_review"
    elif o.status == "needs_input":
        questions = "\n".join(f"{n}. {q}" for n, q in enumerate(o.questions, 1))
        text = f"{questions}\n\n{FOOTER}" + (f" {task.hint}" if task.hint else "")
        lead, state = task.question, "in_review"
    else:
        lead, text, state = task.failed, o.summary, "in_review" if ctx.resume else task.failed_new
    body = say(lead, text)
    if url and o.status != "needs_input":
        body += f"\n\n{url}"
    if task.approve and o.status == "done":
        body += f"\n\n{approve_line(ctx.repos.get(ctx.project))}"

    try:
        attached = {a["url"] for a in gql(Q_ATTACHED, i=issue)["issue"]["attachments"]["nodes"]}
    except (Exception, SystemExit) as e:
        reraise_signal(e)
        _log(ctx, f"writeback-error {ctx.ident}: read: {one_line(e)}")
        return False

    if task.files and o.status != "needs_input":
        posts = []
        for path in o.files:
            name = os.path.basename(path)
            try:
                data = _read_file(path, ctx.workdir)
            except (OSError, ValueError) as e:
                body += f"\n\nCould not post the file `{name}`: {one_line(e)}"
                continue
            text = data.decode("utf-8", errors="replace")
            posts.append(("Plan" if PLAN_MARK in text else "Spec", name, text, hashlib.sha256(data).hexdigest()[:12]))
        for kind, name, text, digest in sorted(posts, key=lambda p: p[0] == "Plan"):
            if err := _step(ctx, f"file:{name}:{digest}",
                            lambda: comment(gql, issue, f"**{kind}** `{name}`\n\n---\n\n{text}")):
                body += f"\n\nCould not post the {kind} `{name}`: {one_line(err)}"

    if task.retitle and o.status == "done":
        if _step(ctx, "retitle", lambda: call(gql, M_TITLE, "issueUpdate", i=issue, t=f"{task.prefix}: {o.title}")):
            return False
    if state == "in_review":
        body += subscribe(gql, issue, ctx.humans)
    if _step(ctx, "comment", lambda: comment(gql, issue, body)):
        return False

    if o.status == "done" and url:
        def attach():
            if url in attached:
                return
            try:
                call(gql, M_ATTACH, "attachmentLinkURL", i=issue, u=url, t=o.title)
            except (Exception, SystemExit) as e:
                if not any(s in str(e) for s in ATTACHED):  # the GitHub integration links PRs itself
                    raise
        if _step(ctx, f"attach:{url}", attach):
            return False

    return _step(ctx, f"move:{state}", lambda: _guarded_move(ctx, issue, ctx.ident, state, "in_progress")) is None


def bounce(ctx, issue: Issue, reason: str) -> None:
    """The engineering bounce before an agent run: a Handoff issue whose source is in ctx.team goes back to that source (unarchived,
    In Review) and this issue is Canceled; else a `Question:` and In Review. A move happens only from the expected state
    (this issue In Progress, the source Done), else is logged. Raises on any failure but unarchive's and subscribe's
    (noted in the comment), and on a signal SystemExit."""
    gql, h = ctx.gql, issue.handoff
    src = gql(Q_ID, i=h.source)["issue"] if h else None
    if src and src["team"]["id"] == ctx.team:
        text = (f"Repo check failed: {reason}. To build it, move {h.source} to Handoff again with a comment "
                "`Repo: <owner>/<name>` naming the target repo."
                + (f" Or fix {LOCAL}'s [project_repos] entry." if reason.startswith(MAPPED) else ""))
        try:
            call(gql, M_UNARCHIVE, "issueUnarchive", i=src["id"])
        except (Exception, SystemExit) as e:
            reraise_signal(e)
        notes = subscribe(gql, src["id"], ctx.humans)
        comment(gql, src["id"], text + notes)
        _guarded_move(ctx, src["id"], h.source, "in_review", "done")
        comment(gql, ctx.issue_id, text)
        _guarded_move(ctx, ctx.issue_id, ctx.ident, "canceled", "in_progress")
    else:
        notes = subscribe(gql, ctx.issue_id, ctx.humans)
        comment(gql, ctx.issue_id, say("Question:", f"repo check failed: {reason}. Fix the description's `Repo:` "
                                                    "line, then move this issue back to Todo.") + notes)
        _guarded_move(ctx, ctx.issue_id, ctx.ident, "in_review", "in_progress")
