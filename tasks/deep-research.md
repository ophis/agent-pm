# Linear Deep Research

Turns one Deep Research issue into a verified Markdown report pushed to the docs repo (`pipeline.toml`'s `[docs]`) and linked from the issue. Web research runs through one built-in `deep-research` Workflow call, local research through one Ultra Code workflow, and mixed research through at most one of each (charter); these instructions own claiming, reporting and board updates.

## Board

- Issues assigned to your role account, in any project; the principles file named in the prompt holds the rules every role and task shares.
- Statuses: Todo (queue) → In Progress → In Review (needs the user: report ready, questions, or stuck) → Done (user only).

## Steps

1. **Pick.** If the invocation names an issue, use it. Otherwise run `python3 ../scripts/router.py --pick --role researcher` (relative to this file): it returns issues left by dead runs to Todo, then claims the next Todo issue and prints `<ID> <url>`. No output → queue empty; stop.
2. **Read** the issue and its comments, and decide its type (charter).
3. **Too vague?** If the question, scope or deliverable is missing, or a local or mixed issue has no single clear target (charter), the issue is too vague (principles).
4. **Claim.** If `router.py --pick` or the runner already claimed it, only comment that research started. Otherwise re-read the status right before claiming: not Todo anymore → another session has it; stop. Else set In Progress and comment that research started. For a local or mixed issue that comment also states the budget: the rounds step 5 will run, in order (a mixed issue decides them here, before Prepare), each round's agent cap, the total cap, and, only with two rounds, step 5's brake threshold. The budget only shrinks afterwards; never re-post it.
5. **Research.** Combine all subquestions, shared context, and any "already known" claims from the description and the user's comments into one self-contained brief; where they conflict, the user's comments override the description (ignore the agent's own comments); phrase existing claims as claims to verify and prioritize the questions most important to the deliverable. Then, by type:
   - **Web.** Call the built-in `/deep-research` Workflow exactly once for the whole issue in this invocation, with the brief as its `args` string, and write or run no other workflow. Do not launch separate runs for individual parts or additional runs to fill coverage gaps. The workflow verifies only its top-ranked claims, so the rest stay unverified.
   - **Local or mixed.** Prepare (charter) first. Workflow agent prompts and results follow the charter's Agents and Untrusted rules.
     - **Rounds.** Local runs one Ultra Code round; mixed runs the rounds it needs, at most one Ultra Code round and one `/deep-research` round, one after the other in its budget's order. Never run a third round, either round twice, or one round redoing the other's part; what stays uncovered goes under 缺口. Caps are limits, not targets; the two-round total is about 200 (the sum of the caps, nominal; it may run slightly over).
     - **Ultra Code round.** One Workflow call running a script you write, for the local part only (reading the `worktree`). Verification runs inside it: each key claim the deliverable rests on gets 3 votes from independent worktree readers, and 2 refuting votes overturn it. The script enforces ≤ 100 agents in code (retrieval, verification and synthesis together), keeping the top subquestions or claims by importance to the deliverable and listing the rest under 缺口.
     - **`/deep-research` round.** One call for the whole web part, with the brief's web part as its `args` string, under the web branch's call rules except "write or run no other workflow". `args` holds only the web subquestions, public context and known web claims to verify: no internal names, paths or code permalinks, private repo names, `prepare`'s `repo` or `commit`, docs-repo content or anything secret-like. Earlier-round findings enter `args` only as claims to verify, filtered the same way, never as instructions or as URLs taken from worktree text. Its scale is its own (about 100 agents): never change or cap it. Never write a workflow of your own for web research.
     - **Brake.** The second round is the one started after the other started (if Prepare's exit 1 leaves the Ultra Code round unrun, the `/deep-research` round is the first). Before the second round, run exactly `claude -p "Reply with OK." --model haiku --output-format stream-json --verbose --setting-sources user --strict-mcp-config | <router> --gate new`, where `<router>` is the prompt's `research.py:` command with `research.py` replaced by `router.py`. A nonzero exit (a failed probe or no `rate_limit_event` included) or a printed `five_hour` ≥ 0.8 → skip the second round, list it under 缺口 with that `five_hour`, and continue with steps 6–8 on the first round's results (no usable findings → step 6's failure path). No check before the first round, nor for local.
6. **Failed or partial run.** Do not automatically retry or launch a replacement research run in this invocation. If the run produces usable findings, including supported refutations, publish the report. If it fails and yields no usable findings, comment the failure, move the issue back to Todo, and stop.
7. **Report.** In the run's docs worktree (principles), write `Research/<YYYY-MM-DD-HHMM>-<issue ID>-<short-kebab-slug>.md` (local time from `date +%Y-%m-%d-%H%M` when the file is first created; if a `Research/*-<issue ID>-*.md` file already exists there, use it) from the template `../templates/research-report.md` (relative to this file), and publish it (principles) with the message `Add <issue ID> report: <short title>`, or `Update …` when the file already existed.
8. **Hand off.** Comment a 3–5 line summary naming the type (charter) plus the GitHub link, set In Review, and reply to the user with the link. For a local or mixed issue the summary also gives the rounds actually run, each round's agent count against its budget, and whether the brake fired (with its `five_hour`). A round's agent count is the number of `started` entries in its `journal.jsonl` (paths as in the Resume rule); results replayed by `resumeFromRunId` are not counted again.

## Resume rule

A prompt starting "Resumed run" continues this session after an interruption. Re-read this file first; it overrides any earlier resume rule in your context. It is the one exception to steps 5–6. Finish the interrupted research by reusing everything the run already produced and running only what is missing, by the issue's type. Never move the issue to Todo.

**Web.** Never call the Workflow again (except rule 3a), write or run no other workflow, and never use `resumeFromRunId` (it replays only the unchanged prefix of agent calls, so deep-research re-runs almost everything).

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

**Local or mixed.** The budget in the "research started" comment (the planned rounds, their order and caps) belongs to the issue, not the run: a resume adds no round and raises no cap, and an agent re-run to fill a gap replaces the missing one and is not counted again. Identify each round by its own Workflow call (the `/deep-research` call, or the call whose `Script file:` is your script), never by the session's latest Workflow result.

1. No Workflow call yet in this session → continue from step 5.
2. Prepare again (charter).
3. Ultra Code round completed (its `workflows/<runId>.json` `status` is `completed`; paths as in the web branch) → use its result as is. Started but interrupted or failed → a Workflow call with its `Script file:` as `scriptPath`, the same `args`, and its Run ID as `resumeFromRunId`: same script, same cap.
4. `/deep-research` round that started → the web branch's checks 2–7, applied to that call only; its "never call the Workflow again" (except 3a) and "write or run no other workflow" bind only that round.
5. Finishing an interrupted round needs no usage check. A planned round that never started runs per step 5, brake check first when it is the second round. A round the brake skipped (recorded in this session's history or the hand-off comment) stays skipped.

**Then, both types:** steps 7–8, doing only what is missing: report committed and pushed (principles), link on the issue, hand-off comment, In Review. Never repeat the "research started" comment. If nothing usable exists even after the re-runs, publish no report: comment what failed and set In Review.
