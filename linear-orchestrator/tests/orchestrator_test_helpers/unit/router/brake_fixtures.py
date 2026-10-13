import io
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.base_fixtures import Base


class Brake(Base):
    """`--brake ROLE`: ROLE's runtime usage against BRAKE_5H and MAX_WEEK; no lock, no limit row."""

    def brake(self, role):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
        ):
            rc = router.main(["--brake", role], config_path=self.config_path, driver=self.api)
            self.path = os.environ["PATH"]
        self.err = err.getvalue()
        return rc, out.getvalue()
