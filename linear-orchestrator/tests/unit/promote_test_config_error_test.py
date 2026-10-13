import io
import os
from contextlib import redirect_stderr, redirect_stdout

from agent_pm.linear_orchestrator.engine import promote
from orchestrator_test_helpers.unit.board_ids import role as role_table
from orchestrator_test_helpers.unit.promote.test_config_error_fixtures import (
    TestConfigError as TestConfigErrorBase,
)
from orchestrator_test_helpers.unit.promote_support import CONFIG, NOW, FakePruner
from orchestrator_test_helpers.unit.run_fixtures import logged, show


class TestConfigError1(TestConfigErrorBase):
    def test_a_config_error_is_logged_once_a_day_printed_only_on_a_terminal(self):
        from orchestrator_test_helpers.unit.streams import Stderr

        FakePruner.calls.clear()
        cases = (
            ("bogus = 1\n" + CONFIG, "linear-orchestrator/config.toml: unknown keys: bogus"),
            (
                CONFIG + role_table("bogus"),
                "linear-orchestrator/config.toml: role 'bogus' is not in delegate/config.toml",
            ),
        )
        for text, reason in cases:
            with self.subTest(reason=reason):
                self.write_config(text)
                before = len(logged(self.logs))
                for tty, printed in ((False, ""), (True, f"promote.py: {reason}\n")):
                    err, out = Stderr(tty), io.StringIO()
                    with redirect_stderr(err), redirect_stdout(out):
                        rc = promote.main(
                            [],
                            gql=self.fake,
                            now=NOW,
                            config_path=self.config_path,
                            pruner=FakePruner,
                        )
                    self.assertEqual((rc, err.getvalue(), out.getvalue()), (1, printed, ""))
                events = logged(self.logs)[before:]
                self.assertEqual(
                    [(e["src"], show(e)) for e in events],
                    [("promote", f"config-error reason={reason}")],
                )
        self.assertEqual((self.fake.calls, FakePruner.calls), ([], []))

    def test_a_bad_work_dir_is_said_on_stderr_and_logs_nothing(self):
        self.write_local('work_dir = "w"\n')
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(
            self.err,
            "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n",
        )
        self.assertEqual((self.fake.calls, FakePruner.calls), ([], []))
        self.assertFalse(os.path.exists(self.logs))
