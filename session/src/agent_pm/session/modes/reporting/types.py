"""Report worker failures through the owning mode's event stream, including disabled logging."""

from collections.abc import Callable
from logging import ERROR, Handler, Logger, LogRecord


class EventHandler(Handler):
    def __init__(self, deliver: Callable[[BaseException], None]):
        super().__init__()
        self.deliver = deliver

    def emit(self, record: LogRecord) -> None:
        if record.exc_info and record.exc_info[1] is not None:
            self.deliver(record.exc_info[1])


class FailureReport:
    def __init__(self, name: str, error: BaseException, receive: Callable[[BaseException], None]):
        self.error, self.receive, self.delivered = error, receive, False
        self.logger = Logger(name, ERROR)
        self.logger.propagate = False
        self.handler = EventHandler(self._deliver)

    def _deliver(self, error: BaseException) -> None:
        self.receive(error)
        self.delivered = True

    def __enter__(self) -> Logger:
        self.logger.addHandler(self.handler)
        return self.logger

    def __exit__(self, kind, _value, _traceback) -> None:
        try:
            if kind is None and not self.delivered:
                self.receive(self.error)
        finally:
            self.logger.removeHandler(self.handler)
            self.handler.close()
