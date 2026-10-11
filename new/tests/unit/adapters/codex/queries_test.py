from unittest import mock

from adapters.base import SessionInfo
from new.tests.unit.adapters.codex.support import Base, CHILD, NATIVE


class InspectionTest(Base):
    def test_adapter_inspects_binding_session_and_descendant_by_cwd(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        path = self.rollout()
        self.rollout(CHILD, parent=NATIVE, stamp=300)
        info = self.adapter.session_info(self.spec.sid, str(self.cwd))
        self.assertIsInstance(info, SessionInfo)
        self.assertEqual((info.sid, info.native_id, info.cwd, info.resumable, info.last_active),
                         (self.spec.sid, NATIVE, str(self.cwd), "yes", 300))
        self.assertEqual(info.transcript, str(path))
        self.assertIn("codex resume", info.resume_command)
        self.assertIn(NATIVE, info.resume_command)
        self.assertIn("CODEX_HOME=", info.resume_command)

    def test_missing_and_unreadable_storage_have_distinct_status(self):
        self.assertEqual(self.adapter.session_info(self.spec.sid, str(self.cwd)).resumable, "no")
        self.prepare()
        self.assertEqual(self.adapter.session_info(self.spec.sid, str(self.cwd)).resumable, "no")
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertEqual(self.adapter.session_info(self.spec.sid, str(self.cwd)).resumable, "no")
        path = self.rollout()
        with mock.patch("os.open", side_effect=PermissionError("unreadable")):
            self.assertEqual(self.adapter.session_info(self.spec.sid, str(self.cwd)).resumable, "unknown")
        path.unlink()
        self.assertEqual(self.adapter.session_info(self.spec.sid, str(self.cwd)).resumable, "no")
