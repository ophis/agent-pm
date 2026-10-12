# Crew integration parity

Baseline: `origin/main` at `f1d0c595f03063034f77944332615dca2c082cfa`. TASK-321 reread the old integration sources below without importing or executing old code. No other old `tests/integration/` file covers crew or roster: `core/` covers role runs and the Driver, `orchestrator/` the board.

Aliases expand to exact test identities:

| Alias | Module or class |
|---|---|
| C | `new.tests.integration.tmux.crew_integration_test` |
| G | `new.tests.integration.driver.tmux_host_integration_test` |
| CT | `new.tests.integration.driver.claude_tui_integration_test` |
| T | `new.tests.integration.adapters.codex.tui_integration_test.TuiIntegrationTest` |
| U | `new.tests.unit.tmux.crew_test` |
| R | `new.tests.unit.roster_test` |
| A | `new.tests.unit.driver.api_test` |

C runs crew's CLI as subprocesses on a private tmux server with the real Adapters over the fakes (`new/tests/fake_claude.py`, Codex `fake_tui.py`). Placement (grid, settle, retile, splits) moved into the Driver's `show`, so G covers it. Old tmux options `@sid @cwd @client @exe @flags @env @events` are now the worker record (`~/.agent-pm/workers/<sid>/worker.json`). Intentional changes: restart is close + start(resume) + show, so it opens a new pane; a top-level manager that is not a Driver tui session defaults to the config's `runtime`. Delegate `--detach` role entries and `router.py --tui` pipeline entries are not written yet (TASK-315): C writes a role entry and its bare tmux sessions itself.

## Crew

