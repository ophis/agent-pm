"""Hold a fake client step until the test releases its gate."""

import sys
import time
from pathlib import Path


def main():
    deadline = time.monotonic() + 60
    while not Path(sys.argv[1]).exists():
        if time.monotonic() >= deadline:
            return 1
        time.sleep(0.02)
    return 0


if __name__ == "__main__":
    sys.exit(main())
