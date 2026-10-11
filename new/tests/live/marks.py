#!/usr/bin/env python3
"""FR-29 live mark comparison, run by hand, never in CI. Per runtime, --runs headless runs each of the new Delegate's
CLI (new) and main's drive.py (old): researcher light-research on QUESTION, at most --jobs at a time, each in its own
session, interrupted after --timeout s. Every run's HOME is a private one in a fresh 0700 root under the repo's work/
(its local configs set only the researcher's tier, effort and local output; symlinks to the real runtime state, macOS's
login keychain among it), its cwd its own workdir (runs/<n>/, its stderr in runs/<n>.err); the root is removed at the
end. A runtime whose AUTH command fails under the private HOME is not run, its error reported.

Prints one JSON object: per run its marks (progress records in run.jsonl), names, lost (its core's task marks absent),
status (the result record's outcome) or error, seconds; per runtime each core's totals and the check new lost <= old
lost; never reply text. Exits 2, nothing run, when the root lies under a temp dir or a workdir and the private HOME
overlap."""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

WT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
sys.path.insert(0, os.path.join(WT, "new", "core", "src"))

from utils.paths import temp_dirs, under  # noqa: E402

RUNTIMES = ("claude", "codex")
CLI = {"new": (os.path.join(WT, "new", "core", "src", "delegate", "cli.py"), "--runtime"),
       "old": (os.path.join(WT, "core", "src", "drive.py"), "--client")}
TASK = {"new": os.path.join(WT, "new", "core", "team", "tasks", "light-research.md"),
        "old": os.path.join(WT, "core", "team", "tasks", "light-research.md")}
QUESTION = "What is the latest stable Python release and its release date? Cite python.org."
LOCAL = '[roles.researcher]\ntier = 4\neffort = "low"\n\n[roles.researcher.output]\ntype = "local"\n'
LOCALS = ("core.local.toml", "core-new.local.toml")
# macOS's security tool finds the login keychain, which holds claude's login, under $HOME/Library/Keychains.
STATE = (".claude", ".claude.json", ".codex", os.path.join("Library", "Keychains"))
AUTH = {"claude": ["claude", "auth", "status"], "codex": ["codex", "login", "status"]}
MARK = re.compile(r"\[agent-pm-progress:([\w-]+)\]")
WAIT = 60   # seconds: an AUTH command's limit; an interrupted run's before its kill


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="marks.py", description="FR-29 live mark comparison (module docstring)")
    ap.add_argument("--runtimes", nargs="+", choices=RUNTIMES, default=list(RUNTIMES))
    ap.add_argument("--runs", type=_positive, default=3, help="runs per runtime and core")
    ap.add_argument("--jobs", type=_positive, default=2, help="runs at a time")
    ap.add_argument("--timeout", type=_positive, default=1800, help="seconds per run")
    a = ap.parse_args(argv)
    real = os.path.expanduser("~")
    expected = {impl: sorted(set(MARK.findall(_read(path)))) for impl, path in TASK.items()}
    os.makedirs(os.path.join(WT, "work"), exist_ok=True)
    root = tempfile.mkdtemp(prefix="fr29-", dir=os.path.join(WT, "work"))
    try:
        home = os.path.join(root, "home")
        runtimes = list(dict.fromkeys(a.runtimes))
        jobs = [(rt, impl) for rt in runtimes for _ in range(a.runs) for impl in CLI]
        workdirs = [os.path.join(root, "runs", str(n)) for n in range(len(jobs))]
        for d in (home, *workdirs):
            os.makedirs(d)
        if problem := check(root, home, workdirs):
            print(f"marks.py: {problem}", file=sys.stderr)
            return 2
        os.mkdir(os.path.join(home, ".agent-pm"))
        for name in LOCALS:
            with open(os.path.join(home, ".agent-pm", name), "x", encoding="utf-8") as f:
                f.write(LOCAL)
        for name in STATE:
            os.makedirs(os.path.dirname(os.path.join(home, name)), exist_ok=True)
            os.symlink(os.path.join(real, name), os.path.join(home, name))
        env = {**os.environ, "HOME": home, "CODEX_HOME": os.environ.get("CODEX_HOME") or os.path.join(real, ".codex")}
        refused = {rt: why for rt in runtimes if (why := auth(rt, home, env))}
        todo = [(rt, impl, wd) for (rt, impl), wd in zip(jobs, workdirs) if rt not in refused]
        with ThreadPoolExecutor(a.jobs) as pool:
            runs = list(pool.map(lambda job: one(*job, env, a.timeout, expected[job[1]]), todo))
        report = {rt: {"error": refused[rt]} if rt in refused else totals(runs, rt) for rt in runtimes}
        print(json.dumps({"question": QUESTION, "expected": expected, "runs": runs, "runtimes": report}, indent=1))
        return 0
    finally:
        shutil.rmtree(root)


