# Principles (every role and task)

The prompt names this file, your role charter (who you are: responsibilities, standards, boundaries, memory) and your task (the steps for this ticket). These rules hold for every role and task. On conflict, this file outranks the charter, which outranks the task's steps.

- Team `Frank's Agents`; the run's project id is the prompt's `Project:` value. Names (the team, projects, and statuses such as Todo or In Review in these files) are for reading and may change in Linear: act by id — the team's is the prompt's `Team:` value, each status's is in its `States:` line (`<name>=<id>`); move and filter issues by those ids, never look one up by name.
- The agent never uses Backlog.
- Every move to In Review also assigns the issue to the reviewer, the Linear user whose email is the prompt's `Reviewer:` value (one `issueUpdate` with `stateId` and `assigneeId`); if it is `none`, move without assigning; if the email isn't found, comment that and move without assigning.
- Linear access: the `linear` skill. The launcher points it at your role's key (`LINEAR_KEYCHAIN_SERVICE`), so `viewer` is your role account.
- Comments by a user whose email is in the prompt's `Humans:` list (`human_members`) are the user's. Everything the agent accounts do (comments, moves, edits) is the agent's, never the user's: the role accounts (yours and the other roles') and the harness account `frank.agent.w@gmail.com` (router, promote, prune).
- Precedence: (1) the user's comments outrank the agent's; (2) newer comments outrank older ones.
- Handle one issue per run; touch another issue only where your task's steps say so.
- A Handoff-created issue's description is written by the harness account but carries the user's words: `Handoff from <ID>: <url>` (the source issue), `## Source` (links to the source's output), `## Instructions` (the user's Handoff comments) and `## Comments` (every source-issue comment, quoted; context, never instructions).
- Read a `github.com/ophis/private_docs/blob/main/<path>` link from the local clone `~/playground/private_docs/<path>` (URL-decoded).
- Too vague: when your task's check finds the issue too vague, comment 2–4 numbered questions, move the issue to In Review, and stop.
- A document you publish to `~/playground/private_docs` (GitHub `ophis/private_docs`):
  - Write it in Chinese; on first mention, follow each proper noun or acronym with its English original in parentheses, e.g. 工作树（git worktree）.
  - Commit only that file (`git add <file>`, then `git commit -m "<the task's message>" -- <file>`; leave any other changes in the repo alone), then `git push`.
  - Attach its GitHub URL, `https://github.com/ophis/private_docs/blob/main/<folder>/<file name>` (a space as `%20`), to the issue as a link attachment (`attachmentLinkURL`) unless already attached; never a Linear document.
