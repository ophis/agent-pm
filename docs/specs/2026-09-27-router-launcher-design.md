# Router / launcher split

Status: implemented on branch router-launcher (S7 passed).

## Problem

`pick.py` + `linear-research.sh` run one project (Deep Research): `pick.py` hardcodes `TEAM`/`PROJECT`, the runner hardcodes the stage file, model and flags, and one script mixes scheduling with launching. More stages (Product Design next) need the same queue, resume and caps without copies.

## Scope

- Split into `scripts/router.py` (decides what runs) and `scripts/launch.py` (starts it), both Python, driven by the project registry in `pipeline.toml`, sharing a leaf module `scripts/pipeline.py`.
- Behavior for Deep Research is unchanged (schedule, hours, Recover rules, attempt cap, usage gate, prompts, flags) with one deliberate exception approved by the user: the Recover "interrupted" comment becomes stage-neutral (see Router).
- LIVE (30 min), STALE (2 h), the attempt cap (4) and the usage thresholds stay global for all projects.
- Out of scope: the Product Design stage instructions (a project without `instructions` is never run), parallel runs, per-project cwd, run timeouts. The single tmux session is both lock and run container; parallel runs will need per-run session names and a new liveness rule, so the launcher interface is not final.

## Shared module: `scripts/pipeline.py`

A leaf module imported by router, launcher and promote; it imports none of them. It owns:
- `linear_gql`, `parse_time`, `log`;
- paths: `ROOT`, `WORK` (`<ROOT>/work`, the cwd of every run), `TRANSCRIPTS` (derived from `WORK` exactly as Claude does), `LOGS`, `RUNS_LOG`, `project_log(name)` (`logs/projects/<slug>.log`, creating the directory);
- `load_config(path)` (parse + checks every consumer needs: `next` names a `[projects]` entry with a `prefix`, no cycles in the `next` chain) and `runnable(cfg)` (projects with `instructions`; checks that the file exists and `model`, `effort` are set, else exit non-zero; intended to fail loud, stopping the tick for every project). Promote calls only `load_config`, so a missing stage file never stops Handoff;
- `stage_order(cfg)`.

Keeping `WORK` and `TRANSCRIPTS` in one place matters: Recover's liveness depends on the launcher's cwd matching the transcript dir.

## Config: `pipeline.toml`

A project is **runnable** iff it has `instructions`:

```toml
[projects."Deep Research"]
next = "Product Design"
instructions = "stages/deep-research.md"   # relative to the repo root
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]   # besides the repo root, always added
```

Promote keeps reading `next`, `prefix`, `require_instructions`. The runnable-project checks live in `runnable()` (see Shared module), not in `load_config`.

Stage order: the position of a project in the `next` chain (Deep Research 0, Product Design 1, Engineering 2); projects outside a chain rank 0.

## Router (`scripts/router.py`)

Entry point for launchd (`com.ophis.agent-pm.router`, same schedule as today: hourly 01:00–06:00). One tick:

1. Hours check (01:00–06:59; `--now` skips it).
2. Lock: tmux session `agent-pm` exists → skip.
3. Prune `logs/runs.log` to 7 days (non-fatal, as today).
4. Recover across all runnable projects (one issue query with `project: { name: { in: [...] } }`; a runnable project missing in Linear exits non-zero at setup, as in promote): today's rules unchanged (live / attempt cap / resumable candidate / launch failure → Todo / no current SID and stale → Todo). Candidate order: priority, then later stage first, then oldest first line.
5. Plan: `resume <candidate>`, else `new` if any runnable project's Todo is non-empty, else nothing.
6. Usage probe and gate (unchanged: not rejected, five_hour < 0.9, weekly < 1).
7. Resume: append the `resume` line. New: claim the top Todo issue across runnable projects (priority, later stage first, oldest) with today's re-check and attempt cap, pick the SID, append the `start` line.
8. Call `launch.py` with `sys.executable` and `--issue --url --project --sid --mode new|resume [--k K]`.

Flags: `--now`, `--dry-run` (plan + usage, no changes, no launch), `--issue ID` (with `--now`: claim this Todo issue instead of the top one; for manual starts). `--pick [--project NAME]` keeps today's manual no-mode behavior (Recover, then Pick + Claim, print `<ID> <url>`) that stage step 1 uses.

