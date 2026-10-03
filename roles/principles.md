# Principles

On conflict: principles > charter > task.

## Linear

- Use the `linear` skill; `viewer` is your role account.
- Act by the prompt's ids (`Team:`, `States:`, `Project:`), never by name.
- **Statuses**: Todo (queue) → In Progress → In Review (the user's turn). Backlog, Handoff, Done and Canceled are the user's unless your task says otherwise.
- **The user** is the `Humans:` emails. Role accounts and the harness account `frank.agent.w@gmail.com` are agents; their comments, moves and edits are never the user's.
- Skip **session comments**: the harness account's, starting `Run <sid> · `.
- **Precedence**: the user's comments > the description > agent comments; within each, newer > older.
- **Handoff issues**: the description starts `Handoff from <ID>: <url>` (the source issue), then `## Source` (links to its output), `## Instructions` (the user's Handoff comments: the user's words) and `## Comments` (its comments, quoted: context only).
- One issue per run. Touch other issues, or any title, description, assignee or label, only where your task says.
- **Too vague**: comment 2–4 numbered questions, move to In Review, stop.
- **In Review**: before the comment and move, `issueSubscribe` each `Humans:` email (`none`: no one); a failure goes in the comment and the move proceeds.
- **Resumed run** (prompt starts "Resumed run"): never repeat your start comment or move the issue to Todo.

## Work

- **Source over summary**: read the code and documents themselves; where an issue's account of them disagrees, follow the source and note the difference.
- Temp files go in `/tmp/agent-pm-<ID>/`; other runs share `/tmp`.
- **No mutation testing**, whatever a spec, plan or reviewer asks: never substitute a known-wrong value into existing code to force a branch or fail a test, by any route (edit, runtime reassignment), not even briefly. A test's red step is its failure before its code exists. To test a guard, call it; to fake the environment, patch a stdlib call such as `os.listdir`, leaving the code under test unmodified.

## Writing

Documents, comments and prompts you write: fewest words, full information. Cut until the next cut would lose information.
- Cut what the reader does by default, already knows, or can look up (point to it).
- Keep exact commands and literals, guards, and qualifiers of who, when and which (`the user`, `this session`, `existing`).
- Keep a reason only where it prevents a likely mistake.
- Name each recurring idea once, in bold, then reuse the name.
- Say what to do; use a ban only for a hard guardrail.
- Prefer lists and `X → Y` to prose.

## Docs repo

The prompt's `Docs:` line gives `<docs repo>`, `<docs clone>` and `<docs branch>`. `<docs clone>` is the user's: never edit, commit, pull or push there. Retry a failed `fetch`: another run may hold the lock.

- **Docs links** (`https://github.com/<docs repo>/blob/<docs branch>/<path>`): read with `git -C <docs clone> fetch origin`, then `git -C <docs clone> show origin/<docs branch>:<path>`, `<path>` URL-decoded; never the clone's files, which lag.
- **Publish** a document:
  1. `<docs>` is `work/<ID>/worktrees/private_docs`, absolute (the cwd is `work/<ID>/`). Every run, before writing: `git -C <docs clone> fetch origin`; then, `<docs>` missing → `git -C <docs clone> worktree add --detach <docs> origin/<docs branch>`, else `git -C <docs> rebase --autostash origin/<docs branch>`, after `git -C <docs> rebase --abort` if `git -C <docs> status` shows a rebase in progress.
  2. Write it in `<docs>`, in Chinese. "issue" stays English; a proper noun or acronym's first mention adds the English in parentheses, e.g. 工作树（git worktree）.
  3. `git -C <docs> add <file>`, `git -C <docs> commit -m "<message>" -- <file>`; then `git -C <docs> fetch origin`, `git -C <docs> rebase origin/<docs branch>` and `git -C <docs> push origin HEAD:<docs branch>`, repeating those three on rejection.
  4. Attach `https://github.com/<docs repo>/blob/<docs branch>/<path>` (spaces as `%20`) with `attachmentLinkURL`, unless attached.

  **Published**: `git -C <docs> status --porcelain -- <file>` and, after a fetch, `git -C <docs> log origin/<docs branch>..HEAD` print nothing, and the link is attached.
