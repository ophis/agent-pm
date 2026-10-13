import io
import json
import urllib.request
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.unit.board_ids import STATES

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"

OTHER = "00000000-0000-4000-8000-000000000023"

SECRET = "lin_api_SECRET"

VIEWER = "query { viewer { id } }"


def respond(body=b'{"data": {}}'):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body)
    return resp


def refused(case, call):
    with (
        mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("request sent")),
        mock.patch.object(
            urllib.request, "build_opener", side_effect=AssertionError("request sent")
        ),
        case.assertRaises(LinearError) as cm,
    ):
        call()
    case.assertIsInstance(str(cm.exception), str)
    return str(cm.exception)


def http_error(code, body=b""):
    return urllib.error.HTTPError(linear.URL, code, "Error", {}, io.BytesIO(body))


def errors(*codes):
    return json.dumps(
        {"errors": [{"message": "m", "extensions": {"code": c}} for c in codes]}
    ).encode()


IN_PROGRESS, IN_REVIEW, TODO = STATES["in_progress"], STATES["in_review"], STATES["todo"]

NAMES = {
    linear.M_SUBSCRIBE: "subscribe",
    linear.Q_ISSUE_STATE: "read",
    linear.M_STATE: "move",
    linear.M_COMMENT: "comment",
    linear.M_COMMENT_ID: "comment_id",
}

FIELDS = {
    "subscribe": "issueSubscribe",
    "move": "issueUpdate",
    "comment": "commentCreate",
    "comment_id": "commentCreate",
}

CHILD = "b52be5d1-f8aa-436f-af99-3e185bd14ef5"

ISSUE = "issue-uuid"


def comment_gql(node=None, error=None):
    calls = []

    def gql(query, **v):
        calls.append((query, v))
        if error is not None:
            raise error
        return {"comment": node}

    gql.calls = calls
    return gql


def node(body="Run x", issue=ISSUE, *, me=True):
    return {"id": "c-1", "body": body, "issue": {"id": issue}, "user": {"isMe": me}}
