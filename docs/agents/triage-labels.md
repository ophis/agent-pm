# Triage Labels

On this board a triage role is a Linear state and assignee, not a label. When a skill says to apply a role's label, take the action in this table instead. Ids are in `issue-tracker.md`.

| Role | Action | Effect |
|---|---|---|
| `needs-triage` | Backlog, no assignee | none |
| `needs-info` | In Review; comment the questions; `issueSubscribe` the human | waits for the user |
| `ready-for-agent` | Todo; assignee = the role account; add the `Tasks` label if the task is not the role's default | **launches an unattended run within 30 min**; only on the user's go-ahead |
| `ready-for-human` | Todo; assignee = Frank Wang | none |
| `wontfix` | Canceled, with a comment saying why | none |
