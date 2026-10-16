"""Balance integration tests across numbered shards using recorded durations."""

import json
import math
import os
import platform
import re
import statistics
import sys
import time
import unittest
from collections import Counter
from pathlib import Path

import run_tests

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/test.yml"
DURATIONS = ROOT / ".github/integration_durations.json"


def directories(root):
    return sorted(
        str(path.relative_to(root))
        for pattern in ("*/tests/integration", "*/tests/*/integration")
        for path in root.glob(pattern)
        if (path / "__init__.py").is_file()
    )


def tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests(test)
        else:
            yield test


def workflow_jobs(text):
    job = re.search(r"(?ms)^  integration:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", text)
    lines = re.findall(r"^        job: (\[.*\])$", job[1], re.M) if job else []
    try:
        labels = json.loads(lines[0]) if len(lines) == 1 else None
    except json.JSONDecodeError:
        labels = None
    if (
        not isinstance(labels, list)
        or not labels
        or not all(isinstance(label, str) for label in labels)
        or len(set(labels)) != len(labels)
        or labels != [str(number) for number in range(1, len(labels) + 1)]
    ):
        raise ValueError("workflow: matrix.job must be one inline JSON array numbered 1 through N")
    return labels


def weights(found, durations):
    if any(
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
        for value in durations.values()
    ):
        raise ValueError("integration durations must be finite positive seconds")
    known = [durations[test.id()] for test in found if test.id() in durations]
    fallback = statistics.median(known) if known else 1.0
    return {test.id(): durations.get(test.id(), fallback) for test in found}


def assign(found, labels, durations):
    estimates = weights(found, durations)
    split, totals = {job: [] for job in labels}, dict.fromkeys(labels, 0.0)
    for test in sorted(found, key=lambda test: (-estimates[test.id()], test.id())):
        target = min(labels, key=lambda job: (totals[job], job))
        split[target].append(test)
        totals[target] += estimates[test.id()]
    return split


def validate(found, suites, labels):
    counts = Counter(test.id() for suite in suites.values() for test in suite)
    expected = Counter(test.id() for test in found)
    if (
        set(labels) != set(suites)
        or any(not suite for suite in suites.values())
        or counts != expected
        or any(count != 1 for count in counts.values())
    ):
        raise ValueError("integration shards must be nonempty and contain every test exactly once")


def check(root, labels, durations, workflow):
    matrix = workflow_jobs(workflow)
    if set(matrix) != set(labels):
        raise ValueError("workflow matrix.job differs from the duration shards")
    found = list(tests(run_tests.discover(root, directories(root))))
    split = assign(found, labels, durations)
    validate(found, split, matrix)
    return split


def main(argv):
    workflow = WORKFLOW.read_text()
    labels = workflow_jobs(workflow)
    if argv != ["--check"] and (len(argv) != 1 or argv[0] not in labels):
        raise ValueError("give --check or a shard number: " + ", ".join(labels))
    title = f"Integration {argv[0]}"
    started = time.monotonic()
    code = None if argv == ["--check"] else run_tests.supervise(title)
    if code is not None:
        directory = os.environ.get("INTEGRATION_TIMING_DIR")
        if directory and (path := Path(directory) / "manifest.json").is_file():
            manifest = json.loads(path.read_text())
            manifest.update(exit_code=code, elapsed_seconds=time.monotonic() - started)
            path.write_text(json.dumps(manifest, indent=2) + "\n")
        return code
    if argv != ["--check"]:
        run_tests.GATE = run_tests.isolation.install(ROOT)
        run_tests.GATE.unit = False
    durations = json.loads(DURATIONS.read_text())
    split = check(ROOT, labels, durations, workflow)
    if argv == ["--check"]:
        print(
            "duration shards match discovery and workflow: "
            + ", ".join(f"{job}: {len(suite)} tests" for job, suite in split.items())
        )
        return 0
    if directory := os.environ.get("INTEGRATION_TIMING_DIR"):
        found = [test for suite in split.values() for test in suite]
        estimates = weights(found, durations)
        manifest = {
            "run_id": os.environ["GITHUB_RUN_ID"],
            "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "sha": os.environ["GITHUB_SHA"],
            "head_sha": os.environ["INTEGRATION_HEAD_SHA"],
            "runner": os.environ["INTEGRATION_RUNNER"],
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "tmux": os.environ["INTEGRATION_TMUX_VERSION"],
            "shards": labels,
            "shard": argv[0],
            "discovered": sorted(test.id() for test in found),
            "tests": [test.id() for test in split[argv[0]]],
            "expected_seconds": sum(estimates[test.id()] for test in split[argv[0]]),
            "exit_code": None,
        }
        path = Path(directory) / "manifest.json"
        with run_tests.GATE.reporting(path):
            path.write_text(json.dumps(manifest, indent=2) + "\n")
    return run_tests.run(unittest.TestSuite(split[argv[0]]), title)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ValueError as error:
        sys.exit(str(error))
