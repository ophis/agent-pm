# Codex integration parity

Baseline: `origin/main` at `db825af46c3f131d23d7403020e4beff55ca4bf6`. TASK-320 reread the integration sources below, plus `core/src/clients/_codex_worker.py`, `_codex_hook.py` and the old fake, without importing or executing old code. Requirements: [TASK-318](https://linear.app/ophis-workgroup/issue/TASK-318/eng-rearch-core-3-codex-adapter-headless-one-session-store), [TASK-320](https://linear.app/ophis-workgroup/issue/TASK-320/eng-rearch-core-5-codex-tui).

Aliases expand to exact test identities:

| Alias | Module or class |
|---|---|
| H | `new.tests.integration.adapters.codex.headless_integration_test.HeadlessIntegrationTest` |
| G | `new.tests.integration.driver.api_integration_test` |
| U | `new.tests.unit.adapters.codex.common_test.CommonTest` |
| T | `new.tests.integration.adapters.codex.tui_integration_test.TuiIntegrationTest` |
| K | `new.tests.integration.adapters.codex.tui_integration_test.HookTrustIntegrationTest` |
| D | `new.tests.integration.delegate.team_dirs_integration_test` |

All integration processes are fakes. T and K use private tmux servers through `new/tests/live_tmux.py`; caller HOME and scratch live under `AGENT_PM_TEST_TMP` (CI fallback: `new/`). The caller's protected recovery store stays outside explicit and native implicit writable roots; configured runtime HOME may be under workdir or an add-dir grant. No filesystem guards are patched. H preserves headless/probe defaults; T verifies interactive defaults, configured overrides and independent native identities.

**Remaining ownership**: Composer/Delegate = TASK-315; crew roster, client inheritance and event cursor = TASK-322; generic Driver/tmux hosting, layout and session metadata = TASK-319. These omissions refer to caller assertions, not Codex hooks or recovery. Run records never authorize resume; hooks persist through the same protected store as headless. Codex tui role run replaces the old unsupported-mode guard as required by TASK-320.

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
| `CodexIntegration.test_tui_rejected_before_fake_codex_starts` | `H.test_tui_factory_is_supported_without_starting_a_process`; `T.test_start_permission_reply_send_and_separate_process_attach` | Intentionally superseded: terminal user interface (TUI) support is TASK-320's new capability. The factory starts nothing; fake Driver-hosted tui covers the replacement. |

## Team directories

Old module: `tests.integration.core.team_dirs_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `CodexTeamDirs.test_a_codex_run_and_its_resume_add_its_tasks_dir` | `D.CodexTeamDirs.test_a_codex_run_and_its_resume_read_its_tasks_dir_unadded`; `H.test_add_dir_order_survives_resume_and_the_tasks_dir_is_read_unadded` | Inverted on purpose: the tasks dir is read-only, never added. D: team selection, task-dir prompt, run and resume through the Delegate, no workdir task copy. H: ordered add-dir grants replayed even when current access dirs change, supplied task-dir prompt retained. |
| `CodexTeamDirs.test_a_read_dir_holding_the_team_dir_is_refused` | `D.CodexTeamDirs.test_a_read_dir_holding_the_team_dir_runs_unadded`; `D.CodexTeamDirs.test_a_write_dir_holding_the_team_dir_is_refused` | Inverted on purpose: a read dir is read-only, so one holding the team dir runs, unadded; a write dir keeps the refusal. |

The same file's `TeamDirs.*` (Claude and Composer, outside the replaced Codex path) are ported in D's `TeamDirs`.

## Worker behavior

Old module: `tests.integration.tmux.codex_worker_integration_test`.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `CodexLifecycle.test_start_blocked_done_reply_follow_up` | `T.test_start_permission_reply_send_and_separate_process_attach`; `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | Start, BEL notification, model/cwd/home, silent hook subprocesses, root native binding, permission Blocked, raw reply, send and one done per turn. Protected binding and Driver hook store are mode 0600. Old worker-specific JSON/reply files are replaced by shared Adapter binding and Driver-owned records; tmux `@client/@exe/@flags/@env` are replaced by Driver session metadata. Crew roster/resume argv and event cursor: Remaining ownership. |
| `CodexLifecycle.test_restart_and_recovery_from_a_claude_session_keep_the_native_session` | `T.test_resume_keeps_native_id_cwd_home_model_effort_dirs_network_and_commands`; `T.test_start_permission_reply_send_and_separate_process_attach`; `T.test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested` | Stop/native resume, saved settings, startup/resume hook source and transcript, cross-process attach/send, clean parent markers including CLAUDECODE. Crew restart/attach CLI and roster-driven runtime selection: Remaining ownership. |
| `CodexSession.test_a_codex_managers_marker_and_injected_defaults_stay_out_of_its_workers` | `T.test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested`; `T.test_explicit_no_color_controls_interactive_rendering`; `H.test_inherited_session_markers_are_removed_and_headless_defaults_retained` | Fresh/resumed/nested Codex marker isolation and interactive colors/default removal; headless keeps its own defaults. Claude-worker isolation is owned by its Adapter; crew restart/attach CLI: Remaining ownership. |
| `CodexSession.test_a_codex_workers_own_crew_start_defaults_to_codex` | `T.test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested`; `K.test_concurrent_independent_starts_serialize_the_same_home_probe` | A nested/independent Codex start has a distinct native ID and protected binding, no parent markers, its own events file, and per-home serialized preflight. Default runtime inheritance, manager lookup and roster resume command: Remaining ownership. |

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

## Related role tui behavior

Old module: `tests.integration.core.drive_tui_integration_test`. Its runtime is Claude; only the replaced transport is adapted for the new Codex role capability.

| Exact old identity | Exact new identity | Coverage and remaining assertions |
|---|---|---|
| `TuiRun.test_stop_hook_nudge_outcome` | `T.test_start_permission_reply_send_and_separate_process_attach` | Two subsequent turn replies, raw progress text, send and exact done lines, session stays live. Nudge/outcome/deliverable/attach-command presentation: Composer/Delegate in Remaining ownership. |
| `TuiRun.test_detach_from_the_manager` | None | Delegate CLI detach, outcome events and attended layout. Remaining ownership; Codex factory neither detaches nor shows panes. |
| `TuiRun.test_detach_events_default` | None | Delegate/crew manager event-file selection and outcome lines. Remaining ownership; tests pass the event path explicitly. |

## Old interactive and catalog fixture cases

Old module: `tests.integration.core.fake_codex_integration_test`. Native behavior is adapted through the new process fake and public Driver, rather than importing the old fixture.

| Exact old identity | Exact new identity or omission | Coverage and remaining assertions |
|---|---|---|
| `Interactive.test_unknown_option_or_resume_without_an_id_exits_2` | None | Old fake's argparse contract only. Callers supply a Spec, not arbitrary CLI arguments; launch arguments and resume guards are tested by T/H. |
| `Interactive.test_turns_from_the_prompt_then_stdin_lines` | `T.test_start_permission_reply_send_and_separate_process_attach` | Prompt boundary, submitted text and each turn's reply. The persistent tui does not use the old fake's EOF-triggered exit scenario. |
| `Interactive.test_a_fresh_run_gets_a_new_uuid_and_its_rollout` | `T.test_parent_markers_and_injected_defaults_are_removed_fresh_resumed_and_nested`; `K.test_concurrent_independent_starts_serialize_the_same_home_probe` | Distinct canonical native IDs, durable bindings and native session files. |
| `Interactive.test_session_start_waits_for_the_first_turn_then_payloads_follow` | `T.test_session_start_waits_for_first_submission_fresh_and_resumed` | Fresh/resumed empty prompt waits for send before SessionStart, then UserPromptSubmit and Stop. |
| `Interactive.test_a_turn_without_text_stops_with_a_null_message` | `T.test_interrupt_before_stop_and_null_stop_have_empty_turn_replies` | Null Stop payload becomes an empty TurnEnd, one done line. |
| `Interactive.test_resume_keeps_the_session_and_reports_source_resume` | `T.test_resume_keeps_native_id_cwd_home_model_effort_dirs_network_and_commands` | Same native session/file, source resume and saved launch policy. |
| `Interactive.test_resume_of_an_unknown_session_exits_1` | `T.test_resume_missing_binding_native_fifo_and_malformed_record_fail_before_spawn` | Adapted pre-spawn rejection of missing authorization/native file, not a native CLI diagnostic. |
| `Interactive.test_untrusted_or_modified_hooks_show_the_review_and_wait` | `T.test_startup_dialog_screens_are_driver_blocked_events`; `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive`; `K.test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed` | Driver blocks on review screen; invalid/untrusted catalog prevents native interactive start. |
| `Interactive.test_hooks_off_run_none_and_need_no_review` | `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | A hooks-off catalog prevents the worker's start, preserving the old worker preflight. Running the standalone fake without hooks is fixture-only, not an Adapter capability. |
| `Interactive.test_permission_and_sub_agent_steps` | `T.test_start_permission_reply_send_and_separate_process_attach`; `T.test_root_child_cwd_and_rebinding_filters_publish_only_valid_hooks` | Root PermissionRequest is Blocked; child payloads do not publish events or rebind. |
| `Interactive.test_interrupt_ends_the_turn_without_stop` | `T.test_interrupt_completes_without_stop_and_replays_previous_reply_on_attach`; `T.test_interrupt_before_stop_and_null_stop_have_empty_turn_replies` | Interrupt completes a turn without Stop; root previous reply survives live and separate-process replay, empty before any Stop. |
| `Interactive.test_a_users_own_hook_runs_when_trusted_and_needs_review_when_not` | `K.test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed` | Trusted/managed user hook subprocess succeeds; untrusted/modified hook prevents native start; disabled user hook is allowed. No user config writes. |
| `Interactive.test_logs_argv_and_selected_env` | `T.test_start_permission_reply_send_and_separate_process_attach`; `T.test_explicit_operator_environment_is_forwarded_and_not_written_to_config` | Actual native argv, cwd, HOME settings, selected environment and explicit overrides. |
| `Interactive.test_colored_unless_no_color_set_and_not_0` | `T.test_explicit_no_color_controls_interactive_rendering` | None, empty, 1 and 0 are forwarded and rendered by the fake as before. |
| `Interactive.test_hooks_run_in_the_session_cwd_and_their_results_are_logged` | `T.test_start_permission_reply_send_and_separate_process_attach` | Actual hook subprocess cwd, stdout, exit and payloads logged; hooks are silent and return zero. |
| `AppServer.test_hooks_list_after_initialize_with_notifications_interleaved` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | Initialize/initialized/hooks-list messages, interleaved noise and typed ID matching, event rows/timeouts and exact transient trusted hashes. |
| `AppServer.test_state_trusts_the_listed_hash_only` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup`; `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | CLI trusts only catalog-returned hashes; disabled, malformed hash and foreign command rows prevent start. |
| `AppServer.test_hooks_off_list_nothing` | `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | Empty catalog prevents start. Raw --enable/--disable parser ordering is old-fixture-only; the Adapter supplies generated hook overrides. |
| `AppServer.test_errors_for_each_cwd` | `K.test_disabled_unknown_malformed_and_mismatched_catalogs_never_start_interactive` | Per-cwd catalog errors prevent start. One Driver launch has one cwd; old fake's multi-cwd list behavior has no caller here. |
| `AppServer.test_a_users_own_hook_with_its_trust_status` | `K.test_untrusted_modified_user_hooks_fail_and_trusted_disabled_managed_are_allowed` | User trust status gates startup, without enabling or rewriting user hooks. |
| `AppServer.test_only_config_and_feature_options` | None | Old fake parser contract, not replaced production behavior. K validates generated app-server argv through a strict new parser; there is no public raw-argv hook query. |
| `AppServer.test_requests_before_initialize_and_unknown_methods_fail` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | Client sends initialize first and only its documented methods. Old fake's arbitrary-request rejection is fixture-only, not a production server being ported. |
| `AppServer.test_logs_argv_and_each_message` | `K.test_interleaved_catalog_notifications_exact_hashes_and_cleanup` | Actual app-server argv and every message logged and checked; preflight process reaped before interactive start. |

Additional owned regressions: `T.test_hooks_persist_binding_without_any_consumer`, `T.test_headless_to_tui_to_headless_resume_uses_one_store`, `T.test_tui_to_headless_resume_uses_the_hook_binding`, `T.test_protected_paths_and_untrusted_settings_are_rejected_before_tmux_or_runtime`, `T.test_realistic_usage_limit_screen_emits_reset_and_deduplicates`, `T.test_usage_limit_without_reset_is_still_an_event`, `K.test_appserver_exit_is_retried_once_then_cleaned_up`, `K.test_silent_appserver_times_out_and_kills_group_with_a_fake_clock`, `H.test_fifo_and_malformed_native_files_do_not_spawn`, `H.test_activity_and_usage_limit_pass_through_driver` and `H.test_caller_sid_and_handle_info_inspect_native_binding`.
