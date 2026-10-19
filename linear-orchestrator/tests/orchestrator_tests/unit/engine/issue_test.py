"""agent-pm-linear issue: help, the shared stdout/stderr/exit rules, and read through main."""

import io
import json
import os
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.linear_orchestrator import cli
from agent_pm.linear_orchestrator.engine import client, issue
from agent_pm.linear_orchestrator.engine.types import LinearError, Unavailable
from orchestrator_tests.case import Case
from orchestrator_tests.process_fixtures import CalledProcessError, CompletedProcess, TimeoutExpired
from orchestrator_tests.unit import promote_fixtures as tp
from orchestrator_tests.unit.issue_fixtures import Gql, comment, conn, issue_node, rel
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL

KEY = "lin_api_TESTSECRET0123456789"


class IssueCase(Case):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.home, "root")
        os.makedirs(self.root)
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as f:
            f.write(f'[linear_orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL)
        self.config = os.path.join(self.root, "config.toml")
        with open(self.config, "w") as f:
            f.write(tp.CONFIG)

    def run_main(self, argv, gql):
        out, err = io.BytesIO(), io.StringIO()
        code = issue.main(argv, gql=gql, config_path=self.config, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class Help(Case):
    def test_help_needs_no_configuration_credentials_or_files(self):
        for argv in (["issue", "--help"], ["issue", "read", "--help"], ["issue", "-h"]):
            output = io.StringIO()
            with (
                self.subTest(argv=argv),
                redirect_stdout(output),
                mock.patch("builtins.open", side_effect=AssertionError("opened a file")),
                mock.patch("subprocess.run", side_effect=AssertionError("ran a process")),
                self.assertRaises(SystemExit) as cm,
            ):
                cli.main(argv)
            self.assertEqual(cm.exception.code, 0)
            text = output.getvalue()
            for phrase in ("agent-pm-linear issue read TASK-1", "8192", "Exit 0", "--help"):
                self.assertIn(phrase, text)

    def test_top_level_help_lists_the_namespace(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("issue", output.getvalue())

    def test_usage_errors_exit_two(self):
        for argv in ([], ["read"], ["read", "TASK-1", "TASK-2"], ["nope"]):
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                issue.main(argv, gql=Gql(None))
            self.assertEqual(cm.exception.code, 2)


class Read(IssueCase):
    def test_reads_an_issue_without_a_run_record(self):
        gql = mock.Mock(wraps=Gql(issue_node("TASK-2", parent=rel("TASK-9"))))
        code, out, err = self.run_main(["read", "TASK-2"], gql)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith(b"\n"))
        view = json.loads(out)
        self.assertEqual(view["identifier"], "TASK-2")
        self.assertEqual(view["parent"]["identifier"], "TASK-9")
        self.assertIsNone(view["continue"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "runs.jsonl")))
        self.assertEqual(gql.call_args.kwargs["service"], "linear-api-key")

    def test_cut_fields_name_the_full_read(self):
        node = issue_node("TASK-2", comments=conn([comment(n) for n in range(1, 7)]))
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(node))
        view = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(view["truncated"], ["comments"])
        self.assertEqual(view["continue"], "agent-pm-linear issue read TASK-2 --full --output FILE")

    def test_stdout_stays_within_eight_kib_as_valid_json(self):
        wide = "界" * 255
        node = issue_node(
            "TASK-2",
            title=wide,
            description="述" * 5000,
            labels=conn([{"id": f"l{n}", "name": wide} for n in range(50)]),
            comments=conn([comment(n, body="评" * 400) for n in range(1, 6)]),
            children=conn([rel(f"TASK-{n}", title=wide) for n in range(10, 20)]),
            relations=conn(
                [
                    {"type": "blocks", "relatedIssue": rel(f"TASK-{n}", title=wide)}
                    for n in range(30, 40)
                ]
            ),
            inverseRelations=conn(
                [{"type": "blocks", "issue": rel(f"TASK-{n}", title=wide)} for n in range(50, 60)]
            ),
            parent=rel("TASK-9", title=wide),
        )
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(node))
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), 8192)
        view = json.loads(out.decode("utf-8"))
        self.assertEqual(view["title"], wide)
        self.assertIn("description", view["truncated"])
        self.assertIn("labels", view["truncated"])
        self.assertIsNotNone(view["continue"])
        kept = sum(len(view[f]) for f in ("labels", "comments", "children", "blocks", "blocked_by"))
        self.assertLess(kept, 50 + 5 + 30)

    def test_a_lone_surrogate_still_encodes_as_json(self):
        code, out, _ = self.run_main(["read", "TASK-2"], Gql(issue_node(description="a\ud800b")))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.decode("utf-8"))["description"], "a\ud800b")

    def test_a_result_too_large_even_bare_fails(self):
        code, out, err = self.run_main(["read", "TASK-2"], Gql(issue_node(title="t" * 9000)))
        self.assertEqual((code, out), (1, b""))
        self.assertIn("8192", err)
        self.assertIn("--full --output FILE", err)


