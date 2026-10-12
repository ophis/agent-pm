"""CI integration jobs: JOBS entries are directories ending in /, files, or <file>::<Class>.
Exact classes outrank the longest matching path. --check rejects loader errors, unmatched classes/tests,
empty jobs, duplicate tests, and a workflow matrix.job unlike JOBS; <job> runs that job's tests."""

import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = "tests/integration"
WORKFLOW = ROOT / ".github/workflows/test.yml"
JOBS = {
    "driver": ["driver/"],
    "adapters": ["adapters/"],
    "delegate-composer": ["delegate/", "composer/"],
    "orchestrator": ["orchestrator/"],
    "crew-1": [
        "tmux/crew_integration_test.py::ClaudeLifecycleTest",
        "tmux/crew_integration_test.py::RuntimeTest",
    ],
    "crew-2": [
        "tmux/crew_integration_test.py::CodexLifecycleTest",
        "tmux/crew_integration_test.py::RosterTest",
    ],
    "crew-3": [
        "tmux/crew_integration_test.py::ManagersTest",
        "tmux/crew_integration_test.py::RosterRecoveryTest",
    ],
    "shared": ["imports_integration_test.py", "live_tmux_integration_test.py"],
}


def tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests(test)
        else:
            yield test


def owners(file, jobs, class_name):
    exact = [job for job, entries in jobs.items() if f"{file}::{class_name}" in entries]
    if exact:
        return exact
    longest = {
        job: max(
            (len(e) for e in entries if file == e or e.endswith("/") and file.startswith(e)),
            default=0,
        )
        for job, entries in jobs.items()
    }
    best = max(longest.values(), default=0)
    return [job for job, length in longest.items() if length and length == best]


def workflow_jobs(text):
    job = re.search(r"(?ms)^  integration:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    lines = re.findall(r"^        job: (\[.*\])$", job[1], re.M) if job else []
    try:
        labels = json.loads(lines[0]) if len(lines) == 1 else None
    except json.JSONDecodeError:
        labels = None
    if (
        not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
        or len(set(labels)) != len(labels)
    ):
        raise ValueError(
            "workflow: the integration job's matrix.job must be one inline JSON array of unique strings"
        )
    return labels


def validate(found, suites, labels):
    if set(labels) != set(suites):
        raise ValueError(f"workflow matrix.job {sorted(labels)} differs from JOBS {sorted(suites)}")
    counts = Counter(test for ids in suites.values() for test in ids)
    empty = [job for job, ids in suites.items() if not ids]
    missing, stale = sorted(found - counts.keys()), sorted(counts.keys() - found)
    duplicate = sorted(test for test, count in counts.items() if count > 1)
    if empty or missing or stale or duplicate:
        raise ValueError(
            f"integration jobs: empty={empty}, missing={missing}, stale={stale}, duplicate={duplicate}"
        )


def check(root, directory, jobs, workflow):
    loader = unittest.TestLoader()
    start = str(root / directory)
    found = list(tests(loader.discover(start, pattern="*_test.py", top_level_dir=str(root))))
    if loader.errors:
        raise ValueError("\n".join(loader.errors))
    split, unassigned = {job: [] for job in jobs}, set()
    prefix = directory.replace("/", ".") + "."
    locations = [
        (test, type(test).__module__.removeprefix(prefix).replace(".", "/") + ".py")
        for test in found
    ]
    classes = {f"{file}::{type(test).__name__}" for test, file in locations}
    missing_classes = sorted(
        {entry for entries in jobs.values() for entry in entries if "::" in entry} - classes
    )
    if missing_classes:
        raise ValueError("no integration tests for: " + ", ".join(missing_classes))
    for test, file in locations:
        matched = owners(file, jobs, type(test).__name__)
        if not matched:
            unassigned.add(file)
        for job in matched:
            split[job].append(test)
    if unassigned:
        raise ValueError("no integration job for: " + ", ".join(sorted(unassigned)))
    ids = {job: [test.id() for test in suite] for job, suite in split.items()}
    validate({test.id() for test in found}, ids, workflow_jobs(workflow))
    return split


def run(suite):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(suite))
    return 0 if result.wasSuccessful() else 1


def main(argv):
    if argv != ["--check"] and (len(argv) != 1 or argv[0] not in JOBS):
        raise ValueError("give --check or a job name: " + ", ".join(JOBS))
    split = check(ROOT, DIRECTORY, JOBS, WORKFLOW.read_text())
    if argv == ["--check"]:
        counts = ", ".join(f"{job} {len(suite)}" for job, suite in split.items())
        print(f"integration jobs match discovery and the workflow: {counts}")
        return 0
    return run(split[argv[0]])


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ValueError as error:
        sys.exit(str(error))
