# TASK-69: In Review subscribes the humans — plan

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-69/worktrees/TASK-69-in-review-subscribes-the-humans-instead branch=TASK-69-in-review-subscribes-the-humans-instead base_ref=909c4d368340a06a43184710c43bc80f8d484672 review_round=0 spec_file=docs/specs/2026-09-30-task-69-subscribe-on-review-design.md

## Implementation plan

(S4)

## Progress
- decision(placement): subscribe inline in router `comment_and_move` and promote `move`, each in its file's idiom, over a shared pipeline helper - issue forbids new abstraction; dissent: none
- decision(order): subscribe before the state change - idempotent, a failure leaves the issue unmoved for retry; dissent: none
- decision(engineering step 7): replace "assign the reviewer" with "subscribe the humans" - the GitHub integration makes that move, so the principles rule would not fire; dissent: none
