# CLAUDE.md

## Gotchas

- This file never loads in an agent run outside a trusted cwd (README › Install). An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls.
- Every task has exactly one `[agent-pm-progress:start]` line and no `budget` one, which start replaced (`delegate/tests/unit/composer/real_test.py` checks): without the start report the Delegate logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts or skills.
- Skills and `config.toml` comments name a file by its path and a skill by its slash command (`/agent-pm:act-as`), never "the act-as skill" or `agent-pm:act-as`.
- A project's `config.toml` holds only its keys, each a real line (a local-only one its empty default, its comment saying what empty means); a `# local:` line only shows a value for `~/.agent-pm/config.local.toml` (`tests/guards.py` `explicit_keys`). Levels, merge and ownership: `utils/src/agent_pm/utils/config_file.py`'s docstring.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`linear.move` does).
- Tmux calls: `session/src/agent_pm/session/hosts/tmux/tmux_host.py`. `/agent-pm:tmux`: removed, rebuilt by TASK-356.
- Keep `<work_dir>/logs/runs.jsonl` (README › Operating).
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Import rules

Their only home; `tests/guards.py` (`layer_imports`, `delegate_session`, `pure_utils`, `orchestrator_imports`, `layer_commands`, `standalone`, `session_only`, `manager_imports`) enforces them.

- One way: delegate → delegate.composer, agent_pm.session or session.engine.api; session.engine.api → session.adapters, .modes, .hosts, .engine.types or .engine.records; session.modes → session.adapters, .hosts or .engine.types; session.adapters → session.engine.types; session/adapters/registry.py → session.hosts (until TASK-368); session/engine/base.py → session.adapters (Session.configure) and session.hosts (Session.parse_hosts); a module outside the layers → agent_pm.session, session.engine.api or session.engine.types.
- A module is in its longest matching package (delegate.engine.plan in delegate, delegate.composer.teams in delegate.composer, delegate.event.reply in the shared delegate.event).
- delegate/ imports from the session layer only the names in session/__init__.py's __all__.
- A layer imports no module outside the layers but the shared ones (utils/, delegate/engine/config.py, delegate/event/); a session layer no delegate module (the Delegate passes its reply protocol in Spec.reply_tags).
- agent_pm.linear_orchestrator → delegate, delegate.composer (engine/config.py only), agent_pm.session's api (and Session, engine/config.py only) and the shared modules but delegate/engine/config.py (its names via agent_pm.delegate); no other agent_pm module imports it, nor does a file under delegate/, session/, utils/ or manager/ (but their tests/) name it.
- Besides the standard library: session/ imports only session/ and utils/ and names no config reader (config_file, tomllib, agent_pm.delegate.engine.config), reply protocol tag (agent-pm-progress, agent-pm-outcome) or team/; utils/ only utils/; delegate/engine/config.py only utils/; delegate/event/ only the shared modules (event/outcome.py also delegate.composer); delegate/team/tools/ only utils/ and delegate/engine/config.py, and nothing imports it.
- agent_pm.manager sits on top, imported by nothing else: manager/crew.py imports only utils/ and manager/roster.py, roster.py only utils/, but for the named exceptions TASK-371 removes (agent_pm.delegate.engine.config and agent_pm.session's api, crew also Session).
- A layer reaches the next in-process: session/, delegate/ (team/tools/ included) and agent_pm.linear_orchestrator run no agent-pm-* command (agent-pm-session within session/ excepted).

## Tests

- A test of `agent_pm.<sub>` goes in its project's `tests/unit/` or `tests/integration/`; a shared helper with its owner (`session/tests/live_tmux.py`); each project's `tests/case.py` holds its own `Case` copy and only the helpers its tests use; only repo-wide checks in the root `tests/` (`case.py`, `guards.py`).
- Tests follow the layer rule (utils < session < delegate < linear-orchestrator < manager): a project's tests import their own project, lower ones (source or tests) and the stdlib, never the root `tests` (`tests/guards.py` `test_layers`).
- Imports: the repo root on `sys.path` (`-t .`) makes each project's `tests/` the package `<project>.tests` (e.g. `session.tests.fakes.host`), the root's `tests`; `linear-orchestrator`'s hyphen leaves its tests only relative imports. No two test packages share a dotted name, so one process may run them all.
- Run (PYTHONPATH: README › Tests):

```bash
python3 -m unittest discover -s session/tests/unit -t . -p "*_test.py"   # one project's unit tests; any <project>/tests/<kind>
python3 .github/integration_jobs.py JOB   # one CI integration job, or --check
```

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
