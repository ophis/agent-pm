"""Orchestrator runs behavior at its dependency boundaries."""

import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from agent_pm.linear_orchestrator.engine import runs
from agent_pm.linear_orchestrator.engine.types import Claim, ConfigError
from agent_pm.utils import save
from orchestrator_tests import case

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)

EAST, WEST = timezone(timedelta(hours=8)), timezone(timedelta(hours=-4))


def stamp(t, tz=WEST):
    return t.astimezone(tz).isoformat(timespec="seconds")


def row(kind, issue, sid, role="researcher", client="a", **ago):
    return json.dumps(
        {
            "ts": stamp(NOW - timedelta(**ago)),
            "kind": kind,
            "issue": issue,
            "sid": sid,
            "role": role,
            "client": client,
            "delegate_kind": "harness",
        }
    )


def limit(resets_at, client="a", **extra):
    return json.dumps(
        {"ts": stamp(NOW), "kind": "limit", "issue": "TASK-1", "sid": "s", "client": client}
        | {"resets_at": resets_at, "delegate_kind": "harness"}
        | extra
    )


def entry(kind, issue, sid, role, client="a", **ago):
    return runs.Entry(NOW - timedelta(**ago), kind, issue, sid, role, client, "harness")


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
        self.assertEqual(
            entries[1]._fields, ("ts", "kind", "issue", "sid", "role", "client", "delegate_kind")
        )

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
            json.dumps({**good, "client": None}),
            json.dumps({**good, "client": ""}),
            json.dumps({**good, "client": 3}),
        ]
        self.write([*bad, json.dumps(good), *bad, row("resume", "TASK-9", "z", "pm", minutes=30)])
        self.assertEqual(
            [e[1:] for e in runs.parse_log(self.log)],
            [
                ("start", "TASK-1", "a", "researcher", "a", "harness"),
                ("resume", "TASK-9", "z", "pm", "a", "harness"),
            ],
        )

    def test_saved_delegate_survives_round_trip_and_a_kindless_row_has_none(self):
        kindless = json.loads(row("start", "TASK-1", "old"))
        del kindless["delegate_kind"]
        saved = json.loads(row("start", "TASK-2", "new")) | {"delegate_kind": "external"}
        self.write([json.dumps(kindless), json.dumps(saved)])
        self.assertEqual([e.delegate_kind for e in runs.parse_log(self.log)], [None, "external"])
        self.assertEqual(self.read(), json.dumps(kindless) + "\n" + json.dumps(saved) + "\n")

    def test_invalid_delegate_kind_is_no_entry(self):
        good = json.loads(row("start", "TASK-1", "s"))
        for kind in (None, "", 4):
            with self.subTest(kind=kind):
                self.assertIsNone(runs.parse_line(json.dumps(good | {"delegate_kind": kind})))

    def test_a_line_without_client_and_a_limit_row_are_no_entries(self):
        legacy = json.loads(row("start", "TASK-1", "a", minutes=5))
        del legacy["client"]
        for line in (json.dumps(legacy), limit(stamp(NOW + timedelta(hours=1)))):
            with self.subTest(line=line):
                self.assertIsNone(runs.parse_line(line))


