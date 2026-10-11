"""The Composer: reads the team root (TEAM: guide.md, principles.md, roles/, tasks/, templates/, methods/, output/) and
the team dirs (the core config's team_dirs) in place, resolves a role's run config and assembles its prompt; the agent
run reads its task's file itself. It imports only repo and the stdlib (tests/guards.py layer_imports). Ported from the
old core's compose.py, plus its driver's team dir checks.

Formats of roles/, tasks/ and templates/ files, the team root's and a team dir's:
- roles/<role>.md, a charter: a `# ` title, an intro, then `## Tasks`, the role's task index and the only list of its
  tasks: an opening sentence naming the default, "unsure → `<task>` (`<tasks>/<task>.md`)", then one line per task,
  "- `<task>` (`<tasks>/<task>.md`): …". The default is a listed task; each task's file is in the role's own tasks
  dir (a team dir's role: that team dir's tasks/, else the team root's).
- tasks/<task>.md: a `# ` heading, exactly one `[agent-pm-progress:start] <what to report>` line (a progress mark,
  PROGRESS) and a `## Resume` section outside code fences.
- Text only: no front matter (---), no {{…}} placeholder; each `<methods>/<m>.md` named is a file of the team root's
  methods/.
A role's index is checked whenever it is read, a team dir file whenever it is looked up (Team.lookup); a defect raises
ConfigError naming the file.
"""
