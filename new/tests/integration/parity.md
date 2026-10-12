# Integration parity with main

Baseline: `origin/main` at `f1d0c595f03063034f77944332615dca2c082cfa`, `tests/integration/`: 214 test methods (core 101, orchestrator 76, tmux 37), one row each: `| Exact old identity | Exact new identity | Coverage and remaining assertions |`. TASK-357 built it from each old test's body (`git show origin/main:<path>`; old code never imported or run) and the tests in `new/tests/integration/`, merging the Codex parity list (TASK-318, TASK-320), the crew parity list (TASK-321) and the parity lists in merged `re-architecture` PR bodies (#166, #167, #171, #172, #173, #181, #182, #186, #187, #188); every cited new identity exists by exact name.

The new identity is one of:

- new tests (aliases below) asserting the old behavior; the row names what changed on purpose and what they leave out;
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
| OGH | `new.tests.integration.orchestrator.fake_gh_integration_test` |
| OFL | `new.tests.integration.orchestrator.fake_linear_integration_test` |
| OPE | `new.tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer` |
| OP | `new.tests.integration.orchestrator.promote_integration_test` |
| ORP | `new.tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm` |
| ORT | `new.tests.integration.orchestrator.router_integration_test.Router` |
| OTU | `new.tests.integration.orchestrator.run_tui_integration_test.Tui` |
| DTH | `new.tests.integration.driver.tmux_host_integration_test` |
| DCT | `new.tests.integration.driver.claude_tui_integration_test` |
| UCR | `new.tests.unit.tmux.crew_test` |
| URO | `new.tests.unit.roster_test` |
| UDA | `new.tests.unit.driver.api_test` |

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
| `tests.integration.core.extension_client_integration_test.ExtensionWorkerIntegration.test_its_session_marker_makes_it_the_default` | `C.RuntimeTest.test_a_worker_started_in_a_codex_worker_defaults_to_codex`; `C.RuntimeTest.test_env_scrubbed_both_ways` | Replaced on purpose (TASK-321, PR #187): the default runtime is the caller's own Driver tui session's, not an Adapter's env marker, for any runtime (`agent_pm.manager.crew` `_runtime`); session markers stay out of the worker's env. |

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
| `tests.integration.core.fake_claude_integration_test.Gate.test_interactive_parks_until_the_file_appears` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Gate.test_print_mode_parks_before_anything_is_said` | `F.Gate.test_a_session_waits_for_its_gate_after_its_log_line` | Parks after its log line with no transcript until the file appears, then runs; also a resume. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_no_hook_or_report_before_the_gate` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Gate.test_an_existing_file_does_not_hold` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Gate.test_no_gate_or_a_null_one_does_not_hold` | `F.Interactive.test_turns_from_stdin` | No gate: every F test without one runs at once. An explicit null is the scenario default (`fake_claude.DEFAULTS`), the same path. |
| `tests.integration.core.fake_claude_integration_test.Gate.test_a_resume_without_its_transcript_fails_without_waiting` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.core.fake_claude_integration_test.Gate.test_orphaned_at_the_gate_exits_1` | `not ported: deleted` ([entry](#deleted)) | |
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
| `tests.integration.core.resume_integration_test.Draft.test_a_resume_keeps_the_draft_until_the_deliverable_replaces_it` | `CLI.Draft.test_a_resume_keeps_the_draft_until_the_deliverable_replaces_it` | The fake writes a draft to out and hangs; killing it exits 3 with `the client exited -9` and leaves the draft; the resume's deliverable replaces it. Changed: the resumed fake read the draft first (the read step is gone); now the draft is still at out once the resume recorded its input, its client held at a gate. |

## tests.integration.core.team_dirs_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.team_dirs_integration_test.TeamDirs.test_a_dry_run` | `DTD.TeamDirs.test_a_dry_run` | Changed on purpose (TASK-354, PR #180): the tasks dir is a read rule, never an added dir. |
| `tests.integration.core.team_dirs_integration_test.TeamDirs.test_list` | `DTD.TeamDirs.test_list` | Complete. |
| `tests.integration.core.team_dirs_integration_test.TeamDirs.test_a_headless_run_reads_its_task_from_its_tasks_dir` | `DTD.TeamDirs.test_a_run_in_headless_mode_reads_its_task_from_its_tasks_dir`; `CTD.TeamDirs.test_the_prompt_leads_to_its_task_file_in_its_tasks_dir` | The run, its tasks dir named in the prompt and a `Read` rule, no workdir copy, out. The fake reading the task file is gone with its read step. |
| `tests.integration.core.team_dirs_integration_test.CodexTeamDirs.test_a_codex_run_and_its_resume_add_its_tasks_dir` | `DTD.CodexTeamDirs.test_a_codex_run_and_its_resume_read_its_tasks_dir_unadded`; `H.test_add_dir_order_survives_resume_and_the_tasks_dir_is_read_unadded` | Inverted on purpose (TASK-354): run and resume read the tasks dir unadded, named in the prompt (DTD); add-dir order replayed on resume (H). |
| `tests.integration.core.team_dirs_integration_test.CodexTeamDirs.test_a_read_dir_holding_the_team_dir_is_refused` | `DTD.CodexTeamDirs.test_a_read_dir_holding_the_team_dir_runs_unadded`; `DTD.CodexTeamDirs.test_a_write_dir_holding_the_team_dir_is_refused` | Inverted on purpose (TASK-354): a read dir is read-only, so it runs, unadded; a write dir keeps the refusal. |

## tests.integration.orchestrator.fake_gh_integration_test

The new module is the old one reformatted; every test body is unchanged (PR #182).

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_install_writes_an_executable_gh` | `OGH.Shim.test_install_writes_an_executable_gh` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_api_prints_the_scenarios_object` | `OGH.Shim.test_api_prints_the_scenarios_object` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_an_unlisted_repo_is_a_404_on_stderr` | `OGH.Shim.test_an_unlisted_repo_is_a_404_on_stderr` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_any_other_argv_is_a_usage_error` | `OGH.Shim.test_any_other_argv_is_a_usage_error` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_a_malformed_scenario_is_an_error` | `OGH.Shim.test_a_malformed_scenario_is_an_error` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_a_missing_or_unreadable_scenario_is_an_error` | `OGH.Shim.test_a_missing_or_unreadable_scenario_is_an_error` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Shim.test_an_unset_scenario_is_an_error` | `OGH.Shim.test_an_unset_scenario_is_an_error` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_a_dir_is_a_sorted_list_of_its_direct_entries` | `OGH.Contents.test_a_dir_is_a_sorted_list_of_its_direct_entries` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_a_dir_with_raw_is_the_same_list` | `OGH.Contents.test_a_dir_with_raw_is_the_same_list` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_raw_prints_the_file_verbatim` | `OGH.Contents.test_raw_prints_the_file_verbatim` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_a_file_without_the_header_is_its_entry` | `OGH.Contents.test_a_file_without_the_header_is_its_entry` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_a_quoted_path_and_ref_are_decoded` | `OGH.Contents.test_a_quoted_path_and_ref_are_decoded` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_repos_is_unchanged_beside_contents` | `OGH.Contents.test_repos_is_unchanged_beside_contents` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_a_missing_path_branch_or_repo_is_a_404_on_stderr` | `OGH.Contents.test_a_missing_path_branch_or_repo_is_a_404_on_stderr` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_no_contents_key_is_a_404_for_every_path` | `OGH.Contents.test_no_contents_key_is_a_404_for_every_path` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_any_other_argv_is_a_usage_error` | `OGH.Contents.test_any_other_argv_is_a_usage_error` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_a_bad_contents_shape_or_another_key_is_an_error` | `OGH.Contents.test_a_bad_contents_shape_or_another_key_is_an_error` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Contents.test_repos_stays_required` | `OGH.Contents.test_repos_stays_required` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_bare_makes_the_repo_at_owner_name_dot_git` | `OGH.Bare.test_bare_makes_the_repo_at_owner_name_dot_git` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_each_branch_is_a_commit` | `OGH.Bare.test_each_branch_is_a_commit` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_no_branches_is_an_empty_repo` | `OGH.Bare.test_no_branches_is_an_empty_repo` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_gitconfig_maps_github_to_the_root` | `OGH.Bare.test_gitconfig_maps_github_to_the_root` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_ls_remote_through_the_gitconfig_lists_the_seeded_branch_and_only_it` | `OGH.Bare.test_ls_remote_through_the_gitconfig_lists_the_seeded_branch_and_only_it` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_ls_remote_of_a_repo_without_the_branch_is_empty` | `OGH.Bare.test_ls_remote_of_a_repo_without_the_branch_is_empty` | Complete. |
| `tests.integration.orchestrator.fake_gh_integration_test.Bare.test_targets_ls_remote_argv_works` | `OGH.Bare.test_targets_ls_remote_argv_works` | Complete. |

## tests.integration.orchestrator.fake_linear_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_every_query_constant_has_an_op` | `OFL.Table.test_every_query_constant_has_an_op` | Complete; ops are named by the module's short name (package `agent_pm_linear`). |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_two_names_for_one_text_are_refused` | `OFL.Table.test_two_names_for_one_text_are_refused` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_fail_refuses_a_misuse` | `OFL.Table.test_fail_refuses_a_misuse` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_seeding_refuses_a_used_or_malformed_issue_id` | `OFL.Table.test_seeding_refuses_a_used_or_malformed_issue_id` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_backlog_is_a_state_outside_the_config` | `OFL.Table.test_backlog_is_a_state_outside_the_config` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_a_backlog_issue_moves_to_todo_and_back` | `OFL.Table.test_a_backlog_issue_moves_to_todo_and_back` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_relate_records_the_issues_ids_and_refuses_an_unknown_one` | `OFL.Table.test_relate_records_the_issues_ids_and_refuses_an_unknown_one` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Table.test_a_time_without_an_offset_is_utc` | `OFL.Table.test_a_time_without_an_offset_is_utc` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_projection_keeps_only_the_selection` | `OFL.Served.test_projection_keeps_only_the_selection` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_a_selected_field_the_model_lacks_is_an_error` | `OFL.Served.test_a_selected_field_the_model_lacks_is_an_error` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_is_me_follows_the_key` | `OFL.Served.test_is_me_follows_the_key`; `OFL.Served.test_a_session_comment_is_created_then_updated_by_its_session_id` | `isMe` per key in `issues.Q_ISSUE` and `linear.Q_COMMENT`; another account's update of the harness's comment refused. Changed on purpose (FR-55, PR #182): a session comment is found by its sid-derived client id, not by prefix (`sessions.Q_FIND` is gone); `sessions.write` creating then updating its own comment: the second test. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_a_move_is_history_by_the_caller` | `OFL.Served.test_a_move_is_history_by_the_caller` | Complete; the history read through `promote.Q_DETAIL`. `router.Q_HISTORY`: `OFL.Served.test_the_board_reads`. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_each_failure_kind_as_the_caller_sees_it` | `OFL.Served.test_each_failure_kind_as_the_caller_sees_it` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_account_and_times_scope_a_failure` | `OFL.Served.test_account_and_times_scope_a_failure` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_an_unknown_query_and_a_bad_key_are_errors_and_logged` | `OFL.Served.test_an_unknown_query_and_a_bad_key_are_errors_and_logged` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_only_post_graphql_is_served` | `OFL.Served.test_only_post_graphql_is_served` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_a_bad_call_is_a_logged_error` | `OFL.Served.test_a_bad_call_is_a_logged_error` | Complete; the `TypeError` case through `promote.Q_HANDOFF`, not `prune.Q_FINISHED`. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_a_bad_body_is_a_logged_400` | `OFL.Served.test_a_bad_body_is_a_logged_400` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_finished_issues_page` | `OFL.Served.test_finished_issues_page` | Complete, plus the archived flag. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_issue_create_takes_the_next_number_and_refuses_a_used_id` | `OFL.Served.test_issue_create_takes_the_next_number_and_refuses_a_used_id` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_the_board_reads` | `OFL.Served.test_the_board_reads` | Complete, plus the recheck's assignee and project (FR-56) and `router.Q_HISTORY`. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_the_team_lists_backlog_beside_the_config_states` | `OFL.Served.test_the_team_lists_backlog_beside_the_config_states` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_a_backlog_issue_is_not_in_a_todo_list` | `OFL.Served.test_a_backlog_issue_is_not_in_a_todo_list` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_issue_create_takes_the_backlog_state` | `OFL.Served.test_issue_create_takes_the_backlog_state` | Complete. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_relate_shows_in_relations_and_inverse_relations` | `OFL.Served.test_relate_shows_in_relations_and_inverse_relations`; `OFL.Served.test_the_board_reads`; `ORP.test_a3_blocked_until_its_blocker_is_done` | Relations and inverse relations in `issues.Q_ISSUE`. `router.Q_RELATIONS` and `router.blockers`: the board reads (a relation made by `promote.M_RELATE`, the store `relate` seeds) and a3 (seeded by `relate`; the router logs `blocked`). Not asserted: a Backlog blocker read back with state type `backlog` (`router.blockers` by state type: unit `router_test` `Blockers`). |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_the_write_back_ops` | `OFL.Served.test_the_write_back_ops`; `OFL.Served.test_finished_issues_page` | Attach once, subscribe, title, `writeback.Q_ID` and `issues.read_issue` on an archived issue, unarchive. Changed: the test sets the archived flag itself (PR #182); `prune.M_ARCHIVE` and an archived issue left out of a list: the finished page. Not asserted: `router.q_issues` listing the issue again after the unarchive. |
| `tests.integration.orchestrator.fake_linear_integration_test.Served.test_the_promote_and_prune_reads` | `OFL.Served.test_the_promote_and_prune_reads` | Complete. |

## tests.integration.orchestrator.pm_engineer_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b1_prd_to_an_engineer_issue` | `OPE.test_b1_prd_to_an_engineer_issue` | Complete. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b2_rework_starts_a_new_session` | `OPE.test_b2_rework_starts_a_new_session` | Complete. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b3_a_failed_run_is_not_promoted` | `OPE.test_b3_a_failed_run_is_not_promoted` | Complete. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b4_a_linear_outage_relating_the_child_is_retried` | `OPE.test_b4_a_linear_outage_relating_the_child_is_retried` | Complete. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b5_a_run_without_an_outcome_is_resumed` | `OPE.test_b5_a_run_without_an_outcome_is_resumed` | Complete. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b6_an_attended_run_that_gives_up_is_resumed_headless` | `not ported: (a)` ([entry](#a-task-332-router---tui)) | run.py's side: `OTU.test_old_tuis_of_this_role_and_issue_close_and_the_tui_shows_beside_split_from` (an attended run, FR-43 closing the old TUI); a headless resume: `OPE.test_b5_a_run_without_an_outcome_is_resumed`. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b7_a_lost_transcript_starts_a_new_session` | `OPE.test_b7_a_lost_transcript_starts_a_new_session` | Changed on purpose (FR-47, PR #186): the recover reason is `not resumable` (the Driver's `session_info`), not `no transcript`. |
| `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b8_a_managed_attended_run_is_recovered_from_the_roster` | `not ported: (a)` ([entry](#a-task-332-router---tui)) | |

## tests.integration.orchestrator.promote_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.orchestrator.promote_integration_test.Promote.test_handoff_to_a_child_issue` | `OP.Promote.test_handoff_to_a_child_issue` | Complete. |
| `tests.integration.orchestrator.promote_integration_test.Promote.test_a_failed_move_is_retried_without_a_second_child` | `OP.Promote.test_a_failed_move_is_retried_without_a_second_child` | Complete. |
| `tests.integration.orchestrator.promote_integration_test.Promote.test_a_linear_outage_moving_the_source_is_retried_without_a_second_child` | `OP.Promote.test_a_linear_outage_moving_the_source_is_retried_without_a_second_child` | Complete. |
| `tests.integration.orchestrator.promote_integration_test.Promote.test_a_linear_outage_reading_the_handoffs_is_retried` | `OP.Promote.test_a_linear_outage_reading_the_handoffs_is_retried` | Complete. |
| `tests.integration.orchestrator.promote_integration_test.Prune.test_prune_after_done` | `OP.Prune.test_prune_after_done` | Complete; the TUI session is closed through `api.close` on the test's private tmux server. |

## tests.integration.orchestrator.researcher_pm_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm.test_a1_research_to_a_pm_issue` | `ORP.test_a1_research_to_a_pm_issue` | Complete. |
| `tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm.test_a2_light_research` | `ORP.test_a2_light_research` | Complete. |
| `tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm.test_a3_blocked_until_its_blocker_is_done` | `ORP.test_a3_blocked_until_its_blocker_is_done` | Complete. |
| `tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm.test_a4_an_answer_starts_a_new_session` | `ORP.test_a4_an_answer_starts_a_new_session` | Complete. |
| `tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm.test_a5_a_failed_run_is_not_promoted` | `ORP.test_a5_a_failed_run_is_not_promoted` | Complete. |
| `tests.integration.orchestrator.researcher_pm_integration_test.ResearcherToPm.test_a6_capped_after_four_failed_runs` | `ORP.test_a6_capped_after_four_failed_runs` | Complete. |

## tests.integration.orchestrator.router_integration_test

Changed on purpose: comments are posted and found by client id (`linear.M_COMMENT_ID`, `linear.Q_COMMENT`; FR-55); `runs.jsonl` rows carry `runtime` (FR-46).

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.orchestrator.router_integration_test.Router.test_claim_to_in_review` | `ORT.test_claim_to_in_review` | Complete; the prompt is read from `run.jsonl`'s `input` record, not the transcript. |
| `tests.integration.orchestrator.router_integration_test.Router.test_a_crash_mid_run_resumes_the_session` | `ORT.test_a_crash_mid_run_resumes_the_session` | Complete. |
| `tests.integration.orchestrator.router_integration_test.Router.test_a_linear_failure_mid_write_back_resumes_the_session` | `ORT.test_a_linear_failure_mid_write_back_resumes_the_session` | Complete. |
| `tests.integration.orchestrator.router_integration_test.Router.test_a_linear_outage_stops_the_tick` | `ORT.test_a_linear_outage_stops_the_tick` | Complete. |
| `tests.integration.orchestrator.router_integration_test.Router.test_a_linear_outage_stops_issue_and_the_rerun_claims_it` | `ORT.test_a_linear_outage_stops_issue_and_the_rerun_claims_it` | Complete. |

## tests.integration.tmux.crew_integration_test

C runs crew's CLI as subprocesses on a private tmux server, the real Adapters over the fakes (TASK-321, PR #187). Placement (grid, settle, retile, splits) moved into the Driver's `show` (DTH); the old tmux options `@sid @cwd @client @exe @flags @env @events` are the worker record (`~/.agent-pm/workers/<sid>/worker.json`). Role entries are written by the tests: nothing records a detached role run yet ([(b)](#b-task-356-layers)).

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.tmux.crew_integration_test.Lifecycle.test_start_state_events` | `C.ClaudeLifecycleTest.test_start_done_next_event_reply_send_done_reply`; `DCT.TurnsTest.test_turns_their_lines_state_and_reply_send_and_the_border_no_pane_opened` | Plain argv (`--session-id`, `--name`, hook `--settings`, prompt; no role-run policy), worker record, roster resume argv, done, `next-event`, `tmux_host.py send`, second done, `reply`. Border format: DCT. |
| `tests.integration.tmux.crew_integration_test.Lifecycle.test_blocked` | `C.ClaudeLifecycleTest.test_blocked_on_a_hook` | Complete, plus the early watch's `waits on a dialog` line. |
| `tests.integration.tmux.crew_integration_test.Lifecycle.test_dead_restart` | `C.ClaudeLifecycleTest.test_dead_then_restart_resumes_the_sid_in_a_new_pane` | Dead event, its content, `--resume <sid>`, a turn after it. Changed on purpose (PR #187): a new pane, not the same pane and cell. |
| `tests.integration.tmux.crew_integration_test.Lifecycle.test_early_death` | `C.ClaudeLifecycleTest.test_an_early_death_exits_1_with_its_last_lines_its_session_kept` | Complete. |
| `tests.integration.tmux.crew_integration_test.Grid.test_columns_retile` | `DTH.GridTest.test_columns_retile` | The Driver's grid. |
| `tests.integration.tmux.crew_integration_test.Grid.test_name_order` | `DTH.GridTest.test_name_order` | The Driver's grid. |
| `tests.integration.tmux.crew_integration_test.Grid.test_sub_workers` | `DTH.GridTest.test_sub_sessions` | The Driver's grid. |
| `tests.integration.tmux.crew_integration_test.Settle.test_grid_up_settles` | `DTH.SettleTest.test_grid_up_settles` | The Driver's tile. |
| `tests.integration.tmux.crew_integration_test.Settle.test_concurrent_tiles_settle` | `DTH.SettleTest.test_concurrent_tiles_settle` | The Driver's tile. |
| `tests.integration.tmux.crew_integration_test.Settle.test_busy_mutes_the_hooks` | `DTH.SettleTest.test_busy_mutes_the_hooks` | The Driver's tile. |
| `tests.integration.tmux.crew_integration_test.Settle.test_breaker_stops_a_loop` | `DTH.SettleTest.test_breaker_stops_a_loop` | The Driver's tile. |
| `tests.integration.tmux.crew_integration_test.Retile.test_open_close_retiles_on_an_open_only` | `DTH.RetileTest.test_open_close_retiles_on_an_open_only` | The Driver's retile. |
| `tests.integration.tmux.crew_integration_test.Retile.test_open_close_retiles_on_a_stop` | `DTH.RetileTest.test_open_close_retiles_on_a_close`; `UCR.StopTest.test_a_worker` | `crew stop` closes through `api.close` (UCR), which DTH re-tiles on. |
| `tests.integration.tmux.crew_integration_test.Retile.test_off_places_only_till_tile` | `DTH.RetileTest.test_off_places_only_till_tile` | The Driver's placement. |
| `tests.integration.tmux.crew_integration_test.Retile.test_a_switch_to_open_close_leaves_only_its_hooks` | `DTH.RetileTest.test_a_switch_to_open_close_leaves_only_its_hooks` | The Driver's retile. |
| `tests.integration.tmux.crew_integration_test.Retile.test_restart_keeps_a_dragged_layout` | `not ported: deleted` ([entry](#deleted)) | |
| `tests.integration.tmux.crew_integration_test.Managers.test_own_session_and_worker_in_worker` | `C.ManagersTest.test_own_session_and_worker_in_worker` | Complete; `@events` read from the worker record. |
| `tests.integration.tmux.crew_integration_test.Managers.test_manager_flag` | `C.ManagersTest.test_manager_flag` | Complete. |
| `tests.integration.tmux.crew_integration_test.Managers.test_no_pane_needs_a_manager` | `C.ManagersTest.test_no_pane_needs_a_manager` | Complete. |
| `tests.integration.tmux.crew_integration_test.Splits.test_split_outside_grid` | `DTH.SplitsTest.test_split_outside_grid`; `UCR.MainTest.test_start_split_from_below` | Placement: DTH; crew passing `--split`/`--split-from` to `show`: UCR. |
| `tests.integration.tmux.crew_integration_test.Roster.test_second_manager_after_release` | `C.RosterTest.test_second_manager_after_release` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_second_manager_after_the_holder_ends` | `C.RosterTest.test_second_manager_after_the_holder_ends` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_concurrent_attaches_one_holds` | `C.RosterTest.test_concurrent_attaches_one_holds` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_killed_worker_gone_then_recovered` | `C.RosterTest.test_killed_worker_gone_then_recovered` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_stop_all_stops_what_mgr_opened_and_only_that` | `C.RosterTest.test_stop_all_stops_what_mgr_opened_and_only_that` | Workers, a sub-manager's worker and a role entry's two sessions stopped; another manager's session kept. The role entry is written by the test; the role run's TUI in the grid: `DD.Tui.test_from_the_manager_shown_in_its_grid_turn_ends_then_one_outcome_done`. |
| `tests.integration.tmux.crew_integration_test.Roster.test_killed_role_run_gone_then_recovered` | `not ported: (b)` ([entry](#b-task-356-layers)) | Role recovery argv and its run: `URO.RosterTest.test_recovery_forms_each_a_copy`, `UCR.ResumeTest.test_each_gone_entry_runs_its_recovery_argv_in_its_cwd_unlocked_no_shell_in_table_order`. |
| `tests.integration.tmux.crew_integration_test.Roster.test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one` | `C.RosterTest.test_attach_resume_brings_back_a_killed_worker_not_a_stopped_one` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_stop_a_role_run_by_its_tui_session` | `C.RosterTest.test_stop_a_role_run_by_its_tui_session` | Complete over a role entry the test writes. |
| `tests.integration.tmux.crew_integration_test.Roster.test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume` | `C.RosterTest.test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume`; `DD.Headless.test_needs_input_one_outcome_line_the_driver_session_gone` | `waiting` from the run's `run.jsonl` result, left alone by `attach --resume`; the entry and that result are written by the test. A detached headless run ending on `needs_input`, its outcome line, its Driver session gone: DD. |
| `tests.integration.tmux.crew_integration_test.Roster.test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker` | `not ported: (b)` ([entry](#b-task-356-layers)) | Covered for workers: `C.RosterTest.test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker`. |
| `tests.integration.tmux.crew_integration_test.Roster.test_unshown_worker_reopened` | `C.RosterTest.test_unshown_worker_reopened`; `DTH.GridTest.test_an_unshown_session_reopens_in_the_grid` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_take_over_resumes_from_the_cursor` | `C.RosterTest.test_take_over_resumes_from_the_cursor` | Complete. |
| `tests.integration.tmux.crew_integration_test.Roster.test_rotation_keeps_the_unhandled_lines` | `C.RosterTest.test_rotation_keeps_the_unhandled_lines` | Complete. |

## tests.integration.tmux.codex_worker_integration_test

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.tmux.codex_worker_integration_test.CodexLifecycle.test_start_blocked_done_reply_follow_up` | `C.CodexLifecycleTest.test_start_blocked_done_reply_follow_up`; `T.test_start_permission_reply_send_and_separate_process_attach` | Plain argv (no `-s`, approval or reviewer), `CODEX_HOME`, worker record, roster resume argv, blocked, done, `reply`, follow-up (C); silent hook subprocesses, the native binding, mode 0600 (T). The old `core-sessions/workers/<sid>.json` and `.reply`: the Adapter's binding. |
| `tests.integration.tmux.codex_worker_integration_test.CodexLifecycle.test_restart_and_recovery_from_a_claude_session_keep_the_native_session` | `C.CodexLifecycleTest.test_dead_then_restart_and_recovery_from_a_claude_session_resume_the_native_session`; `T.test_resume_keeps_native_id_cwd_home_model_effort_dirs_network_and_commands` | Restart, `attach --resume` with `CLAUDECODE` set, same native session and argv, `CLAUDECODE` dropped (C); hook source `resume`, saved settings (T). C restarts a dead worker; the old test restarted a live one, whose restart does not wait for the old process: [(b)](#b-task-356-layers) (`ExtensionWorkerIntegration` entry). |
| `tests.integration.tmux.codex_worker_integration_test.CodexSession.test_a_codex_managers_marker_and_injected_defaults_stay_out_of_its_workers` | `C.RuntimeTest.test_env_scrubbed_both_ways`; `UDA.PlainTest.test_a_plain_env_drops_every_adapters_identity_then_applies_the_launch_then_spec_env`; `T.test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested`; `H.test_inherited_session_markers_are_removed_and_headless_defaults_retained` | Codex and Claude identity dropped both ways, Codex defaults dropped inside Codex and kept outside, colors (C); restart and recovery take the same `api.start` env path (UDA); fresh, resumed and nested Codex starts (T); headless keeps its defaults (H). Changed on purpose (PR #187): a top-level Codex manager's workers default to the config's `runtime` (`C.RuntimeTest.test_a_top_level_manager_defaults_to_the_configs_runtime`), so C passes `--client codex`. |
| `tests.integration.tmux.codex_worker_integration_test.CodexSession.test_a_codex_workers_own_crew_start_defaults_to_codex` | `C.RuntimeTest.test_a_worker_started_in_a_codex_worker_defaults_to_codex`; `K.test_concurrent_independent_starts_serialize_the_same_home_probe` | From its own row's `runtime`: inner events file, distinct native session, no parent markers, `--client codex` in the entry (C); independent starts' bindings (K). |

## Not ported

### (a) TASK-332 router --tui

- `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b6_an_attended_run_that_gives_up_is_resumed_headless`
  - Behavior: `router.py --issue ID --tui --split-from <manager>` claims a pm issue and starts its run attended, its TUI session `<role>-<ID>-<sid8>` beside the manager; the agent ends its turns without an outcome until the run gives up, leaving the TUI open; the next headless claim resumes the same session, run.py first closes that TUI (`run tui-closed <session>`), the resumed run is done and promote makes the engineer issue.
  - New code lacks: `router.py` has no `--tui`, `--split`, `--split-from` or `--events` (FR-49, PR #186). run.py's attended side exists: `OTU.test_old_tuis_of_this_role_and_issue_close_and_the_tui_shows_beside_split_from`.
  - Requirement: `router.py --issue ID --tui` starts the claimed run attended, its TUI beside the caller or `--split-from`; a run that ends without an outcome is resumed headless on the same session by a later tick, which closes the attended TUI first, and the issue goes on to promote.
- `tests.integration.orchestrator.pm_engineer_integration_test.PmToEngineer.test_b8_a_managed_attended_run_is_recovered_from_the_roster`
  - Behavior: `router.py --issue ID --tui --events F --manager M` claims the issue attended and records a `pipeline` entry in M's roster (sid, the issue's workdir, note ID, its TUI session, `split_from` M, a resume command repeating that router call); after the human kills the TUI session, running the entry's resume resumes the same session attended (a `resume` row, `claude --resume <sid>`), the entry's opener is then M, and the resumed run is done.
  - New code lacks: router `--tui` (FR-49); nothing prints or records a pipeline Record (TASK-356: `crew run` records it, FR-17; the Record router `--tui` prints is TASK-332's, FR-28).
  - Requirement: `router.py --issue ID --tui` prints a pipeline Record (kind `pipeline`, workdir the issue's run dir, resume `router --issue ID --tui`) that `crew run` records; after the TUI session is killed, that resume resumes the same session attended and the resumed run writes back as usual.

### (b) TASK-356 layers

- `tests.integration.core.drive_tui_integration_test.TuiRun.test_detach_events_default`
  - Behavior: a tui role run detached from inside a manager's tmux session, given no events path, writes its turn-end lines and then one `<role>-<sid8>-drive outcome done` line to that manager's events file (`~/.agent-pm/managers/<manager>/events`); its result is done.
  - New code lacks: `delegate/cli.py --detach` refuses to run without `--events FILE` (PR #188), and nothing finds the caller's manager or its events file.
  - Requirement: a role run detached from a manager's session (for example by `crew run`) writes its turn-end and outcome lines to that manager's events file without its caller naming the path; the manager passes the path down.
- `tests.integration.core.extension_client_integration_test.ExtensionWorkerIntegration.test_client_flag_starts_its_worker_and_a_restart_waits_for_the_old_one`
  - Behavior: `crew start --client <runtime>` starts that runtime's worker in its cwd, without the Adapter's session marker in its env, records it and enters a roster resume argv with `--client <runtime> --resume <sid>`; `crew restart` starts the resumed worker only after the old worker's process has exited.
  - New code lacks: `crew restart` is `api.close` (tmux `kill-session`) then a start; nothing waits for the old process, so a runtime that outlives SIGHUP runs beside its resumed session.
  - Requirement: `crew restart <name>`, and any resume of a worker whose process may still run, starts the resumed process only after the old one has exited.
- `tests.integration.tmux.crew_integration_test.Roster.test_killed_role_run_gone_then_recovered`
  - Behavior: a tui role run detached from inside a manager (`drive.py --runner tui --detach`) is recorded in the manager's roster as a `role` entry (sid, cwd, TUI session, opener, resume command); once its Driver and TUI sessions are killed and the old Driver's outcome line is written, `crew attach` lists it `gone` with a resume line that resumes the sid with "Continue the unfinished task."; running that line brings back both sessions, the TUI in the manager's grid running `claude --resume <sid>`, and the entry is unchanged but `working`.
  - New code lacks: the Delegate's `--detach` records nothing and prints no Record (PR #188); there is no `crew run`; the roster takes as `resume` only crew's module or a `drive.py`/`router.py` path (`roster.SCRIPTS`), so a Delegate role run can be neither recorded nor resumed. Crew's role-entry handling is tested only on entries the tests write: `C.RosterTest.test_stop_all_stops_what_mgr_opened_and_only_that`, `C.RosterTest.test_stop_a_role_run_by_its_tui_session`, `C.RosterTest.test_a_role_run_ended_on_needs_input_is_waiting_and_left_alone_by_attach_resume`.
  - Requirement: `crew run <name> -- <Delegate CLI> --mode tui --detach …` records the role run's Record; after its sessions are killed, `crew attach` lists it gone with its resume command, and `crew resume <name>` brings back its Driver and TUI sessions on the same sid, the TUI in the manager's grid; the three C tests above start their role run through `crew run` instead of writing its entry.
- `tests.integration.tmux.crew_integration_test.Roster.test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker`
  - Behavior: with a worker, a stopped worker, a tui role run and a pipeline (a `router.py --tui --manager` stand-in) recorded, the tmux server dies; on a new server `crew attach --resume` resumes the pipeline, the role run and the worker (`resumed <name>: exit 0` each), each runtime resumes its own sid, the stopped worker stays gone and no entry is left `gone`.
  - New code lacks: recovery is tested for workers only (`C.RosterTest.test_a_lost_tmux_server_comes_back_by_attach_resume_but_a_stopped_worker`); no role-run or pipeline Record is recorded (the entry above; the pipeline's Record: the `test_b8` entry under [(a)](#a-task-332-router---tui)).
  - Requirement: after the tmux server is lost, `crew resume` of each recorded worker, role run and pipeline brings it back on its own sid by running its Record's resume; a stopped worker is not resumed; no entry is left `gone`.

### (c) No ticket

### Deleted

- `tests.integration.core.fake_claude_integration_test.Reports.test_command_from_the_prompts_parameters`, `Reports.test_a_value_holding_a_backtick_is_read_as_compose_writes_it`, `Reports.test_command_serves_later_turns`, `Reports.test_report_step_without_a_report_line_fails`: the fake's `report.py` command. TASK-315 FR-4 removed the report command (PR #167: "`Reports` tests the report command FR-4 removes"; PR #166: `report.py` not ported, PRD › 现有代码去向); progress marks and the outcome block are in replies (`CLI.NewRun.test_done`).
- `tests.integration.core.fake_claude_integration_test.Files.test_a_failing_write_or_read_goes_on`: PR #166 dropped the fake's write and read steps (they served only the out-draft case); PR #171 re-added only `write`, for `deliverable_file`, without going on after a failure, and no read step ("the new fake has no read step").
- `tests.integration.core.fake_claude_integration_test.PrintMode.test_stop_runs_once_before_the_result`: headless runs install no hooks (PR #172: "Headless installs no hooks"); `F.PrintMode.test_valid_steps` asserts print mode fires none.
- `tests.integration.core.fake_claude_integration_test.PrintMode.test_a_read_is_an_assistant_line`: the read step (PR #166: it served only the out-draft case; PR #171: "the new fake has no read step").
- `tests.integration.core.fake_claude_integration_test.Gate.test_interactive_parks_until_the_file_appears`, `Gate.test_no_hook_or_report_before_the_gate`, `Gate.test_an_existing_file_does_not_hold`, `Gate.test_a_resume_without_its_transcript_fails_without_waiting`, `Gate.test_orphaned_at_the_gate_exits_1`: the gate on an interactive run. The gate is orchestrator-only (PR #172: "the gate is orchestrator-only"), and no new test or flow gates an interactive run: `new/tests/integration/orchestrator/segments.py` gates router.py's headless runs (router `--tui`: TASK-332). Print mode's gate: `F.Gate.test_a_session_waits_for_its_gate_after_its_log_line`; the report half went with `report.py` (`Reports.*` above).
- `tests.integration.core.fake_codex_integration_test.Interactive.test_unknown_option_or_resume_without_an_id_exits_2`: the old fake's parser; callers give the Driver a Spec, not CLI arguments (TASK-320, PR #181: "Legacy fake parser; the new entry point accepts Spec").
- `tests.integration.core.fake_codex_integration_test.AppServer.test_only_config_and_feature_options`: the old fake's app-server parser (TASK-320, PR #181: "there is no public raw-argv catalog entry point"); K checks the generated app-server argv.
- `tests.integration.tmux.crew_integration_test.Retile.test_restart_keeps_a_dragged_layout`: `crew restart` is close, `start(resume)`, `show`, so it opens a new pane that re-tiles (PR #187, Intentional behaviour changes: "`restart` opens a new pane, re-tiled into the grid, instead of reusing the same pane").
