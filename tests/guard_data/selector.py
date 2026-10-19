"""The selector layer's guard data (tests/guard_data/__init__.py)."""

from tests.guards import PKG

SELECTOR_SRC = f"selector/src/{PKG}/selector/"


SUBPACKAGES = {
    "selector": {"backends"},
}


OBJECT_EXPORT_MODULES = ("selector/src/agent_pm/selector/__init__.py",)


OBJECT_EXPORTS_ALLOWED = {
    (f"{PKG}.selector", "JEV_DEFAULT"): "TASK-744 Jev's default SelectorConfig, for the router",
}
