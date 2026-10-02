# Issue tracker: Linear (agent-pm board)

Issues live in the Linear team Frank's Agents (`06159b6b-5efe-4bc5-a27b-875701f40d61`), which agent-pm drives. Use the `linear` skill (`linear.py`). Pass text as variables. Refer to states, users, projects and labels by id.

## Read this first

- **Todo with a role account as assignee launches an unattended Claude run** (the router ticks every 30 min). Do this only for `ready-for-agent`, and only after the user confirms.
- State and assignee are the state machine. Labels start nothing, except a `Tasks` label, which picks the role's task.
- Never set Handoff or Done; the user moves issues there. Never edit an issue that is In Progress.

## Board

States:

| State | id |
|---|---|
| Backlog | `802ba76c-1d23-46fa-8140-97da583ed0b1` |
| Todo | `717e27f7-c97e-43d8-aa47-13d513ad8ec6` |
| In Progress | `cda9dcfe-c2ab-4853-8e17-8d6ad76b0795` |
| In Review | `5efb3bbc-ea0b-4982-a3d5-31338c9bade3` |
| Handoff | `6d876dc6-7732-4473-a0b2-30041d93d0aa` |
| Done | `414a4381-7260-46e7-8057-d8cec9d4b11f` |
| Canceled | `ed6093ef-3f2e-4e8e-bb59-65ec56ae1938` |

Assignees (the assignee is the stage):

| Assignee | User id | Task | Description holds |
|---|---|---|---|
| researcher | `1fd9a4e1-60ab-4056-80a3-eb32d17e772e` | deep-research (default); light-research with label `7cb3a7cc-05b4-4dec-bbf8-d4fce87cea1d` | the question |
| pm | `c9281896-83a0-4588-89f9-de2dd9dd45d4` | product-design | the brief |
| engineer | `409785ce-da51-404b-ab4d-1665ebea0d72` | engineering | `## Source` (PRD link), `## Instructions` (`Repo: <owner>/<name>`, optional in a mapped project, and the phase to build) |
| Frank Wang (human) | `87e5948e-29db-4188-ab8d-bb908f4559ec` | none; the router ignores this assignee | |

Engineering issues normally come from promote after the user approves a PRD. Create one directly only when the user asks.

Projects (the project is the product):

| Project | id | Engineering repo |
|---|---|---|
| Agent PM | `121166b1-191a-4461-bec4-42f1c2dc0ddd` | `ophis/agent-pm` |
| Autopilot | `ae72ede7-67a6-469d-a959-8ea51ab71fb8` | `ophis/claude-autopilot` |
| Tax Agent | `5f6769c8-8a65-4e9e-bebc-606ef8a8643d` | none: needs a `Repo:` line |
| MISC | `c71d11cc-1e51-4dea-b784-34ea73495648` | none: needs a `Repo:` line |

Ask the user when the product is unclear.

## Conventions

- **Create**: `linear.py 'mutation($i: IssueCreateInput!) { issueCreate(input: $i) { issue { identifier url } } }' '{"i": {"teamId": "…", "projectId": "…", "stateId": "…", "title": "…", "description": "…"}}'`. Optional fields: `assigneeId`, `parentId`, `labelIds`.
- **Read**: `issue(id: "TASK-123") { title description state { name } assignee { name } labels { nodes { name } } comments { nodes { body createdAt user { name } } } }`.
- **List**: `issues(filter: { team: { id: { eq: "<team id>" } }, state: { id: { eq: "<state id>" } } }, first: 50)`.
- **Comment**: `commentCreate(input: { issueId: "…", body: "…" })`.
- **Move or assign**: `issueUpdate(id: "…", input: { stateId: "…", assigneeId: "…" })`.
- **Labels**: `issueAddLabel(id: "…", labelId: "…")` and `issueRemoveLabel(id: "…", labelId: "…")`.

## When a skill says "publish to the issue tracker"

Create the issue in Backlog with no assignee (`needs-triage`). Move it to `ready-for-agent` only when the user says so.

## When a skill says "fetch the relevant ticket"

Run the Read query with the identifier.

## Wayfinding operations

- **Map**: a parent issue. Each child ticket sets `parentId` to the map.
- **Blocking**: `issueRelationCreate(input: { issueId: "<blocker>", relatedIssueId: "<blocked>", type: blocks })`. A ticket is unblocked when every blocker is Done or Canceled.
- **Frontier query**: the map's children that are not Done or Canceled, have no open blocker and no assignee. The first by sort order wins.
- **Claim**: assign to Frank Wang. Never assign a role account; that launches a run.
- **Resolve**: comment the answer, ask the user to move the ticket to Done, then append a context pointer (gist and link) to the map's Decisions-so-far.

## Pull requests as a triage surface

**PRs as a request surface: no.**
