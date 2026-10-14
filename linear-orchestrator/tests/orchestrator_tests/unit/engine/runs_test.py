"""Orchestrator runs behavior at its dependency boundaries."""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from agent_pm.linear_orchestrator.engine import runs
from agent_pm.utils import save
from orchestrator_tests import case

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)

EAST, WEST = timezone(timedelta(hours=8)), timezone(timedelta(hours=-4))


def stamp(t, tz=WEST):
    return t.astimezone(tz).isoformat(timespec="seconds")


def row(kind, issue, sid, role="researcher", runtime="a", **ago):
    return json.dumps(
        {
            "ts": stamp(NOW - timedelta(**ago)),
            "kind": kind,
            "issue": issue,
            "sid": sid,
            "role": role,
            "runtime": runtime,
        }
    )


def limit(resets_at, runtime="a", **extra):
    return json.dumps(
        {"ts": stamp(NOW), "kind": "limit", "issue": "TASK-1", "sid": "s", "runtime": runtime}
        | {"resets_at": resets_at}
        | extra
    )


def entry(kind, issue, sid, role, runtime="a", **ago):
    return runs.Entry(NOW - timedelta(**ago), kind, issue, sid, role, runtime)


class Case(case.Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.log = os.path.join(self.tmp, "runs.jsonl")

    def write(self, lines):
        with open(self.log, "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def read(self):
        with open(self.log) as f:
            return f.read()


class Parse(Case):
    def test_parse_log_is_utc_entries_in_file_order(self):
        self.assertEqual(runs.parse_log(os.path.join(self.tmp, "nope")), [])
        self.write(
            [
                row("start", "TASK-1", "a", "researcher", minutes=60),
                row("start", "TASK-2", "b", "pm", "b", minutes=50),
                row("resume", "TASK-1", "a", "pm-lead", minutes=40),
            ]
        )
        entries = runs.parse_log(self.log)
        self.assertEqual(
            entries,
            [
                entry("start", "TASK-1", "a", "researcher", minutes=60),
                entry("start", "TASK-2", "b", "pm", "b", minutes=50),
                entry("resume", "TASK-1", "a", "pm-lead", minutes=40),
            ],
        )
        self.assertEqual({e.ts.utcoffset() for e in entries}, {timedelta(0)})
        self.assertEqual(entries[1]._fields, ("ts", "kind", "issue", "sid", "role", "runtime"))

    def test_parse_log_skips_what_is_not_a_complete_start_or_resume_line(self):
        good = json.loads(row("start", "TASK-1", "a", minutes=60))
        bad = [
            "not json",
            "",
            "[]",
            '"start"',
            "7",
            "null",
            "{",
            "2026-09-26 23:00:00 start TASK-3 session=c role=x",
            "2026-09-26 23:00:00 start TASK-4 session=d task=deep-research",
            json.dumps({k: v for k, v in good.items() if k != "role"}),
            json.dumps({k: v for k, v in good.items() if k != "ts"}),
            json.dumps({**good, "role": None}),
            json.dumps({**good, "role": 3}),
            json.dumps({**good, "issue": ["TASK-2"]}),
            json.dumps({**good, "sid": None}),
            json.dumps({**good, "kind": "end"}),
            json.dumps({**good, "kind": ["start"]}),
            json.dumps({**good, "ts": "yesterday"}),
            json.dumps({**good, "ts": 5}),
            json.dumps({**good, "ts": None}),
            json.dumps({**good, "ts": "9999-12-31T23:59:59-05:00"}),
            json.dumps({**good, "runtime": None}),
            json.dumps({**good, "runtime": ""}),
            json.dumps({**good, "runtime": 3}),
        ]
        self.write([*bad, json.dumps(good), *bad, row("resume", "TASK-9", "z", "pm", minutes=30)])
        self.assertEqual(
            [e[1:] for e in runs.parse_log(self.log)],
            [("start", "TASK-1", "a", "researcher", "a"), ("resume", "TASK-9", "z", "pm", "a")],
        )

    def test_a_line_without_runtime_and_a_limit_row_are_no_entries(self):
        legacy = json.loads(row("start", "TASK-1", "a", minutes=5))
        del legacy["runtime"]
        for line in (json.dumps(legacy), limit(stamp(NOW + timedelta(hours=1)))):
            with self.subTest(line=line):
                self.assertIsNone(runs.parse_line(line))


class Limits(Case):
    def limits(self, *lines):
        self.write(lines)
        return runs.limits(self.log, NOW)

    def test_a_live_row_names_its_runtime_and_reset_time(self):
        at = NOW + timedelta(hours=1)
        got = self.limits(row("start", "TASK-1", "s", minutes=5), limit(stamp(at, EAST)))
        self.assertEqual(got, {"a": at})
        self.assertEqual(got["a"].utcoffset(), timedelta(hours=8))

    def test_the_latest_reset_per_runtime(self):
        early, late = NOW + timedelta(hours=1), NOW + timedelta(hours=3)
        got = self.limits(limit(stamp(late)), limit(stamp(early)), limit(stamp(early), runtime="b"))
        self.assertEqual(got, {"a": late, "b": early})

    def test_what_is_not_live_is_left_out_and_never_raises(self):
        for at in (
            stamp(NOW - timedelta(minutes=1)),
            stamp(NOW),
            None,
            "2026-09-27T05:00:00",
            "2026-09-27",
            1790000000,
            ["2026-09-27T05:00:00+00:00"],
            "soon",
            "",
            stamp(NOW + runs.KEEP + timedelta(seconds=1)),
            "9999-12-31T23:59:59-05:00",
        ):
            with self.subTest(at=at):
                self.assertEqual(self.limits(limit(at)), {})

    def test_at_most_keep_ahead_is_live(self):
        at = NOW + runs.KEEP
        self.assertEqual(self.limits(limit(stamp(at))), {"a": at})

    def test_a_row_without_a_usable_runtime_or_of_another_kind_is_left_out(self):
        at = stamp(NOW + timedelta(hours=1))
        good = json.loads(limit(at))
        self.assertEqual(
            self.limits(
                limit(at, runtime=""),
                limit(at, runtime=None),
                limit(at, runtime=3),
                json.dumps({k: v for k, v in good.items() if k != "runtime"}),
                json.dumps({**good, "kind": "end"}),
                "[]",
                "null",
                "not json",
            ),
            {},
        )

    def test_a_missing_log_has_none(self):
        self.assertEqual(runs.limits(self.log, NOW), {})


class Trim(Case):
    def test_drops_old_and_unparsable_lines(self):
        keep = [
            row("start", "TASK-1", "b", days=6),
            row("resume", "TASK-1", "b", hours=1),
            row("start", "TASK-2", "c", days=7),
        ]
        junk = [
            row("start", "TASK-1", "a", days=8),
            "plain text",
            "",
            "[1, 2]",
            '{"ts": "x"}',
            row("start", "TASK-1", "a", days=7, seconds=1),
            json.dumps({**json.loads(keep[0]), "ts": "yesterday"}),
            json.dumps({**json.loads(keep[0]), "kind": "end"}),
        ]
        self.write([junk[0], keep[0], *junk[1:4], keep[1], *junk[4:], keep[2]])
        with mock.patch.object(os, "replace", wraps=os.replace) as rep:
            runs.trim(self.log, NOW)
        [(src, dst), _] = rep.call_args
        self.assertEqual((os.path.dirname(src), str(dst)), (self.tmp, self.log))
        self.assertEqual(self.read(), "".join(line + "\n" for line in keep))
        self.assertEqual(os.listdir(self.tmp), ["runs.jsonl"])
        self.write([keep[1], "plain text"])
        runs.trim(self.log, NOW)
        self.assertEqual(self.read(), keep[1] + "\n")

    def test_keeps_live_limit_rows_and_drops_the_rest(self):
        live = limit(stamp(NOW + timedelta(hours=2)), ts=stamp(NOW - timedelta(days=9)))
        start = row("start", "TASK-1", "a", hours=1)
        self.write(
            [
                limit(stamp(NOW - timedelta(minutes=1))),
                live,
                limit(None),
                limit("2026-09-27T05:00:00"),
                start,
                limit(stamp(NOW + runs.KEEP + timedelta(hours=1))),
            ]
        )
        runs.trim(self.log, NOW)
        self.assertEqual(self.read(), live + "\n" + start + "\n")

    def test_no_temp_left_when_the_rewrite_fails(self):
        self.write([row("start", "TASK-1", "a", days=8), row("start", "TASK-1", "b", hours=1)])
        with (
            mock.patch.object(os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            runs.trim(self.log, NOW)
        self.assertEqual(os.listdir(self.tmp), ["runs.jsonl"])
        self.assertEqual(len(self.read().splitlines()), 2)

    def test_unchanged_file_not_rewritten(self):
        lines = [
            row("start", "TASK-1", "a", days=1),
            limit(stamp(NOW + timedelta(hours=1))),
            row("resume", "TASK-1", "a", minutes=5),
        ]
        self.write(lines)
        before = os.stat(self.log)
        with (
            mock.patch.object(os, "replace") as rep,
        ):
            runs.trim(self.log, NOW)
        rep.assert_not_called()
        self.assertEqual(os.stat(self.log).st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(os.stat(self.log).st_ino, before.st_ino)
        self.assertEqual(self.read(), "".join(line + "\n" for line in lines))

    def test_rewrites_through_the_atomic_save(self):
        self.assertIs(runs.save, save)

    def test_missing_log_is_noop(self):
        runs.trim(self.log, NOW)
        self.assertFalse(os.path.exists(self.log))


class RuntimeOf(case.Case):
    def test_the_start_entries_runtime_first(self):
        entries = [
            entry("resume", "TASK-1", "s", "pm", "r", minutes=50),
            entry("start", "TASK-2", "t", "pm", "other", minutes=45),
            entry("start", "TASK-1", "s", "pm", "x", minutes=40),
            entry("resume", "TASK-1", "s", "pm", "y", minutes=30),
        ]
        self.assertEqual(runs.runtime_of(entries, "s"), "x")

    def test_else_the_earliest_entries(self):
        entries = [
            entry("resume", "TASK-1", "s", "pm", "late", minutes=10),
            entry("resume", "TASK-1", "s", "pm", "early", minutes=30),
            entry("start", "TASK-1", "t", "pm", "other", minutes=60),
        ]
        self.assertEqual(runs.runtime_of(entries, "s"), "early")


class NoteLimit(Case):
    def test_writes_the_limit_row_durably_and_limits_reads_it(self):
        at = stamp(NOW + timedelta(hours=1), EAST)
        path = os.path.join(self.tmp, "logs", "runs.jsonl")
        with mock.patch.object(os, "fsync", wraps=os.fsync) as spy:
            runs.note_limit(path, issue="TASK-1", sid="s", runtime="a", resets_at=at)
            runs.note_limit(path, issue="TASK-2", sid="t", runtime="b", resets_at=None)
        self.assertEqual(spy.call_count, 4)
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(
            [list(r) for r in rows], [["ts", "kind", "issue", "sid", "runtime", "resets_at"]] * 2
        )
        self.assertEqual(
            [{k: v for k, v in r.items() if k != "ts"} for r in rows],
            [
                {"kind": "limit", "issue": "TASK-1", "sid": "s", "runtime": "a", "resets_at": at},
                {"kind": "limit", "issue": "TASK-2", "sid": "t", "runtime": "b", "resets_at": None},
            ],
        )
        self.assertEqual(runs.limits(path, NOW), {"a": datetime.fromisoformat(at)})


if __name__ == "__main__":
    unittest.main()
