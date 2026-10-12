# Integration parity with main

Baseline: `origin/main` at `f1d0c595f03063034f77944332615dca2c082cfa`, `tests/integration/`: 214 test methods (core 101, orchestrator 76, tmux 37), one row each: `| Exact old identity | Exact new identity | Coverage and remaining assertions |`. TASK-357 built it from each old test's body (`git show origin/main:<path>`; old code never imported or run) and the tests in `new/tests/integration/`, merging the Codex parity list (TASK-318, TASK-320) and the parity lists in merged `re-architecture` PR bodies (#166, #167, #171, #172, #173, #181, #188); every cited new identity exists by exact name.

The new identity is one of:

- new tests (aliases below) asserting the old behavior; the row names what changed on purpose and what they leave out;
- `to port` (until TASK-357 ports it): the new code has the behavior, but only a unit test, or no test, asserts it;
- `not ported: (a)`, `(b)`, `(c)` or `deleted`: its entry under [Not ported](#not-ported).

| Alias | Module or class |
|---|---|
| CLI | `new.tests.integration.delegate.cli_integration_test` |
| DX | `new.tests.integration.delegate.codex_integration_test` |
| DT | `new.tests.integration.delegate.tui_integration_test` |
| DD | `new.tests.integration.delegate.detach_integration_test` |
| DTD | `new.tests.integration.delegate.team_dirs_integration_test` |
| CD | `new.tests.integration.composer.drive_integration_test` |
| CR | `new.tests.integration.composer.resume_integration_test` |
| CE | `new.tests.integration.composer.extension_runtime_integration_test` |
| CTD | `new.tests.integration.composer.team_dirs_integration_test` |
| DCL | `new.tests.integration.driver.claude_integration_test` |
| DEA | `new.tests.integration.driver.extension_adapter_integration_test` |
| H | `new.tests.integration.adapters.codex.headless_integration_test.HeadlessIntegrationTest` |
| T | `new.tests.integration.adapters.codex.tui_integration_test.TuiIntegrationTest` |
| K | `new.tests.integration.adapters.codex.tui_integration_test.HookTrustIntegrationTest` |
| F | `new.tests.integration.fake_claude_integration_test` |
| L | `new.tests.integration.live_tmux_integration_test` |
| C | `new.tests.integration.tmux.crew_integration_test` |
| UR | `new.tests.unit.delegate.runner_test` |

## tests.integration.core.drive_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.drive_integration_test.NewRun.test_done` | `CLI.NewRun.test_done`; `DCL.RunTest.test_a_run_in_its_own_cwd_is_recorded_there_and_resumes_there`; `CD.NewRun.test_a_new_runs_prompt_and_intent` | The CLI over the fake: exit 0, session, progress, reply and status on stderr, out, run.jsonl, `--session-id`, stream-json, only `out.md` and `run.jsonl` in the workdir. The session record's `resume` command: DCL; the prompt: CD. Changed: records open `session`, `input`, `client_session`; no `outcome` record (the outcome block is in a reply). |
| `tests.integration.core.drive_integration_test.Outcomes.test_needs_input` | `CLI.Outcomes.test_needs_input` | Complete. |
| `tests.integration.core.drive_integration_test.Outcomes.test_no_outcome` | `CLI.Outcomes.test_no_outcome` | Complete. |
| `tests.integration.core.drive_integration_test.Outcomes.test_invalid_outcome_then_fixed` | `CLI.Outcomes.test_invalid_outcome_then_fixed_by_the_automatic_resume`; `CLI.Outcomes.test_invalid_outcome_twice` | Changed on purpose (TASK-327, PR #171): an invalid outcome gets one automatic fix resume of its session; a second invalid one exits 1 with its error, no out. A later manual `--resume`: `CLI.Resume.test_resume_reuses_the_recorded_cwd_and_session`. |
| `tests.integration.core.drive_integration_test.Outcomes.test_nonzero_exit_preserves_report_for_recovery` | `CLI.Outcomes.test_nonzero_exit_preserves_report_for_recovery` | Complete. |
| `tests.integration.core.drive_integration_test.Outcomes.test_nonzero_exit_without_valid_report_still_fails` | `CLI.Outcomes.test_nonzero_exit_without_valid_report_still_fails` | Complete. |
| `tests.integration.core.drive_integration_test.Stderr.test_the_clients_stderr_lines_land_in_the_record` | `CLI.Stderr.test_the_clients_stderr_lines_land_in_the_record` | Complete. |
| `tests.integration.core.drive_integration_test.Resume.test_resume_reuses_the_recorded_cwd_and_session` | `CLI.Resume.test_resume_reuses_the_recorded_cwd_and_session` | Complete. |
| `tests.integration.core.drive_integration_test.Interrupt.test_interrupt_kills_a_hung_agent` | `CLI.Interrupt.test_interrupt_kills_a_hung_agent` | Complete. |

## tests.integration.core.drive_tui_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.drive_tui_integration_test.TuiRun.test_stop_hook_nudge_outcome` | `DT.TurnEnds.test_the_nudges_turn_ends_done_the_deliverable_saved_the_session_left_open` | Private tmux server, Claude Adapter over the fake: the nudge as the second prompt, two `done` lines in the events file, outcome done, out saved, the session left open, its attach command on stderr. Changed: through `delegate.run` in process (the CLI's tui path: `DD.Tui.test_from_the_manager_shown_in_its_grid_turn_ends_then_one_outcome_done`); record kind `nudge` for `stop`; default session name `<runtime>-<sid8>`. |
| `tests.integration.core.drive_tui_integration_test.TuiRun.test_detach_from_the_manager` | `DD.Tui.test_from_the_manager_shown_in_its_grid_turn_ends_then_one_outcome_done` | Exits at once naming the Driver session's attach command, no outcome line yet; the TUI in the manager's grid; turn-end lines, then one `outcome done`; result done. Changed: `--detach` needs `--events FILE` (PR #188). |
| `tests.integration.core.drive_tui_integration_test.TuiRun.test_detach_events_default` | `not ported: (b)` ([entry](#b-task-356-layers)) | With an explicit events file: DD above. |

## tests.integration.core.extension_client_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.extension_client_integration_test.ExtensionClientIntegration.test_extension_module_and_config_support_dry_run_capability_catalog_and_resolution` | `CE.ExtensionRuntime.test_its_runtime_resolves_composes_and_the_catalog_lists_every_role`; `DEA.ExtensionAdapterIntegration.test_a_copied_adapter_package_and_its_table_run_a_headless_session` | A runtime added as one package and one `[adapters.<name>]` table runs (DEA); its tier resolves, the prompt composes with the input, the catalog lists every role (CE). Dropped on purpose: the client's `handover` text in the prompt (CE asserts none; PR #166, PRD › 现有代码去向) and a dry run printing the runtime's argv (`--dry-run` prints the Delegate's plan). |
| `tests.integration.core.extension_client_integration_test.ExtensionClientIntegration.test_configured_client_without_module_fails_before_launch` | `DEA.ExtensionAdapterIntegration.test_a_table_without_its_package_fails_before_launch` | `ConfigError` before any record or process; the CLI's exit 2 on one: `CLI.ConfigErrors.test_a_config_error_exits_2_and_starts_nothing`. |
| `tests.integration.core.extension_client_integration_test.ExtensionClientIntegration.test_module_without_config_table_fails_before_launch` | `DEA.ExtensionAdapterIntegration.test_a_package_without_its_table_fails_before_launch` | As the row above. |
| `tests.integration.core.extension_client_integration_test.ExtensionWorkerIntegration.test_client_flag_starts_its_worker_and_a_restart_waits_for_the_old_one` | `not ported: (b)` ([entry](#b-task-356-layers)) | Covered apart from the wait: `crew start --client` starts that runtime's worker with its record and roster resume argv (`C.CodexLifecycleTest.test_start_blocked_done_reply_follow_up`); restart resumes the sid (`C.ClaudeLifecycleTest.test_dead_then_restart_resumes_the_sid_in_a_new_pane`, a new pane on purpose, TASK-321); a package-added runtime runs (DEA above). |
| `tests.integration.core.extension_client_integration_test.ExtensionWorkerIntegration.test_its_session_marker_makes_it_the_default` | `C.RuntimeTest.test_a_worker_started_in_a_codex_worker_defaults_to_codex`; `C.RuntimeTest.test_env_scrubbed_both_ways` | Replaced on purpose (TASK-321, PR #187): the default runtime is the caller's own Driver tui session's, not an Adapter's env marker, for any runtime (`agent_pm.crew` `_runtime`); session markers stay out of the worker's env. |

## tests.integration.core.fake_claude_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.fake_claude_integration_test.Interactive.test_turns_from_stdin` | `F.Interactive.test_turns_from_stdin` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_last_line_without_newline_is_a_line` | `F.Interactive.test_last_line_without_newline_is_a_line` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_no_prompt_waits_for_a_line` | `F.Interactive.test_no_prompt_waits_for_a_line` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_eof_before_any_turn` | `F.Interactive.test_eof_before_any_turn` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_resume_needs_the_transcript` | `F.Interactive.test_resume_needs_the_transcript` | Complete; through the Adapter: `DCL.RunTest.test_resuming_a_session_without_a_transcript_exits_1_and_its_stderr_is_recorded`. |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_resume_without_a_prompt_appends` | `F.Interactive.test_resume_without_a_prompt_appends` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_output_is_plain_even_for_stream_json` | `F.Interactive.test_output_format_is_print_mode_only` | Inverted on purpose (PR #172): interactive mode refuses `--output-format` (exit 2). |
| `tests.integration.core.fake_claude_integration_test.Interactive.test_hang_is_ignored` | `F.Interactive.test_hang_and_rate_limit_are_print_mode_only` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Reports.test_command_from_the_prompts_parameters` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Reports.test_a_value_holding_a_backtick_is_read_as_compose_writes_it` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Reports.test_command_serves_later_turns` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Reports.test_report_step_without_a_report_line_fails` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Files.test_write_replaces_and_read_says_the_text` | `F.PrintMode.test_a_write_step_writes_its_file_under_the_cwd_then_the_next_step_runs` | The write step, relative to the cwd, then the next step. Changed: print mode only (`F.BadScenario.test_a_bad_interactive_step_exits_1_before_the_session`); the read step is gone (`PrintMode.test_a_read_is_an_assistant_line` under `Deleted`); replacing an existing file is not asserted. |
| `tests.integration.core.fake_claude_integration_test.Files.test_a_failing_write_or_read_goes_on` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Hooks.test_payloads_and_order` | `F.Hooks.test_payloads_and_order` | Complete, plus PostToolUse, PermissionRequest and Stop's `last_assistant_message` and `background_tasks`. |
| `tests.integration.core.fake_claude_integration_test.Hooks.test_matchers_and_malformed_entries` | `F.Hooks.test_matchers_and_malformed_entries` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Hooks.test_failing_hooks_do_not_stop_the_fake` | `F.Hooks.test_failing_hooks_do_not_stop_the_fake` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Hooks.test_cwd_env_stdout_and_stderr` | `F.Hooks.test_cwd_env_stdout_and_stderr` | Complete. |
| `tests.integration.core.fake_claude_integration_test.Hooks.test_settings_without_usable_hooks` | `F.Hooks.test_settings_without_usable_hooks` | Complete. |
| `tests.integration.core.fake_claude_integration_test.PrintMode.test_stop_runs_once_before_the_result` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.PrintMode.test_hang_runs_no_stop` | `DCL.RunTest.test_a_silent_session_yields_idle_timeouts_until_stop_ends_it`; `CLI.Interrupt.test_interrupt_kills_a_hung_agent` | `hang`: the process lives on after its steps, no `result` line. Print mode's hooks: dropped (`PrintMode.test_stop_runs_once_before_the_result` under `Deleted`). |
| `tests.integration.core.fake_claude_integration_test.PrintMode.test_a_read_is_an_assistant_line` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Gate.test_interactive_parks_until_the_file_appears` | `to port` | The new fake gates any `--session-id` or `--resume` run, interactive too; `F.Gate.test_a_session_waits_for_its_gate_after_its_log_line` runs print mode only. Port into `F.Gate`: an interactive run parks after its log line, no transcript, until the gate file appears, then its turn runs. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_print_mode_parks_before_anything_is_said` | `F.Gate.test_a_session_waits_for_its_gate_after_its_log_line` | Parks after its log line with no transcript until the file appears, then runs; also a resume. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_no_hook_or_report_before_the_gate` | `to port` | Port into `F.Gate`: an interactive run with a UserPromptSubmit hook fires none before its gate opens, one after. The report half went with `report.py` (`Reports.*` under `Deleted`). |
| `tests.integration.core.fake_claude_integration_test.Gate.test_an_existing_file_does_not_hold` | `to port` | Port into `F.Gate`: a gate file that exists before the start does not hold the run. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_no_gate_or_a_null_one_does_not_hold` | `F.Interactive.test_turns_from_stdin` | No gate: every F test without one runs at once. An explicit null is the scenario default (`fake_claude.DEFAULTS`), the same path. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_a_resume_without_its_transcript_fails_without_waiting` | `to port` | The new fake checks the transcript before the gate; `F.Interactive.test_resume_needs_the_transcript` sets no gate. Port into `F.Gate`: with a gate set, `--resume` without a transcript exits 1 at once with `No conversation found`. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_orphaned_at_the_gate_exits_1` | `to port` | `F.Interactive.test_orphaned_it_ends` covers an orphan waiting on stdin only. Port into `F.Gate`: orphaned at its gate, the fake exits 1 with `fake_claude.py: gate`, no transcript. |
| `tests.integration.core.fake_claude_integration_test.BadScenario.test_bad_turns_and_steps_exit_1` | `F.BadScenario.test_a_bad_step_or_value_exits_1_before_the_session`; `F.BadScenario.test_a_bad_interactive_step_exits_1_before_the_session` | Exit 1 before any session file. Its read-step cases went with the read step. |
| `tests.integration.core.fake_claude_integration_test.BadScenario.test_a_gate_that_is_no_string_exits_1` | `F.BadScenario.test_a_bad_step_or_value_exits_1_before_the_session` | `{"gate": 5}`; one check covers every non-string (`fake_claude.CHECKS`). |
| `tests.integration.core.fake_claude_integration_test.BadScenario.test_valid_hook_steps` | `F.Hooks.test_settings_without_usable_hooks`; `F.Hooks.test_payloads_and_order` | Hook steps in interactive turns, an empty later turn among them, are valid and exit 0. |

## tests.integration.core.fake_codex_integration_test

The `Interactive` and `AppServer` classes tested the old fake; their native behavior is now asserted through the new fakes and the public Driver.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_fixture_home_is_ignored_and_owned_cleanup_removes_it` | `H.test_fixture_cleanup_removes_owned_home_outside_writable_roots` | Owned cleanup; the HOME outside the writable roots. The old gitignore check does not apply to a task temp root. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_reports_start_and_valid_outcome_through_channel` | `DX.Codex.test_reports_start_and_valid_outcome`; `H.test_fresh_launch_has_exact_flags_prompt_cwd_and_stdin_eof` | The CLI over the fake Codex: progress, reply and status on stderr, out, records, the native id as `client_session`, stdin EOF (end of file), cwd, `exec --json`, the input in the prompt; exact flags: H. Changed: default sandbox `danger-full-access` (TASK-349); `workspace-write` flags: `H.test_workspace_write_exact_flags_and_saved_policy_through_driver`. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_two_turns_resume_the_same_native_session_and_keep_previous_progress` | `DX.Codex.test_two_runs_resume_the_same_native_session_and_keep_previous_progress`; `H.test_resume_same_native_and_saved_settings` | Same native id, both progress records; flags before `resume`: H. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_interrupted_first_turn_resumes_same_run_and_keeps_progress` | `DX.Codex.test_interrupted_first_turn_resumes_the_same_run_and_keeps_progress` | Complete. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_missing_native_file_fails_before_fake_spawn` | `DX.Codex.test_missing_native_file_fails_before_spawn` | Complete. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_missing_mapping_fails_before_fake_spawn` | `DX.Codex.test_missing_mapping_fails_before_spawn` | Complete. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_different_native_id_on_resume_is_rejected` | `H.test_different_native_id_on_resume_is_not_published_or_saved` | Through the Driver: a `ConfigError`, no changed binding or `client_session`, the session gone. Not asserted: the CLI's non-zero exit and message on it. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_missing_outcome_is_not_inferred_from_final_reply` | `DX.Codex.test_a_bare_json_final_reply_is_no_outcome` | Complete. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_invalid_report_fails_validation` | `DX.Codex.test_an_invalid_outcome_is_fixed_by_one_resume`; `CLI.Outcomes.test_invalid_outcome_twice` | Changed on purpose (TASK-327): one fix resume; a second invalid outcome fails. |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_stderr_is_readable_and_recorded` | `H.test_stderr_is_an_event_and_run_record`; `CLI.Stderr.test_the_clients_stderr_lines_land_in_the_record` | Codex stderr as events and records (H); the CLI prints and records them (CLI, Claude). |
| `tests.integration.core.fake_codex_integration_test.CodexIntegration.test_tui_rejected_before_fake_codex_starts` | `CLI.ConfigErrors.test_a_config_error_exits_2_and_starts_nothing`; `H.test_tui_factory_is_supported_without_starting_a_process` | Codex tui is supported on purpose (TASK-320; `T.test_start_permission_reply_send_and_separate_process_attach`); a runtime without tui still exits 2 before anything starts (CLI, `print_prompt`). |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_unknown_option_or_resume_without_an_id_exits_2` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_turns_from_the_prompt_then_stdin_lines` | `T.test_start_permission_reply_send_and_separate_process_attach` | Prompt boundary, submitted text, each turn's reply. The old fake's exit on end of input does not apply to a persistent tui. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_a_fresh_run_gets_a_new_uuid_and_its_rollout` | `T.test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested`; `K.test_concurrent_independent_starts_serialize_the_same_home_probe` | Distinct native ids, durable bindings and native session files. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_session_start_waits_for_the_first_turn_then_payloads_follow` | `T.test_session_start_waits_for_first_submission_fresh_and_resumed` | Fresh and resumed: SessionStart waits for the first send, then UserPromptSubmit and Stop. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_a_turn_without_text_stops_with_a_null_message` | `T.test_interrupt_before_stop_and_null_stop_have_empty_turn_replies` | A null Stop message is an empty TurnEnd, one `done` line. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_resume_keeps_the_session_and_reports_source_resume` | `T.test_resume_keeps_native_id_cwd_home_model_effort_dirs_network_and_commands` | Same native session and file, source `resume`, the saved launch policy. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_resume_of_an_unknown_session_exits_1` | `T.test_resume_missing_binding_native_fifo_and_malformed_record_fail_before_spawn` | Refused before spawn (missing binding or native file), not by a native diagnostic. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_untrusted_or_modified_hooks_show_the_review_and_wait` | `T.test_startup_dialog_screens_are_driver_blocked_events`; `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive`; `K.test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed` | The review screen is a Blocked event; an invalid or untrusted catalog never starts the native tui. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_hooks_off_run_none_and_need_no_review` | `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | A hooks-off catalog prevents the start (the old worker preflight). Running the fake without hooks was fixture-only. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_permission_and_sub_agent_steps` | `T.test_start_permission_reply_send_and_separate_process_attach`; `T.test_root_child_cwd_and_rebinding_filters_publish_only_valid_hooks` | A root PermissionRequest is Blocked; a child's payloads publish nothing and never rebind. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_interrupt_ends_the_turn_without_stop` | `T.test_interrupt_completes_without_stop_and_replays_previous_reply_on_attach`; `T.test_interrupt_before_stop_and_null_stop_have_empty_turn_replies` | An interrupt ends a turn without Stop; the previous root reply survives, live and replayed to another process. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_a_users_own_hook_runs_when_trusted_and_needs_review_when_not` | `K.test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed` | A trusted or managed user hook runs; an untrusted or modified one prevents the start; a disabled one is allowed. No user config written. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_logs_argv_and_selected_env` | `T.test_start_permission_reply_send_and_separate_process_attach`; `T.test_explicit_operator_environment_is_forwarded_and_not_written_to_config` | Native argv, cwd, HOME settings, the selected environment and explicit overrides. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_colored_unless_no_color_set_and_not_0` | `T.test_explicit_no_color_controls_interactive_rendering` | Unset, empty, 1 and 0 forwarded and rendered as before. |
| `tests.integration.core.fake_codex_integration_test.Interactive.test_hooks_run_in_the_session_cwd_and_their_results_are_logged` | `T.test_start_permission_reply_send_and_separate_process_attach` | The hook subprocess's cwd, stdout, exit and payload logged; hooks silent, exit 0. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_hooks_list_after_initialize_with_notifications_interleaved` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | initialize, initialized, hooks/list with interleaved notifications and typed ids; event rows, timeouts, exact trusted hashes. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_state_trusts_the_listed_hash_only` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup`; `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | Only catalog-returned hashes are trusted; disabled, malformed-hash and foreign-command rows prevent the start. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_hooks_off_list_nothing` | `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | An empty catalog prevents the start. The old fake's `--enable`/`--disable` ordering was fixture-only. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_errors_for_each_cwd` | `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | Catalog errors prevent the start. One launch has one cwd; the multi-cwd list has no caller. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_a_users_own_hook_with_its_trust_status` | `K.test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed` | The user hook's trust status gates the start; user hooks never enabled or rewritten. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_only_config_and_feature_options` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_requests_before_initialize_and_unknown_methods_fail` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | The client sends initialize first and only its documented methods. Rejecting arbitrary requests was fixture-only. |
| `tests.integration.core.fake_codex_integration_test.AppServer.test_logs_argv_and_each_message` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | The app-server's argv and every message logged and checked; the probe reaped before the tui starts. |

## tests.integration.core.live_tmux_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.live_tmux_integration_test.Env.test_env_is_built_from_scratch` | `L.Env.test_env_is_built_from_scratch` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Env.test_tmpdir_and_socket_reach_one_server` | `L.Env.test_tmpdir_and_socket_reach_one_server` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Env.test_inside_runs_a_process_in_the_session_pane` | `L.Env.test_inside_runs_a_process_in_the_session_pane` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Client.test_attach_shows_the_session_in_a_control_mode_client` | `L.Client.test_attach_shows_the_session_in_a_control_mode_client` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Readers.test_option_pane_of_panes_events` | `L.Readers.test_option_pane_of_panes_events` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Cleanup.test_cleanups_kill_the_server_and_client_and_remove_the_root` | `L.Cleanup.test_cleanups_kill_the_server_and_client_and_remove_the_root` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Grid.test_assert_grid_on_a_hand_built_window` | `L.Grid.test_assert_grid_on_a_hand_built_window` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Wait.test_returns_the_truthy_value` | `L.Wait.test_returns_the_truthy_value` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Wait.test_a_failed_assertion_counts_as_falsy` | `L.Wait.test_a_failed_assertion_counts_as_falsy` | Complete. |
| `tests.integration.core.live_tmux_integration_test.Wait.test_times_out_with_what_and_the_last_value` | `L.Wait.test_times_out_with_what_and_the_last_value` | Complete. |

## tests.integration.core.resume_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.resume_integration_test.NeedsInput.test_the_answer_resumes_the_session` | `CLI.NeedsInput.test_the_answer_resumes_the_session` | Complete. |
| `tests.integration.core.resume_integration_test.RolePicks.test_the_role_picks_and_a_resume_keeps_the_session` | `CLI.RolePicks.test_the_role_picks_and_a_resume_keeps_the_session`; `CR.RolePicks.test_the_role_picks_and_its_resume_prompt_too` | The run, its pick as progress, the resume in the same session, no missing mark, out (CLI); the task index in the prompt, no task's steps, the resume prompt (CR). |
| `tests.integration.core.resume_integration_test.Draft.test_a_resume_keeps_the_draft_until_the_deliverable_replaces_it` | `to port` | Only `UR.Start.test_a_resume_keeps_the_draft_its_session_wrote_to_out` keeps a draft at out through a resume; parts: the same deliverable file named on resume (`CR.Draft.test_a_new_run_and_its_resume_name_the_same_deliverable_file`), a killed session resumes (`DCL.RunTest.test_a_session_killed_outside_the_driver_ends_with_its_signal_and_resumes`). Port into `CLI`: the fake writes a draft to out and hangs; killing it exits 3 with `the client exited -9` and leaves the draft; the resume's deliverable replaces it. The resumed fake reading the draft (old read step) is gone. |

## tests.integration.core.team_dirs_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.team_dirs_integration_test.TeamDirs.test_a_dry_run` | `DTD.TeamDirs.test_a_dry_run` | Changed on purpose (TASK-354, PR #180): the tasks dir is a read rule, never an added dir. |
| `tests.integration.core.team_dirs_integration_test.TeamDirs.test_list` | `DTD.TeamDirs.test_list` | Complete. |
| `tests.integration.core.team_dirs_integration_test.TeamDirs.test_a_headless_run_reads_its_task_from_its_tasks_dir` | `DTD.TeamDirs.test_a_run_in_headless_mode_reads_its_task_from_its_tasks_dir`; `CTD.TeamDirs.test_the_prompt_leads_to_its_task_file_in_its_tasks_dir` | The run, its tasks dir named in the prompt and a `Read` rule, no workdir copy, out. The fake reading the task file is gone with its read step. |
| `tests.integration.core.team_dirs_integration_test.CodexTeamDirs.test_a_codex_run_and_its_resume_add_its_tasks_dir` | `DTD.CodexTeamDirs.test_a_codex_run_and_its_resume_read_its_tasks_dir_unadded`; `H.test_add_dir_order_survives_resume_and_the_tasks_dir_is_read_unadded` | Inverted on purpose (TASK-354): run and resume read the tasks dir unadded, named in the prompt (DTD); add-dir order replayed on resume (H). |
| `tests.integration.core.team_dirs_integration_test.CodexTeamDirs.test_a_read_dir_holding_the_team_dir_is_refused` | `DTD.CodexTeamDirs.test_a_read_dir_holding_the_team_dir_runs_unadded`; `DTD.CodexTeamDirs.test_a_write_dir_holding_the_team_dir_is_refused` | Inverted on purpose (TASK-354): a read dir is read-only, so it runs, unadded; a write dir keeps the refusal. |

## Not ported

### (a) TASK-332 router --tui

### (b) TASK-356 layers

- `tests.integration.core.drive_tui_integration_test.TuiRun.test_detach_events_default`
  - Behavior: a tui role run detached from inside a manager's tmux session, given no events path, writes its turn-end lines and then one `<role>-<sid8>-drive outcome done` line to that manager's events file (`~/.agent-pm/managers/<manager>/events`); its result is done.
  - New code lacks: `delegate/cli.py --detach` refuses to run without `--events FILE` (PR #188), and nothing finds the caller's manager or its events file.
  - Requirement: a role run detached from a manager's session (for example by `crew run`) writes its turn-end and outcome lines to that manager's events file without its caller naming the path; the manager passes the path down.
- `tests.integration.core.extension_client_integration_test.ExtensionWorkerIntegration.test_client_flag_starts_its_worker_and_a_restart_waits_for_the_old_one`
  - Behavior: `crew start --client <runtime>` starts that runtime's worker in its cwd, without the Adapter's session marker in its env, records it and enters a roster resume argv with `--client <runtime> --resume <sid>`; `crew restart` starts the resumed worker only after the old worker's process has exited.
  - New code lacks: `crew restart` is `api.close` (tmux `kill-session`) then a start; nothing waits for the old process, so a runtime that outlives SIGHUP runs beside its resumed session.
  - Requirement: `crew restart <name>`, and any resume of a worker whose process may still run, starts the resumed process only after the old one has exited.

### (c) No ticket

### Deleted

- `tests.integration.core.fake_claude_integration_test.Reports.test_command_from_the_prompts_parameters`, `Reports.test_a_value_holding_a_backtick_is_read_as_compose_writes_it`, `Reports.test_command_serves_later_turns`, `Reports.test_report_step_without_a_report_line_fails`: the fake's `report.py` command. TASK-315 FR-4 removed the report command (PR #167: "`Reports` tests the report command FR-4 removes"; PR #166: `report.py` not ported, PRD › 现有代码去向); progress marks and the outcome block are in replies (`CLI.NewRun.test_done`).
- `tests.integration.core.fake_claude_integration_test.Files.test_a_failing_write_or_read_goes_on`: PR #166 dropped the fake's write and read steps (they served only the out-draft case); PR #171 re-added only `write`, for `deliverable_file`, without going on after a failure, and no read step ("the new fake has no read step").
- `tests.integration.core.fake_claude_integration_test.PrintMode.test_stop_runs_once_before_the_result`: headless runs install no hooks (PR #172: "Headless installs no hooks"); `F.PrintMode.test_valid_steps` asserts print mode fires none.
- `tests.integration.core.fake_claude_integration_test.PrintMode.test_a_read_is_an_assistant_line`: the read step (PR #166: it served only the out-draft case; PR #171: "the new fake has no read step").
- `tests.integration.core.fake_codex_integration_test.Interactive.test_unknown_option_or_resume_without_an_id_exits_2`: the old fake's parser; callers give the Driver a Spec, not CLI arguments (TASK-320, PR #181: "Legacy fake parser; the new entry point accepts Spec").
- `tests.integration.core.fake_codex_integration_test.AppServer.test_only_config_and_feature_options`: the old fake's app-server parser (TASK-320, PR #181: "there is no public raw-argv catalog entry point"); K checks the generated app-server argv.
