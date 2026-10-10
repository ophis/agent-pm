#!/usr/bin/env bash
# Usage: ./gen-preview.sh   (replaces the files in <repo>/previews)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
previews="$here/previews"
[ -L "$previews" ] && { echo "gen-preview.sh: $previews is a symlink" >&2; exit 1; }
mkdir -p "$previews"
find "${previews:?}" -mindepth 1 -maxdepth 1 ! -type d -delete
python3 - "$here/core/src" "$previews" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
import clients, compose, repo

client = clients.get("claude", compose.ROOT)
workdir = "/agent-pm-preview/work"
n = 0
for role in repo.read_config(os.path.join(compose.ROOT, compose.CONFIG))["roles"]:
    for task in compose.index(compose.ROOT, role):
        run = compose.load_run(compose.ROOT, role, task, layers=[client.config])
        text = ("Repo: <owner>/<name>. " if run.output["type"] == "pull-request" else "") + "(input text here)"
        params = compose.RunParams(input=text, out=os.path.join(workdir, "out.md"), workdir=workdir)
        with open(os.path.join(sys.argv[2], f"{role}-{task}.md"), "w") as f:
            f.write(compose.render(compose.ROOT, run, params, client=client))
        n += 1
print(f"{n} prompts in {sys.argv[2]}")
PY
