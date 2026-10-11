# Codex integration parity

Baseline: `origin/main` at `a0a2d1200413b6c1c3da0182dbcec955db346bd1`. Integration sources match the TASK-318 `tmp/main-parity` snapshot, read without importing or executing it. Requirements: [TASK-318](https://linear.app/ophis-workgroup/issue/TASK-318/eng-rearch-core-3-codex-adapter-headless-one-session-store).

Aliases expand to exact test identities:

| Alias | Module or class |
|---|---|
| H | `new.tests.integration.adapters.codex.headless_run_integration_test.HeadlessIntegrationTest` |
| G | `new.tests.integration.driver.api_integration_test` |
| U | `new.tests.unit.adapters.codex.common_test.CommonTest` |

The 25 H tests use a new standard-library process fixture: 22 exercise the public Driver application programming interface (API), two run direct Adapter launches through fake subprocesses (`H.test_fresh_native_prompt_boundary_preserves_subcommands_and_options`, `H.test_resumed_native_prompt_boundary_preserves_options`), and one checks fixture isolation. The fixture explicitly trusts its cwd with `Access(project=True)`; it patches no filesystem guard. Untrusted project launch policy remains covered by `U.test_untrusted_project_configuration_and_cwd` and `H.test_untrusted_project_settings_are_rejected_before_spawn`. HOME is separate from the workdir, Codex config home, add-dir grants and standard-library TMPDIR.

## Codex headless

Old module: `tests.integration.core.fake_codex_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `CodexIntegration.test_fixture_home_is_ignored_and_owned_cleanup_removes_it` | `H.test_fixture_cleanup_removes_owned_home_outside_writable_roots` | Owned cleanup and protected HOME separation. The old repo-specific gitignore assertion does not apply to an externally supplied task temp root. |
| `CodexIntegration.test_reports_start_and_valid_outcome_through_channel` | `H.test_fresh_launch_has_exact_flags_prompt_cwd_and_stdin_eof`; `H.test_driver_replies_native_sid_and_snapshot`; `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted` | Adapted launch, stdin EOF (end of file), cwd, exact prompt, native binding and raw messages. Progress interpretation, status logging, outcome validation and deliverable writing remain unported: Delegate, TASK-315. |
| `CodexIntegration.test_two_turns_resume_the_same_native_session_and_keep_previous_progress` | `H.test_resume_same_native_and_saved_settings`; `H.test_interrupted_first_turn_keeps_binding_and_resumes` | Same native ID, settings, flags before resume, appended transcript and raw progress messages. Interpreted progress ledger remains unported: Delegate, TASK-315. |
| `CodexIntegration.test_interrupted_first_turn_resumes_same_run_and_keeps_progress` | `H.test_interrupted_first_turn_keeps_binding_and_resumes` | Actual process stop before TurnEnd, preserved durable binding, same caller/native IDs on recovery, raw progress replies. Missing-outcome failure, outcome count and deliverable remain unported: Delegate, TASK-315. |
| `CodexIntegration.test_missing_native_file_fails_before_fake_spawn` | `H.test_missing_session_does_not_spawn` | Complete adapted pre-spawn rejection. |
| `CodexIntegration.test_missing_mapping_fails_before_fake_spawn` | `H.test_missing_mapping_does_not_spawn`; `H.test_missing_binding_record_and_fifo_do_not_spawn` | Complete adapted rejection, also after run.jsonl exists. |
| `CodexIntegration.test_different_native_id_on_resume_is_rejected` | `H.test_different_native_id_on_resume_is_not_published_or_saved` | Complete adapted rejection: no changed SessionBound/run record or protected binding; Driver stops the failed process. |
| `CodexIntegration.test_missing_outcome_is_not_inferred_from_final_reply` | `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted` | Final JSON (JavaScript Object Notation) remains raw Reply/TurnEnd text; no outcome interpretation. Missing-outcome failure remains unported: Delegate, TASK-315. |
| `CodexIntegration.test_invalid_report_fails_validation` | `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted` | Invalid outcome-shaped text survives unchanged. Validation and failed-result policy remain unported: Delegate, TASK-315. |
| `CodexIntegration.test_stderr_is_readable_and_recorded` | `H.test_stderr_is_an_event_and_run_record` | Complete adapted Stderr events and run records; terminal presentation belongs to the caller. |
| `CodexIntegration.test_tui_rejected_before_fake_codex_starts` | `H.test_tui_is_rejected_without_spawn` | Complete adapted rejection of the unsupported terminal user interface (TUI), before process or run record. |

## Team directories

Old module: `tests.integration.core.team_dirs_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `CodexTeamDirs.test_a_codex_run_and_its_resume_add_its_tasks_dir` | `H.test_add_dir_order_and_tasks_prompt_survive_resume` | Supplied task-dir prompt retained, ordered add-dir grants replayed even when current access dirs change, no workdir task copy. Team selection, task-dir discovery and generated prompt remain unported: Composer, TASK-315. |
| `CodexTeamDirs.test_a_read_dir_holding_the_team_dir_is_refused` | None | No Adapter equivalent for team/read-dir overlap policy. Unported: Composer/access planning, TASK-315. |

The same file's `TeamDirs.test_a_dry_run`, `TeamDirs.test_list` and `TeamDirs.test_a_headless_run_reads_its_task_from_its_tasks_dir` exercise Claude and Composer, outside the replaced Codex path.

## Worker common behavior

Old module: `tests.integration.tmux.codex_worker_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `CodexLifecycle.test_start_blocked_done_reply_follow_up` | `H.test_driver_replies_native_sid_and_snapshot`; `H.test_resume_same_native_and_saved_settings` | Shared binding/settings subset only. Worker hooks, blocked/done, reply retrieval, same-process follow-up, notification flags and tmux records remain deferred to the Codex TUI/crew slice of TASK-314. |
| `CodexLifecycle.test_restart_and_recovery_from_a_claude_session_keep_the_native_session` | `H.test_interrupted_first_turn_keeps_binding_and_resumes`; `H.test_resume_same_native_and_saved_settings` | Shared native recovery/settings subset only. Worker restart/attach, tmux state and recovery from another runtime remain deferred to Codex TUI/crew, TASK-314. |
| `CodexSession.test_a_codex_managers_marker_and_injected_defaults_stay_out_of_its_workers` | `H.test_inherited_session_markers_are_removed_and_headless_defaults_retained` | Fresh/resumed Codex markers and CLAUDECODE removed. Headless installs its own no-color/pager defaults. Interactive color/default removal, Claude-worker isolation, worker restart and attach remain deferred to Codex TUI/crew, TASK-314. |
| `CodexSession.test_a_codex_workers_own_crew_start_defaults_to_codex` | None | Worker client inheritance, nested crew, distinct worker identities and roster resume command remain deferred to Codex TUI/crew, TASK-314. |

## Related generic headless behavior

These old tests use Claude; only runtime-independent behavior replaced by Driver/Codex is mapped here. G coverage belongs to TASK-317 and was neither changed nor run for this port.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `tests.integration.core.drive_integration_test.NewRun.test_done` | `H.test_driver_replies_native_sid_and_snapshot`; `H.test_fresh_launch_has_exact_flags_prompt_cwd_and_stdin_eof`; `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted` | Adapted session identity, launch, messages and run records. Claude argv/transcript layout is runtime-specific; composed prompt, interpreted reports and deliverable remain unported: Composer/Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Outcomes.test_needs_input` | `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted` | Raw needs_input-shaped message. Question/outcome handling and deliverable suppression remain unported: Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Outcomes.test_no_outcome` | `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted` | Raw final reply; no result inference. Missing-outcome policy remains unported: Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Outcomes.test_invalid_outcome_then_fixed` | `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted`; `H.test_resume_same_native_and_saved_settings` | Raw invalid report-shaped text and same-native resume. Validation, correction and deliverable replacement remain unported: Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Outcomes.test_nonzero_exit_preserves_report_for_recovery` | `H.test_nonzero_exit_preserves_raw_reply_and_final_turn`; `G.ExitTest.test_a_non_zero_exit` | Raw reply and TurnEnd retained before Exited(7); binding remains recoverable. Valid-report success override and deliverable remain unported: Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Outcomes.test_nonzero_exit_without_valid_report_still_fails` | `H.test_nonzero_exit_without_final_turn_is_not_inferred`; `G.ExitTest.test_a_non_zero_exit` | Nonzero process status without a fabricated TurnEnd. Report validity, result/error records and command exit policy remain unported: Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Stderr.test_the_clients_stderr_lines_land_in_the_record` | `H.test_stderr_is_an_event_and_run_record`; `H.test_missing_session_does_not_spawn` | Stderr transport/recording. Codex rejects a missing native file before spawn; the old Claude missing-transcript diagnostic and failure-result policy are not Codex behavior. |
| `tests.integration.core.drive_integration_test.Resume.test_resume_reuses_the_recorded_cwd_and_session` | `H.test_resume_same_native_and_saved_settings` | Saved cwd and native session, new prompt, same transcript. Resume guide/input composition and outcome records remain unported: Composer/Delegate, TASK-315. |
| `tests.integration.core.drive_integration_test.Interrupt.test_interrupt_kills_a_hung_agent` | `H.test_interrupted_first_turn_keeps_binding_and_resumes`; `G.ExitTest.test_stop_mid_sleep_ends_with_a_negative_exited_and_is_idempotent` | Public Handle.stop kills the process, negative Exited, idempotence, recovery. Old wrapper SIGINT (interrupt signal), result/end policy and terminal logging remain unported: Delegate command runner, TASK-315. |
| `tests.integration.core.resume_integration_test.NeedsInput.test_the_answer_resumes_the_session` | `H.test_raw_progress_outcome_and_json_reply_are_uninterpreted`; `H.test_resume_same_native_and_saved_settings` | Raw asking text plus same-session resume with new input. Answer policy, composed resume guide and deliverable remain unported: Composer/Delegate, TASK-315. |
| `tests.integration.core.resume_integration_test.RolePicks.test_the_role_picks_and_a_resume_keeps_the_session` | `H.test_interrupted_first_turn_keeps_binding_and_resumes` | Same-session recovery subset. Role/task index, task selection, progress interpretation and deliverable remain unported: Composer/Delegate, TASK-315. |
| `tests.integration.core.resume_integration_test.Draft.test_a_resume_keeps_the_draft_until_the_deliverable_replaces_it` | `H.test_interrupted_first_turn_keeps_binding_and_resumes` | Process interruption/recovery subset. Draft read/write and final deliverable replacement remain unported: Delegate, TASK-315; filesystem task behavior is not owned by an Adapter. |

## Unrelated old fixture coverage

All identities below are in `tests.integration.core.fake_codex_integration_test`, execute only the old fake's interactive or hook-catalog mode, and have no new headless counterpart. They remain deferred to Codex TUI/crew, TASK-314. The app-server hook catalog is distinct from the account usage probe.

| Exact old identity | Reason |
|---|---|
| `Interactive.test_unknown_option_or_resume_without_an_id_exits_2` | Interactive argument parser. |
| `Interactive.test_turns_from_the_prompt_then_stdin_lines` | Interactive submissions. |
| `Interactive.test_a_fresh_run_gets_a_new_uuid_and_its_rollout` | Interactive session creation. |
| `Interactive.test_session_start_waits_for_the_first_turn_then_payloads_follow` | Interactive hook timing/payloads. |
| `Interactive.test_a_turn_without_text_stops_with_a_null_message` | Stop-hook payload. |
| `Interactive.test_resume_keeps_the_session_and_reports_source_resume` | Interactive resume hook. |
| `Interactive.test_resume_of_an_unknown_session_exits_1` | Interactive native resume validation. |
| `Interactive.test_untrusted_or_modified_hooks_show_the_review_and_wait` | Hook approval screen. |
| `Interactive.test_hooks_off_run_none_and_need_no_review` | Interactive hook feature toggles. |
| `Interactive.test_permission_and_sub_agent_steps` | Permission hook payloads. |
| `Interactive.test_interrupt_ends_the_turn_without_stop` | Interrupt hook. |
| `Interactive.test_a_users_own_hook_runs_when_trusted_and_needs_review_when_not` | User hook trust. |
| `Interactive.test_logs_argv_and_selected_env` | Old interactive fixture logging. |
| `Interactive.test_colored_unless_no_color_set_and_not_0` | Interactive colors. |
| `Interactive.test_hooks_run_in_the_session_cwd_and_their_results_are_logged` | Hook subprocesses. |
| `AppServer.test_hooks_list_after_initialize_with_notifications_interleaved` | Hook catalog and hashes. |
| `AppServer.test_state_trusts_the_listed_hash_only` | Hook hash trust. |
| `AppServer.test_hooks_off_list_nothing` | Hook feature toggles. |
| `AppServer.test_errors_for_each_cwd` | Per-project hook catalog errors. |
| `AppServer.test_a_users_own_hook_with_its_trust_status` | User hook catalog. |
| `AppServer.test_only_config_and_feature_options` | Hook-catalog argument parser. |
| `AppServer.test_requests_before_initialize_and_unknown_methods_fail` | Old fixture protocol rejection. |
| `AppServer.test_logs_argv_and_each_message` | Old hook-catalog fixture logging. |

Additional owned regressions: `H.test_fifo_and_malformed_native_files_do_not_spawn`, `H.test_activity_and_usage_limit_pass_through_driver`, and `H.test_caller_sid_and_handle_info_inspect_native_binding`.
