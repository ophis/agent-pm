import os
import stat
import sys
from pathlib import Path

ROOT = Path("/opt/agent-pm/runtime")
LIMIT = 512 << 20


def update(action, stream, root=ROOT):
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit("unsafe Codex runtime directory")
    stage, active = root / "codex.next", root / "codex"
    if action == "reset":
        stage.unlink(missing_ok=True)
        active.unlink(missing_ok=True)
    elif action == "publish":
        info = stage.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit("unsafe staged Codex")
        os.replace(stage, active)
    elif action == "stage":
        with stage.open("xb") as target:
            size = 0
            while chunk := stream.read(1 << 20):
                size += len(chunk)
                if size > LIMIT:
                    raise SystemExit("Codex executable exceeds limit")
                target.write(chunk)
            if size == 0:
                raise SystemExit("Codex executable missing")
            target.flush()
            os.fchmod(target.fileno(), 0o755)
    else:
        raise SystemExit("invalid Codex update action")


if __name__ == "__main__":
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit("Codex update requires root and one action")
    update(sys.argv[1], sys.stdin.buffer)
