# Product Design

Turn the input into a reviewed PRD.

## Steps

1. **Read** the input. Precedence: the **user's later words** (their messages after the **brief**, the request) > the brief > reports.
2. **Judge.** What to build unclear → `needs_input`, stop; only the audience or depth unclear → self-grill. Decide whether the PRD needs **research** and a **self-grill** (open decisions or rival approaches).
3. **Report progress:**
   [agent-pm-progress:start] whether the PRD needs research and a self-grill
4. **Research**, if needed, only what the PRD needs; too deep for now → Risks. The input names a target repo → check it out first (Principles › Worktree), `<branch>` `<id>-product-design`, and read the product's code there, read-only; exit 2 or 1 → the error under Risks, go on without the code.
5. **Self-grill**, if needed: one fresh subagent, in one round, lists each key decision (one changing requirements or scope) with a suggested answer. Settle each in the PRD; what only the user can decide → Open questions.
6. **Write** the PRD per the Template; the outcome's `title` is `[Product name]` alone, unchanged on revision.
7. **Review.** One fresh subagent, given the PRD's full text, the brief and the user's later words, flags missing, contradictory or untestable requirements, scope beyond what the user asked for and over-engineering, asking no more rigor than the brief does; fix the findings that hold up, once.
8. **Finish.** `status: done`.

## Resume

Use this session's history; do only what's left:
- `needs_input` written this session → stop.
- Grilling subagent answered this session → use its answers; never spawn it again.
- PRD written this session → continue it, never rewrite it; then what's left of steps 7–8.
