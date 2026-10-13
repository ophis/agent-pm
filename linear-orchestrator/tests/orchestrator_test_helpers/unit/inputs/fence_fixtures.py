from agent_pm.linear_orchestrator.engine import inputs
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.inputs.data import BASE, DOCS, HUMANS, issue


class Fence(Case):
    def render_doc(self, text):
        src = inputs.Sources((inputs.Doc(BASE + "Research/a.md", "Research/a.md", text),), None)
        return inputs.render(issue("Q"), "researcher", src, humans=HUMANS, target=None, docs=DOCS)
