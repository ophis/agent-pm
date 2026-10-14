"""Codex resume run as `<python> -I resume.py <roots-json> <store> <workdir> <sid> <native>`: execs
the stored resume with the environment its recovery state keeps, which public text never holds.
"""

from __future__ import annotations

import json
import os
import sys


def _root(path) -> bool:
    return (
        isinstance(path, str) and path.isprintable() and os.path.isabs(path) and os.path.isdir(path)
    )


def _bootstrap(argv: list[str]) -> list[str]:
    # This runs before package imports, when only the standard library is available.
    if len(argv) != 5:
        raise ValueError("expected <roots-json> <store> <workdir> <sid> <native>")
    try:
        roots = json.loads(argv[0])
    except (ValueError, RecursionError):
        roots = None
    if (
        not isinstance(roots, list)
        or not roots
        or not all(map(_root, roots))
        or len(set(roots)) != len(roots)
    ):
        raise ValueError("invalid import roots: expected distinct absolute directories")
    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]
    return argv[1:]


def main(argv: list[str]) -> int:
    # Imported after _bootstrap: -I leaves only the standard library on sys.path.
    from agent_pm.session.adapters.codex import CodexAdapter
    from agent_pm.session.adapters.codex.common import RecoveryError, SessionStore
    from agent_pm.session.engine.types import ConfigError

    store, workdir, sid, native = argv
    try:
        state = SessionStore(store).load(workdir, sid)
        if state is None:
            raise ConfigError(f"missing Codex recovery state for run ID {sid!r} in {store}")
        if state.native_id != native:
            raise ConfigError(
                f"recovery state for run ID {sid!r} is bound to native ID "
                f"{state.native_id!r}, not {native!r}"
            )
        launch = CodexAdapter({}).common.resume_launch(state)
        os.chdir(launch.cwd)
        os.execvpe(launch.argv[0], launch.argv, {**os.environ, **launch.env})
    except (ConfigError, RecoveryError, OSError) as exc:
        print(f"codex resume: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        locators = _bootstrap(sys.argv[1:])
    except ValueError as exc:
        print(f"codex resume: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(locators))
