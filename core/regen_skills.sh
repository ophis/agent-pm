#!/usr/bin/env bash
# Regenerates skills/<role>-<task>/SKILL.md for every role and task in the config (config.toml, config.local.toml on
# top), except the roles in SKIP (pipeline test roles, not for people).
# Usage: ./regen_skills.sh [OUT_DIR]   (default: <core>/skills)
set -euo pipefail
core="$(cd "$(dirname "$0")" && pwd)"
SKIP="dummy-tester"
out="${1:-$core/skills}"
python3 -c '
import sys
sys.path.insert(0, sys.argv[1] + "/src")
import repo
skip = sys.argv[2].split()
for role, r in repo.read_config(sys.argv[1] + "/config.toml")["roles"].items():
    for task in r.get("tasks", {}) if role not in skip else ():
        print(role, task)
' "$core" "$SKIP" | while read -r role task; do
  python3 "$core/src/drive.py" --role "$role" --task "$task" --client skill --out "$out"
done
