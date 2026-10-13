
from orchestrator_test_helpers.unit.outage import FIELDS
from orchestrator_test_helpers.unit.router.tick_base_fixtures import TickBase


class Outage(TickBase):
    SAID = {name: f"linear-error op=teams {fields}" for name, fields in FIELDS.items()}
