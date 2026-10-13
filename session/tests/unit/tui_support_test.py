"""tui_support.Stream's managed worker preserves events and original failures."""

from types import SimpleNamespace

from agent_pm.session import Reply

from session.tests.case import Case
from session.tests.tui_support import Stream


class FakeHandle:
    def __init__(self, error=None):
        self.error = error
        self.stopped = False

    def events(self):
        yield Reply("first")
        if self.error is not None:
            raise self.error

    def stop(self):
        self.stopped = True


class StreamTest(Case):
    def stream(self, handle):
        cleanups = []
        case = SimpleNamespace(addCleanup=lambda call, *args: cleanups.append((call, args)))
        stream = Stream(case, handle)
        self.addCleanup(stream.executor.shutdown, wait=False, cancel_futures=True)
        self.assertEqual(cleanups, [(stream.close, (handle,))])
        return stream

    def test_event_and_managed_cleanup(self):
        handle = FakeHandle()
        stream = self.stream(handle)
        self.assertEqual(stream.next(), Reply("first"))
        stream.close(handle)
        self.assertTrue(handle.stopped)

    def test_future_forwards_the_original_error(self):
        error = ValueError("worker failed")
        handle = FakeHandle(error)
        stream = self.stream(handle)
        self.assertEqual(stream.next(), Reply("first"))
        with self.assertRaises(ValueError) as caught:
            stream.next()
        self.assertIs(caught.exception, error)
        with self.assertRaises(ValueError) as caught:
            stream.close(handle)
        self.assertIs(caught.exception, error)
        self.assertTrue(handle.stopped)
