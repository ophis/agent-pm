"""Standard-library test isolation, child environments and owned process cleanup."""

import os
import shlex
import subprocess
import sys
from pathlib import Path


def clean_env(base=None, *import_roots):
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith(("GIT_", "PYTHON"))
        and key not in {"TMUX", "TMUX_PANE", "PYTHONUSERBASE"}
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if import_roots:
        env["PYTHONPATH"] = os.pathsep.join(map(str, import_roots))
    return env


def git_run(*argv, cwd=None, env=None, **kwargs):
    return subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            *argv,
        ],
        cwd=cwd,
        env=clean_env(env),
        **{"capture_output": True, "text": True, **kwargs},
    )


def git(*argv, cwd=None, env=None):
    return git_run(*argv, cwd=cwd, env=env, check=True).stdout.strip()


def reap(proc, timeout=10):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=timeout)


def python_path(bin_dir, tail=None):
    directory = Path(bin_dir)
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "python3"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -S "$@"\n')
    shim.chmod(0o700)
    return os.pathsep.join((str(directory), os.defpath if tail is None else tail))
