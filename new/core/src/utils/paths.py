"""Path checks: printable_abs (a printable absolute path), under (the root that holds a path), temp_dirs (the dirs any
process may write)."""
import os
import subprocess
import tempfile

GETCONF_TIMEOUT = 60   # seconds


def printable_abs(value) -> bool:
    """Whether `value` is a str that is printable and an absolute path."""
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


def under(path: str, roots) -> str | None:
    """The first of `roots` that holds realpath `path`, or None."""
    return next((r for r in map(os.path.realpath, roots) if os.path.commonpath([path, r]) == r), None)


def temp_dirs() -> tuple[str, ...]:
    """Realpaths of the dirs any process, an agent run included, may write: TMPDIR, Python's temp dir, /tmp, /var/tmp
    and macOS's per-user one."""
    dirs = [os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/var/tmp"]
    try:
        dirs.append(subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                                   timeout=GETCONF_TIMEOUT, stdin=subprocess.DEVNULL).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return tuple(dict.fromkeys(os.path.realpath(d) for d in dirs if d))
