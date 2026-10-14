"""Query batches preserve order, validate identity and aggregate failures."""

import io
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace

from agent_pm.utils import Record, query_batch, query_inputs

from utils.tests.case import Case

RECORD = Record(1, "pipeline", "TASK-1", (), None, None, ("ctl",), ("ctl", "resume"), None, None)


class Queries(Case):
    def test_explicit_keys_do_not_read_stdin(self):
        self.assertEqual(query_inputs(("TASK-2", "TASK-1"), ("invalid",)), ("TASK-2", "TASK-1"))

    def test_stdin_records_keep_order_and_ignore_blank_lines(self):
        second = replace(RECORD, name="TASK-2")
        for keys in ((), ("-",)):
            self.assertEqual(
                query_inputs(keys, ("\n", second.to_line(), RECORD.to_line())), (second, RECORD)
            )

    def test_duplicate_names_and_invalid_stdin_fail_before_operations(self):
        for keys, lines in (
            (("TASK-1", "TASK-1"), ()),
            ((), (RECORD.to_line(), RECORD.to_line())),
            (("-", "TASK-1"), (RECORD.to_line(),)),
            ((), (RECORD.to_line(), "invalid")),
            ((), ()),
        ):
            with self.subTest(keys=keys, lines=lines), self.assertRaises(ValueError):
                query_inputs(keys, lines)

    def test_batch_finishes_valid_keys_and_configuration_errors_take_precedence(self):
        calls = []

        def operation(key):
            calls.append(key)
            if key == "config":
                raise ValueError("bad config\nmore")
            if key == "failed":
                raise RuntimeError("unavailable")
            return key

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = query_batch(
                ("ok", "config", "failed", "last"),
                operation,
                configuration_errors=(ValueError,),
                operational_errors=(RuntimeError,),
            )
        self.assertEqual(result, 2)
        self.assertEqual(calls, ["ok", "config", "failed", "last"])
        self.assertEqual(stdout.getvalue(), "ok\nlast\n")
        self.assertEqual(stderr.getvalue(), "config: bad config more\nfailed: unavailable\n")

    def test_operational_failure_returns_one_and_names_record(self):
        def operation(_record):
            raise OSError("failed")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = query_batch(
                (RECORD,),
                operation,
                configuration_errors=(ValueError,),
                operational_errors=(OSError,),
            )
        self.assertEqual((result, stderr.getvalue()), (1, "TASK-1: failed\n"))

    def test_silent_success_produces_no_line(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = query_batch(
                ("TASK-1",),
                lambda _key: None,
                configuration_errors=(ValueError,),
                operational_errors=(OSError,),
            )
        self.assertEqual((result, stdout.getvalue()), (0, ""))
