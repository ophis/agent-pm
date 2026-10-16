import os
import tempfile
from unittest import mock

from agent_pm.session import SessionError
from agent_pm.session.hosts.reclaimed import ReclaimedGroup
from agent_pm.utils import ProcessControl

from session.tests.case import Case


class ReclaimTest(Case):
    def test_a_token_naming_no_controller_reclaims_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            for token in (None, 5, "", "junk", home, os.path.join(home, "apm-group-none")):
                with self.subTest(token=token):
                    self.assertIsNone(ReclaimedGroup.reclaim(token))


class GroupTest(Case):
    def setUp(self):
        self.control = mock.create_autospec(ProcessControl, instance=True)
        self.control.path = "/tmp/apm-group-1"
        self.group = ReclaimedGroup(self.control)

    def test_it_is_named_and_tokened_by_its_controller_with_no_record_or_display(self):
        self.assertEqual(
            (self.group.name, self.group.token, self.group.record(), self.group.display()),
            ("apm-group-1", "/tmp/apm-group-1", None, None),
        )

    def test_alive_is_its_controllers_and_a_failure_to_tell_is_a_session_error(self):
        for live in (True, False):
            with self.subTest(live=live):
                self.control.live.return_value = live
                self.assertIs(self.group.alive(), live)
        self.control.live.side_effect = OSError("refused")
        with self.assertRaisesRegex(SessionError, "^process group apm-group-1: refused$"):
            self.group.alive()

    def test_close_terminates_a_group_that_then_exits_without_a_kill(self):
        self.control.live.return_value = False
        self.group.close()
        self.control.terminate.assert_called_once_with()
        self.control.kill.assert_not_called()
