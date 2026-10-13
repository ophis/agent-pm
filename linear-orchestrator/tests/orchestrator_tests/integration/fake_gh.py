import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import unquote

ENV = "FAKE_GH_SCENARIO"
RAW = "Accept: application/vnd.github.raw+json"
API = re.compile(r"repos/([^/]+)/([^/]+)(?:/contents/([^?]+)\?ref=([^?]+))?")
TAIL = "/usr/bin:/bin"
IDENTITY = {
    "GIT_AUTHOR_NAME": "fake gh",
    "GIT_AUTHOR_EMAIL": "fake-gh@agents.test",
    "GIT_COMMITTER_NAME": "fake gh",
    "GIT_COMMITTER_EMAIL": "fake-gh@agents.test",
}


def install(bin_dir: str) -> str:
    path = os.path.join(bin_dir, "gh")
    with open(path, "w") as f:
        f.write(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f'{shlex.quote(os.path.abspath(__file__))} "$@"\n'
        )
    os.chmod(path, 0o755)
    return path


def bare(root: str, owner: str, name: str, branches=()) -> str:
    path = os.path.join(root, owner, f"{name}.git")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tempfile.TemporaryDirectory() as home:
        env = {"HOME": home, "PATH": TAIL, "GIT_CONFIG_NOSYSTEM": "1", **IDENTITY}

        def git(*args: str) -> str:
            res = subprocess.run(["git", *args], env=env, input="", capture_output=True, text=True)
            if res.returncode != 0:
                raise GitCommandError(args, res.stderr.strip())
            return res.stdout.strip()

        git("init", "--bare", "--quiet", "--initial-branch=main", path)
        tree = git("--git-dir", path, "mktree")
        for branch in branches:
            commit = git("--git-dir", path, "commit-tree", tree, "-m", branch)
            git("--git-dir", path, "update-ref", f"refs/heads/{branch}", commit)
    return path


def gitconfig(home: str, root: str) -> None:
    # `home` is a test's temp HOME, never the real one.
    with open(os.path.join(home, ".gitconfig"), "w") as f:
        f.write(f'[url "file://{root}/"]\n\tinsteadOf = https://github.com/\n')


def nested(value, depth: int) -> bool:
    return (
        isinstance(value, str)
        if depth == 0
        else isinstance(value, dict) and all(nested(v, depth - 1) for v in value.values())
    )


def scenario() -> dict:
    path = os.environ.get(ENV)
    if not path:
        sys.exit(f"fake_gh.py: {ENV} is unset")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"fake_gh.py: {ENV}: {e}")
    if not isinstance(data, dict) or not {"repos"} <= set(data) <= {"repos", "contents"}:
        sys.exit(
            'fake_gh.py: the scenario is no JSON object with the key "repos" and '
            'optionally "contents"'
        )
    if not isinstance(data["repos"], dict) or not all(
        isinstance(v, dict) for v in data["repos"].values()
    ):
        sys.exit('fake_gh.py: "repos" is no object of objects')
    if not nested(data.setdefault("contents", {}), 3):
        sys.exit('fake_gh.py: "contents" is no object of objects of objects of strings')
    return data


def entries(files: dict, path: str) -> list[dict] | None:
    below = [f[len(path) + 1 :] for f in files if f.startswith(path + "/")]
    if not below:
        return None
    kinds = {rest.partition("/")[0]: "dir" if "/" in rest else "file" for rest in below}
    return [{"name": n, "path": f"{path}/{n}", "type": kinds[n]} for n in sorted(kinds)]


def main(argv: list[str]) -> None:
    raw = argv[1:3] == ["-H", RAW]
    rest = argv[3:] if raw else argv[1:]
    m = API.fullmatch(rest[0]) if argv[:1] == ["api"] and len(rest) == 1 else None
    if not m or (raw and not m[3]):
        sys.exit(
            f"fake_gh.py: usage: gh api [-H '{RAW}"
            f"'] repos/<owner>/<name>[/contents/<path>?ref=<branch>]"
        )
    data, repo = scenario(), f"{m[1]}/{m[2]}"
    if not m[3]:
        if repo not in data["repos"]:
            sys.exit("gh: Not Found (HTTP 404)")
        print(json.dumps(data["repos"][repo]))
        return
    path = unquote(m[3])
    files = data["contents"].get(repo, {}).get(unquote(m[4]), {})
    if (listing := entries(files, path)) is not None:
        print(json.dumps(listing))
    elif path not in files:
        sys.exit("gh: Not Found (HTTP 404)")
    elif raw:
        sys.stdout.buffer.write(files[path].encode())
    else:
        print(json.dumps({"name": path.rsplit("/", 1)[-1], "path": path, "type": "file"}))


class GitCommandError(RuntimeError):
    def __init__(self, argv, stderr):
        super().__init__(f"git {' '.join(argv)}: {stderr}")


if __name__ == "__main__":
    main(sys.argv[1:])
