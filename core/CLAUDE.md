# core

The portable core pack: `team/` text (guide, principles, roles, tasks, methods, templates) and `output/` (how a run hands back) compiled by `src/compose.py` and run by `src/drive.py` through a client in `src/clients/`. It knows nothing of Linear or a fixed docs repo (`docs/adr/0001`).

## Commands

```bash
python3 -m unittest discover -s core/src/tests -p "*_test.py"   # no network, no Claude
python3 core/src/drive.py --role R --task T --input X --out O --workdir W --dry-run   # the command a run gets
core/regen_skills.sh                                             # after editing team/, output/, config/ or src/repo.py
```

Commit the regenerated `core/skills/` with the change that caused it.

## Rules

- `team/` and `output/` say what a run does and what it reports, client-neutrally. How a client starts a run and how the run's outcome and progress come back live in that client's class.
- A task marks where a run reports progress with a line `[agent-pm-progress:<name>] what to report`, under its own **Report progress** item (a step, or a bullet under the parent it belongs to), never inside another item; how the run reports it is the client's `handover()` (Claude: `report.py progress <name> <report>`; skill: before the next tool call, a text message holding only that line, the report after the mark). A done or failed run whose task marks `start` but never reported it gets a `missing` event (`drive.start`).
- Config is layered (`compose.load_run`): `config/config.toml` is client-neutral and valid on its own; `config/clients/<name>.toml` has the same layout and its run keys replace the neutral ones.
- `src/repo.py` is stdlib-only and imports nothing from `src/`: the skill client copies it into each skill, and `team/methods/` into a skill that names it.

## Add a client

1. **Class.** `src/clients/<name>.py`, a `Client` subclass (it is also the prompt's `compose.Vehicle`) setting:
   - `keys`: its own config keys (`"roles"` too for per-role/task entries);
   - `needs_config`: whether it reads `config/clients/<name>.toml`;
   - `runs`: `True` → `launch()` starts a run; `False` → `export()` writes files (as `skill` does);
   - `scripts_path(root)` and `methods_path(root)`, only when prompts must name `core/src/` or `core/team/methods/` other than by its absolute path (skill: `${CLAUDE_SKILL_DIR}/scripts`, `${CLAUDE_SKILL_DIR}/methods`).
2. **Launch** (`runs = True`): `launch(prompt, run, *, params, access) -> Launch(argv, env, cwd)`.
   - Map `run.tier` and `run.effort` through its config; a value with no mapping is a `ConfigError`.
   - Turn `access.dirs` and `access.commands` into its own permission flags; whatever it can't enforce stays a prompt request (`docs/adr/0003`).
   - New session vs resume from `params.sid` and `params.resume`; `cwd` = `params.workdir`.
3. **Outcome and progress** (`docs/adr/0006`): override `handover()`, the Output › Return text telling the run to report both with `{{report}}` (filled with `compose.report_command`: `src/report.py --to <workdir>/.report.jsonl`, pre-approved for every run): `progress <name> <text>`, and `outcome --status … --title … --summary …` as its last action. `drive.start` creates that channel before launch, tails it during the run (only lines appended after it began) and once more after exit, keeps the last outcome and validates it (`drive.validate`, the only check). `events(lines)` turns the run's stdout into `Event("text")`.
4. **Register** it in `REGISTRY` (`src/clients/__init__.py`); add `config/clients/<name>.toml` when `needs_config`.
5. **Test** in `src/tests/drive_test.py`: its argv for a role/task, resume, an unmapped tier, its events from a sample of its output.

Done when `drive.py --client <name> --dry-run` prints the expected command for every role/task in `config/config.toml` and the suite passes.