class Limits(Case):
    def limits(self, *lines):
        self.write(lines)
        return runs.limits(self.log, NOW)

    def test_a_live_row_names_its_client_and_reset_time(self):
        at = NOW + timedelta(hours=1)
        got = self.limits(row("start", "TASK-1", "s", minutes=5), limit(stamp(at, EAST)))
        self.assertEqual(got, {("harness", "a"): at})
        self.assertEqual(got["harness", "a"].utcoffset(), timedelta(hours=8))

    def test_the_latest_reset_per_client(self):
        early, late = NOW + timedelta(hours=1), NOW + timedelta(hours=3)
        got = self.limits(limit(stamp(late)), limit(stamp(early)), limit(stamp(early), client="b"))
        self.assertEqual(got, {("harness", "a"): late, ("harness", "b"): early})

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
        self.assertEqual(self.limits(limit(stamp(at))), {("harness", "a"): at})

    def test_a_row_without_a_usable_client_or_of_another_kind_is_left_out(self):
        at = stamp(NOW + timedelta(hours=1))
        good = json.loads(limit(at))
        self.assertEqual(
            self.limits(
                limit(at, client=""),
                limit(at, client=None),
                limit(at, client=3),
                json.dumps({k: v for k, v in good.items() if k != "client"}),
                json.dumps({**good, "kind": "end"}),
                "[]",
                "null",
                "not json",
            ),
            {},
        )

    def test_a_kindless_row_is_left_out(self):
        kindless = json.loads(limit(stamp(NOW + timedelta(hours=1))))
        del kindless["delegate_kind"]
        self.assertEqual(self.limits(json.dumps(kindless)), {})

    def test_limits_are_separate_for_each_delegate_client_key(self):
        early, late = NOW + timedelta(hours=1), NOW + timedelta(hours=2)
        self.assertEqual(
            self.limits(limit(stamp(early)), limit(stamp(late), delegate_kind="external")),
            {("harness", "a"): early, ("external", "a"): late},
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
        self.assertEqual(sorted(os.listdir(self.tmp)), ["runs.jsonl", "runs.jsonl.lock"])
        self.write([keep[1], "plain text"])
        runs.trim(self.log, NOW)
        self.assertEqual(self.read(), keep[1] + "\n")

    def test_a_limit_row_appended_while_it_trims_is_kept(self):
        self.write(["plain text", row("start", "TASK-2", "b", hours=1)])
        at = stamp(NOW + timedelta(hours=2))
        writer = threading.Thread(
            target=runs.note_limit,
            args=(self.log,),
            kwargs={
                "issue": "TASK-2",
                "sid": "b",
                "client": "a",
                "resets_at": at,
                "delegate_kind": "harness",
            },
        )
        real = os.replace

        def replace(src, dst):
            writer.start()
            writer.join(0.5)
            real(src, dst)

        with mock.patch.object(os, "replace", replace):
            runs.trim(self.log, NOW)
        writer.join(5)
        self.assertEqual(
            [json.loads(line)["kind"] for line in self.read().splitlines()], ["start", "limit"]
        )

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

    def test_keeps_failed_launch_rows_for_keep(self):
        def failed(**ago):
            return json.dumps(
                {
                    "ts": stamp(NOW - timedelta(**ago)),
                    "kind": "failed",
                    "issue": "TASK-1",
                    "sid": "s",
                }
            )

        recent = failed(days=7)
        self.write([failed(days=7, seconds=1), recent])
        runs.trim(self.log, NOW)
        self.assertEqual(self.read(), recent + "\n")
        self.assertEqual(runs.failures(self.log), [runs.Failure(NOW - timedelta(days=7), "TASK-1")])

    def test_no_temp_left_when_the_rewrite_fails(self):
        self.write([row("start", "TASK-1", "a", days=8), row("start", "TASK-1", "b", hours=1)])
        with (
            mock.patch.object(os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            runs.trim(self.log, NOW)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["runs.jsonl", "runs.jsonl.lock"])
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

    def test_trim_preserves_retained_rows_exact_bytes_and_file_order(self):
        live = limit(stamp(NOW + timedelta(hours=2)), ts=stamp(NOW - timedelta(days=9)))
        recent = row("resume", "TASK-1", "s", minutes=5)
        retained = ("  " + live + "  \n\n" + recent).encode()
        with open(self.log, "wb") as stream:
            stream.write((row("start", "TASK-1", "old", days=8) + "\n").encode())
            stream.write(retained)
        runs.trim(self.log, NOW)
        with open(self.log, "rb") as stream:
            self.assertEqual(stream.read(), retained.replace(b"\n\n", b"\n"))

    def test_latest_sid_older_than_eight_days_survives_repeated_trim(self):
        latest = row("start", "TASK-1", "latest", days=8)
        resumed = row("resume", "TASK-1", "latest", days=8, seconds=-1)
        old = row("start", "TASK-1", "old", days=9)
        self.write([latest, old, resumed])
        runs.trim(self.log, NOW)
        runs.trim(self.log, NOW + timedelta(minutes=1))
        self.assertEqual(self.read(), latest + "\n" + resumed + "\n")

    def test_retained_crlf_bytes_survive_rewrite(self):
        kept = ("  " + row("start", "TASK-1", "latest", days=8) + "  \r\n").encode()
        with open(self.log, "wb") as stream:
            stream.write((row("start", "TASK-1", "old", days=9) + "\n").encode() + kept)
        runs.trim(self.log, NOW)
        with open(self.log, "rb") as stream:
            self.assertEqual(stream.read(), kept)

    def test_rewrites_through_the_atomic_save(self):
        self.assertIs(runs.save, save)

    def test_missing_log_is_noop(self):
        runs.trim(self.log, NOW)
        self.assertFalse(os.path.exists(self.log))


class ClientOf(case.Case):
    def test_the_start_entries_client_first(self):
        entries = [
            entry("resume", "TASK-1", "s", "pm", "r", minutes=50),
            entry("start", "TASK-2", "t", "pm", "other", minutes=45),
            entry("start", "TASK-1", "s", "pm", "x", minutes=40),
            entry("resume", "TASK-1", "s", "pm", "y", minutes=30),
        ]
        self.assertEqual(runs.client_of(entries, "s"), "x")

    def test_else_the_earliest_entries(self):
        entries = [
            entry("resume", "TASK-1", "s", "pm", "late", minutes=10),
            entry("resume", "TASK-1", "s", "pm", "early", minutes=30),
            entry("start", "TASK-1", "t", "pm", "other", minutes=60),
        ]
        self.assertEqual(runs.client_of(entries, "s"), "early")


class Latest(case.Case):
    def test_timestamp_wins_and_last_append_breaks_ties(self):
        old = entry("start", "TASK-1", "old", "pm", minutes=2)
        first = entry("resume", "TASK-1", "first", "pm", minutes=1)
        last = entry("start", "TASK-1", "last", "pm", minutes=1)
        other = entry("start", "TASK-2", "other", "engineer", minutes=4)
        self.assertEqual(runs.latest([first, other, last, old]), {"TASK-1": last, "TASK-2": other})


class DelegateOf(case.Case):
    def test_start_kind_wins_and_without_start_earliest_kind_wins(self):
        entries = [
            entry("resume", "TASK-1", "s", "pm", minutes=50)._replace(delegate_kind="old"),
            entry("start", "TASK-1", "s", "pm", minutes=40)._replace(delegate_kind="start"),
            entry("resume", "TASK-1", "s", "pm", minutes=30)._replace(delegate_kind="recent"),
        ]
        self.assertEqual(runs.delegate_of(entries, "s"), "start")
        self.assertEqual(runs.delegate_of([entries[2], entries[0]], "s"), "old")

    def test_a_kindless_row_of_the_sid_is_a_config_error(self):
        entries = [
            entry("start", "TASK-1", "s", "pm", minutes=40),
            entry("resume", "TASK-1", "s", "pm", minutes=30)._replace(delegate_kind=None),
        ]
        with self.assertRaisesRegex(ConfigError, "TASK-1: session s's runs.jsonl row has no"):
            runs.delegate_of(entries, "s")


class NoteLimit(Case):
    def test_writes_the_limit_row_durably_and_limits_reads_it(self):
        at = stamp(NOW + timedelta(hours=1), EAST)
        path = os.path.join(self.tmp, "logs", "runs.jsonl")
        with mock.patch.object(os, "fsync", wraps=os.fsync) as spy:
            runs.note_limit(
                path, issue="TASK-1", sid="s", client="a", resets_at=at, delegate_kind="harness"
            )
            runs.note_limit(
                path, issue="TASK-2", sid="t", client="b", resets_at=None, delegate_kind="harness"
            )
        self.assertEqual(spy.call_count, 4)
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(
            [list(r) for r in rows],
            [["ts", "kind", "issue", "sid", "client", "resets_at", "delegate_kind"]] * 2,
        )
        self.assertEqual(
            [{k: v for k, v in r.items() if k != "ts"} for r in rows],
            [
                {
                    "kind": "limit",
                    "issue": "TASK-1",
                    "sid": "s",
                    "client": "a",
                    "resets_at": at,
                    "delegate_kind": "harness",
                },
                {
                    "kind": "limit",
                    "issue": "TASK-2",
                    "sid": "t",
                    "client": "b",
                    "resets_at": None,
                    "delegate_kind": "harness",
                },
            ],
        )
        self.assertEqual(runs.limits(path, NOW), {("harness", "a"): datetime.fromisoformat(at)})


if __name__ == "__main__":
    unittest.main()


SID_A, SID_B = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d", "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"

UUID = "11111111-2222-4333-8444-555555555555"


def start(sid, **fields):
    return json.dumps(
        {
            "ts": stamp(NOW),
            "kind": "start",
            "issue": "TASK-1",
            "sid": sid,
            "role": "engineer",
            "client": "claude",
            "delegate_kind": "harness",
            "uuid": UUID,
            "project": "p1",
            "target": "ophis/agent-pm",
            "key": "agent-pm-engineer",
        }
        | fields
    )


class ClaimOf(Case):
    def test_the_sids_start_row_not_the_latest(self):
        self.write(
            [
                start(SID_A),
                start(SID_B, role="researcher", key="agent-pm-researcher", target=None),
                json.dumps({**json.loads(start(SID_A)), "kind": "resume", "key": "changed"}),
            ]
        )
        self.assertEqual(
            runs.claim(self.log, "TASK-1", SID_A),
            Claim(
                issue="TASK-1",
                sid=SID_A,
                role="engineer",
                uuid=UUID,
                project="p1",
                target=("ophis", "agent-pm"),
                key="agent-pm-engineer",
                client="claude",
                delegate_kind="harness",
            ),
        )
        self.assertEqual(runs.claim(self.log, "TASK-1", SID_B).key, "agent-pm-researcher")
        self.assertIsNone(runs.claim(self.log, "TASK-1", SID_B).target)

    def test_null_project_and_target_are_none(self):
        self.write([start(SID_A, project=None, target=None)])
        found = runs.claim(self.log, "TASK-1", SID_A)
        self.assertEqual((found.project, found.target), (None, None))

    def test_no_start_row_for_the_issue_and_sid_raises(self):
        self.write(
            [
                "not json",
                json.dumps({**json.loads(start(SID_A)), "kind": "resume"}),
                start(SID_B),
                json.dumps({**json.loads(start(SID_A)), "issue": "TASK-2"}),
            ]
        )
        with self.assertRaisesRegex(ValueError, f"TASK-1: no start row for session {SID_A}"):
            runs.claim(self.log, "TASK-1", SID_A)
        with self.assertRaisesRegex(ValueError, "no start row"):
            runs.claim(os.path.join(self.tmp, "missing.jsonl"), "TASK-1", SID_A)

    def test_an_incomplete_start_row_raises_naming_the_field(self):
        for field, value in (
            ("role", None),
            ("role", ""),
            ("uuid", "TASK-1"),
            ("project", 7),
            ("target", "not a slug"),
            ("key", ""),
            ("key", None),
            ("client", ""),
            ("delegate_kind", None),
        ):
            with self.subTest(field=field, value=value):
                self.write([start(SID_A, **{field: value})])
                with self.assertRaisesRegex(ValueError, f"start row {field}"):
                    runs.claim(self.log, "TASK-1", SID_A)
        for field in ("role", "uuid", "project", "target", "key", "client", "delegate_kind"):
            with self.subTest(missing=field):
                row = json.loads(start(SID_A))
                del row[field]
                self.write([json.dumps(row)])
                with self.assertRaisesRegex(ValueError, f"start row {field}"):
                    runs.claim(self.log, "TASK-1", SID_A)
