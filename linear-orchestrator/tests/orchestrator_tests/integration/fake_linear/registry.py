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
    "linear.M_COMMENT": Operations._comment_create,
    "linear.M_COMMENT_ID": Operations._comment_create_id,
    "linear.Q_COMMENT": Operations._comment_by_id,
    "linear.M_STATE": Operations._set_state,
    "linear.M_SUBSCRIBE": Operations._subscribe,
    "linear.Q_ISSUE_STATE": Operations._issue,
    "linear.Q_USER": Operations._users,
    "linear.Q_TEAM": Operations._teams,
    "linear.Q_TASK_GROUP": Operations._label_group,
    "router.Q_RELATIONS": Operations._issue,
    "router.Q_RECHECK": Operations._issue,
    "router.Q_HISTORY": Operations._issue,
    "router.issues": Operations._board,
    "router.issues+relations": Operations._board,
    "issues.Q_ISSUE": Operations._issue,
    "sessions.M_UPDATE": Operations._comment_update,
    "writeback.M_TITLE": Operations._set_title,
    "writeback.M_ATTACH": Operations._attach,
    "writeback.M_UNARCHIVE": Operations._unarchive,
    "writeback.Q_ATTACHED": Operations._issue,
    "writeback.Q_ID": Operations._issue,
    "promote.Q_HANDOFF": Operations._handoff,
    "promote.Q_DETAIL": Operations._issue,
    "promote.Q_CHILD": Operations._child,
    "promote.M_CREATE": Operations._create,
    "promote.M_RELATE": Operations._relate,
    "prune.Q_ISSUE": Operations._issue,
    "prune.Q_FINISHED": Operations._finished,
    "prune.M_ARCHIVE": Operations._archive,
}


TABLE = index(texts())
