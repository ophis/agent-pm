# TASK-69: In Review subscribes the humans instead of assigning a reviewer — design

Source: TASK-69 (https://linear.app/ophis-workgroup/issue/TASK-69), PR-B of the TASK-62 PRD (`private_docs/Product Design/2026-09-29-1540-TASK-62-board-model-products-roles-tasks.md`, 实施顺序 step 4; FR-15, FR-16, FR-17, FR-23). Dispatch is unchanged (still by project); the router's claim still assigns the harness account.

## Goals

1. **G1 — Subscribe on In Review (FR-16).** Every move to In Review, by a session or by the harness, also subscribes each `human_members` email to the issue with `issueSubscribe(id:, userEmail:)`. No user lookup, no history check, never unsubscribe; already subscribed is a no-op (Linear's).
2. **G2 — No reviewer assignment (FR-17).** Moving to In Review keeps the issue's assignee. Delete the rule and its code: `roles/principles.md`'s rule, `tasks/engineering.md` steps 2 and 7, router's and promote's `assigneeId` on In Review, `pipeline.reviewer()`, the reviewer note on `human_members` in `pipeline.toml`.
3. **G3 — Launch prompt (FR-15, rest).** The tail drops `Reviewer:`; `Humans:` stays.
4. **G4 — Docs (FR-23).** README and CLAUDE.md describe subscribing.

## Non-goals

- New validation, config or abstraction (prototype). An unknown `human_members` email still stops the router (`pipeline.humans()`, used for the attempt-cap reset); promote gains no lookup, so an unknown email fails only its In Review moves (the `issueSubscribe` error is logged like any handoff error).
- Moves to other states: Recover's requeue to Todo still clears the assignee; the claim still assigns the harness account (PR-C changes both).
- Historical docs in `docs/specs/`.

## G1 — Harness

Mutation: `mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }`, once per email in `cfg["human_members"]`, in order, before the state change. Subscribing first is safe (idempotent) and a failed subscribe leaves the issue unmoved, so the next tick retries the whole move; the humans are also subscribed before the move's comment is posted.

- **router** (`Board.comment_and_move`): for `state == "in_review"`, subscribe, then comment, then `issueUpdate` (no `assigneeId`). Covers Recover's and `take`'s attempt-cap moves. `Board` keeps `self.humans` (ids, for `last_move(by_user=True)`) and drops `self.reviewer`; it reads the emails from `cfg`. Query inlined like its other mutations.
- **promote** (`Promoter.move`): for `state == "in_review"`, subscribe (each checked with `ok(..., "issueSubscribe")`), then `M_STATE`. Covers the no-instructions bounce and `bounce_failed`. `M_REVIEW` and `self.reviewer` are deleted; a module constant `M_SUBSCRIBE` follows the file's idiom. Promote no longer imports `reviewer` or queries users.
- **pipeline**: delete `reviewer()`; `humans()` stays.
- Dry-run: no subscribe (both already return before mutating).

## G1/G2 — Rules

- `roles/principles.md`: replace the reviewer rule with: every move to In Review also subscribes each email in the prompt's `Humans:` list to the issue (`issueSubscribe(id:, userEmail:)`, one call per email) and leaves the assignee as is; `none` → subscribe no one.
- `tasks/engineering.md`: step 2 drops "(reviewer assigned)"; step 7's "assign the reviewer" becomes "subscribe the humans" (the GitHub integration makes that move, not the session, so the principles rule alone would not fire).

## G3 — Launch

`launch.py` tail: `" Humans: {emails or 'none'}. Project: … Team: … States: …"`.

## G4 — Docs

- `pipeline.toml`: `human_members` comment → the human users, subscribed to every issue moved to In Review.
- README "Your turn": the agent moves the issue to In Review and subscribes you.
- CLAUDE.md: the launch tail list drops `Reviewer:`; one line states that every move to In Review (runs and harness) subscribes the `human_members` and keeps the assignee.

## Tests (`scripts/tests`, no network)

- router: attempt cap (Recover and `take`) with two `human_members` → one `issueSubscribe` per email, in order, before the state update; the assignee is unchanged; a move to Todo subscribes no one; with no `human_members`, no subscribe. Replace `test_attempt_cap_assigns_reviewer` and rename `test_interrupted_unassigns_even_with_reviewer` / `test_unknown_reviewer_fails_loud` (behavior kept).
- promote: no-instructions bounce and `bounce_failed` → `issueSubscribe` per email before `M_STATE`, assignee unchanged, no `users` query; promotion to Done subscribes no one; a failed subscribe posts no comment and leaves the issue in Handoff. Replace `test_bounce_assigns_reviewer`; update `test_failed_bounce_move_posts_no_comment` for the removed `M_REVIEW`.
- launch: tail strings without `Reviewer:`; `test_reviewer_none` → `Humans: none`.

## Done when

- An issue moved to In Review by a session, router or promote keeps its assignee and has every `human_members` user subscribed.
- `grep -rniE 'reviewer|assigneeId' roles tasks scripts/*.py README.md CLAUDE.md pipeline.toml` finds no reviewer assignment (the claim's and Recover's `assigneeId` remain).
- `python3 -m unittest discover -s scripts/tests` passes.
