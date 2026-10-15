# Light Build

Build the requirement with `/autopilot:light-build`, then open a pull request.

## Input processing

[Engineer › Input processing](#input-processing), the requirement being the input's text; a PRD, if given, is context for it.

## Steps

1. **Read** the input. The user's requirements outrank the requirement, which outranks a PRD.
2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.
3. **Which build:**
   - The newest plan doc (`plan_docs`' last) not at `done` → continue it.
   - Else a plan doc or a `pr` (a **finished build**) and a user requirement → a new build of the user's requirements. Review input alone never starts one.
   - Else a finished build → `needs_input` asking what next; stop.
   - Else → build the requirement.
4. **Report progress:**
   [agent-pm-progress:start] the build (continue, new or first)
5. **Build**: continuing → [Engineer › Repo › Continue](#repo); else `/autopilot:light-build` ([Engineer › Autopilot](#autopilot)), the requirements being the requirement, a PRD if given and the user's requirements; docs line "The plan doc stays where autopilot puts it; S7 copies it to where the repo keeps design docs; never `git add -f` it."; push points: S5 and each fix.
   - **Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:
     [agent-pm-progress:step] the step and its result; an S5 task as `S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` the plan doc's task count (its `### Task` headings)
6. **Finish** ([Engineer › Finish](#finish)).

## Resume

[Engineer › Repo › Resume](#repo).
