"""Tracker.archive: the archive rule once, over an adapter's archive_plan and _write_archive."""

from agent_pm.orchestrator.engine.flow.tracker import Tracker
from agent_pm.orchestrator.engine.flow.types import ArchivePlan, ArchiveRefused, SubIssue
from orchestrator_tests.case import Case

DONE = SubIssue("k-2", "TASK-2", "Done", finished=True)
OPEN = SubIssue("k-3", "TASK-3", "Todo", finished=False)


def plan(*, archived=False, unfinished=()):
    return ArchivePlan("k-1", "TASK-1", "u", archived, (DONE, OPEN), tuple(unfinished))


def _unused(*_, **__):
    raise AssertionError("not part of the archive rule")


class Adapter(Tracker):
    """Only the archive hooks; every other abstract method fails if called."""

    locals().update(dict.fromkeys(Tracker.__abstractmethods__, _unused))

    def __init__(self, found):
        self.found, self.reads, self.writes = found, [], []

    def archive_plan(self, key):
        self.reads.append(key)
        return self.found

    def _write_archive(self, key):
        self.writes.append(key)


class Outcome(Case):
    def test_archived_then_unfinished_then_changed(self):
        cases = (
            (plan(archived=True), "unchanged"),
            (plan(unfinished=(OPEN,)), "refused"),
            (plan(), "changed"),
        )
        for found, outcome in cases:
            with self.subTest(outcome=outcome):
                self.assertEqual(found.outcome, outcome)


class Archive(Case):
    def test_nothing_unfinished_writes_once(self):
        adapter = Adapter(plan())
        self.assertTrue(adapter.archive("k-1"))
        self.assertEqual((adapter.reads, adapter.writes), (["k-1"], ["k-1"]))

    def test_already_archived_writes_nothing(self):
        adapter = Adapter(plan(archived=True))
        self.assertFalse(adapter.archive("k-1"))
        self.assertEqual(adapter.writes, [])

    def test_anything_unfinished_refuses_with_the_plan(self):
        found = plan(unfinished=(OPEN,))
        adapter = Adapter(found)
        with self.assertRaises(ArchiveRefused) as raised:
            adapter.archive("k-1")
        self.assertIs(raised.exception.plan, found)
        self.assertEqual(adapter.writes, [])

    def test_a_given_plan_is_not_read_again(self):
        adapter = Adapter(plan(unfinished=(OPEN,)))
        self.assertTrue(adapter.archive("k-1", plan()))
        self.assertEqual((adapter.reads, adapter.writes), ([], ["k-1"]))
