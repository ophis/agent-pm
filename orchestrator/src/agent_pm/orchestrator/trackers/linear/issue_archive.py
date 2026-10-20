"""agent-pm-orchestrator issue archive: the ident checked, then the Tracker's archive rule
(Tracker.archive over the plan it reads first) applied and its result formatted: a receipt, a refusal naming
the unfinished issues, or the dry run's plan.
"""

from agent_pm.orchestrator.engine.flow.types import ArchiveRefused
from agent_pm.orchestrator.engine.types import Unavailable, WriteUnknown
from agent_pm.orchestrator.trackers.linear import issue_read, issue_write

SHOWN = 20
Q_ISSUE = "query($i: String!) { viewer { id name } issue(id: $i) { id identifier url } }"


def archive(tracker, gql, ident):
    """Archive ident once it and every unarchived descendant are finished; ArchiveRefused names the
    unfinished ones.
    """
    data = issue_read.lookup(gql, Q_ISSUE, ident)
    node = data["issue"]
    plan = tracker.archive_plan(node["id"])
    try:
        changed = tracker.archive(node["id"], plan)
    except ArchiveRefused as e:
        raise ArchiveRefused(e.plan, _refusal(ident, e.plan)) from None
    except Unavailable as error:  # the plan was read: only the write can be unanswered
        raise WriteUnknown(
            f"archive {ident}: result unknown ({error}): "
            f"check with agent-pm-orchestrator issue read {ident} before retrying"
        ) from None
    result = "changed" if changed else "unchanged"
    return issue_write.receipt(node, "archive", result, data["viewer"])


def dry_run(tracker, gql, ident):
    """What archive would do, with the descendants it would archive and the unfinished issues."""
    plan = tracker.archive_plan(issue_read.lookup(gql, Q_ISSUE, ident)["issue"]["id"])
    return {
        "issue": {"id": plan.key, "identifier": plan.ident, "url": plan.url},
        "operation": "archive",
        "dry_run": True,
        "result": plan.outcome,
        "descendant_count": len(plan.descendants),
        "descendants": [_item(s) for s in plan.descendants],
        "unfinished_count": len(plan.unfinished),
        "unfinished": [_item(s) for s in plan.unfinished],
    }


def _item(sub):
    return {"id": sub.key, "identifier": sub.ident, "state": sub.state}


def _refusal(ident, plan):
    unfinished = plan.unfinished
    shown = ", ".join(f"{s.ident} ({s.state})" for s in unfinished[:SHOWN])
    more = f" and {len(unfinished) - SHOWN} more" if len(unfinished) > SHOWN else ""
    return (
        f"archive {ident} refused, nothing archived: {len(unfinished)} unfinished of "
        f"{len(plan.descendants) + 1} (it and its unarchived sub-issues): {shown}{more}; complete "
        f"or cancel each first (agent-pm-orchestrator issue archive {ident} --dry-run lists them)"
    )
