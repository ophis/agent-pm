import os
import subprocess
from dataclasses import replace

from agent_pm.linear_orchestrator.engine import config, inputs, target
from agent_pm.linear_orchestrator.engine import run as run_py
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.session import ConfigError, DriverError, SessionStatus
from agent_pm.utils import GUARD, SHORT
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.router_data import (
    DESIGN_LISTING,
    ENG_RUN,
    INPUT,
    LISTING,
    LS_REMOTE,
    NO,
    OUTER,
    ROLE,
    forwarded,
    inner,
)
from orchestrator_tests.unit.router_scenarios import OuterBase
from orchestrator_tests.unit.run_fixtures import (
    ENGINEER,
    HARNESS,
    ID,
    KEY,
    PROJECT,
    RESEARCHER,
    SID,
    USER_NOTE,
    UUID,
    Gql,
    node,
)
from orchestrator_tests.unit.target_support import ok


class Outer(OuterBase):
    """FR-45, FR-47's resume check: the outer reads the issue, checks its repo, renders its input
    and starts run.py in a detached Driver session.
    """

    def local_clone(self):
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (
            ["init", "-q", path],
            ["-C", path, "remote", "add", "origin", "https://github.com/ophis/agent-pm.git"],
        ):
            subprocess.run(["git", *GUARD, *argv], check=True)
        self.write_config(OUTER + f'[local_clones]\n"Ophis/Agent-PM" = "{path}"\n')
        return path

    def test_build_starts_run_py_in_a_detached_driver_session(self):
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.detached(), inner("--target", "Ophis/Agent-PM", *forwarded()))
        self.assertEqual((len(self.api.detached), os.listdir(self.rd)), (1, []))
        self.assertEqual(self.gql.calls, [("issue", HARNESS, {"i": ID})])
        self.assertEqual(
            self.run_.calls, [(("gh", "api", "repos/ophis/agent-pm"), SHORT), (LS_REMOTE, SHORT)]
        )
        self.assertEqual(
            (os.environ["PATH"], self.keychain_calls, self.api.info), (config.path(), [KEY], [])
        )
        self.assertEqual((self.err, self.said()), ("", []))

    def test_a_given_task_reaches_run_py_whose_argv_parses_it(self):
        self.assertEqual(self.outer(task="light-build"), 0)
        argv = self.detached()
        self.assertEqual(argv, inner("--target", "Ophis/Agent-PM", *forwarded(task="light-build")))
        found = run_py.options(argv[4:])
        self.assertEqual(
            (
                found.uuid,
                found.target,
                found.issue,
                found.project,
                found.assignee,
                found.sid,
                found.task,
                found.mode,
                found.runtime,
            ),
            (UUID, "Ophis/Agent-PM", ID, PROJECT, ENGINEER, SID, "light-build", "new", "a"),
        )
        self.assertEqual((found.input, found.tui), (INPUT, False))

    def test_a_long_input_reaches_run_py_intact(self):
        big = "".join(
            f"Requirement {i}: keep the session registry small.\n" for i in range(500)
        ).strip()
        self.gql.issue = node(description=big, comments=[USER_NOTE])
        self.assertEqual(self.outer(), 0)
        text = INPUT.replace("Add a session registry.", big)
        self.assertGreater(len(text.encode()), 20_000)
        self.assertEqual(
            self.detached(), inner("--target", "Ophis/Agent-PM", *forwarded(), text=text)
        )

    def test_nul_is_removed_and_a_lone_surrogate_replaced_in_the_input(self):
        for description, want in (
            ("Add a session\0 registry.\0", INPUT),
            (
                "Add a session\ud800 registry.",
                INPUT.replace("Add a session registry.", "Add a session? registry."),
            ),
        ):
            with self.subTest(description=description):
                self.gql.issue = node(description=description, comments=[USER_NOTE])
                self.assertEqual(self.outer(), 0)
                self.assertEqual(self.input(), want)

    def test_research_and_design_have_no_target(self):
        rows = (
            (RESEARCHER, "researcher", LISTING, "research", "Question", "Compare queues", "Which?"),
            (ROLE["pm"], "pm", DESIGN_LISTING, "design", "Brief", "Queues PRD", "Build a PRD."),
        )
        for who_, role, listing, kind, heading, title, description in rows:
            with self.subTest(role=role):
                self.gql.issue = node(title=title, description=description)
                self.run_.table, self.run_.calls = [(listing, ok("[]"))], []
                self.assertEqual(self.outer(who_), 0)
                self.assertEqual(
                    self.detached(role),
                    inner(
                        *forwarded(who_),
                        text=(
                            f"Reference: TASK-7\nRepo: ophis/agent-pm\nCheckout: TASK-7\n\n"
                            f"{inputs.PRECEDENCE[kind]}\n\n## {heading}\n\n{title}\n\n{description}"
                        ),
                    ),
                )
                self.assertEqual(self.run_.calls, [(listing, SHORT)])

    def test_a_configured_local_clone_is_the_inputs_repo(self):
        clone = self.local_clone()
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.input(), INPUT.replace("Repo: Ophis/Agent-PM", f"Repo: {clone}"))
        self.assertEqual(
            self.run_.calls, [(("gh", "api", "repos/ophis/agent-pm"), SHORT), (LS_REMOTE, SHORT)]
        )
        self.assertEqual(self.err, "")

    def test_research_and_design_name_the_local_clone(self):
        clone = self.local_clone()
        for who_, role, listing in (
            (RESEARCHER, "researcher", LISTING),
            (ROLE["pm"], "pm", DESIGN_LISTING),
        ):
            with self.subTest(who=who_):
                self.run_.table = [(listing, ok("[]"))]
                self.assertEqual(self.outer(who_), 0)
                argv = self.detached(role)
                self.assertNotIn("--target", argv)
                self.assertEqual(argv[-1].splitlines()[1], f"Repo: {clone}")

    def test_a_resume_over_a_clone_checkout_keeps_its_repo(self):
        self.local_clone()
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        os.makedirs(
            os.path.join(target.checkout(ID, "Ophis", "Agent-PM", work=cfg.runs_dir), ".git")
        )
        self.run_.table = [(("git", *GUARD, "-C"), ok()), *ENG_RUN]
        self.assertEqual(self.outer(mode="resume"), 0)
        self.assertEqual(self.input(), INPUT)
        self.assertEqual([argv[0] for argv, _ in self.run_.calls], ["gh", "git", "git"])

    def test_a_resume_reads_its_session_at_its_runtime_and_rebuilds_the_input(self):
        self.assertEqual(self.outer(mode="resume", runtime="b"), 0)
        self.assertEqual(
            self.detached(),
            inner("--target", "Ophis/Agent-PM", *forwarded(mode="resume", runtime="b")),
        )
        self.assertEqual(self.api.info, [("b", SID, self.rd)])

    def test_a_resume_not_resumable_starts_nothing(self):
        for value, why in (
            (NO, "no"),
            (SessionStatus("unknown", None, ""), "unknown"),
            (RuntimeError("boom"), "RuntimeError: boom"),
        ):
            with self.subTest(why=why):
                self.api.statuses[SID] = value
                self.assertEqual(self.outer(mode="resume"), 3)
                self.assertEqual(
                    (self.said(), self.err),
                    ([f"transient TASK-7 reason=session {SID} not resumable ({why})"], ""),
                )
        self.assertEqual((self.api.detached, self.gql.calls, self.run_.calls), ([], [], []))
        self.assertEqual(self.api.info, [("a", SID, self.rd)] * 3)

    def test_bad_issue_or_session_id(self):
        for ident, sid in (("task-7", SID), (ID, "not-a-sid"), (ID, "z" * 36)):
            with self.subTest(ident=ident, sid=sid):
                self.assertEqual(self.outer(ident=ident, sid=sid), 2)
                self.assertEqual(self.err, f"router.py: bad issue or session id: {ident} {sid}\n")
        self.assertEqual((self.api.detached, self.gql.calls, self.keychain_calls), ([], [], []))

    def test_role_and_task_errors_are_config_errors_of_the_issue(self):
        for kw, reason in (
            ({"assignee": "x@y.com"}, "'x@y.com' is not a role account"),
            (
                {"assignee": RESEARCHER, "task": "build"},
                "task 'build' is not one of researcher's tasks (deep-research, light-research)",
            ),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self.outer(mode="resume", **kw), 2)
                self.assertEqual(
                    (self.said(), self.err), ([f"config-error TASK-7 reason={reason}"], "")
                )
        self.assertEqual(
            (self.api.detached, self.api.info, self.gql.calls, self.keychain_calls),
            ([], [], [], []),
        )

    def test_no_keychain_item(self):
        self.missing.add(KEY)
        for mode in ("new", "resume"):
            with self.subTest(mode=mode):
                self.assertEqual(self.outer(mode=mode), 2)
                self.assertEqual(
                    self.said(),
                    [f"config-error TASK-7 reason=no Keychain item for role key {KEY}"],
                )
        self.assertEqual((self.api.detached, self.api.info, self.gql.calls), ([], [], []))

    def test_linear_errors_are_transient(self):
        for gql, reason in (
            (Gql(), "IssueError: TASK-7: issue not found"),
            (
                Gql(lambda n, v: LinearError("linear api error: boom"), node()),
                "LinearError: linear api error: boom",
            ),
        ):
            with self.subTest(reason=reason):
                self.gql = gql
                self.assertEqual(self.outer(), 3)
                self.assertEqual(self.said(), [f"transient TASK-7 reason=Linear: {reason}"])
        self.assertEqual((self.api.detached, self.run_.calls), ([], []))

    def test_invalid_new_bounces_without_a_run(self):
        self.run_.table = [(("gh", "api"), ok(code=1, stderr="gh: Not Found (HTTP 404)"))]
        reason = "project mapping ophis/agent-pm: not found or no access (HTTP 404)"
        for task in (None, "light-build"):
            with self.subTest(task=task):
                self.gql = Gql(issue=node(comments=[USER_NOTE]))
                self.assertEqual(self.outer(task=task), 0)
                self.assertEqual(
                    self.gql.calls,
                    [
                        ("issue", HARNESS, {"i": ID}),
                        ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                        (
                            "post",
                            KEY,
                            {
                                "i": UUID,
                                "b": (
                                    f"Question: repo check failed: {reason}"
                                    f". Fix the description's `Repo:` line, then move this"
                                    f" issue back to Todo."
                                ),
                            },
                        ),
                        ("reread", KEY, {"i": UUID}),
                        ("state", KEY, {"i": UUID, "s": IDS_BY_KEY["in_review"]}),
                    ],
                )
                self.assertEqual(self.said(), [f"bounce TASK-7 reason={reason}"])
                self.assertEqual((self.api.detached, os.path.exists(self.rd)), ([], False))

    def test_bounce_failure_is_transient(self):
        self.run_.table = [(("gh", "api"), ok(code=1, stderr="HTTP 404"))]
        self.gql = Gql(
            lambda n, v: LinearError("linear api error: boom") if n == "post" else None, node()
        )
        self.assertEqual(self.outer(), 3)
        self.assertEqual(
            self.said(), ["transient TASK-7 reason=bounce: LinearError: linear api error: boom"]
        )
        self.assertEqual(self.api.detached, [])

    def test_invalid_resume_and_transient_check_start_nothing(self):
        for mode, r, reason in (
            (
                "resume",
                ok(code=1, stderr="HTTP 404"),
                "project mapping ophis/agent-pm: not found or no access (HTTP 404)",
            ),
            (
                "new",
                ok(code=1, stderr="HTTP 502: Bad Gateway"),
                "gh api repos/ophis/agent-pm: HTTP 502: Bad Gateway",
            ),
            (
                "new",
                subprocess.TimeoutExpired(["gh", "api"], 60),
                "repo check: TimeoutExpired: Command '['gh', 'api']' timed out after 60 seconds",
            ),
        ):
            with self.subTest(reason=reason):
                self.run_.table = [(("gh", "api"), r)]
                self.assertEqual(self.outer(mode=mode), 3)
                self.assertEqual(self.said(), [f"transient TASK-7 reason={reason}"])
        self.assertEqual(self.gql.calls, [("issue", HARNESS, {"i": ID})] * 3)
        self.assertEqual(self.api.detached, [])

    def test_docs_failure_is_transient(self):
        self.gql.issue = node(title="Compare queues", description="Which queue fits?")
        for r, reason in (
            (ok(code=1, stderr="HTTP 500"), "RuntimeError: gh api contents Research: HTTP 500"),
            (OSError("gh missing"), "OSError: gh missing"),
        ):
            with self.subTest(reason=reason):
                self.run_.table = [(LISTING, r)]
                self.assertEqual(self.outer(RESEARCHER), 3)
                self.assertEqual(self.said(), [f"transient TASK-7 reason=docs: {reason}"])
        self.assertEqual(self.api.detached, [])

    def test_a_run_dir_that_cannot_be_made_starts_nothing(self):
        self.write(self.rd, "")
        self.assertEqual(self.outer(), 1)
        self.assertEqual(
            (self.said(), self.err, self.api.detached),
            (
                [
                    (
                        f"launch-error TASK-7 error=input: FileExistsError: [Errno 17] "
                        f"File exists: '{self.rd}'"
                    )
                ],
                "",
                [],
            ),
        )

    def test_a_driver_session_that_cannot_start_exits_1(self):
        for error in (
            DriverError("detach agent-pm-engineer-TASK-7: duplicate session"),
            ConfigError("cwd must be an existing directory"),
        ):
            with self.subTest(error=error):
                self.api.detach_error = error
                self.assertEqual(self.outer(), 1)
                self.assertEqual(
                    (self.said(), self.err),
                    ([f"launch-error TASK-7 error={type(error).__name__}: {error}"], ""),
                )
        self.assertEqual(self.input(), INPUT)
