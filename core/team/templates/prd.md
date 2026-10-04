# PRD: [Reference] [Product name]

## Problem and goals
What problem, for whom, and its evidence (a report finding, a source or the user's words); the goals.

## Non-goals
What it explicitly won't do.

## Users and scenarios
Target users and where they use it.

## User flow
The main flow from start to finish: what the user sees and does at each step.

## Assumptions, open questions and risks
### Assumptions
Inferences the PRD rests on; one resting on research cites the finding and its confidence.
### Open questions
What the user must decide, each with a suggested answer; until the user answers, the suggested answer holds.
### Risks
Unknowns, and the low-confidence or single-source findings the PRD relies on. A risk the user accepts is marked accepted, with why.

## Approach and trade-offs
Optional; omit when nothing is compared. 2–3 viable approaches and their trade-offs, which one is chosen and why.

## Requirements
Ids (`FR-<n>`, `NFR-<n>`, `P<n>`) stay as written, in ASCII, and are never renumbered on revision. Tag each requirement new, changed or removed against the current code, and whether the system or the user ensures it; give the concrete values it ships with (config entries, ids, defaults).
### Functional requirements
One per line: `FR-<n>`: the requirement. `Check:` an observable pass condition (an input → an output or state).
### Non-functional requirements
Optional; omit when none. One per line: `NFR-<n>`: a constraint this product actually has, with its threshold or check.

## Success metrics
How and when each goal is measured.

## Scope and phased delivery
One `### P<n>: <name>` per phase, in delivery order, each shippable alone: one numbered order interleaving its build steps (the requirement ids each delivers) with each step only the user can do (accounts, keys, settings, deploys), each user step marked needed before build or only before deploy, with its exact command; then its exit check. One phase → `P1` only.

## Done
Optional; omit when the product has nothing built yet. What the product already has, under `###` copies of the original headings, by id, naming where each is implemented (file or commit). Other sections hold only what's left, except a shipped phase, which stays in Scope and phased delivery marked ✅.
