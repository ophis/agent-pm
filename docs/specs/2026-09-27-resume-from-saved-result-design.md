# Resume by reusing the interrupted run's work

Status: design v4, reviewed (fitness, run-file facts, skill usability: all PASS). Replaces the SKILL resume rule 2 of `2026-09-27-auto-resume-design.md` (`resumeFromRunId`). A first version of the SKILL text is on main (~/.claude c9fa26e); this version supersedes it.

## Problem

The runner resumes an interrupted issue with `claude -p "Resumed run …" --resume <SID>`: the conversation continues, but the deep-research Workflow that ran inside it has either finished (result saved) or been killed. The old rule 2 called `Workflow({resumeFromRunId})`, which replays only "the longest unchanged prefix of agent() calls"; deep-research starts Fetch agents in completion order, so almost everything re-runs (TASK-15: 23 Fetch and 26+ Verify re-run for 4 failed votes and a failed synthesis).

## Approach

The resumed session itself finishes the interrupted research: it reuses every result the run already produced and runs only the missing agents, one Agent call each, with the original prompts from the run's agent transcripts. No script, no new Workflow run (except when nothing past Search exists), no `resumeFromRunId`, never back to Todo.

## Where the run's work is

From this session's latest Workflow result for the issue: `Run ID: wf_…` and `Script file: …/workflows/scripts/…`. Under that session directory:
- `workflows/<runId>.json`: written when the Workflow returned (status `completed`); `result` has `summary`/`findings` (synthesis succeeded) or `confirmed`, `refuted`, `unverified` (claim, erroredVotes, validVotes, source) and `sources`. One long line: read it with `jq`, never Read or grep.
- `subagents/workflows/<runId>/journal.jsonl`: `started` (agentId, phase, label), `result`, `failed` per agent. May contain entries from an earlier `resumeFromRunId` attempt under the same runId. Labels are truncated and can collide; identify an agent by its agentId.
- `subagents/workflows/<runId>/agent-<id>.jsonl`: the first line's user message is that agent's full prompt (a harness header, then the prompt with every line indented 2 spaces); read only the first line (`head -1 … | jq -r .message.content`). Verify prompts contain the full claim text, source and quote; Fetch prompts contain the URL.

## Rule

A resumed session ("Resumed run …") first re-reads the current `SKILL.md`; it overrides any earlier resume rule in context. This is the one exception to steps 5–6. Checked in order:

1. No Workflow call yet in this session → continue from step 5; it is still the single run.
2. **Completed run** (`workflows/<runId>.json` has `status` `completed`): the result is authoritative; do not use the journal and do not re-fetch.
   - `findings` present → use them.
   - Every claim in `unverified` (fewer than 2 valid votes): run a fresh round of 3 votes. Its `validVotes` are not reused. Prompt: the claim's original vote prompt (find it with a fixed-string search, `grep -lF -f <file holding the claim text> agent-*.jsonl` (claims contain apostrophes and regex characters), keeping only agents whose `agent-<id>.meta.json` has `"workflowPhase":"Verify"`; take its first line), changing only the voter number. This is the one journal-side lookup on this path.
3. **Killed run** (no completed json): continue the script's pipeline from where it stopped, using the script's own rules and prompts from the `Script file:`: `FETCH_PROMPT`, `VERIFY_PROMPT`, URL dedup, `MAX_FETCH`, the claim ranking (importance, then source quality) and `MAX_VERIFY_CLAIMS`, 3 votes per claim.
   - Journals from legacy `resumeFromRunId` attempts (two attempts under one runId) are out of scope: none was killed, and new ones cannot arise now that `resumeFromRunId` is banned.
   - No Search results → call the deep-research Workflow once more with the same args; it is still the single run.
   - Fetch: reuse returned results; run the missing ones (started without a result, or never started within the budget) with `FETCH_PROMPT`.
   - Verify: select the claims as the script does from all Fetch claims. A claim with ≥ 2 valid votes keeps them; any other selected claim gets a fresh round of 3 votes with `VERIFY_PROMPT`. Find a claim's existing votes by searching the Verify agents' prompts for its claim text and source (the same fixed-string search, Verify agents only; the prompt holds the claim after the script's `webText()` normalization: quote-family and control characters removed, whitespace collapsed), then read their `refuted` from the journal by agentId.
4. Every re-run is one Agent call with the prompt text (harness header removed, dedented), ending with an instruction to reply with only JSON (`{refuted, evidence, confidence}` for votes; the script's fetch fields for Fetch). At most 10 in parallel.
5. Decide votes as the script does: ≥ 2 refutes → refuted; ≥ 2 valid votes and < 2 refutes → confirmed; else unverified (Gaps).
6. A later resume of this session also reuses the votes and fetches that earlier resumes already got back (they are in this conversation, not under `workflows/`).
7. Synthesis: if there are no `findings`, merge the confirmed claims yourself while writing the report (step 7 already synthesizes).
8. Steps 7–8, doing only what is missing: report committed and pushed, link on the issue, hand-off comment, In Review. Never repeat the "research started" comment; never move the issue to Todo. If nothing usable exists even after the re-runs, publish no report: comment what failed and set In Review.

Read large files with `jq` projections (the json is one long line; the journal is hundreds of KB): e.g. `jq -c 'select(.type=="result") | {agentId, refuted: .result.refuted}'`; read an agent's prompt with `head -1 agent-<id>.jsonl | jq -r .message.content`.

## Runner change

`linear-research.sh` resume prompt: `Resumed run <k> for <ISSUE> (<url>) after an interruption. Re-read <skill dir>/SKILL.md — it may have changed since this session started — and follow its resume rule.` `<skill dir>` is resolved by the script. Dispatcher test updated.

## Verification

- Dispatcher unit test: resume prompt text.
- Rehearsal (read-only), from `~/playground/linear-research/work`: fork TASK-15's session `1b48b9a1-d118-4319-a285-904b45200fb5` with `--fork-session`, `--permission-mode dontAsk`, `--add-dir ~/.claude`, tools `Read,Grep,Glob,Bash(jq:*),Bash(head:*),mcp__linear-server__get_issue,mcp__linear-server__list_comments`; prompt = the new resume prompt (k=2) plus "Read-only rehearsal: list what you would reuse and exactly which agents you would re-run, then stop." Expect: re-reads SKILL.md; reuses `wf_0059deae-35d.json` (23 confirmed); plans 3 votes for the 1 unverified claim; no `Workflow` or `Agent` call in the transcript. Try Haiku first; if the fork is rejected, use Opus.
- Pass for the TASK-15 rehearsal: it plans exactly the 3 votes for the unverified claim (a fresh round), not the 8 unfinished Verify agents of the earlier resume attempt nor the Synthesize agent `a2c0ce9452b8a981c`.
- Rehearsal 2 (killed run): read-only fork of session `d0d8004c-e490-457e-91eb-e3ae42f93cd9` (project `-Users-francis--local-state-linear-research-work`, run `wf_e2394e9e-32a`, killed during Scope; the only local run without a result file), same flags, run from `~/playground/linear-research/work` (`--resume <id>` finds sessions of other projects since v2.1.223; if it does not, create `~/.local/state/linear-research/work` and run there). Pass = it picks "no Search results → call the Workflow once more" and makes no `Workflow` or `Agent` call. No local run was killed during Fetch or Verify, so the journal-rebuild path is first checked on a real occurrence.
- The first real resume after merge is reviewed against the rule.
