import sys
import tempfile

from agent_pm.session.adapters.base import Adapter, JsonHeadless
from agent_pm.session.engine.types import Launch, Prepared, Reply, Spec, Stderr
from agent_pm.session.modes.headless import HeadlessMode

from session.tests.case import Case
from session.tests.fakes.terminal import MemoryHost

SID = "s1"
NOTHING = (sys.executable, "-I", "-c", "")


class StubAdapter(Adapter):
    forbidden_selection = ("--resume",)
    runtime_note = "STUB NOTE"

    def __init__(self, launched=None, run=None):
        super().__init__({})
        self.launched, self.run = launched or Launch(NOTHING), run or StubRun

    def headless(self):
        return self.run(self)

    def tui(self):
        return None


class StubRun(JsonHeadless):
    specs: list[Spec] = []

    def __init__(self, adapter: StubAdapter):
        self.adapter = adapter

    def launch(self, spec: Spec):
        self.specs.append(spec)
        return self.adapter.launched

    def parse(self, record):
        if "raise" in record:
            raise RuntimeError(record["raise"])
        if "diag" in record:
            yield Stderr(record["diag"])
        else:
            yield Reply(record["text"])


class HeadlessCase(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.mode, self.recorded = None, []
        StubRun.specs = []

    def start(self, adapter=None, display=None, **fields) -> Prepared:
        spec = Spec(**{"workdir": self.dir, "sid": SID, "runtime": "stub", **fields})
        self.mode = HeadlessMode.start(
            spec,
            adapter or StubAdapter(),
            registry=self.driver.registry,
            record=self.recorded.append,
            host=MemoryHost(),
            display=display,
        )
        return self.mode.prepared
