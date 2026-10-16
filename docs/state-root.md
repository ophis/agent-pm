# State root

agent-pm keeps its state in its state root, `~/.agent-pm/`. `work/` and `logs/` are in `<work_dir>` instead, `~/.agent-pm` by default.

```text
~/.agent-pm/
├── config.local.toml             this machine's settings
├── work/<ID>/                    an issue's agent-run workdir
│   ├── agent/                    the agent's cwd and only write grant
│   │   ├── src/  publish/        the run's checkouts; the report repo's clone
│   │   └── tmp/                  the run's temp files
│   ├── run/                      the harness's files, kept as the run's record
│   │   ├── delegate.json         the run's state, beside its locks
│   │   ├── run.jsonl             session records; resume reads them
│   │   └── out.md                the deliverable
│   └── writeback.json            the write-back ledger, beside its lock
├── logs/
│   ├── runs.jsonl                each run's start, resume and limit rows
│   ├── orchestrator.jsonl        the event log, also launchd's stdout and stderr
│   ├── orchestrator.jsonl.1      the event log before its last rotation
│   ├── orchestrator.jsonl.lock   one rotation at a time
│   └── router.lock               one router at a time
├── sessions/claude/  codex/      what a client session's resume command replays
├── tui/<name>/                   a running tui session's state and hook log
├── tui/<name>.lock               that name's start and close lock
├── pty/<name>/                   a pty-host session's files
├── pty/<name>.lock               that name's start lock
├── workers/<sid>/                a worker's state; by-name/<name>: its latest sid
├── hidden/<name>.json            whether that name's tmux session is hidden, for its sid
├── managers/<name>/              a manager's Roster and events file
├── adhoc/<date>-<role>-<name>/   workdirs of /agent-pm:act-as and /agent-pm:manage role runs
├── probe/                        the empty cwd of the client usage check
└── bin/                          ctmux --install's launchers
```

| Entry | Cleaned by | Safe to delete by hand |
|---|---|---|
| `config.local.toml` | nobody | never: your settings |
| `work/<ID>/agent/` | prune ([Finish](board.md#using-the-board)) | no: prune also drops the issue's worktrees and pushed branches in a `[local_clones]` clone |
| `work/<ID>/run/` | prune deletes the whole `work/<ID>/` 7 days after Done or Canceled, once `agent/` is gone and the run is not live | once you won't attach or resume the run; by hand where prune never reaches: the run's delegate kind is no longer configured, or the issue was deleted in Linear |
| `work/<ID>/writeback.json`, `writeback.lock` | prune, with `work/<ID>/run/` | once the issue's run won't resume or be recovered: Recover replays a write-back missing from the ledger; where prune never reaches: as `work/<ID>/run/` |
| `logs/runs.jsonl` | the router: rows over 7 days old, except each issue's latest run | never ([Operating](board.md#operating)) |
| `logs/orchestrator.jsonl`, `.1`, `.lock` | each event's writer: at 10 MiB the file becomes `.1`, replacing the old `.1` | any time; keep `logs/` ([Setup](board.md#setup) step 6) |
| `logs/router.lock` | nobody | while no router runs |
| `sessions/` | saving a client session's state: an entry not saved or resumed for 30 days | an entry once its session ended and you won't resume it |
| `tui/<name>/` | closing or stopping the session; the tui sweep | no: closing the session removes it |
| `tui/<name>.lock` | the tui sweep | while no session of that name starts or closes |
| `pty/<name>/`, `pty/<name>.lock` | closing the session or a detached session's exit (the dir); the pty sweep | no: closing the session removes it; a `<name>.lock` while no session of that name starts |
| `workers/<sid>/` | each `agent-pm-session` start, restart or resume that launches a worker's client: a sid whose `worker.json` is over 30 days old, that no `by-name/` entry names and no worker lock holds | once you won't restart or resume the worker |
| `hidden/<name>.json` | each write of one (a `--display none` start, `show`, `hide`): an entry unwritten for 30 days whose tmux session is gone | any time: that session then reads not hidden |
| `managers/<name>/` | `agent-pm-crew forget` drops an entry; `events` rotates to `events.1` past 1 MiB | while no manager holds it; its Roster is lost |
| `adhoc/` | nobody: your working directory | a folder whose run is not running (`agent-pm-delegate status <dir>`) |
| `probe/`, `bin/` | nobody | any time; `ctmux --install` restores `bin/` |

Each tui start runs the tui sweep and each pty start the pty sweep, over the other names of their dir whose files all went unchanged for 30 days: the tui sweep drops a name its host has no session of, the pty sweep one with no process left; a name whose lock is held stays.
