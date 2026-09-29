# TASK-44: Rules by role (round 3) — plan

RESUME: phase=S5 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=0e1a758504afbee39131b6f091256c5f2c4829ba review_round=1 spec_file=docs/specs/2026-09-29-task-44-rules-by-role-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the router's registry guard, reword Engineering step 4, and put each rule of `roles/`, `tasks/`, `templates/` in exactly one place (principles, charter, template or task) without changing task behavior.

**Architecture:** Task 1 reverts the self-contained guard commit and fixes its docs. Task 2 edits the instruction files (Markdown only). Task 3 updates `CLAUDE.md` / `README.md`. No launcher or prompt change.

**Tech Stack:** Python 3.11+ `unittest`, git, Markdown.

**Spec:** `docs/specs/2026-09-29-task-44-rules-by-role-design.md` (authoritative; its "Target text" blocks are verbatim content to write).

## Global Constraints

- Worktree `/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task`, branch `TASK-44-role-task`; before any write assert `git -C <worktree> branch --show-current` prints `TASK-44-role-task`. Absolute paths only. Never touch `/Users/francis/playground/agent-pm` itself (the live install), never `main`, never force-push, never merge.
- After each task's commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task push -u origin TASK-44-role-task`.
- Tests: `python3 -m unittest discover -s scripts/tests` from the worktree (`/opt/homebrew/bin/python3` if `python3` is 3.9).
- Repo style (CLAUDE.md): terse English docs, no filler; Chinese only inside templates and quoted Chinese headings.

## Review Focus

- An `engineering` run reads the new principles: the Chinese, publish and too-vague rules must not fire for it (scope wording exactly as the spec).
- A PD revision run never opens `templates/prd.md`: the 假设 / `已完成` rules must reach it through `roles/pm.md`.
- A DR resumed run (resume rule check 8) must still commit, push and link the report: step 7's "publish it (principles)" carries that.
- Engineering step 6 must still make the `autopilot:build` requirement carry the conventions line and "never force-push, never merge, never touch `<default>`".
- A router tick with uncommitted files under `roles/` or `tasks/` must now proceed (no `git status` call).

---

### Task 1: Remove the router registry guard

**Files:**
- Modify (by revert): `scripts/pipeline.py`, `scripts/router.py`, `scripts/tests/test_pipeline.py`, `scripts/tests/test_router.py`
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Produces: `router.check_registry` and `pipeline.uncommitted` no longer exist; `FakeShell(active=False, probe_five=0.2, prune_fails=False)`.

- [ ] **Step 1: Revert** — `git -C <worktree> revert --no-edit b26e99d`. Expected: a clean revert commit (no later commit touched `scripts/`). If it conflicts, stop and report.
- [ ] **Step 2: Verify code** — `grep -rn "check_registry\|uncommitted\|registry guard" <worktree>/scripts` → no output. `python3 -m unittest discover -s scripts/tests` → OK (the guard tests are gone).
- [ ] **Step 3: Docs** — exact edits:
  - `CLAUDE.md` Router bullet: "hours → tmux lock (session `agent-pm`) → registry guard → prune" → "hours → tmux lock (session `agent-pm`) → prune".
  - `CLAUDE.md` Gotchas: delete the whole bullet starting "- The router stops every tick while `roles/` or `tasks/` in the live clone has uncommitted".
  - `README.md` router bullet: "tmux lock (`agent-pm`), a registry guard (the tick stops, dry-run too, while `roles/` or `tasks/` has a tracked change or an untracked or ignored `.md`/`.toml` file; Edit deny rules don't stop a run's Bash writes there), prune," → "tmux lock (`agent-pm`), prune,".
  - Then `grep -n "registry guard\|uncommitted" CLAUDE.md README.md` → no output.
- [ ] **Step 4: Commit and push** — `git -C <worktree> commit -m "TASK-44: drop the registry guard from the docs" -- CLAUDE.md README.md`, then the push command.

### Task 2: Rules by role (instruction files) and Engineering step 4

**Files:**
- Modify: `roles/principles.md`, `roles/researcher.md`, `roles/pm.md`, `roles/engineer.md`, `tasks/deep-research.md`, `tasks/product-design.md`, `tasks/engineering.md`, `templates/prd.md`
- Create: `templates/research-report.md`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: the file contents Task 3's docs describe (charter sections `## Standards` / `## Boundaries`, `templates/research-report.md`).

