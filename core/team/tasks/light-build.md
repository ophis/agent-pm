---
description: "Build a small, clearly specified change into a pull request on its target repo with autopilot:light-build: implementation, verification and a light review, no spec or plan docs, then push the branch and open the PR. Use when the requirement text alone is enough to build from; for a PRD that needs a spec and plan, use engineer build."
---

# Light Build

Build the requirement with `autopilot:light-build`, then open a pull request.

## Input

Engineer › Input, the requirement being the input's text; a PRD, if given, is context for it.

## Steps

1. **Read** the input. The user's requirements outrank the requirement, which outranks a PRD.
2. **Repo** (Engineer › Repo); from status, count only entries after `<worktree>`'s latest commit (`git -C <worktree> log -1 --format=%cI`).
3. **Which build:**
   - A light-build state file in `<worktree>` (its `RESUME:` line) → continue it, adding the user's requirements.
   - Else a `pr` (a **finished build**) and a user requirement → a new build of the user's requirements. Review input alone never starts one.
   - Else a finished build → `needs_input` asking what next; stop.
   - Else → build the requirement.
4. **Report progress:**
   [agent-pm-progress:start] the build (continue, new or first)
5. **Build**: `autopilot:light-build` (Engineer › Autopilot), the requirements being the requirement, a PRD if given and the user's requirements; docs line "A state file, if you write one, goes where the repo keeps design docs, else in `docs/.autopilot/`; never `git add -f` it."; push points: S5 and each fix.
   - **Report progress** at the end of each build step (`S<n>`) you run:
     [agent-pm-progress:step] the step and its result
6. **Finish** (Engineer › Finish).

## Resume

Engineer › Resume.
