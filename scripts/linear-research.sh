#!/bin/bash
# One runner tick: resumes an interrupted Deep Research run or claims the next Todo issue (pick.py)
# and hands it to stages/deep-research.md in a detached tmux session.
# --now skips the 01:00-06:59 hours check. --dry-run changes nothing, starts no run, and prints
# pick.py's plan and the usage probe.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
# claude -p kills background workflows after 10 idle minutes by default; a research run takes longer.
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=3600000
DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION=linear-research
STATE="$(dirname "$DIR")"
STAGE="$STATE/stages/deep-research.md"
RUNS="$STATE/logs/runs.log"
mkdir -p "$STATE/work" "$STATE/logs"

DRY=; NOW=
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --now) NOW=1 ;;
    *) echo "usage: $0 [--dry-run] [--now]" >&2; exit 2 ;;
  esac
done

skip() {
  if [[ -n "$DRY" ]]; then echo "would skip: $1" >&2; else echo "$(date '+%F %T') skip: $1" >> "$RUNS"; fi
}

HOUR=$(date +%H)
if [[ -z "$NOW" ]] && (( 10#$HOUR < 1 || 10#$HOUR > 6 )); then
  skip "outside hours"
  [[ -n "$DRY" ]] || exit 0
fi

# Checked before pick.py: Recover assumes no run is active.
if tmux has-session -t "$SESSION" 2>/dev/null; then
  skip "previous run still active"
  [[ -n "$DRY" ]] || exit 0
fi

# stderr stays out of runs.log: --prune replaces the file.
if [[ -z "$DRY" ]] && ! python3 "$DIR/pick.py" --prune "$RUNS"; then
  echo "$(date '+%F %T') skip: prune failed" >> "$RUNS"
fi

if [[ -n "$DRY" ]]; then
  PLAN=$(python3 "$DIR/pick.py" --plan --dry-run "$RUNS")
else
  PLAN=$(python3 "$DIR/pick.py" --plan "$RUNS" 2>> "$RUNS")
fi
read -r KIND ISSUE SID K URL <<< "$PLAN"
if [[ -z "${KIND:-}" && -z "$DRY" ]]; then
  skip "nothing to do"
  exit 0
fi

PROBE=$( (cd "$STATE/work" && claude -p "Reply with OK." --model haiku --output-format stream-json --verbose < /dev/null 2>/dev/null) || true)
if USAGE=$(printf '%s\n' "$PROBE" | python3 "$DIR/pick.py" --gate "${KIND:-new}"); then OK=allowed; else OK=blocked; fi

if [[ -n "$DRY" ]]; then
  echo "plan: ${PLAN:-nothing}" >&2
  echo "usage: $USAGE (${KIND:-new} $OK)" >&2
  exit 0
fi
if [[ "$OK" == blocked ]]; then
  skip "$KIND blocked by usage: $USAGE"
  exit 0
fi

if [[ "$KIND" == resume ]]; then
  echo "$(date '+%F %T') resume $ISSUE session=$SID n=$K" >> "$RUNS"
  PROMPT="Resumed run $K for $ISSUE ($URL) after an interruption. Re-read $STAGE first (it may have changed since this session started) and follow its resume rule."
  SESSION_ARG="--resume $SID"
else
  PICKED=$(python3 "$DIR/pick.py" --claim "$RUNS" 2>> "$RUNS")
  if [[ -z "$PICKED" ]]; then
    skip "queue empty"
    exit 0
  fi
  read -r ISSUE URL <<< "$PICKED"
  PROMPT="Follow $STAGE to handle $ISSUE ($URL). The runner has already claimed it."
  SID=$(uuidgen | tr 'A-Z' 'a-z')
  TRANSCRIPT="$HOME/.claude/projects/$(echo "$STATE/work" | tr '/.' '--')/$SID.jsonl"
  echo "$(date '+%F %T') start $ISSUE session=$SID transcript=$TRANSCRIPT" >> "$RUNS"
  SESSION_ARG="--session-id $SID"
fi

tmux new-session -d -s "$SESSION" -c "$STATE/work" \
  "claude -p $(printf '%q' "$PROMPT") $SESSION_ARG --model opus --effort xhigh --permission-mode auto --add-dir $(printf '%q' "$STATE") --add-dir $(printf '%q' "$HOME/playground/private_docs") < /dev/null; rc=\$?; echo \"\$(date '+%F %T') end $ISSUE session=$SID exit=\$rc\" >> $(printf '%q' "$RUNS")"
