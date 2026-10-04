#!/usr/bin/env bash
# Regenerates skills/<role>-<task>/SKILL.md for every role and task in config/config.toml.
# Usage: scripts/regen_skills.sh [OUT_DIR]   (default: <core>/skills)
set -euo pipefail
core="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$core/skills}"
python3 -c '
import sys, tomllib
for role, r in tomllib.load(open(sys.argv[1], "rb"))["roles"].items():
    for task in r.get("tasks", {}):
        print(role, task)
' "$core/config/config.toml" | while read -r role task; do
  python3 "$core/scripts/drive.py" --role "$role" --task "$task" --client skill --out "$out"
done
