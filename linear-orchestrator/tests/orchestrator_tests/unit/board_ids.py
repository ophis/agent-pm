TEAM = "00000000-0000-4000-8000-000000000001"
STATES = {
    k: f"00000000-0000-4000-8000-0000000000{i:02d}"
    for i, k in enumerate(
        ("todo", "in_progress", "in_review", "handoff", "done", "canceled"), start=11
    )
}
# Not a config state: prune finds it by its type.
DUPLICATE = "00000000-0000-4000-8000-000000000018"
TYPES = {
    **dict(
        zip(
            STATES.values(),
            ("unstarted", "started", "started", "started", "completed", "canceled"),
            strict=True,
        )
    ),
    DUPLICATE: "canceled",
}
FINISHED = frozenset({STATES["done"], STATES["canceled"], DUPLICATE})
TASK_GROUP = "00000000-0000-4000-8000-000000000002"
# Inline tables: fixtures may append top-level keys or tables after it.
HEADER = (
    'team = "{}"\nharness_key = "linear-api-key"\nstates = {{ {} }}\n'.format(
        TEAM, ", ".join(f'{k} = "{v}"' for k, v in STATES.items())
    )
    + f'task_label_group = "{TASK_GROUP}"\n'
    + 'delegates = { default = "harness", harness = { implementation = "agent_pm.delegate:HarnessDelegate" } }\n'
)
ACCOUNTS = {r: f"{r}@agents.test" for r in ("researcher", "pm", "engineer")}


def role(name, *lines):
    return (
        f'[roles.{name}]\naccount = "{name}@agents.test"\nkey = "linear-api-key-{name}"\n'
        + "".join(f"{x}\n" for x in lines)
    )


def team_node(state_ids=None):
    return {
        "id": TEAM,
        "name": "Team",
        "states": {
            "nodes": [{"id": i, "type": TYPES.get(i, "started")} for i in (state_ids or TYPES)]
        },
    }