Old module: `tests.integration.tmux.crew_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `Lifecycle.test_start_state_events` | `C.ClaudeLifecycleTest.test_start_done_next_event_reply_send_done_reply`; `CT.TurnsTest.test_turns_their_lines_state_and_reply_send_and_the_border_no_pane_opened` | Plain argv (`--session-id`, `--name`, hook `--settings`, prompt; no role-run policy), worker record, roster resume argv, done, `next-event`, `tmux_host.py send`, second done, `reply`. Border format: CT. |
| `Lifecycle.test_blocked` | `C.ClaudeLifecycleTest.test_blocked_on_a_hook` | Adds the early watch's `waits on a dialog` line. |
| `Lifecycle.test_dead_restart` | `C.ClaudeLifecycleTest.test_dead_then_restart_resumes_the_sid_in_a_new_pane` | Dead event, its content, `--resume <sid>`, a turn after it. Changed: a new pane, not the same pane and cell. |
| `Lifecycle.test_early_death` | `C.ClaudeLifecycleTest.test_an_early_death_exits_1_with_its_last_lines_its_session_kept` | Exit 1, report head, the runtime's error line, dead session kept. |
| `Grid.test_columns_retile` | `G.GridTest.test_columns_retile` | The Driver's grid. |
| `Grid.test_name_order` | `G.GridTest.test_name_order` | The Driver's grid. |
| `Grid.test_sub_workers` | `G.GridTest.test_sub_sessions` | The Driver's grid. |
| `Settle.test_grid_up_settles` | `G.SettleTest.test_grid_up_settles` | The Driver's tile. |
| `Settle.test_concurrent_tiles_settle` | `G.SettleTest.test_concurrent_tiles_settle` | The Driver's tile. |
| `Settle.test_busy_mutes_the_hooks` | `G.SettleTest.test_busy_mutes_the_hooks` | The Driver's tile. |
| `Settle.test_breaker_stops_a_loop` | `G.SettleTest.test_breaker_stops_a_loop` | The Driver's tile. |
| `Retile.test_open_close_retiles_on_an_open_only` | `G.RetileTest.test_open_close_retiles_on_an_open_only` | The Driver's retile. |
| `Retile.test_open_close_retiles_on_a_stop` | `G.RetileTest.test_open_close_retiles_on_a_close`; `U.StopTest.test_a_worker` | `crew.py stop` closes through `api.close` (U), which G re-tiles on. |
| `Retile.test_off_places_only_till_tile` | `G.RetileTest.test_off_places_only_till_tile` | The Driver's placement. |
| `Retile.test_a_switch_to_open_close_leaves_only_its_hooks` | `G.RetileTest.test_a_switch_to_open_close_leaves_only_its_hooks` | The Driver's retile. |
| `Retile.test_restart_keeps_a_dragged_layout` | None | Restart now opens a new pane (close + start + show), which re-tiles. |
| `Managers.test_own_session_and_worker_in_worker` | `C.ManagersTest.test_own_session_and_worker_in_worker` | `@events` read from the worker record. |
| `Managers.test_manager_flag` | `C.ManagersTest.test_manager_flag` | Complete. |
| `Managers.test_no_pane_needs_a_manager` | `C.ManagersTest.test_no_pane_needs_a_manager` | Complete. |
| `Splits.test_split_outside_grid` | `G.SplitsTest.test_split_outside_grid`; `U.MainTest.test_start_split_from_below` | Placement: G; crew passing `--split`/`--split-from` to `show`: U. |
| `Roster.test_second_manager_after_release` | `C.RosterTest.test_second_manager_after_release` | Complete. |
| `Roster.test_second_manager_after_the_holder_ends` | `C.RosterTest.test_second_manager_after_the_holder_ends` | Complete. |
| `Roster.test_concurrent_attaches_one_holds` | `C.RosterTest.test_concurrent_attaches_one_holds` | Complete. |
| `Roster.test_killed_worker_gone_then_recovered` | `C.RosterTest.test_killed_worker_gone_then_recovered` | Complete. |
| `Roster.test_stop_all_stops_what_mgr_opened_and_only_that` | `C.RosterTest.test_stop_all_stops_what_mgr_opened_and_only_that` | Workers, a sub-manager's worker and a role entry's sessions stopped; another manager's session kept. The role run is a written entry over bare sessions (TASK-315). |
| `Roster.test_killed_role_run_gone_then_recovered` | None; `R.RosterTest.test_recovery_forms_each_a_copy`; `U.ResumeTest.test_each_gone_entry_runs_its_recovery_argv_in_its_cwd_unlocked_no_shell_in_table_order` | A detached role run needs Delegate `--detach` (TASK-315). Role recovery argv and its run: R, U. |
| `Roster.test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one` | `C.RosterTest.test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one` | Complete. |
| `Roster.test_stop_a_role_run_by_its_tui_session` | `C.RosterTest.test_stop_a_role_run_by_its_tui_session` | The role run is a written entry over bare sessions (TASK-315). |
| `Roster.test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume` | `C.RosterTest.test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume` | The entry and its `run.jsonl` result are written by the test, not a headless role run (TASK-315). |
| `Roster.test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker` | `C.RosterTest.test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker` | Workers only. Role and pipeline recovery after a lost server: None, needs Delegate `--detach` and `router.py --tui` entries (TASK-315). |
| `Roster.test_unshown_worker_reopened` | `C.RosterTest.test_unshown_worker_reopened`; `G.GridTest.test_an_unshown_session_reopens_in_the_grid` | Complete. |
| `Roster.test_take_over_resumes_from_the_cursor` | `C.RosterTest.test_take_over_resumes_from_the_cursor` | Complete. |
| `Roster.test_rotation_keeps_the_unhandled_lines` | `C.RosterTest.test_rotation_keeps_the_unhandled_lines` | Complete. |

## Codex workers

Old module: `tests.integration.tmux.codex_worker_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `CodexLifecycle.test_start_blocked_done_reply_follow_up` | `C.CodexLifecycleTest.test_start_blocked_done_reply_follow_up`; `T.test_start_permission_reply_send_and_separate_process_attach` | Plain argv (no `-s`, approval or reviewer), `CODEX_HOME`, worker record, roster resume argv, blocked, done, `reply`, follow-up. Old `core-sessions/workers/<sid>.json` and `.reply`: the Adapter's binding (T). |
| `CodexLifecycle.test_restart_and_recovery_from_a_claude_session_keep_the_native_session` | `C.CodexLifecycleTest.test_dead_then_restart_and_recovery_from_a_claude_session_resume_the_native_session` | Restart after a death, `attach --resume` with `CLAUDECODE` set, same native session and argv, `CLAUDECODE` dropped. Hook source `resume`: T. |
| `CodexSession.test_a_codex_managers_marker_and_injected_defaults_stay_out_of_its_workers` | `C.RuntimeTest.test_env_scrubbed_both_ways`; `A.PlainTest.test_a_plain_env_drops_every_adapters_identity_then_applies_the_launch_then_spec_env` | Codex and Claude identity dropped both ways, Codex defaults dropped inside Codex and kept outside, colors. Restart and recovery under a Codex session take the same `api.start` env path: A. |
| `CodexSession.test_a_codex_workers_own_crew_start_defaults_to_codex` | `C.RuntimeTest.test_a_worker_started_in_a_codex_worker_defaults_to_codex` | From its own row's `runtime`; inner events file, distinct native session, `--client codex` in the entry. |

Additional: `C.CodexLifecycleTest.test_the_trust_dialog_waits_on_a_dialog` (FR-27), `C.RuntimeTest.test_a_top_level_manager_defaults_to_the_configs_runtime` (the changed default), `C.RuntimeTest.test_a_codex_worker_whose_cwd_covers_the_workers_dir_is_refused` (`Spec.protect`).
