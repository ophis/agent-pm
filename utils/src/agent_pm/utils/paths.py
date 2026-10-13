"""The repo ROOT, the state root STATE, path normalization, containment, writable temp roots and the
projects' import roots.
"""

import contextlib
import os
import subprocess
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))
STATE = "~/.agent-pm"


class PathError(ValueError):
    def __init__(self, label, path):
        super().__init__(f"{label}: {path!r:.80} is not an absolute or expandable ~ path")


def real_dirs(paths, label: str) -> frozenset[str]:
    if not isinstance(paths, list):
        raise ValueError(f"{label}: want a list of local paths")
    for p in paths:
        if (
            not isinstance(p, str)
            or not p.startswith(("/", "~"))
            or os.path.expanduser(p).startswith("~")
        ):
            raise PathError(label, p)
    return frozenset(os.path.realpath(os.path.expanduser(p)) for p in paths)


def temp_dirs() -> tuple[str, ...]:
    dirs = [os.environ.get("TMPDIR", ""), tempfile.gettempdir(), "/tmp", "/var/tmp"]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        dirs.append(
            subprocess.run(
                ["getconf", "DARWIN_USER_TEMP_DIR"],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
            ).stdout.strip()
        )
    return tuple(dict.fromkeys(os.path.realpath(d) for d in dirs if d))


def under(path: str, roots) -> str | None:
    return next(
        (r for r in map(os.path.realpath, roots) if os.path.commonpath([path, r]) == r), None
    )


def printable_abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


def import_roots(repo: str = ROOT) -> tuple[str, ...]:
    """Each <repo>/<dir>/src whose agent_pm/ (no __init__.py) holds just package <dir> (- as _)."""
    found = []
    for name in sorted(os.listdir(repo)):
        top = os.path.join(repo, name, "src", "agent_pm")
        subs = [s for s in _entries(top) if os.path.isfile(os.path.join(top, s, "__init__.py"))]
        if subs == [name.replace("-", "_")] and not os.path.isfile(
            os.path.join(top, "__init__.py")
        ):
            found.append(os.path.dirname(top))
    return tuple(found)


def _entries(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []
