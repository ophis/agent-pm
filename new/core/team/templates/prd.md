# PRD: [Reference] [Product name]

Title: no gloss; `[Product name]` never repeats `[Reference]`. One fact per list item or FR line; its reason goes under Assumptions or Approach and trade-offs, not on that line.

## Problem and goals
Opens with one sentence: the problem, for whom, and the goal. Then list items: its evidence (a report finding, a source or the user's words), the goals, and what it explicitly won't do.

## Users and scenarios
Target users and where they use it.

## User flow
The main flow from start to finish: what the user sees and does at each step; a diagram where [Principles › Writing › Diagrams and tables](#writing) calls for one.

## Assumptions, open questions and risks
### Assumptions
Inferences the PRD rests on; one resting on research cites the finding and its confidence.
### Open questions
What the user must decide, each with a suggested answer; until the user answers, the suggested answer holds.
### Risks
Unknowns, and the low-confidence or single-source findings the PRD relies on.

## Approach and trade-offs
Optional; omit when nothing is compared. 2–3 viable approaches and their trade-offs as a table ([Principles › Writing › Diagrams and tables](#writing)), which one is chosen and why.

## Requirements
Ids (`FR-<n>`, `NFR-<n>`) are never renumbered on revision.
### Functional requirements
Each: `- FR-<n>: <the requirement>` on one line, then one indented sub-item `  - <Check, translated>: <an observable pass condition (an input → an output or state)>`.
### Non-functional requirements
Optional; omit when none. One per line: `NFR-<n>`: a constraint this product actually has, with its threshold or check.

## Done
Optional; omit when the product has nothing built yet. Under `###` copies of the original headings, by id, naming where each is implemented (file or commit).
