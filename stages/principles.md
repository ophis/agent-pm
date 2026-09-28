# Principles (every stage)

The stage file named with this one says what to do; these rules hold in every stage.

- Team `Frank's Agents`; the stage's project id is the prompt's `Project:` value (the name may change).
- The agent never uses Backlog.
- Every move to In Review also assigns the issue to the reviewer, the Linear user whose email is the prompt's `Reviewer:` value (one `issueUpdate` with `stateId` and `assigneeId`); if it is `none`, move without assigning; if the email isn't found, comment that and move without assigning.
- Linear access: the `linear` skill. Its API key belongs to the agent account `frank.agent.w`, so `viewer` is the agent.
- Comments by a user whose email is in the prompt's `Humans:` list are the user's. Everything the agent account does (comments, moves, edits) is the agent's, never the user's.
- The user's comments outrank the agent's, and newer comments outrank older ones.
