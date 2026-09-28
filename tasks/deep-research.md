# Linear Deep Research

Turns one Deep Research issue into a verified Markdown report pushed to the user's `private_docs` GitHub repo and linked from the issue. Research runs through the built-in `deep-research` Workflow; these instructions own claiming, reporting and board updates.

## Board

- The Deep Research project; the principles file named in the prompt holds the rules every role and task shares.
- Statuses: Todo (queue) → In Progress → In Review (needs the user: report ready, questions, or stuck) → Done (user only).

## Steps

1. **Pick.** If the invocation names an issue, use it. Otherwise run `python3 ../scripts/router.py --pick --project 03495382-48f7-4280-a11c-4375df80a561` (relative to this file): it returns issues left by dead runs to Todo, then claims the next Todo issue and prints `<ID> <url>`. No output → queue empty; stop.
2. **Read** the issue and its comments.
3. **Too vague?** If the question, scope or deliverable is missing, comment 2–4 numbered questions, move the issue to In Review, and stop.
4. **Claim.** If `router.py --pick` or the runner already claimed it, only comment that research started. Otherwise re-read the status right before claiming: not Todo anymore → another session has it; stop. Else set In Progress, assignee `viewer`, and comment that research started.
5. **Research.** Call the built-in `/deep-research` Workflow exactly once for the whole issue in this invocation. Combine all subquestions, shared context, and any "already known" claims from the description and the user's comments into one self-contained `args` string; where they conflict, the user's comments override the description (ignore the agent's own comments); phrase existing claims as claims to verify and prioritize the questions most important to the deliverable. Do not launch separate runs for individual parts or additional runs to fill coverage gaps. The workflow verifies only its top-ranked claims, so list uncovered or unverified parts under Gaps.
6. **Failed or partial run.** Do not automatically retry or launch a replacement research run in this invocation. If the run produces usable findings, including supported refutations, publish the report and list missing or unverified parts under Gaps. If it fails and yields no usable findings, comment the failure, move the issue back to Todo, and stop.
7. **Report.** Write `~/playground/private_docs/Research/<YYYY-MM-DD-HHMM>-<issue ID>-<short-kebab-slug>.md` (local time from `date +%Y-%m-%d-%H%M` when the file is first created; if a `Research/*-<issue ID>-*.md` file already exists, use it), starting with `# Report: <issue ID> <issue title>`. Commit only that file (`git add <file>` then `git commit -m "Add <issue ID> report: <short title>" -- <file>`, or `Update …` when the file already existed; leave any other changes in the repo alone) and `git push`. Add its GitHub URL, `https://github.com/ophis/private_docs/blob/main/Research/<file name>`, to the issue as a link attachment (the `linear` skill). Do not create a Linear document. Write the report in Chinese; on first mention, follow each proper noun or acronym with its English original in parentheses, e.g. 工作树（git worktree）. Sections:
   - Answer and recommendation (one paragraph)
   - Comparison table, when the deliverable asks for one
   - Findings per part, each with its confidence and sources
   - Corrections to anything the issue listed as already known
   - Gaps: unverified parts, refuted claims, open questions
   Present unverified or single-source points as such, never as fact. The recommendation and comparison table are your synthesis of findings from the single run; say so.
8. **Hand off.** Comment a 3–5 line summary plus the GitHub link, set In Review, and reply to the user with the link. Handle one issue per invocation.

## Resume rule

A prompt starting "Resumed run" continues this session after an interruption. Re-read this file first; it overrides any earlier resume rule in your context. It is the one exception to steps 5–6. Finish the interrupted research by reusing everything the run already produced and running only what is missing. Never call the Workflow again (except rule 3a), never use `resumeFromRunId` (it replays only the unchanged prefix of agent calls, so deep-research re-runs almost everything), and never move the issue to Todo.

Where the run's work is: this session's latest Workflow result for the issue prints `Run ID: wf_…` and `Script file: <session folder>/workflows/scripts/…`. In that session folder:
- `workflows/<runId>.json`: one long line, read only with `jq`. `status`, and `result` with `summary`, `findings` (empty when synthesis failed), `confirmed`, `refuted`, `unverified` (claim, erroredVotes, validVotes, source) and `sources`.
- `subagents/workflows/<runId>/journal.jsonl`: `started` (agentId, phase), `result`, `failed` per agent. Hundreds of KB: use `jq` projections, e.g. `jq -c 'select(.type=="result") | {agentId, refuted: .result.refuted}'`.
- `subagents/workflows/<runId>/agent-<id>.jsonl`: the first line holds that agent's full prompt: `head -1 agent-<id>.jsonl | jq -r .message.content` (a harness header, then the prompt indented 2 spaces). `agent-<id>.meta.json` has its `workflowPhase`. Find a claim's vote agents with a fixed-string search, `grep -lF -f <file holding the claim text> agent-*.jsonl`, keeping only `"workflowPhase":"Verify"` agents.

Check in order:

1. No Workflow call yet in this session → continue from step 5; it is still the single run.
2. Completed run (`status` is `completed`): the result is authoritative; do not re-fetch or use the journal otherwise. Use `findings` if non-empty, plus any claim the re-votes below confirm. Every claim in `unverified` (fewer than 2 valid votes) gets a fresh round of 3 votes with its original vote prompt, changing only the voter number.
3. Killed run (no completed json): continue the script's pipeline where it stopped, with the rules and prompts in the `Script file:` (`FETCH_PROMPT`, `VERIFY_PROMPT`, URL dedup, `MAX_FETCH`, claim ranking by importance then source quality, `MAX_VERIFY_CLAIMS`, 3 votes per claim).
   a. Search not finished (any Search agent without a result) → call the deep-research Workflow once more with the same args; it is still the single run.
   b. Fetch: reuse returned results; run the missing ones (started without a result, or never started within the budget).
   c. Verify: select claims as the script does from all Fetch claims. A claim with ≥ 2 valid votes keeps them (find them by claim text and source as above, read `refuted` from the journal by agentId; the prompt text has quote-family and control characters removed and whitespace collapsed); every other selected claim gets a fresh round of 3 votes.
4. Each re-run is one Agent call with the prompt text (header removed, dedented), ending with "Reply with only JSON: {refuted, evidence, confidence}" for votes, or the script's fetch fields for Fetch. At most 10 at a time.
5. Votes decide as in the script: ≥ 2 refutes → refuted; ≥ 2 valid and < 2 refutes → confirmed; else unverified.
6. A later resume also reuses the fetches and votes earlier resumes got back (they are in this conversation).
7. Empty or absent `findings` → merge the confirmed claims yourself while writing the report.
8. Steps 7–8, doing only what is missing: report committed and pushed, link on the issue, hand-off comment, In Review. Never repeat the "research started" comment. Unresolved parts go under Gaps. If nothing usable exists even after the re-runs, publish no report: comment what failed and set In Review.
