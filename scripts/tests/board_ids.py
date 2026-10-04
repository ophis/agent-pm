"""Linear team and state ids for the tests' pipeline.toml fixtures and fake Linear servers."""
TEAM = "00000000-0000-4000-8000-000000000001"
STATES = {k: "00000000-0000-4000-8000-0000000000%02d" % i for i, k in
          enumerate(("todo", "in_progress", "in_review", "handoff", "done", "canceled"), start=11)}
TASK_GROUP = "00000000-0000-4000-8000-000000000002"
# Inline tables: fixtures may append top-level keys or tables after it.
HEADER = ('team = "%s"\nharness_key = "linear-api-key"\nstates = { %s }\n' % (TEAM, ", ".join('%s = "%s"' % kv for kv in STATES.items()))
          + 'task_label_group = "%s"\n' % TASK_GROUP)
ACCOUNTS = {r: f"{r}@agents.test" for r in ("researcher", "pm", "engineer")}


def role(name, *lines):
    """A [roles.<name>] table with its account and key, then lines such as 'next = "pm"'."""
    return f'[roles.{name}]\naccount = "{name}@agents.test"\nkey = "linear-api-key-{name}"\n' + "".join(f"{x}\n" for x in lines)


def team_node(state_ids=None):
    """teams.nodes[0] of pipeline.Q_TEAM."""
    return {"id": TEAM, "name": "Team", "states": {"nodes": [{"id": i} for i in (state_ids or STATES.values())]}}
