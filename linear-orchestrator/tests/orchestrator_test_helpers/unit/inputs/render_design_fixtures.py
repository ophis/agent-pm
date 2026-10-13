
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.inputs.data import BASE, lines


class RenderDesign(Case):
    HANDOFF = lines(
        "Handoff from RES-4: https://linear.app/t/issue/RES-4",
        "",
        "## Source",
        f"- Spec: {BASE}Research/2026-09-01-RES-4-queues.md",
        "",
        "## Instructions",
        "Ann, 2026-09-01T00:00:00.000Z:",
        "Write the PRD.",
        "",
        "## Comments",
        "- Ann, 2026-09-01T00:00:00.000Z:",
        "  > hello",
        "- integration, 2026-09-01T01:00:00.000Z:",
        "  > bot",
    )
