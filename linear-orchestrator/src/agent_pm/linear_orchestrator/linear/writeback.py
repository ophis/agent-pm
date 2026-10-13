"""Write-back: an agent run's progress, outcome and usage limit as Linear comments, attachments and moves, each step
once per session id (the ledger; comment steps by client id, found again on resume); bounce, the router's repo-check
report."""

import fcntl
import functools
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from agent_pm.delegate import Outcome, Progress
from agent_pm.linear_orchestrator.engine.config import LOCAL, TASKS
from agent_pm.linear_orchestrator.engine.target import MAPPED
from agent_pm.linear_orchestrator.linear.issues import Issue
from agent_pm.linear_orchestrator.linear.linear import (
    call,
    client_id,
    comment,
    find_comment,
    log,
    move,
    one_line,
    reraise_signal,
    subscribe,
)
from agent_pm.utils import save

M_TITLE = (
    "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
)
M_ATTACH = "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
M_UNARCHIVE = "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
Q_ATTACHED = "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }"
Q_ID = "query($i: String!) { issue(id: $i) { id team { id } } }"

LEDGER = "writeback.json"
MAX_FILE = 1_000_000
PLAN_MARK = "RESUME: phase="
FOOTER = "Answer in a comment, then move this issue back to Todo."
LIMIT = "Usage limit,"
ATTACHED = ("already been linked", "Duplicate attachment", "Unable to create issue attachment")
# Opens a file the agent run could replace. O_NONBLOCK: a FIFO planted there would block a plain open forever; regular-file
# reads ignore it.
NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class Context:
    ident: str
    issue_id: str
    role: str
    sid: str
    resume: bool
    project: str | None
    workdir: str
    gql: Callable[..., dict]
    humans: tuple[str, ...]
    states: Mapping[str, str]
    repos: Mapping[str, str]
    team: str
    target: tuple[str, str] | None


def context(cfg, role, gql, *, ident, issue_id, sid, resume, project, target):
    return Context(
        ident=ident,
        issue_id=issue_id,
        role=role,
        sid=sid,
        resume=resume,
        project=project,
        workdir=cfg.run_dir(ident),
        gql=functools.partial(gql, service=cfg.roles[role].key),
        humans=tuple(e.lower() for e in cfg.human_members),
        states=cfg.states,
        repos=cfg.project_repos,
        team=cfg.team,
        target=target,
    )


def say(lead, text):
    if not lead:
        return text
    if not text:
        return lead.removesuffix(":")
    return f"{lead}\n{text}" if "\n" in text else f"{lead} {text}"


def approve_line(repo):
    if repo is None:
        return "**🔴 To approve, move this issue to Handoff with a comment `Repo: <owner>/<name>` naming the target repo.**"
    return (
        f"**🔴 Target repo:** `{repo}` **(from the project mapping). To approve, move this issue to Handoff; "
        "to use another repo, comment** `Repo: <owner>/<name>` **first.**"
    )


def local_time(epoch):
    try:
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _error(ctx, step, e):
    log("writeback", "step-error", ctx.ident, sid=ctx.sid, step=step, error=one_line(e))


def _guarded_move(ctx, issue, ident, to, frm):
    now = move(ctx.gql, issue, ctx.states[to], ctx.states[frm])
    if now not in (None, ctx.states[to]):
        log("writeback", "skip", ident, reason=f"move to {to}: issue is {now}")


def _ledger(ctx):
    try:
        with os.fdopen(os.open(os.path.join(ctx.workdir, LEDGER), NO_FOLLOW), "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    valid = isinstance(data, dict) and all(
        isinstance(v, list) and all(isinstance(s, str) for s in v) for v in data.values()
    )
    return data if valid else {}


def _step(ctx, step, call, cid=None):
    if step in _ledger(ctx).get(ctx.sid, []):
        return None
    try:
        # on resume an unledgered post may have landed with its ledger write lost
        found = (
            cid is not None and ctx.resume and find_comment(ctx.gql, cid, ctx.issue_id) is not None
        )
        if not found:
            call()
        ledger = _ledger(ctx)
        ledger.setdefault(ctx.sid, []).append(step)
        save(os.path.join(ctx.workdir, LEDGER), json.dumps(ledger))
    except (Exception, SystemExit) as e:
        reraise_signal(e)
        _error(ctx, step, e)
        return e
    log("writeback", "step", ctx.ident, sid=ctx.sid, step=step, found=found or None)
    return None


def _post(ctx, step, body):
    cid = client_id(ctx.sid, step)
    return _step(ctx, step, lambda: comment(ctx.gql, ctx.issue_id, body, cid), cid)


