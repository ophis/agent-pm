# Build

Build the PRD with `autopilot:build`, then open a pull request.

## Input processing

[Engineer › Input processing](#input-processing), the requirement being the PRD (a path or its text).

## Steps

1. **Read** the input and the PRD. The user's requirements outrank the PRD.
2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.
3. **Which build:**
   - A plan doc before S9 → continue it, first updating its spec and plan to the user's requirements.
   - Else a plan doc at S9 (a **finished build**) and a user requirement → a new build, new plan doc. Review input alone never starts one.
   - Else a finished build → `needs_input` asking what next; stop.
   - Else → a first build of the PRD.
4. **Report progress:**
   [agent-pm-progress:start] the build (continue `<plan doc>`, new or first)
5. **Build**: `autopilot:build` ([Engineer › Autopilot](#autopilot)), the requirements being the PRD and the user's requirements; docs line "Spec and plan go where the repo keeps design docs, else in `docs/.autopilot/`; commit them unless git ignores them, never with `git add -f`."; push points: each task and review round.
   - **Report progress** at the end of each build step (`S<n>`) you run:
     [agent-pm-progress:step] the step and its result
6. **Finish** ([Engineer › Finish](#finish)), adding `files`: this build's spec (its plan doc's `spec_file=`) and plan doc, whichever exist on failure; the PR description names the PRD (path or title).

## Resume

[Engineer › Resume](#resume).
