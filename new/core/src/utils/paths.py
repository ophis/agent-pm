"""Shared path normalization, containment and writable temporary roots; printable_abs, a printable absolute path."""
import os
import subprocess
import tempfile


def real_dirs(paths, label: str) -> frozenset[str]:
    """Real paths of a list of absolute or expandable ~ paths; ValueError names `label` on invalid input."""
    if not isinstance(paths, list):
        raise ValueError(f"{label}: want a list of local paths")
    for p in paths:
        if not isinstance(p, str) or not p.startswith(("/", "~")) or os.path.expanduser(p).startswith("~"):
            raise ValueError(f"{label}: {p!r:.80} is not an absolute or expandable ~ path")
    return frozenset(os.path.realpath(os.path.expanduser(p)) for p in paths)


def temp_dirs() -> tuple[str, ...]:
    """Realpaths of the dirs any process, an agent run included, may write: TMPDIR, Python's temp dir, /tmp, /var/tmp
    and macOS's per-user one."""
    dirs = [os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/var/tmp"]
    try:
        dirs.append(subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=60,
                                   stdin=subprocess.DEVNULL).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return tuple(dict.fromkeys(os.path.realpath(d) for d in dirs if d))


def under(path: str, roots) -> str | None:
    """The first real root containing `path`, or None; `path` must already be real."""
    return next((r for r in map(os.path.realpath, roots) if os.path.commonpath([path, r]) == r), None)


def printable_abs(value) -> bool:
    """Whether `value` is a str that is printable and an absolute path."""
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)
