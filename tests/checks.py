"""Run repository design checks with the same discovery as their unit tests."""

import inspect
import os
import sys
from pathlib import Path

from tests import guards

ROOT = Path(__file__).resolve().parents[1]


def checks(root):
    for name, check in inspect.getmembers(guards, inspect.isfunction):
        if name.startswith("_") or check.__module__ != guards.__name__:
            continue
        roots = (
            [os.path.join(root, project, "src") for project in guards.PROJECTS]
            if name == "private_imports"
            else [root]
        )
        yield name, [violation for at in roots for violation in check(str(at))]


def main():
    failed = False
    for name, found in checks(ROOT):
        print(f"{name}: {len(found)} violations")
        if found:
            failed = True
            print("\n".join(found))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