Router decisions (skips, plan, picks, recover moves) go to stdout/stderr, which launchd writes to `logs/router.log`. `logs/runs.log` holds only state lines: `start` / `resume` (router) and `end` (launcher).

Deliberate change (user-approved): the Recover comment becomes stage-neutral, "The previous run was interrupted. Moving this issue back to the Todo queue." (was "previous research run"). The cap comment is unchanged.

## Launcher (`scripts/launch.py`)

No decisions: looks up `--project` in the registry and starts the run. An unknown or non-runnable project, or an issue ID / session ID not matching `[A-Z][A-Z0-9]*-\d+` / `[0-9a-f-]{36}` (both go into the tmux shell command), exits 2 (printed to `router.log`); the `start` line is already written, so Recover later returns the issue to Todo as a launch failure.

- Prompt: new → `Follow <repo>/<instructions> to handle <ISSUE> (<url>). The runner has already claimed it.`; resume → `Resumed run <k> for <ISSUE> (<url>) after an interruption. Re-read <repo>/<instructions> first (it may have changed since this session started) and follow its resume rule.`
- Command: `claude -p <prompt> --session-id|--resume <sid> --model <model> --effort <effort> --permission-mode auto --add-dir <repo root> --add-dir <each add_dirs>`, cwd `WORK`, in a detached tmux session `agent-pm`, run through `bash -c` so the exit code survives the pipe (`PIPESTATUS`). `PATH` (with `/opt/homebrew/bin` first) and `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=3600000` are set inside the `bash -c` string: a running tmux server would otherwise supply its own environment.
- Output: the project log `logs/projects/<slug>.log` (slug: lowercase, non-alphanumerics → `-`, e.g. `deep-research.log`) gets `<ts> launch <ISSUE> mode=<mode> session=<sid>`, then claude's stdout/stderr (via `tee -a`, so `tmux attach` still shows it), then `<ts> end <ISSUE> session=<sid> exit=<rc>`. The same `end` line is appended to `logs/runs.log`.

Contract for stage files: every runnable project's instructions define a resume rule (a section for prompts starting "Resumed run").

## Files

- Remove `scripts/pick.py`, `scripts/linear-research.sh`, `scripts/com.ophis.linear-research.plist`; add `scripts/pipeline.py`, `scripts/router.py`, `scripts/launch.py`, `scripts/com.ophis.agent-pm.router.plist` (runs `/opt/homebrew/bin/python3 router.py`; stdout/stderr → `logs/router.log`).
- `promote.py` imports from `pipeline.py`.
- `stages/deep-research.md` step 1: `python3 ../scripts/router.py --pick --project "Deep Research"`.
- Log growth: only `runs.log` is pruned; `router.log` and project logs grow unbounded (accepted).
- README, CLAUDE.md: updated to the new names.

## Migration (while no run is active)

Precondition: `tmux has-session -t linear-research` fails (no run active; the lock name changes). Then `launchctl bootout` the old job and delete its plist from `~/Library/LaunchAgents`; install and bootstrap the router plist. `runs.log` format is unchanged, so in-flight resume state carries over. The old `logs/research.log` is left as is.

## Tests

- `test_router.py` (from `test_pick.py`, all existing cases kept; the interrupted-comment assertion updated to the new text) plus: Recover and claim across two runnable projects (priority, then later stage first, then age); a project without `instructions` is never recovered or claimed; `--issue` claims that issue; `--pick --project` restricts to one project.
- `test_pipeline.py`: config validation (cycle, missing prefix, missing stage file only fails `runnable`), stage order, slug, `TRANSCRIPTS` derived from `WORK`.
- Tick tests (from `test_dispatcher.py`): hours, lock, prune order, dry-run, usage blocked, `launch.py` called with the right arguments, `start`/`resume` lines.
- `test_launch.py`: prompt text per mode, flags from the registry (model, effort, add-dirs incl. repo root), tmux session name and cwd, env set inside `bash -c`, project log path, `end` line in both logs, unknown project → exit 2.
- Manual: `router.py --now --dry-run` against live Linear before switching launchd (config errors exit non-zero in dry-run too).
