
from orchestrator_test_helpers.unit.router.tick_base_fixtures import TickBase


class TickGate(TickBase):
    """FR-46, FR-48, FR-60 over a tick: each candidate gates on its runtime; a resume keeps its recorded runtime."""
