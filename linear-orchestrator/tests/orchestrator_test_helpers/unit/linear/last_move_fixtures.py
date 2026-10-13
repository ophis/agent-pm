
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.linear.data import IN_PROGRESS, IN_REVIEW, TODO


class LastMove(Case):
    NODES = [
        {"createdAt": "2026-09-27T10:00:00.000Z", "actorId": "human", "toStateId": TODO},
        {"createdAt": "2026-09-27T12:00:00.000Z", "actorId": "agent", "toStateId": TODO},
        {"createdAt": "2026-09-27T11:00:00.000Z", "actorId": "agent", "toStateId": IN_REVIEW},
        {"createdAt": "2026-09-27T13:00:00.000Z", "actorId": None, "toStateId": IN_PROGRESS},
    ]
