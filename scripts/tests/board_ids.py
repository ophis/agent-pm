"""Linear team and state ids for the tests' pipeline.toml fixtures and fake Linear servers."""
TEAM = "00000000-0000-4000-8000-000000000001"
STATES = {k: "00000000-0000-4000-8000-0000000000%02d" % i for i, k in
          enumerate(("todo", "in_progress", "in_review", "handoff", "done", "canceled"), start=11)}
TASK_GROUP = "00000000-0000-4000-8000-000000000002"
DOCS_CLONE = "/nonexistent/acme-notes"
# Inline tables: fixtures may append top-level keys or [roles] tables after it; docs stays the last line.
HEADER = ('team = "%s"\nharness_key = "linear-api-key"\nstates = { %s }\n' % (TEAM, ", ".join('%s = "%s"' % kv for kv in STATES.items()))
          + 'task_label_group = "%s"\n' % TASK_GROUP
          + 'docs = { repo = "acme/notes", clone = "%s", branch = "trunk" }\n' % DOCS_CLONE)


def team_node(state_ids=None):
    """teams.nodes[0] of pipeline.Q_TEAM."""
    return {"id": TEAM, "name": "Team", "states": {"nodes": [{"id": i} for i in (state_ids or STATES.values())]}}
