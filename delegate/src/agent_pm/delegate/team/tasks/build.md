# Build

Build the PRD with `/autopilot:build`, then open a pull request.

## Input processing

[Engineer › Input processing](#input-processing), the requirement being the PRD (a path or its text).

## Steps

1. **Read** the input and the PRD. The user's requirements outrank the PRD.
2. **Repo** ([Engineer › Repo](#repo)); from status, act on `plan_docs` too.
3. **Which build:**
   - The newest plan doc (`plan_docs`' last) not at `done` → continue it, its verbatim input the requirement; autopilot's resolver `mode` not `resume` → `needs_input` naming the plan doc; stop. Once it resumes, give it the rest as mid-run input, each logged in its progress log: the user's requirements, the review input (marked as [Engineer › Autopilot](#autopilot) says), failing merge checks, and with a user requirement a review cap 2 rounds beyond its RESUME `review_round` (a capped plan doc stops again at once). Added to its input, they would start a new plan doc at HEAD, leaving its commits unreviewed.
   - Else a plan doc or a `pr` (a **finished build**) and a user requirement → a new build, new plan doc. Review input alone never starts one.
   - Else a finished build → `needs_input` asking what next; stop.
   - Else → a first build of the PRD.
4. **Report progress:**
   [agent-pm-progress:start] the build (continue `<plan doc>`, new or first)
5. **Build**: `/autopilot:build` ([Engineer › Autopilot](#autopilot)), the requirements being the PRD and the user's requirements; docs line "Spec and plan doc stay where autopilot puts them; S7 copies them to where the repo keeps design docs; never `git add -f` them."; push points: each task and review round.
   - **Report progress** at the end of each build step (`S<i>`) you run and of each S5 task:
     [agent-pm-progress:step] the step and its result; an S5 task as `S5 task <k>/<n> done: <commit>; <checks run>`, `<k>` its number, `<n>` the plan doc's task count (its `### Task` headings)
6. **Finish** ([Engineer › Finish](#finish)), adding `files`: this build's spec (the `-spec.md` beside its `-plan.md` plan doc) and plan doc (on failure, those that exist); the PR description names the PRD (path or title).

## Resume

[Engineer › Repo › Resume](#repo).
