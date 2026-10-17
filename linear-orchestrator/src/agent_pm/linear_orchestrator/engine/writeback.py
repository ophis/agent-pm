"""Write-back: an agent run's progress, outcome and usage limit as Linear comments, attachments and
moves, each step once per session id (the ledger; comment steps by client id, found again on
resume); `deliver`, one DelegateEvent's write-back; `replay`, Recover's of an ended run's outcome;
bounce, the router's repo-check report.
"""

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from traceback import format_exception

from agent_pm.delegate import DelegateEvent, Outcome, Progress
from agent_pm.linear_orchestrator.engine import run_comments, runs
from agent_pm.linear_orchestrator.engine.config import LOCAL, TASKS
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.linear_orchestrator.engine.log import log
from agent_pm.linear_orchestrator.engine.ops import (
    M_STATE,
    Q_ISSUE_STATE,
    call,
    client_id,
    comment,
    find_comment,
    move,
    subscribe,
)
from agent_pm.linear_orchestrator.engine.types import (
    ISSUE_ID,
    Claim,
    ConfigError,
    Invalid,
    Issue,
    LinearError,
    Spec,
    Stopped,
    Unavailable,
)
from agent_pm.utils import (
    ConfinedFileError,
    ConfinedReason,
    OutcomeStatus,
    one_line,
    read_confined,
    save,
    timestamp,
)


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
    # look each comment step up by its client id before posting it, as on resume: its event may
    # come again after the post landed and its ledger write was lost
    redelivered: bool = False


M_TITLE = (
    "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
)
M_ATTACH = "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
M_UNARCHIVE = "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
Q_ATTACHED = "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }"
Q_ID = "query($i: String!) { issue(id: $i) { id team { id } } }"

LEDGER = "writeback.json"
LOCK = "writeback.lock"
MAX_FILE = 1_000_000
PLAN_MARK = "RESUME: phase="
FOOTER = "Answer in a comment, then move this issue back to Todo."
LIMIT = "Usage limit,"
ATTACHED = ("already been linked", "Duplicate attachment", "Unable to create issue attachment")


def context(cfg, role, gql, *, key, ident, issue_id, sid, resume, project, target):
    return Context(
        ident=ident,
        issue_id=issue_id,
        role=role,
        sid=sid,
        resume=resume,
        project=project,
        workdir=cfg.issue_workdir(ident),
        gql=functools.partial(gql, service=key),
        humans=tuple(e.lower() for e in cfg.human_members),
        states=cfg.states,
        repos=cfg.project_repos,
        team=cfg.team,
        target=target,
    )


def for_run(cfg, role, gql, spec: Spec):
    return context(
        cfg,
        role,
        gql,
        key=cfg.roles[role].key,
        ident=spec.issue,
        issue_id=spec.uuid,
        sid=spec.sid,
        resume=spec.mode == "resume",
        project=spec.project,
        target=None,
    )


