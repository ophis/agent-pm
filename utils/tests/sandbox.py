"""Standard-library test isolation, child environments and owned process cleanup."""

import atexit
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_TMUX_DIRECTORIES: dict[int, str] = {}
_TMUX_LOCK = threading.Lock()


def _after_fork():
    global _TMUX_LOCK
    _TMUX_LOCK = threading.Lock()
    _TMUX_DIRECTORIES.clear()


os.register_at_fork(after_in_child=_after_fork)


def _remove_tmux_directory(pid, directory):
    if os.getpid() == pid and os.path.isdir(directory) and not os.path.islink(directory):
        try:
            stop_tmux_servers(directory, {"PATH": os.environ.get("PATH", os.defpath)})
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def _tmux_directory():
    pid = os.getpid()
    with _TMUX_LOCK:
        if pid not in _TMUX_DIRECTORIES or not os.path.isdir(_TMUX_DIRECTORIES[pid]):
            sandbox = os.environ.get("AGENT_PM_TEST_SANDBOX") or None
            directory = tempfile.mkdtemp(prefix="tmux-", dir=sandbox)
            if sandbox is None:
                atexit.register(_remove_tmux_directory, pid, directory)
            _TMUX_DIRECTORIES[pid] = directory
        return _TMUX_DIRECTORIES[pid]


def clean_env(base=None, *import_roots):
    source = os.environ if base is None else base
    env = {
        key: value
        for key, value in source.items()
        if not key.startswith(("GIT_", "PYTHON"))
        and key not in {"TMUX", "TMUX_PANE", "TMUX_TMPDIR"}
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env["TMUX_TMPDIR"] = _tmux_directory()
    env["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(
        dict.fromkeys(
            os.path.dirname(os.path.abspath(source[key]))
            for key in ("HOME", "AGENT_PM_TEST_WORKSPACE")
            if source.get(key)
        )
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if os.environ.get("AGENT_PM_CONTROL_ROOT") and "AGENT_PM_CONTROL_ROOT" not in env:
        env["AGENT_PM_CONTROL_ROOT"] = os.environ["AGENT_PM_CONTROL_ROOT"]
    if import_roots:
        env["PYTHONPATH"] = os.pathsep.join(map(str, import_roots))
    return env


def stop_tmux_servers(root, env):
    if Path(root).is_symlink():
        return
    failures = []
    for path, directories, files in os.walk(root):
        directory = Path(path)
        directories[:] = [name for name in directories if not (directory / name).is_symlink()]
        if directory.name != f"tmux-{os.getuid()}":
            continue
        for name in sorted(files):
            socket = directory / name
            if socket.is_symlink() or not socket.is_socket():
                continue
            try:
                subprocess.run(
                    ["tmux", "-S", str(socket), "kill-server"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(error)
    if failures:
        raise ExceptionGroup(f"{root}: tmux kill-server failed", failures)


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


def owned_directory(case, root, *, prefix="fixture-"):
    root = os.path.abspath(root)
    if os.path.realpath(root) != root:
        case.fail(f"{root}: test fixture root contains a symlink")
    os.makedirs(root, exist_ok=True)
    path = tempfile.mkdtemp(dir=root, prefix=prefix)
    case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


def python_path(bin_dir, tail=None):
    directory = Path(bin_dir)
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "python3"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -S "$@"\n')
    shim.chmod(0o700)
    return os.pathsep.join((str(directory), os.defpath if tail is None else tail))
