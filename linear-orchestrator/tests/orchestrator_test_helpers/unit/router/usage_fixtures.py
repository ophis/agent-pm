import io
import os
from contextlib import redirect_stderr

from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.base_fixtures import Base


class Usage(Base):
    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = router.main(argv, gql=None, config_path=os.path.join(self.tmp, "absent.toml"))
        return rc, err.getvalue()
