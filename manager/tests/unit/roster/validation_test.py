"""Validation behavior."""

import contextlib
import io
import os

from agent_pm.manager.engine import roster

from manager.tests.unit.roster.case import RosterCase
from manager.tests.unit.roster.data import DROP, INVALID, KIND, RESUME_LIST, TOP, VALID, WHEN, good


class ValidationTest(RosterCase):
    def test_write_back_validates_the_whole_dict(self):
        raw = self.store(self.doc(entries={"w1": good()}))
        bad = [
            (lambda r: r.update(cursor=-1), "cursor: -1: want a non-negative integer"),
            (lambda r: r.update(x=1), "keys: unexpected ['x']"),
            (
                lambda r: r["entries"].update(w2=good(kind="boss")),
                "entry 'w2': " + KIND.format("boss"),
            ),
            (
                lambda r: r["entries"].update({"a b": good()}),
                "entry 'a b': name: want [A-Za-z0-9_-]+",
            ),
            (
                lambda r: r["entries"].update(w2=good(resume=("/p", "/x/crew.py"))),
                f"entry 'w2': resume: ('/p', '/x/crew.py'): want {RESUME_LIST}",
            ),
            (
                lambda r: r["entries"].update(w2={}),
                "entry 'w2': keys: missing " + str(sorted(good())),
            ),
        ]
        for change, why in bad:
            with (
                self.subTest(why=why),
                self.assertRaises(roster.RosterError) as cm,
                roster.locked(self.dir) as r,
            ):
                change(r)
            self.assertEqual(str(cm.exception), f"{self.path}: {why}")
            self.assertEqual(self.bytes(), raw)
            self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_valid_entries_are_kept(self):
        for field, values in VALID.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.store(self.doc(entries={"w1": good(**{field: value})}))
                    r, err = self.load()
                    self.assertEqual(r["entries"], {"w1": good(**{field: value})})
                    self.assertEqual(err, "")

    def test_invalid_entry_is_skipped_with_one_stderr_line_and_the_rest_kept(self):
        for field, value, why in INVALID:
            with self.subTest(field=field, value=value):
                raw = self.store(self.doc(entries={"ok": good(), "bad": good(**{field: value})}))
                r, err = self.load()
                self.assertEqual(r["entries"], {"ok": good()})
                self.assertEqual(err, f"manager: roster.json: entry 'bad': {why.format(value)}\n")
                self.assertEqual(self.bytes(), raw)

    def test_entry_of_other_keys_than_the_twelve_or_a_bad_name_is_skipped(self):
        missing = {k: v for k, v in good().items() if k != "note"}
        cases = [
            ("bad", {**good(), "x": 1}, "keys: unexpected ['x']"),
            ("bad", missing, "keys: missing ['note']"),
            ("bad", {**missing, "y": 1}, "keys: missing ['note'], unexpected ['y']"),
            ("bad", [], "value: []: want an object"),
            ("bad", "w", "value: 'w': want an object"),
            *(
                (name, good(), "name: want [A-Za-z0-9_-]+")
                for name in ("a b", "", "a;", "a\nb", "w/1")
            ),
        ]
        for name, value, why in cases:
            with self.subTest(name=name, why=why):
                self.store(self.doc(entries={name: value}))
                r, err = self.load()
                self.assertEqual(
                    (r["entries"], err), ({}, f"manager: roster.json: entry {name!r}: {why}\n")
                )

    def test_stderr_line_shows_values_by_repr(self):
        self.store(self.doc(entries={"bad": good(cwd="/w\n\x1b[31mforged\u2028")}))
        err = self.load()[1]
        self.assertEqual(err.count("\n"), 1)
        self.assertNotIn("\x1b", err)
        self.assertIn(repr("/w\n\x1b[31mforged\u2028"), err)

    def test_later_write_drops_a_skipped_entry(self):
        self.store(self.doc(entries={"bad": good(sid="; rm -rf ~")}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), roster.locked(self.dir) as record:
            roster.record(record, "w1", good())
        self.assertEqual(list(self.entries()), ["w1"])
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_top_level_valid_forms(self):
        for top in (
            {},
            {"holder": {"session": "s1", "since": WHEN}},
            {"cursor": 7, "gen": 3},
            {"holder": {"session": "s1", "since": "2026-01-01T00:00:00Z"}},
        ):
            with self.subTest(top=top):
                self.store(self.doc(**top))
                self.assertEqual(self.load()[0], self.doc(**top))

    def test_top_level_faults_refuse_and_leave_the_bytes(self):
        for label, data, why in TOP:
            with self.subTest(label):
                top = (
                    data
                    if isinstance(data, bytes)
                    else {k: v for k, v in self.doc(**data).items() if v is not DROP}
                )
                raw = self.store(top)
                for kw in ({}, {"write": False}):
                    with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir, **kw):
                        self.fail("entered")
                    self.assertTrue(
                        str(cm.exception).startswith(f"{self.path}: {why}"), str(cm.exception)
                    )
                    self.assertEqual(self.bytes(), raw)
                self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])
