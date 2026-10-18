import re
from pathlib import Path

from tests.case import REPO, Case

WORKFLOWS = Path(REPO, ".github/workflows")
STATICCHECK = "sh with-go-state.sh go run honnef.co/go/tools/cmd/staticcheck@v0.8.2 ./..."


def job(workflow, name):
    text = (WORKFLOWS / workflow).read_text()
    found = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", text)
    assert found, f"{workflow} has no job {name}"
    return found[1]


def steps(block):
    return dict(re.findall(r"(?ms)^      - name: ([^\n]+)\n(.*?)(?=^      - name: |\Z)", block))


class BrokerJob(Case):
    def test_broker_job_runs_pinned_staticcheck_unswallowed(self):
        found = steps(job("test.yml", "broker"))
        names = list(found)
        for name in (
            "Gofmt",
            "Go vet",
            "Go test",
            "Go build",
            "Build darwin/arm64 binary with cgo",
        ):
            self.assertIn(name, names)
        self.assertEqual(names.index("Staticcheck"), names.index("Go vet") + 1)
        self.assertEqual(found["Staticcheck"].strip(), f"run: {STATICCHECK}")
        self.assertNotIn("continue-on-error", job("test.yml", "broker"))