class Failures(IssueCase):
    def test_exit_codes_and_stderr(self):
        cases = (
            (["read", "task-2"], Gql(None), 2, "task-2"),
            (["read", "TASK-2"], Gql(None), 2, "no such issue"),
            (["read", "TASK-2"], Gql(None, fail=LinearError("linear api error: boom")), 1, "boom"),
            (
                ["read", "TASK-2"],
                Gql(None, fail=Unavailable("issue", reason="timed out")),
                1,
                "timed out",
            ),
            (
                ["read", "TASK-2"],
                Gql(None, fail=CalledProcessError(44, ["security"])),
                2,
                "Keychain",
            ),
            (["read", "TASK-2"], Gql(None, fail=FileNotFoundError("security")), 2, "Keychain"),
            (["read", "TASK-2"], Gql(None, fail=TimeoutExpired("security", 30)), 2, "Keychain"),
        )
        for argv, gql, want, phrase in cases:
            with self.subTest(phrase=phrase):
                code, out, err = self.run_main(argv, gql)
                self.assertEqual((code, out), (want, b""))
                self.assertTrue(err.startswith("agent-pm-linear issue: "), err)
                self.assertIn(phrase, err)

    def test_configuration_errors_exit_two(self):
        with open(self.config, "w") as f:
            f.write(tp.CONFIG.replace('harness_key = "linear-api-key"\n', ""))
        code, out, err = self.run_main(["read", "TASK-2"], Gql(issue_node()))
        self.assertEqual((code, out), (2, b""))
        self.assertIn("harness_key", err)

    def test_diagnostics_stay_within_eight_kib(self):
        gql = Gql(None, fail=LinearError("linear api error: " + "错" * 9000))
        code, _, err = self.run_main(["read", "TASK-2"], gql)
        self.assertEqual(code, 1)
        self.assertLessEqual(len(err.encode("utf-8")), 8192)
        self.assertIn("bytes omitted", err)

    def test_an_auth_failure_shows_no_credential(self):
        found = CompletedProcess([], 0, stdout=KEY + "\n", stderr="")
        refused = urllib.error.HTTPError(client.URL, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with (
            mock.patch("subprocess.run", return_value=found),
            mock.patch("urllib.request.urlopen", side_effect=refused),
        ):
            code, out, err = self.run_main(["read", "TASK-2"], client.linear_gql)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)
        self.assertNotIn(KEY, out.decode() + err)


class Full(IssueCase):
    def gql(self):
        node = issue_node(
            "TASK-2",
            comments=conn([comment(9, body="评" * 400)], more=True, cursor="c-1"),
            children=conn([rel("TASK-10")], more=True, cursor="k-1"),
        )
        return Gql(node, {"comments": [conn([comment(1)])], "children": [conn([rel("TASK-11")])]})

    def test_writes_every_page_to_the_file_and_prints_its_path(self):
        path = os.path.join(self.root, "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], self.gql())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"identifier": "TASK-2", "output": path})
        with open(path, encoding="utf-8") as f:
            text = f.read()
        view = json.loads(text)
        self.assertEqual(
            text, json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        self.assertEqual([c["id"] for c in view["comments"]], ["c9", "c1"])
        self.assertEqual(len(view["comments"][0]["body"]), 400)
        self.assertEqual([c["identifier"] for c in view["children"]], ["TASK-10", "TASK-11"])

    def test_a_relative_output_prints_its_absolute_path(self):
        with mock.patch("os.getcwd", return_value=self.root):
            code, out, _ = self.run_main(
                ["read", "TASK-2", "--output", "out.json", "--full"], self.gql()
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["output"], os.path.join(self.root, "out.json"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out.json")))

    def test_full_and_output_go_together(self):
        for argv in (["read", "TASK-2", "--full"], ["read", "TASK-2", "--output", "x.json"]):
            gql = self.gql()
            with self.subTest(argv=argv):
                code, out, err = self.run_main(argv, gql)
                self.assertEqual((code, out, gql.calls), (2, b"", []))
                self.assertIn("--full --output FILE", err)

    def test_an_unwritable_output_exits_two(self):
        path = os.path.join(self.root, "missing", "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], self.gql())
        self.assertEqual((code, out), (2, b""))
        self.assertIn("--output", err)

    def test_a_failed_page_writes_no_file(self):
        gql = self.gql()
        gql.pages["children"] = [Unavailable("issue", reason="timed out")]
        path = os.path.join(self.root, "TASK-2.json")
        code, out, err = self.run_main(["read", "TASK-2", "--full", "--output", path], gql)
        self.assertEqual((code, out), (1, b""))
        self.assertIn("timed out", err)
        self.assertFalse(os.path.exists(path))

    def test_an_auth_failure_writes_no_file_and_shows_no_credential(self):
        found = CompletedProcess([], 0, stdout=KEY + "\n", stderr="")
        refused = urllib.error.HTTPError(client.URL, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        path = os.path.join(self.root, "TASK-2.json")
        with (
            mock.patch("subprocess.run", return_value=found),
            mock.patch("urllib.request.urlopen", side_effect=refused),
        ):
            code, out, err = self.run_main(
                ["read", "TASK-2", "--full", "--output", path], client.linear_gql
            )
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out.decode() + err)
        self.assertFalse(os.path.exists(path))