def progress(ctx, event: Progress) -> None:
    # raises only a signal SystemExit: an on_event exception stops the Delegate's session
    try:
        if not isinstance(event, Progress):
            return
        task = TASKS[ctx.role]
        if event.name == "start":
            _post(ctx, "start", say(task.start, event.text))
        else:
            digest = hashlib.sha256(event.text.encode()).hexdigest()[:12]
            _post(ctx, f"progress:{event.name}:{digest}", f"Progress ({event.name}): {event.text}")
    except (Exception, SystemExit) as e:
        reraise_signal(e)
        _error(ctx, "sink", e)


def _read_file(path, workdir):
    # re-checked on the open fd: a process the agent run left behind could swap it after the Delegate's check
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
    if not url or TASKS[ctx.role].kind != "build":
        return url
    if ctx.target and url.lower().startswith(
        f"https://github.com/{ctx.target[0]}/{ctx.target[1]}/".lower()
    ):
        return url
    log(
        "writeback",
        "skip",
        ctx.ident,
        reason=f"url not on {'/'.join(ctx.target) if ctx.target else 'a target repo'}",
    )
    return ""


def finish(ctx, outcome: Outcome) -> bool:
    # never raises: a signal SystemExit stops the steps and returns False, so run.py still posts the session comment's
    # end
    try:
        return _finish(ctx, outcome)
    except (Exception, SystemExit) as e:
        _error(ctx, "finish", e)
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
    if url and (o.status == "failed" or o.status == "done" and task.kind != "build"):
        body += f"\n\n{url}"
    if task.approve and o.status == "done":
        body += f"\n\n{approve_line(ctx.repos.get(ctx.project))}"

    try:
        attached = {a["url"] for a in gql(Q_ATTACHED, i=issue)["issue"]["attachments"]["nodes"]}
    except (Exception, SystemExit) as e:
        reraise_signal(e)
        _error(ctx, "read", e)
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
            posts.append(
                (
                    "Plan" if PLAN_MARK in text else "Spec",
                    name,
                    text,
                    hashlib.sha256(data).hexdigest()[:12],
                )
            )
        for kind, name, text, digest in sorted(posts, key=lambda p: p[0] == "Plan"):
            if err := _post(ctx, f"file:{name}:{digest}", f"**{kind}** `{name}`\n\n---\n\n{text}"):
                body += f"\n\nCould not post the {kind} `{name}`: {one_line(err)}"

    if (
        task.retitle
        and o.status == "done"
        and _step(
            ctx,
            "retitle",
            lambda: call(gql, M_TITLE, "issueUpdate", i=issue, t=f"{task.prefix}: {o.title}"),
        )
    ):
        return False
    if state == "in_review":
        body += subscribe(gql, issue, ctx.humans)
    if _post(ctx, "comment", body):
        return False

    if o.status == "done" and url:

        def attach():
            if url in attached:
                return
            try:
                call(gql, M_ATTACH, "attachmentLinkURL", i=issue, u=url, t=o.title)
            except (Exception, SystemExit) as e:
                if not any(
                    s in str(e) for s in ATTACHED
                ):  # the GitHub integration links PRs itself
                    raise

        if _step(ctx, f"attach:{url}", attach):
            return False

    return (
        _step(
            ctx, f"move:{state}", lambda: _guarded_move(ctx, issue, ctx.ident, state, "in_progress")
        )
        is None
    )


def limit(ctx, resets_at: float | None) -> bool:
    try:
        return _limit(ctx, resets_at)
    except (Exception, SystemExit) as e:
        _error(ctx, "limit", e)
        return False


def _limit(ctx, resets_at):
    at = local_time(resets_at)
    body = say(LIMIT, f"resets at {at}." if at else "reset time unknown.")
    if _post(ctx, "limit", body + subscribe(ctx.gql, ctx.issue_id, ctx.humans)):
        return False
    # not ledgered: a resumed sid that hits the limit again moves again
    try:
        _guarded_move(ctx, ctx.issue_id, ctx.ident, "in_review", "in_progress")
    except (Exception, SystemExit) as e:
        reraise_signal(e)
        _error(ctx, "move:in_review", e)
        return False
    return True


def bounce(ctx, issue: Issue, reason: str) -> None:
    gql, h = ctx.gql, issue.handoff
    src = gql(Q_ID, i=h.source)["issue"] if h else None
    if src and src["team"]["id"] == ctx.team:
        text = (
            f"Repo check failed: {reason}. To build it, move {h.source} to Handoff again with a comment "
            "`Repo: <owner>/<name>` naming the target repo."
            + (f" Or fix {LOCAL}'s [project_repos] entry." if reason.startswith(MAPPED) else "")
        )
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
        comment(
            gql,
            ctx.issue_id,
            say(
                "Question:",
                f"repo check failed: {reason}. Fix the description's `Repo:` "
                "line, then move this issue back to Todo.",
            )
            + notes,
        )
        _guarded_move(ctx, ctx.issue_id, ctx.ident, "in_review", "in_progress")