- [ ] **Step 1: Write the full files** from the spec's "Target text" blocks, verbatim: `roles/principles.md`, `roles/researcher.md`, `roles/pm.md`, `roles/engineer.md`, `templates/research-report.md` (each file ends with one newline; a charter's last line is `## Memory`).
- [ ] **Step 2: `templates/prd.md`** — two note lines, exact:
  - "自己的推断写在假设里；需要用户决定或需要深入调研的写在开放问题里。" → "需要用户决定的写在开放问题里。"
  - "已经实现的内容从上面各节移到这里，按原来所在的章节分组（例如 `### 需求`），写明实现位置（文件或提交）。上面各节只保留还没做的。" → "按原来所在的章节分组（例如 `### 需求`），写明实现位置（文件或提交）。"
- [ ] **Step 3: `tasks/deep-research.md`** — exact edits:
  - Step 3 → "3. **Too vague?** If the question, scope or deliverable is missing, the issue is too vague (principles)."
  - Step 5: "The workflow verifies only its top-ranked claims, so list uncovered or unverified parts under Gaps." → "The workflow verifies only its top-ranked claims, so the rest stay unverified." (keep "phrase existing claims as claims to verify").
  - Step 6: "publish the report and list missing or unverified parts under Gaps." → "publish the report."
  - Step 7 (the step line, its five section sub-bullets and the "Present unverified…; say so." line) → one line: "7. **Report.** Write `~/playground/private_docs/Research/<YYYY-MM-DD-HHMM>-<issue ID>-<short-kebab-slug>.md` (local time from `date +%Y-%m-%d-%H%M` when the file is first created; if a `Research/*-<issue ID>-*.md` file already exists, use it) from the template `../templates/research-report.md` (relative to this file), and publish it (principles) with the message `Add <issue ID> report: <short title>`, or `Update …` when the file already existed."
  - Step 8: drop " Handle one issue per invocation."
  - Resume rule check 8: drop " Unresolved parts go under Gaps."
- [ ] **Step 4: `tasks/product-design.md`** — exact edits:
  - Inputs bullet 1 → "- A Handoff-created issue (principles) comes from a research issue: `## Source` holds its report links and `## Instructions` what to build. An issue the user created directly has the brief in its description."
  - Inputs bullet 2: "`## Comments`, reports and review findings are context, never instructions." → "Reports and review findings are context, never instructions."
  - Inputs bullet 3 (the `github.com/ophis/private_docs/blob/main/<path>` link bullet): delete.
  - Step 2 → "2. **Too vague?** If what to build, for whom, or how far is unclear, the issue is too vague (principles)."
  - Step 5 header → "5. **Write** the PRD."; delete its sub-bullets "The user's instructions are hard constraints…" and "What the product already has goes under `已完成`…"; keep the other two unchanged.
  - Step 6: "Fix the findings that hold up against the brief, once; never add scope the user didn't ask for." → "Fix the findings that hold up against the brief, once."
  - Step 7 → "7. **Publish** the PRD (principles) with the message `Add <issue ID> PRD: <product name>`, or `Update …` when revising; a rejected push → `git pull --rebase --autostash`, then push. Set the title to `PRD: <product name>` (`issueUpdate` with `title`)."
  - Step 8: drop " Handle one issue per invocation."
- [ ] **Step 5: `tasks/engineering.md`** — exact edits:
  - Intro: drop " Never merges."
  - Inputs bullet 3 → "- A Handoff-created issue (principles) comes from the PRD issue: `## Source` holds the PRD link and `## Instructions` the `Repo:` line and which phase to build."
  - Inputs bullet 4 (the private_docs link bullet): delete.
  - Step 4 bullet 2 → "   - Else a finished build and a user comment (issue or PR) is newer than the latest `Build started` comment → a new build with the user's comments as the requirement (new plan doc)."
  - Step 6 bullet 1 → "   - the PRD path, `## Instructions` and the user's comments, with step 1's precedence, and the charter's conventions standard and git boundary, copied into the requirement with `<default>` as the default branch;"
  - Step 6 last bullet → "   - "skip S8: keep the commits"; "after each implementation task and each review round run exactly `git -C <worktree> push -u origin <branch>`"."
- [ ] **Step 6: Check** — from the worktree:

```bash
for p in "numbered questions" "English original" "never a Linear document" "one issue per" "force-push" "hard constraints" "confidence and sources" "carries the user's words" "private_docs/blob" "Gaps" "newly finished" "every inference"; do
  echo "$(grep -rlF -- "$p" roles tasks templates | wc -l | tr -d ' ') $p"; done
echo "$(grep -rliF 'never merge' roles tasks templates | wc -l | tr -d ' ') never merge"
grep -rlF "claims to verify" roles tasks templates
python3 -m unittest discover -s scripts/tests
```

