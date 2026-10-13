from orchestrator_tests.integration.fake_linear.data import BUILT, MODULES
from orchestrator_tests.integration.fake_linear.operations import Operations
from orchestrator_tests.integration.fixture_errors import FixtureError


def texts():
    return {
        op: BUILT[op] if op in BUILT else getattr(MODULES[op.split(".")[0]], op.split(".")[1])
        for op in OPS
    }


def index(named):
    out = {}
    for name, text in named.items():
        if text in out:
            raise FixtureError.conflicting_operations(out[text], name)
        out[text] = name
    return out


OPS = {
    "linear.M_COMMENT": Operations.handle_comment_create,
    "linear.M_COMMENT_ID": Operations.handle_comment_create_id,
    "linear.Q_COMMENT": Operations.handle_comment_by_id,
    "linear.M_STATE": Operations.handle_set_state,
    "linear.M_SUBSCRIBE": Operations.handle_subscribe,
    "linear.Q_ISSUE_STATE": Operations.handle_issue,
    "linear.Q_USER": Operations.handle_users,
    "linear.Q_TEAM": Operations.handle_teams,
    "linear.Q_TASK_GROUP": Operations.handle_label_group,
    "router.Q_RELATIONS": Operations.handle_issue,
    "router.Q_RECHECK": Operations.handle_issue,
    "router.Q_HISTORY": Operations.handle_issue,
    "router.issues": Operations.handle_board,
    "router.issues+relations": Operations.handle_board,
    "issues.Q_ISSUE": Operations.handle_issue,
    "sessions.M_UPDATE": Operations.handle_comment_update,
    "writeback.M_TITLE": Operations.handle_set_title,
    "writeback.M_ATTACH": Operations.handle_attach,
    "writeback.M_UNARCHIVE": Operations.handle_unarchive,
    "writeback.Q_ATTACHED": Operations.handle_issue,
    "writeback.Q_ID": Operations.handle_issue,
    "promote.Q_HANDOFF": Operations.handle_handoff,
    "promote.Q_DETAIL": Operations.handle_issue,
    "promote.Q_CHILD": Operations.handle_child,
    "promote.M_CREATE": Operations.handle_create,
    "promote.M_RELATE": Operations.handle_relate,
    "prune.Q_ISSUE": Operations.handle_issue,
    "prune.Q_FINISHED": Operations.handle_finished,
    "prune.M_ARCHIVE": Operations.handle_archive,
}


TABLE = index(texts())
