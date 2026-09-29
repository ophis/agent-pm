"""Linear team and state ids for the tests' pipeline.toml fixtures and fake Linear servers."""
TEAM = "00000000-0000-4000-8000-000000000001"
STATES = {k: "00000000-0000-4000-8000-0000000000%02d" % i for i, k in
          enumerate(("todo", "in_progress", "in_review", "handoff", "done", "canceled"), start=11)}
# Inline table: fixtures may append top-level keys or [projects] tables after it.
HEADER = 'team = "%s"\nstates = { %s }\n' % (TEAM, ", ".join('%s = "%s"' % kv for kv in STATES.items()))


def team_node(state_ids=None, projects=()):
    """teams.nodes[0] of pipeline.Q_TEAM; projects: (id, name) pairs."""
    return {"id": TEAM, "name": "Team", "states": {"nodes": [{"id": i} for i in (state_ids or STATES.values())]},
            "projects": {"nodes": [{"id": i, "name": n} for i, n in projects]}}
