"""Single source for tmux CI shard membership; discovery guards missing, stale and duplicate classes."""
from collections import Counter
import json
import re
from pathlib import Path
import sys
import unittest

DIRECTORY = Path(__file__).resolve().parents[1] / "core/skills/tmux/scripts/integration"
WORKFLOW = Path(__file__).resolve().parent / "workflows/test.yml"
SHARDS = {
    "A": ["crew_integration_test.Grid", "crew_integration_test.Lifecycle", "crew_integration_test.Splits"],
    "B": ["crew_integration_test.Roster"],
    "C": ["crew_integration_test.Retile", "crew_integration_test.Managers", "crew_integration_test.Settle"],
}


def discover(directory):
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory), pattern="*_integration_test.py")
    if loader.errors:
        raise ValueError("\n".join(loader.errors))
    def classes(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                yield from classes(test)
            else:
                yield f"{type(test).__module__}.{type(test).__qualname__}"
    return set(classes(suite))


def workflow_shards(path):
    job = re.search(r"(?ms)^  integration-tmux:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", path.read_text())
    lines = re.findall(r"^        shard: (\[.*\])$", job[1], re.M) if job else []
    if len(lines) != 1:
        raise ValueError("integration-tmux matrix.shard must be one inline JSON array")
    labels = json.loads(lines[0])
    if not labels or any(not isinstance(label, str) for label in labels) or len(set(labels)) != len(labels):
        raise ValueError("workflow shard labels must be unique strings")
    return labels


def validate(discovered, groups, labels):
    if set(groups) != set(labels):
        raise ValueError(f"workflow shards {sorted(labels)} differ from helper shards {sorted(groups)}")
    counts = Counter(name for names in groups.values() for name in names)
    missing = discovered - counts.keys()
    stale = counts.keys() - discovered
    duplicate = sorted(name for name, count in counts.items() if count != 1)
    if missing or stale or duplicate:
        raise ValueError(f"shard inventory: missing={sorted(missing)}, stale={sorted(stale)}, duplicate={duplicate}")


def main():
    validate(discover(DIRECTORY), SHARDS, workflow_shards(WORKFLOW))
    if sys.argv[1:] == ["--check"]:
        print("Tmux shard inventory matches discovery and workflow")
        return 0
    if len(sys.argv) != 2 or sys.argv[1] not in SHARDS:
        raise ValueError("give --check or a shard name: " + ", ".join(SHARDS))
    suite = unittest.defaultTestLoader.loadTestsFromNames(SHARDS[sys.argv[1]])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        sys.exit(str(error))
