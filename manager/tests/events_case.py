"""Private event and ledger fixtures shared by logic and process-death tests."""

import json
import os
from pathlib import Path

from agent_pm.manager.engine.events import EVENTS_MAX, Events
from agent_pm.utils import append_line, file_lock, save

from manager.tests.case import Case


class EventsCase(Case):
    def setUp(self):
        self.directory = os.path.join(self.home, "events-test")
        os.mkdir(self.directory, 0o700)
        self.events = Events(self.directory)
        self.original = b"h" * EVENTS_MAX + b"\nold\n"
        append_line(self.events.path, self.original, own=True, locked=True)
        self.ledger = Path(self.directory, "ledger.json")
        save(self.ledger, '{"gen":0,"cursor":1}\n', own=True, durable=True)

    def generation(self):
        return json.loads(self.ledger.read_text())["gen"]

    def recover(self):
        with file_lock(self.events.path, own=True):
            self.events.recover(self.generation())
