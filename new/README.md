# new/

The new core ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)) and the Linear orchestrator ([TASK-315](https://linear.app/ophis-workgroup/issue/TASK-315)), under construction; they replaced the old root `core/` and `orchestrator/` code and move to the root at cutover ([TASK-334](https://linear.app/ophis-workgroup/issue/TASK-334)). Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture. Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the import root (`agent_pm.utils.paths.IMPORT_ROOT`): only package `agent_pm`, imported by package path; set PYTHONPATH to this tree's, never rely on an inherited one (`tests/case.py` `Case` fails a test on another tree's `agent_pm`). An agent session with commands or a gate gets it as its PYTHONPATH, replacing the user's own (`agent_pm.role.composer.run`) |
| `core/src/agent_pm/` | a package per layer: `session/` (layers `driver/`, `adapters/`), `role/` (layers `delegate/`, `composer/`; `contract/`, `repos/`, `agent_tools/`), `manager/` (outside the layers), `utils/`; `config.py`, the only home of the core config's paths (`CONFIG`, `LOCAL`) |
| `core/src/agent_pm/role/agent_tools/` | agent-run CLIs (git/gh side effects), outside the layers: `repo.py`, which agent sessions run as `python3 -P -m agent_pm.role.agent_tools.repo worktree …` / `status …` (`core/config.toml` `commands`) |
| `core/src/agent_pm/role/delegate/` | the Delegate: one role's run through the Composer and the Driver, read from its replies; `cli.py` its command line and the exit codes' home: `PYTHONPATH=new/core/src python3 -m agent_pm.role.delegate.cli --role ROLE --input TEXT --out PATH\|- --workdir DIR`, `… --list`, `… --detach --events FILE` (the run in Driver session `<role>-<sid8>-drive`) |
| `core/src/agent_pm/utils/` | shared utilities; `config_file.py` loads and merges TOML files; `paths.py` normalizes paths and checks containment, temporary roots and printable absolute paths (`printable_abs`); `files.py` reads regular files (`read_regular`), writes one atomically (`save`) and appends a line (`append_line`); `text.py` drops control characters (`printable`); `markdown.py` finds Markdown code fences; `args.py` holds argparse helpers (`Once`) |
| `core/src/agent_pm/role/repos/` | `__init__.py` parses repo specs and origin URLs and resolves clones and checkout paths (`resolve`, `checkout`); `shell.py` runs a command with a timeout, URL userinfo masked in its stderr (`sh`, `err_text`); `git.py` holds git options that keep a checkout's fsmonitor and hooks off (`GUARD`), finds a worktree's clone (`common_dir`) and runs git with lock retries (`git`) |
| `core/src/agent_pm/role/contract/` | names the reply protocol's progress mark tag and outcome fence info |
| `core/src/agent_pm/session/adapters/claude/` | Claude Code's Adapter, runtime `claude` |
| `core/src/agent_pm/session/adapters/print_prompt/` | act-as's Adapter, runtime `print_prompt`: in_process only; hands the composed prompt back as the deliverable, no process |
| `core/src/agent_pm/session/driver/tmux_host.py` | hosts a command in a detached tmux session and shows it in a pane or grid; the only module calling tmux (`tests/guards.py` `tmux_calls`) |
| `core/src/agent_pm/session/driver/modes/` | the Driver's Modes: `headless.py` (a command on pipes), `tui.py` (a tmux session through `tmux_host.py`), `in_process.py` (an Adapter's run on a thread, no process); `tui_hook.py`, the command a tui session's hooks run |
| `core/src/agent_pm/manager/roster.py` | a manager's roster, outside the layers: `~/.agent-pm/managers/<name>/roster.json`, beside its `events` file |
| `core/src/agent_pm/manager/crew.py` | a manager's workers, each a plain tui session (`core/src/agent_pm/session/adapters/base.py`), and its roster, outside the layers: the old crew's CLI (`/agent-pm:tmux`: removed, rebuilt by TASK-356) as `env PYTHONPATH=<absolute new/core/src>[:<your PYTHONPATH>] python3 -P -m agent_pm.manager.crew …` (`env`: the act-as row; PYTHONPATH: its docstring) |
| `core/team/` | the Composer's built-in team: main's root `core/team/` at a0bf18c, except that `output/output.md` and `output/destinations/{local,orchestrator}.md` name the outcome's `deliverable_file` and agent commands run `agent_pm.role.agent_tools.repo` |
| `core/config.toml` | the Composer's run keys (`runtime` among them) and `[roles.<role>]` tables, the Driver's `[adapters.<name>]` tables and tui display keys (the registry reads both once per process); which layer reads which keys, and this machine's settings in `~/.agent-pm/core-new.local.toml`: the file's header |
| `core/.claude-plugin/plugin.json` | the Claude Code plugin `agent-pm`, root `core/`, its skills `core/skills/`; `../.claude-plugin/marketplace.json` lists it. This checkout's: `claude --plugin-dir new/core`; check: `claude plugin validate new/core && claude plugin validate .` |
| `linear-orchestrator/src/` | the Linear orchestrator's import root: only package `agent_pm_linear`; on PYTHONPATH with `core/src/`, by the same rule |
| `linear-orchestrator/src/agent_pm_linear/` | the Linear orchestrator; entries `router.py` (a tick or `--issue`: starts or resumes one agent run as `run.py` in a detached Driver session, its PYTHONPATH both roots; `--brake ROLE`: ROLE's runtime usage check), `run.py` (one issue's agent run through `delegate.run`, its events written back to Linear) and `promote.py` (a Handoff becomes the next role's Todo issue; then `prune.py` closes finished issues' TUI sessions, deletes their checkouts and archives them): `PYTHONPATH=new/core/src:new/linear-orchestrator/src python3 -P -m agent_pm_linear.router\|run\|promote …` |
| `linear-orchestrator/config.toml` | Linear ids, `[roles.<role>]`, `work_dir` and the `[core]` overlay on `core/config.toml`; `config.load` reads it once per entry's `main`; this machine's settings in `~/.agent-pm/linear-new.local.toml`: the file's header |
| `../orchestrator/com.ophis.agent-pm.{router,promote}.plist` | launchd jobs running the router (`--now`, at :00 and :30) and promote (every 300 s) as `python3 -P -m agent_pm_linear.router\|promote`, PYTHONPATH both roots, absolute paths under `/Users/francis/playground/agent-pm/`; installed at cutover |
| `core/skills/act-as/SKILL.md` | `/agent-pm:act-as` on the new core: the Delegate CLI as `env PYTHONPATH=${CLAUDE_SKILL_DIR}/../../src python3 -P -m agent_pm.role.delegate.cli …`, runtime `print_prompt`. `env`, not a leading `PYTHONPATH=`: Claude Code's allow rule must match the whole command, which such an assignment prevents |
| `core/skills/manage/SKILL.md` | `/agent-pm:manage`: guidelines for the user's commander session, text only |
| `tests/` | `case.py` (`Case`, every test's base: a fresh temp HOME per test, none set at import), `unit/`, `integration/` (adapter tests in `adapters/<runtime>/`; the Linear orchestrator's in their `orchestrator/`; `integration/orchestrator/` also holds `fake_linear.py` and `fake_gh.py`), `fake_adapter/` (the test-only runtime `fake`), `integration/adapters/claude/fake_claude.py` (a fake `claude` for the integration tests), `live_tmux.py` (private tmux servers for them), `guards.py` |
| `~/.agent-pm/tui/<name>/` | tui session `<name>`'s dir: `session.json` (its state, which `driver.api.attach` reads), `hooks.jsonl` (its hook records); `driver.api.close` removes it. `~/.agent-pm/tui/<name>.lock` serializes a name's starts, closes and stops |
| `~/.agent-pm/workers/<sid>/` | crew worker `<sid>`'s dir, its `Spec.workdir`: `worker.json` (0600), its record for `agent_pm.manager.crew restart`, keeps the worker's flags and env in plain text |

Imports go one way: `role.delegate` → `role.composer` or `agent_pm.session`, `session.driver` → `session.adapters`, a module outside the layers → `agent_pm.session` or `session.driver`; outside `session/driver/`, only `session.driver.api`; `role/` imports from the session layer only the names in `session/__init__.py`'s `__all__` (its public API); a layer never imports a module outside the layers but the shared ones (`utils/`, `config.py`, `role/contract/`, `role/repos/`), and a session layer no `role` module (until TASK-366, `session/adapters/print_prompt/in_process.py` → `role.contract`); `agent_pm_linear` → `role.delegate`, `role.composer` (`config.py` only), `session.driver.api`, the shared modules, and no `agent_pm` module imports it, nor does any file under `core/` name it; besides the standard library, `utils/` imports only `utils/`, `config.py` only `utils/`, `role/contract/` and `role/repos/` only the shared modules, `role/agent_tools/` only `utils/`, `config.py` and `role/repos/`, and nothing imports `role/agent_tools/`. A layer reaches the next in-process: `session/`, `role/` and `agent_pm_linear` run no `agent-pm-*` command (`agent-pm-session` within `session/` excepted). `tests/guards.py` (`layer_imports`, `role_session`, `pure_utils`, `orchestrator_imports`, `layer_commands`, `core_standalone`) enforces it, with the other rules in its docstring.

## Codex sandbox

`[adapters.codex].sandbox` accepts `danger-full-access` (default) or `workspace-write`. Both Modes explicitly pass `-s`, `approval_policy="on-request"` and `approvals_reviewer="auto_review"`. Automatic review only handles actions requiring approval. Crew workers are plain sessions (`core/src/agent_pm/session/adapters/base.py`): none of this section applies to them.

With full access, `shell_network`, writable-root boundaries and team-dir protection do not restrict the runtime. `workspace-write` retains the existing sandbox flags. Recovery saves the effective policy; resume keeps it despite config changes. Older snapshots without `sandbox` retain `workspace-write`.

## Access

A run's `read` dirs and its role's tasks dir are never `--add-dir`: no runtime is granted write to them. Codex has no read-only-dir mechanism usable here, so it adds nothing for them: under `workspace-write` its sandbox reads the whole disk and writes only its roots; under the default `danger-full-access` (Codex sandbox, above) it can write them too. Each runtime's grant and its limits: `core/config.toml`'s `read` comment.

## Tests

```bash
PYTHONPATH=new/core/src:new/linear-orchestrator/src python3 -m unittest discover -s new/tests/unit -t . -p "*_test.py"
PYTHONPATH=new/core/src:new/linear-orchestrator/src python3 .github/integration_jobs.py JOB  # one CI integration job, or --check
```

CI (`.github/workflows/test.yml`) runs the unit tests in one job, the integration tests in one job per package (`.github/integration_jobs.py`: a test file outside its `JOBS` fails `--check`), and Ruff over the whole repo (root `CLAUDE.md` › Commands).
