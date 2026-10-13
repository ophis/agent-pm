from agent_pm.linear_orchestrator.engine import inputs, target
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.inputs.data import ANN, BASE, note


class RenderBuild(Case):
    TARGET = target.Target("ophis", "agent-pm", "ENG-7-session-registry")

    PRD = inputs.Doc(
        BASE + "Product%20Design/2026-09-05-PM-9-q.md",
        "Product Design/2026-09-05-PM-9-q.md",
        "# PRD\n",
    )

    def notes(self):
        return (
            note("early user", "2026-09-02T00:00:00.000Z", ANN),
            note("agent early", "2026-09-03T00:00:00.000Z"),
            note("Build started: https://github.com/o/r/tree/b", "2026-09-04T00:00:00.000Z"),
            note("at the cutoff", "2026-09-04T00:00:00.000Z", ANN),
            note("Question: which?", "2026-09-05T00:00:00.000Z"),
            note("Use Redis.", "2026-09-06T00:00:00.000Z", ANN),
            note("bot late", "2026-09-07T00:00:00.000Z", (None, None)),
            note("Bob says more", "2026-09-08T00:00:00.000Z", ("bob@example.com", "Bob")),
        )
