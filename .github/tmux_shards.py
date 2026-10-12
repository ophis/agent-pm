import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "tests/integration/tmux"
WORKFLOW = Path(__file__).resolve().parent / "workflows/test.yml"
SHARDS = {
    "A": ["tests.integration.tmux.crew_integration_test.Grid", "tests.integration.tmux.crew_integration_test.Lifecycle",
          "tests.integration.tmux.crew_integration_test.Splits"],
    "B": ["tests.integration.tmux.crew_integration_test.Roster"],
    "C": ["tests.integration.tmux.crew_integration_test.Retile",
          "tests.integration.tmux.crew_integration_test.Managers", "tests.integration.tmux.crew_integration_test.Settle"],
    "D": ["tests.integration.tmux.codex_worker_integration_test.CodexLifecycle",
          "tests.integration.tmux.codex_worker_integration_test.CodexSession"],
}


def discover(directory):
    top = Path(directory)
    while (top / "__init__.py").is_file():
        top = top.parent
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory), pattern="*_integration_test.py", top_level_dir=str(top))
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
    sys.path.insert(0, str(ROOT))
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
