"""The Composer: reads the team root (TEAM: guide.md, principles.md, roles/, tasks/, templates/, methods/, output/) and
the team dirs (the core config's team_dirs) in place, resolves a role's run config (resolve: the core config, then the
caller's overlays; `runtime` an opaque key) and access intent (access), and assembles its prompt (render); the agent run
reads its task's file itself. It imports only utils and the stdlib (tests/guards.py layer_imports). Callers use this
package's names (composer.resolve(...)); modules: base (paths, ConfigError, text helpers), teams (Team, task indexes,
team dir checks), run (run config, params, access intent), prompt (render, reference checks).

Ported from the old core's compose.py and its driver's bind, access and team dir checks, except: the run reports in its
replies as prompt.RETURN asks (PROGRESS mark lines, one OUTCOME block), not through a report command; command approval
text is the Adapter's runtime_note, not RULE's; access and check_overlap live here, and the Delegate converts the
Intent for the Driver.

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
from composer.base import CONFIG, SCHEMA, SRC, TEAM, ConfigError, anchor, fill, outline, unfenced
from composer.teams import (PROGRESS, Team, catalog, check_overlap, check_team_dirs, index, roles, tasks_path, team,
                            team_dirs)
from composer.run import Intent, RunConfig, RunParams, access, bind, resolve, trusted_dirs
from composer.prompt import OUTCOME, reference_errors, render

__all__ = ["ConfigError", "SRC", "TEAM", "CONFIG", "SCHEMA", "PROGRESS", "OUTCOME", "RunConfig", "RunParams", "Intent",
           "Team", "team", "team_dirs", "tasks_path", "roles", "index", "catalog", "check_team_dirs", "check_overlap",
           "resolve", "trusted_dirs", "bind", "access", "render", "reference_errors", "outline", "unfenced", "anchor",
           "fill"]