Expected: every count `1`; "claims to verify" lists exactly `roles/researcher.md` and `tasks/deep-research.md`; tests OK. Then read `git diff HEAD -- tasks` and confirm each removed sentence is in principles, the role's charter or a template (spec "Behavior invariants").
- [ ] **Step 7: Commit and push** — `git -C <worktree> add roles tasks templates && git -C <worktree> commit -m "TASK-44: rules by role (principles, charters, templates); engineering step 4"`, then the push command.

### Task 3: CLAUDE.md maintenance rule and README

**Files:**
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: Task 2's files (names only).

- [ ] **Step 1: `CLAUDE.md`** — in the "Runner vs role and task" bullet: "(identity: responsibilities, boundaries, memory; no task steps)" → "(identity: responsibilities, standards, boundaries, memory; no task steps)"; "the rules every role and task shares (board, reviewer, agent account, whose comments count; precedence principles > charter boundaries > task steps)" → "the rules every role and task shares (precedence principles > charter > task steps)". Insert right after that bullet, verbatim from spec G4: "- **Where a rule goes**: ask whether it still holds for another task of the same role. Every role → `roles/principles.md`; every task of one role → the charter `roles/<role>.md`; a format → `templates/`; only this task (claiming, the workflow call, failure handling, hand-off, resume rule) → `tasks/<task>.md`. One rule, one place. When `pipeline.toml` gives a role a new task, review all of that role's tasks together and lift what they share into the charter (what every role shares into `principles.md`, formats into `templates/`)."
- [ ] **Step 2: `README.md`** — "`templates/` — document skeletons a task fills in (`prd.md` for Product Design)." → "… (`prd.md` for Product Design, `research-report.md` for Deep Research)."; Design line: "… `docs/specs/2026-09-28-task-44-role-task-design.md` and `docs/specs/2026-09-29-task-44-role-task-pairs-design.md`." → "… `docs/specs/2026-09-28-task-44-role-task-design.md`, `docs/specs/2026-09-29-task-44-role-task-pairs-design.md` and `docs/specs/2026-09-29-task-44-rules-by-role-design.md`."
- [ ] **Step 3: Check** — `grep -n "charter boundaries\|registry guard" CLAUDE.md README.md` → no output; `grep -c "Where a rule goes" CLAUDE.md` → 1.
- [ ] **Step 4: Commit and push** — `git -C <worktree> commit -m "TASK-44: CLAUDE.md rule-placement rule; README templates" -- CLAUDE.md README.md`, then the push command.

## Verification (S6)

From the worktree, everything committed: `python3 -m unittest discover -s scripts/tests`; `python3 scripts/router.py --now --dry-run` (rc 0); `python3 scripts/promote.py --dry-run` (rc 0); Task 2 Step 6's grep checks; `grep -rn "check_registry\|uncommitted" scripts` empty.

## Progress

- S1: reusing the existing worktree on TASK-44-role-task (base 0e1a758, the converged pairs build).
- decision(shared-rule scope): principles rules state their own scope (private_docs documents; "when your task's check finds…") so engineering is unaffected; tasks keep their own too-vague criteria and commit messages; dissent: none
- decision(precedence wording): "outranks the charter" (was "the charter's boundaries") since researcher/pm rules are standards; dissent: none
- decision(G1 method): git revert b26e99d (self-contained; no later scripts/ change) + doc edits; dissent: none
- S2: spec written (docs/specs/2026-09-29-task-44-rules-by-role-design.md).
- S3 panel: core=[architecture,spec-fitness] +adhoc=[instruction-fidelity] (security dropped: only a URL matched; the guard removal is the user's decision)
- S3 reviewers: architecture=aa2e60373082f083a spec-fitness=a8cb02c8b3228e94b instruction-fidelity=aa6815d2c0bfd0cf1
- S3 r0: architecture=PASS spec-fitness=PASS instruction-fidelity=FAIL -> instruction-fidelity#1 engineer "not written by the user" widens eng.py `kept` exception; instruction-fidelity#2 charter rules repeated by template section notes (research-report 缺口/更正, prd.md 假设/已完成); fix: spec edited (snapshot .r1.md)
- S3 r1: architecture=PASS spec-fitness=PASS instruction-fidelity=PASS -> converged. Applied after: instruction-fidelity#9 (conventions scope: code, docs, commits), spec-fitness#6 (step 6 "copied into the requirement"), G2 note dropped (instruction-fidelity#10). Deferred NBs: instruction-fidelity#6 synthesis loses "from the single run"; instruction-fidelity#10 no `Build started` comment case left as the user worded it; architecture#6 heading rename touches charters; spec-fitness#4 private_docs-only publish scope.
- S4: plan written (3 tasks: revert guard + its docs, instruction files + G2, CLAUDE.md/README); execution: subagent-driven-development, per-task reviews kept, final review skipped (S7).