def check(root: str, home: str, workdirs: list[str]) -> str | None:
    """Why the private root is unsafe to run in, or None."""
    if temp := under(os.path.realpath(root), temp_dirs()):
        return f"the root {root} lies under the temp dir {temp}"
    home = os.path.realpath(home)
    for wd in map(os.path.realpath, workdirs):
        if under(home, [wd]) or under(wd, [home]):
            return f"the workdir {wd} and the private HOME {home} overlap"
    return None


def one(runtime: str, impl: str, workdir: str, env: dict, timeout: float, expected: list[str]) -> dict:
    """One run of `impl` on `runtime` in `workdir`, counted from its run.jsonl."""
    script, flag = CLI[impl]
    argv = [sys.executable, script, flag, runtime, "--role", "researcher", "--task", "light-research",
            "--input", QUESTION, "--out", os.path.join(workdir, "out.md"), "--workdir", workdir]
    start, late = time.monotonic(), False
    with open(workdir + ".err", "w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, start_new_session=True)
        try:
            rc = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            late = True
            os.killpg(proc.pid, signal.SIGINT)   # both CLIs stop their client and write the result record on it
            try:
                rc = proc.wait(WAIT)
            except subprocess.TimeoutExpired:
                kill_session(proc.pid)
                rc = proc.wait()
        err.seek(0)
        last = next((line.strip() for line in reversed(err.read().splitlines()) if line.strip()), "")
    records = _records(os.path.join(workdir, "run.jsonl"))
    progress = [r for r in records if r.get("kind") == "progress"]
    names = sorted({r["name"] for r in progress if isinstance(r.get("name"), str)})
    result = next((r for r in reversed(records) if r.get("kind") == "result"), None)
    if late:
        ended = {"error": f"timed out after {timeout:g} s"}
    elif result is None:
        ended = {"error": f"no result record; exit {rc}: {last[:300]}"}
    elif isinstance(result.get("outcome"), dict):
        ended = {"status": result["outcome"].get("status")}
    else:
        ended = {"error": result.get("error")}
    return {"impl": impl, "runtime": runtime, "marks": len(progress), "names": names,
            "lost": [n for n in expected if n not in names], **ended, "seconds": round(time.monotonic() - start)}


def totals(runs: list[dict], runtime: str) -> dict:
    """Each core's runs, marks and lost names on `runtime`, and the check new lost <= old lost."""
    mine = [r for r in runs if r["runtime"] == runtime]
    t = {impl: {"runs": sum(r["impl"] == impl for r in mine),
                "marks": sum(r["marks"] for r in mine if r["impl"] == impl),
                "lost": sum(len(r["lost"]) for r in mine if r["impl"] == impl)} for impl in CLI}
    return {**t, "check": t["new"]["lost"] <= t["old"]["lost"]}


def kill_session(sid: int) -> None:
    """SIGKILL to the group of session `sid`'s leader, not yet reaped, then to each other group in the session: the new
    core's agent leads its own."""
    os.killpg(sid, signal.SIGKILL)
    ps = subprocess.run(["ps", "-A", "-o", "pid=,pgid="], capture_output=True, text=True).stdout
    for pid, pgid in (map(int, line.split()) for line in ps.splitlines()):
        try:
            if os.getsid(pid) == sid:
                os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


def auth(runtime: str, home: str, env: dict) -> str | None:
    """Why `runtime` cannot authenticate or start under the private HOME (its AUTH command failing), or None."""
    try:
        rc = subprocess.run(AUTH[runtime], cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=WAIT).returncode
    except (OSError, subprocess.SubprocessError) as e:
        return f"cannot start: {' '.join(AUTH[runtime])}: {e}"
    return f"cannot authenticate: {' '.join(AUTH[runtime])} exited {rc}" if rc else None


def _records(path: str) -> list[dict]:
    try:
        lines = _read(path).splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _positive(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"want a positive integer, not {text!r}")
    return int(text)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
