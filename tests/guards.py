"""Repository-wide guard data: the layers, the trees the checks scan and the unit isolation
inventory. Each layer's own data: tests/guard_data/; the checks: tests/guard_checks/.
"""

from agent_pm.utils.config_file import LEVELS

UNIT_ISOLATION_ALLOWED = {}


PKG = "agent_pm"


PROJECTS = {p: p.replace("-", "_") for level in LEVELS for p in level}


LAYERS = tuple(PROJECTS)


TREES = (*PROJECTS, "tests", ".github")


TESTS = ("tests", *(f"{p}/tests" for p in PROJECTS))
