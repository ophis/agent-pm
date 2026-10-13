
from orchestrator_test_helpers.unit.router.base_fixtures import Base


class Limits(Base):
    """FR-60: a live limit row blocks its runtime with no probe; any other limit row changes nothing."""
