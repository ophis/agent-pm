import unittest
from unittest import mock

from adapters.base import Adapter, SessionInfo, Usage
from driver import api, registry
from new.tests.unit.driver.api_test import Base


class CallerSidTest(Base):
    def test_explicit_sid_reaches_launch_and_records(self):
        handle, argv = self.run_once(sid="caller")
        self.assertEqual(handle.sid, "caller")
        self.assertEqual(argv[argv.index("--sid") + 1], "caller")
        self.assertEqual(self.written()[0]["sid"], "caller")

    def test_matching_sid_spec_and_resume(self):
        handle, argv = self.run_once(spec=self.spec(sid="same"), sid="same", resume="same")
        self.assertEqual(handle.sid, "same")
        self.assertIn("--resume", argv)

    def test_conflicts_and_invalid_sid_precede_registry_and_spawn(self):
        for sid, fields, error in (("other", {"spec": self.spec(sid="spec")}, "sid other differs from spec.sid spec"),
                                   ("other", {"resume": "saved"}, "sid other differs from resume saved")):
            with self.subTest(sid=sid, fields=fields), mock.patch.object(registry, "adapter") as factory:
                self.refused(api.ConfigError, error, lambda: self.start(sid=sid, **fields))
                factory.assert_not_called()
        for sid in ("", 7, "a/b", "line\nbreak"):
            with self.subTest(sid=sid), mock.patch.object(registry, "adapter") as factory:
                with self.assertRaises(api.ConfigError):
                    self.start(sid=sid)
                factory.assert_not_called()


class QueryTest(unittest.TestCase):
    def test_default_queries_are_unknown_and_session_fields_compatible(self):
        from new.tests.fake_adapter import FakeAdapter
        adapter = FakeAdapter({})
        with mock.patch.object(registry, "adapter", return_value=adapter):
            self.assertEqual(api.usage("fake"), Usage())
            info = api.session_info("fake", "caller", "/work")
        self.assertEqual(info, SessionInfo("caller", "", "/work", False, "", "", "unknown", None))
        self.assertEqual(SessionInfo("s", "n", "/work", False, "file", "quoted resume").resume_command,
                         "quoted resume")
        self.assertIsNone(Adapter.usage(adapter))

    def test_queries_delegate_once_and_exports_include_new_types(self):
        adapter = mock.Mock()
        usage = Usage(61, True, 123, 456)
        info = SessionInfo("s", "n", "/work", False, "file", "resume", "yes", 100)
        adapter.usage.return_value = usage
        adapter.session_info.return_value = info
        with mock.patch.object(registry, "adapter", return_value=adapter):
            self.assertIs(api.usage("fake"), usage)
            self.assertIs(api.session_info("fake", "s", "/work"), info)
        adapter.usage.assert_called_once_with()
        adapter.session_info.assert_called_once_with("s", "/work")
        for name in ("Usage", "LimitHit", "usage", "session_info"):
            self.assertIn(name, api.__all__)