def for_claim(cfg, claim: Claim, gql, *, attempt: int) -> Context:
    """Writes as the claim's account, whatever the role's is now."""
    return replace(
        context(
            cfg,
            claim.role,
            gql,
            key=claim.key,
            ident=claim.issue,
            issue_id=claim.uuid,
            sid=claim.sid,
            resume=attempt > 1,
            project=claim.project,
            target=claim.target,
        ),
        redelivered=True,
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
    log(
        "writeback",
        "step-error",
        ctx.ident,
        sid=ctx.sid,
        step=step,
        error=one_line(e),
        traceback="".join(format_exception(e)),
    )


def _guarded_move(ctx, issue, ident, to, frm):
    now = move(ctx.gql, issue, ctx.states[to], ctx.states[frm])
    if now not in (None, ctx.states[to]):
        log("writeback", "skip", ident, reason=f"move to {to}: issue is {now}")


def _attempt(record):
    return (
        isinstance(record, dict)
        and record.keys() <= {"started_at", "seq", "ended_at"}
        and isinstance(record.get("started_at"), str)
        and type(record.get("seq")) is int
        and record["seq"] >= 0
        and isinstance(record.get("ended_at", ""), str)
    )


def _entry(value):
    if not isinstance(value, dict) or not value.keys() <= {"steps", "attempts"}:
        return False
    steps, attempts = value.get("steps", []), value.get("attempts", {})
    return (
        isinstance(steps, list)
        and all(isinstance(s, str) for s in steps)
        and isinstance(attempts, dict)
        and all(n.isdigit() and _attempt(r) for n, r in attempts.items())
    )


def _ledger(ctx):
    """{sid: {"steps": [step, ...], "attempts": {"<attempt>": {"started_at", "seq", "ended_at"}}}}:
    each attempt's session comment start, last written number and, once written, end; anything
    else reads as empty.
    """
    try:
        # O_NONBLOCK: a FIFO there would block a plain open forever
        with os.fdopen(
            os.open(os.path.join(ctx.workdir, LEDGER), os.O_RDONLY | os.O_NONBLOCK), "rb"
        ) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    valid = isinstance(data, dict) and all(_entry(v) for v in data.values())
    return data if valid else {}


@contextlib.contextmanager
def _exclusive(ctx):
    """The run's write-back lock, held while a callback or Recover writes back: yields False,
    logged, when it cannot be opened.
    """
    try:
        os.makedirs(ctx.workdir, exist_ok=True)
        fd = os.open(
            os.path.join(ctx.workdir, LOCK),
            os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK,
            0o600,
        )
    except OSError as e:
        _error(ctx, "lock", e)
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield True
    finally:
        os.close(fd)


def _done(ctx):
    return _ledger(ctx).get(ctx.sid, {}).get("steps", [])


def _step(ctx, step, call, cid=None):
    if step in _done(ctx):
        return None
    try:
        # on resume or redelivery an unledgered post may have landed with its ledger write lost
        found = (
            cid is not None
            and (ctx.resume or ctx.redelivered)
            and find_comment(ctx.gql, cid, ctx.issue_id) is not None
        )
        if not found:
            call()
        ledger = _ledger(ctx)
        ledger.setdefault(ctx.sid, {}).setdefault("steps", []).append(step)
        save(os.path.join(ctx.workdir, LEDGER), json.dumps(ledger))
    except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
        _error(ctx, step, e)
        return e
    log("writeback", "step", ctx.ident, sid=ctx.sid, step=step, found=found or None)
    return None


def _post(ctx, step, body):
    cid = client_id(ctx.sid, step)
    return _step(ctx, step, lambda: comment(ctx.gql, ctx.issue_id, body, cid), cid)


def client_pinned(ctx, client, selected) -> bool:
    body = (
        f'This run keeps client "{client}"; the current Agent label selection is "{selected}". '
        "The label change has not taken effect. To switch clients, change the label, then personally "
        "move this issue to Todo to start a new run. If already Todo, move it out and back first."
    )
    ctx = replace(ctx, redelivered=True)
    with _exclusive(ctx) as held:
        return held and _post(ctx, "client-pinned", body) is None


def progress(ctx, event: Progress) -> bool:
    # raises only Stopped
    try:
        if not isinstance(event, Progress):
            return True
        task = TASKS[ctx.role]
        if event.name == "start":
            return _post(ctx, "start", say(task.start, event.text)) is None
        digest = hashlib.sha256(event.text.encode()).hexdigest()[:12]
        return (
            _post(ctx, f"progress:{event.name}:{digest}", f"Progress ({event.name}): {event.text}")
            is None
        )
    except Exception as e:  # noqa: BLE001  # A sink failure must not stop the run.
        _error(ctx, "sink", e)
        return False


def _read_file(path, workdir):
    try:
        return read_confined(path, workdir, limit=MAX_FILE)
    except ConfinedFileError as error:
        reasons = {
            ConfinedReason.REGULAR: "not a regular file",
            ConfinedReason.SUFFIX: "not a .md file",
            ConfinedReason.OUTSIDE: "not under the workdir",
            ConfinedReason.TOO_LARGE: f"over {MAX_FILE} bytes",
        }
        raise ValueError(reasons[error.reason]) from None


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
    # never raises: Stopped stops the steps and returns False, so the session comment's end is
    # still written
    try:
        return _finish(ctx, outcome)
    except (Exception, Stopped) as e:  # noqa: BLE001  # Report the session after failed steps.
        _error(ctx, "finish", e)
        return False


def _completion_body(ctx, o):
    task, url = TASKS[ctx.role], _url(ctx, o.url)
    if o.status == OutcomeStatus.DONE:
        lead, text, state = task.done, o.summary, "in_review"
    elif o.status == OutcomeStatus.NEEDS_INPUT:
        questions = "\n".join(f"{n}. {q}" for n, q in enumerate(o.questions, 1))
        text = f"{questions}\n\n{FOOTER}" + (f" {task.hint}" if task.hint else "")
        lead, state = task.question, "in_review"
    else:
        lead, text, state = task.failed, o.summary, "in_review" if ctx.resume else task.failed_new
    body = say(lead, text)
    if url and (
        o.status == OutcomeStatus.FAILED or o.status == OutcomeStatus.DONE and task.kind != "build"
    ):
        body += f"\n\n{url}"
    if task.approve and o.status == OutcomeStatus.DONE:
        body += f"\n\n{approve_line(ctx.repos.get(ctx.project))}"

    return body, state, url


def _post_files(ctx, o, body):
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
    return body


def _attach(ctx, url, title, attached):
    if url in attached:
        return
    try:
        call(ctx.gql, M_ATTACH, "attachmentLinkURL", i=ctx.issue_id, u=url, t=title)
    except LinearError as e:
        if not any(s in str(e) for s in ATTACHED):  # the GitHub integration links PRs itself
            raise


def _finish(ctx, o):
    task, gql, issue = TASKS[ctx.role], ctx.gql, ctx.issue_id
    body, state, url = _completion_body(ctx, o)

    try:
        attached = {a["url"] for a in gql(Q_ATTACHED, i=issue)["issue"]["attachments"]["nodes"]}
    except (LinearError, Unavailable, OSError, subprocess.SubprocessError) as e:
        _error(ctx, "read", e)
        return False

    if task.files and o.status != OutcomeStatus.NEEDS_INPUT:
        body = _post_files(ctx, o, body)

    if (
        task.retitle
        and o.status == OutcomeStatus.DONE
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

    if (
        o.status == OutcomeStatus.DONE
        and url
        and _step(ctx, f"attach:{url}", lambda: _attach(ctx, url, o.title, attached))
    ):
        return False

    if _step(
        ctx, f"move:{state}", lambda: _guarded_move(ctx, issue, ctx.ident, state, "in_progress")
    ):
        return False
    # the write-back is complete: Recover replays a valid outcome's write-back until this is ledgered
    return _step(ctx, "outcome", lambda: None) is None


def _session(ctx, cfg, client, attempt, *, gql, now, end=None) -> bool:
    """The attempt's session comment, as the harness: running at Begun, its `end` (rc, outcome,
    limit) at Finished, each written once.
    """
    record = _ledger(ctx).get(ctx.sid, {}).get("attempts", {}).get(str(attempt))
    record = record or {"started_at": now(), "seq": 0}
    if "ended_at" in record or end is None and record["seq"]:
        return True
    rec = run_comments.base(
        sid=ctx.sid,
        workdir=ctx.workdir,
        client=client,
        started_at=record["started_at"],
        info=lambda: LinearRun.attach(ctx.ident, cfg=cfg, sid=ctx.sid).delegate.session_status(),
        seq=record["seq"],
    )
    harness = functools.partial(gql, service=cfg.harness_key, timeout=run_comments.LIMIT)
    if end is None:
        error = run_comments.post(ctx.ident, rec, gql=harness)
    else:
        ended_at = now()
        rc, outcome, limited = end
        error = run_comments.post(
            ctx.ident, rec, gql=harness, rc=rc, ended_at=ended_at, outcome=outcome, limit=limited
        )
        if error is None:
            record = {**record, "ended_at": ended_at}
            error = run_comments.repair(ctx.ident, ctx.issue_id, rec, gql=harness)
    if error is None or "ended_at" in record:
        record = {**record, "seq": rec["seq"]}
    try:
        ledger = _ledger(ctx)
        ledger.setdefault(ctx.sid, {}).setdefault("attempts", {})[str(attempt)] = record
        save(os.path.join(ctx.workdir, LEDGER), json.dumps(ledger))
    except OSError as e:
        error = error or one_line(e)
    if error:
        log("writeback", "registry-error", ctx.ident, sid=ctx.sid, error=error)
    return error is None


def _limited(ctx, runs_log, claim, attempt, resets_at) -> bool:
    """A usage limit: its comment once per sid; this attempt's guarded move to In Review and its
    runs.jsonl row, each once.
    """
    at = local_time(resets_at)
    move = f"limit-move:{attempt}"
    moved = move in _done(ctx) or (
        _post(
            ctx,
            "limit",
            say(LIMIT, f"resets at {at}." if at else "reset time unknown.")
            + subscribe(ctx.gql, ctx.issue_id, ctx.humans),
        )
        is None
        and _step(
            ctx,
            move,
            lambda: _guarded_move(ctx, ctx.issue_id, ctx.ident, "in_review", "in_progress"),
        )
        is None
    )
    noted = _step(
        ctx,
        f"limit-row:{attempt}",
        lambda: runs.note_limit(
            runs_log,
            issue=ctx.ident,
            sid=ctx.sid,
            client=claim.client,
            resets_at=at,
            delegate_kind=claim.delegate_kind,
            attempt=attempt,
        ),
    )
    return moved and noted is None


def _claim(cfg, issue, sid) -> Claim:
    claim = runs.claim(cfg.runs_log, issue, sid)
    if claim.role not in TASKS:
        raise ConfigError(f"{issue}: role {claim.role!r} has no write-back")
    return claim


def _replayed_attempt(begun, rows) -> int:
    """The latest Begun's attempt, the next one when a start or resume row came after it (a
    launch whose Begun was lost); without a Begun, the rows' count. A row refused before its launch
    precedes the Begun of the attempt that ran, or leaves no outcome to replay.
    """
    if not begun:
        return len(rows)
    last = max(int(n) for n in begun)
    since = datetime.fromisoformat(begun[str(last)]["started_at"]).astimezone()
    return last + any(e.ts > since for e in rows)


def replay(
    cfg, issue: str, sid: str, outcome: Outcome, *, gql, dry=False, now=timestamp
) -> bool | None:
    """Recover's write-back of the ended (issue, sid) run's outcome, in process and each step once,
    as its callback's, then its session comment's end unless the callback's wrote it: None when
    its ledger has it complete; else whether it now is (dry: True, nothing written). Raises
    ConfigError or ValueError on a start row it cannot write back as.
    """
    rows = [e for e in runs.parse_log(cfg.runs_log) if (e.issue, e.sid) == (issue, sid)]
    claim = _claim(cfg, issue, sid)
    ctx = for_claim(cfg, claim, gql, attempt=len(rows))
    attempt = _replayed_attempt(_ledger(ctx).get(sid, {}).get("attempts", {}), rows)
    ctx = replace(ctx, resume=attempt > 1)
    if "outcome" in _done(ctx):
        return None
    if dry:
        return True
    with _exclusive(ctx) as held:
        if not held:
            return False
        done = "outcome" in _done(ctx) or finish(ctx, outcome)
        end = (None, outcome, False)
        _session(ctx, cfg, claim.client, attempt, gql=gql, now=now, end=end)
        return done


def deliver(cfg, issue: str, workdir: str, event: DelegateEvent, *, gql, now=timestamp) -> bool:
    """Writes the (issue, event.sid) run's event back as its start row's claim, each step once
    however often the event arrives; False when a write failed. Raises ConfigError or ValueError,
    before any write, on what it cannot write.
    """
    if not re.fullmatch(ISSUE_ID, issue):
        raise ConfigError(f"bad issue id: {issue}")
    if os.path.abspath(workdir) != os.path.abspath(cfg.issue_workdir(issue)):
        raise ConfigError(f"--workdir {workdir}: {issue}'s is {cfg.issue_workdir(issue)}")
    attempt = event.attempt
    if attempt is None:  # DelegateEvent requires one
        raise ValueError("attempt: missing")
    claim = _claim(cfg, issue, event.sid)
    ctx = for_claim(cfg, claim, gql, attempt=attempt)
    with _exclusive(ctx) as held:
        return held and _deliver(ctx, cfg, claim, event, gql=gql, now=now)


def _deliver(ctx, cfg, claim, event, *, gql, now) -> bool:
    attempt = event.attempt
    session = functools.partial(_session, ctx, cfg, claim.client, attempt, gql=gql, now=now)
    if event.kind == "begun":
        registered = session()
        return _running(ctx, attempt) and registered
    if event.kind == "progress":
        return _running(ctx, attempt) and progress(
            ctx, Progress(event.name or "", event.text or "")
        )
    # Finished: the outcome (once ledgered, never again), the limit, then the session's end
    outcome = event.outcome
    ok = outcome is None or "outcome" in _done(ctx) or finish(ctx, outcome)
    if event.limit is not None:
        ok = _limited(ctx, cfg.runs_log, claim, attempt, event.limit["resets_at"]) and ok
    elif outcome is None:
        log("writeback", "no-outcome", ctx.ident, sid=ctx.sid, error=event.error or "no result")
    ended = session(end=(event.returncode, outcome, event.limit is not None))
    log("writeback", "end", ctx.ident, sid=ctx.sid, exit=event.returncode)
    return ended and ok


def _running(ctx, attempt) -> bool:
    attempts = _ledger(ctx).get(ctx.sid, {}).get("attempts", {})
    if any(int(n) > attempt or int(n) == attempt and "ended_at" in r for n, r in attempts.items()):
        return True

    def activate():
        state = ctx.gql(Q_ISSUE_STATE, i=ctx.issue_id)["issue"]["state"]["id"]
        if state != ctx.states["in_progress"]:
            call(ctx.gql, M_STATE, "issueUpdate", i=ctx.issue_id, s=ctx.states["in_progress"])

    return _step(ctx, f"running:{attempt}", activate) is None


def bounce(ctx, issue: Issue, invalid: Invalid) -> None:
    reason = invalid.reason
    gql, h = ctx.gql, issue.handoff
    src = gql(Q_ID, i=h.source)["issue"] if h else None
    if h is not None and src and src["team"]["id"] == ctx.team:
        text = (
            f"Repo check failed: {reason}. To build it, move {h.source} to Handoff again with a comment "
            "`Repo: <owner>/<name>` naming the target repo."
        ) + (f" Or fix {LOCAL}'s [project_repos] entry." if invalid.mapped else "")
        with contextlib.suppress(LinearError, Unavailable, OSError, subprocess.SubprocessError):
            call(gql, M_UNARCHIVE, "issueUnarchive", i=src["id"])
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
