import hashlib, json, os, re, shutil, sys, tempfile, unittest
from dataclasses import replace
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
import board_ids  # noqa: E402
import issues  # noqa: E402
import config  # noqa: E402
import linear  # noqa: E402
import writeback  # noqa: E402
import drive  # noqa: E402

ID, UUID = "ENG-7", "11111111-2222-4333-8444-555555555555"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
OTHER_SID = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
SRC_UUID = "99999999-8888-4777-8666-555555555555"
HUMANS = ("ann@example.com", "bob@example.com")
STATES = board_ids.STATES
PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"
REPOS = {PROJECT: "ophis/agent-pm"}
TARGET = ("ophis", "agent-pm")
DOC = "https://github.com/ophis/private_docs/blob/main/Research/RES-4-queues.md"
PRD = "https://github.com/ophis/private_docs/blob/main/Product%20Design/PRD-3-registry.md"
PR = "https://github.com/ophis/agent-pm/pull/7"
TREE = "https://github.com/ophis/agent-pm/tree/ENG-7-session-registry"
SPEC_TEXT = "# Spec\n\nBuild the registry.\n"
PLAN_TEXT = "# Plan\n\nRESUME: phase=2\n\n- [ ] task 1\n"
FOOTER = "Answer in a comment, then move this issue back to Todo."
APPROVE = "**🔴 To approve, move this issue to Handoff with a comment `Repo: <owner>/<name>` naming the target repo.**"
APPROVE_MAPPED = ("**🔴 Target repo:** `ophis/agent-pm` **(from the project mapping). To approve, move this issue to "
                  "Handoff; to use another repo, comment** `Repo: <owner>/<name>` **first.**")
TS = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")

NAMES = {linear.M_SUBSCRIBE: "subscribe", linear.M_COMMENT: "comment", linear.M_STATE: "state",
         writeback.M_TITLE: "title", writeback.M_ATTACH: "attach", writeback.M_UNARCHIVE: "unarchive",
         writeback.Q_ATTACHED: "read", linear.Q_ISSUE_STATE: "reread", writeback.Q_ID: "id"}
FIELDS = {"subscribe": "issueSubscribe", "comment": "commentCreate", "state": "issueUpdate", "title": "issueUpdate",
          "attach": "attachmentLinkURL", "unarchive": "issueUnarchive"}


class Gql:
    """Fake role-account gql recording (name, variables). `states`: what each move's state read (Q_ISSUE_STATE)
    returns, the last repeating;
    `fail(name, v)` → an exception to raise, False for `success: false`, else None."""
    def __init__(self, states=(STATES["in_progress"],), attachments=(), fail=lambda name, v: None, issue=None):
        self.calls, self.states, self.attachments, self.fail, self.issue = [], list(states), attachments, fail, issue

    def __call__(self, query, **v):
        name = NAMES[query]
        self.calls.append((name, v))
        r = self.fail(name, v)
        if isinstance(r, BaseException):
            raise r
        if name == "read":
            return {"issue": {"attachments": {"nodes": [{"url": u} for u in self.attachments]}}}
        if name == "reread":
            s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"issue": {"state": {"id": s}}}
        if name == "id":
            return {"issue": self.issue}
        return {FIELDS[name]: {"success": r is not False}}


READ = ("read", {"i": UUID})


def reread(i=UUID):
    return ("reread", {"i": i})


def sub(e, i=UUID):
    return ("subscribe", {"i": i, "e": e})


def comment(b, i=UUID):
    return ("comment", {"i": i, "b": b})


def move(s, i=UUID):
    return ("state", {"i": i, "s": STATES[s]})


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def spec_comment(name="ENG-7-spec.md", text=SPEC_TEXT):
    return comment(f"**Spec** `{name}`\n\n---\n\n{text}")


def plan_comment(name="ENG-7-plan.md", text=PLAN_TEXT):
    return comment(f"**Plan** `{name}`\n\n---\n\n{text}")


def expect(*, body, state, files=(), title=None, subscribe=True, attach=None, attach_title=None):
    """A finish's calls in the spec's order: read → files → retitle → subscribe → comment → attach → re-read, move."""
    calls = [READ, *files]
    if title:
        calls.append(("title", {"i": UUID, "t": title}))
    if subscribe:
        calls += [sub(e) for e in HUMANS]
    calls.append(comment(body))
    if attach:
        calls.append(("attach", {"i": UUID, "u": attach, "t": attach_title}))
    return calls + [reread(), move(state)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plog = os.path.join(self.tmp, "project.log")

    def ctx(self, role="engineer", *, gql=None, resume=False, target=TARGET, project=None, sid=SID, workdir=None):
        if workdir is None:
            workdir = tempfile.mkdtemp(dir=self.tmp)
        return writeback.Context(ident=ID, issue_id=UUID, role=role, sid=sid, resume=resume, project=project,
                                 workdir=workdir, plog=self.plog, gql=gql or Gql(), humans=HUMANS, states=STATES,
                                 repos=REPOS, team=board_ids.TEAM, target=target)

    def write(self, ctx, rel, text):
        path = os.path.join(ctx.workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def files(self, ctx):
        """(spec, plan) paths in the agent run's core clone."""
        return (self.write(ctx, "src/agent-pm/docs/ENG-7-spec.md", SPEC_TEXT),
                self.write(ctx, "src/agent-pm/docs/ENG-7-plan.md", PLAN_TEXT))

    def lines(self):
        if not os.path.exists(self.plog):
            return []
        with open(self.plog) as f:
            out = f.read().splitlines()
        for line in out:
            self.assertRegex(line, TS)
        return [TS.sub("", line, count=1) for line in out]

    def ledger(self, ctx):
        with open(os.path.join(ctx.workdir, "writeback.json")) as f:
            return json.load(f)


def outcome(status, title="Queue options", summary="", questions=(), url="", files=()):
    return drive.Outcome(status, title, summary, list(questions), url, list(files))


class SayAndApprove(Base):
    def test_say(self):
        self.assertEqual(writeback.say("", "just text"), "just text")
        self.assertEqual(writeback.say("Build started:", ""), "Build started")
        self.assertEqual(writeback.say("Build started:", "first build of the PRD"), "Build started: first build of the PRD")
        self.assertEqual(writeback.say("Build ready:", "a\nb"), "Build ready:\na\nb")

    def test_approve_line(self):
        self.assertEqual(writeback.approve_line(None), APPROVE)
        self.assertEqual(writeback.approve_line("ophis/agent-pm"), APPROVE_MAPPED)

    def test_queries_verbatim(self):
        self.assertEqual(writeback.M_TITLE, "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }")
        self.assertEqual(writeback.M_ATTACH, "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }")
        self.assertEqual(writeback.M_UNARCHIVE, "mutation($i: String!) { issueUnarchive(id: $i) { success } }")
        self.assertEqual(writeback.Q_ATTACHED, "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }")
        self.assertEqual(writeback.Q_ID, "query($i: String!) { issue(id: $i) { id team { id } } }")


class FinishGolden(Base):
    def outcomes(self, role, spec, plan):
        kind = config.TASKS[role].kind
        if kind == "research":
            return {"done": outcome("done", summary="Three queues compared.\nRedis streams fit best.", url=DOC),
                    "needs_input": outcome("needs_input", questions=["Which repo?", "Which version?"], url=DOC),
                    "failed": outcome("failed", summary="gh api failed", url=DOC)}
        if kind == "design":
            return {"done": outcome("done", "Session Registry", "PRD for the registry.", url=PRD),
                    "needs_input": outcome("needs_input", "Session Registry", questions=["Who uses it?", "Web or CLI?"]),
                    "failed": outcome("failed", "Session Registry", "Could not publish.")}
        files = [plan, spec]
        return {"done": outcome("done", "ENG-7: Session registry", "Adds the registry.\nVerify: python3 -m unittest",
                                url=PR, files=files),
                "needs_input": outcome("needs_input", "ENG-7: Session registry", questions=["Keep the old API?"],
                                       files=files),
                "failed": outcome("failed", "ENG-7: Session registry", "push not permitted", url=TREE, files=files)}

    def golden(self, role, status, resume):
        research = ("1. Which repo?\n2. Which version?\n\n" + FOOTER
                    + " To change the target repo, edit the description's `Repo:` line.")
        files = [spec_comment(), plan_comment()] if config.TASKS[role].files else []
        table = {
            ("research", "done"): expect(body=f"Three queues compared.\nRedis streams fit best.\n\n{DOC}", state="in_review",
                                         attach=DOC, attach_title="Queue options"),
            ("research", "needs_input"): expect(body=research, state="in_review"),
            ("research", "failed"): (expect(body=f"gh api failed\n\n{DOC}", state="in_review") if resume else
                                     expect(body=f"gh api failed\n\n{DOC}", state="todo", subscribe=False)),
            ("design", "done"): expect(title="PRD: Session Registry", body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE}",
                                       state="in_review", attach=PRD, attach_title="Session Registry"),
            ("design", "needs_input"): expect(body=f"1. Who uses it?\n2. Web or CLI?\n\n{FOOTER}", state="in_review"),
            ("design", "failed"): expect(body="Could not publish.", state="in_review"),
            ("build", "done"): expect(files=files, state="in_review",
                                      body=f"Build ready:\nAdds the registry.\nVerify: python3 -m unittest\n\n{PR}",
                                      attach=PR, attach_title="ENG-7: Session registry"),
            ("build", "needs_input"): expect(body=f"Question:\n1. Keep the old API?\n\n{FOOTER}", state="in_review"),
            ("build", "failed"): expect(files=files, state="in_review",
                                        body=f"Build failed: push not permitted\n\n{TREE}"),
        }
        return table[(config.TASKS[role].kind, status)]

    def test_every_role_status_and_mode(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        for role in config.TASKS:
            for status in drive.STATUSES:
                for resume in (False, True):
                    with self.subTest(role=role, status=status, resume=resume):
                        gql = Gql()
                        build = config.TASKS[role].kind == "build"
                        ctx = self.ctx(role, gql=gql, resume=resume, target=TARGET if build else None)
                        spec, plan = self.files(ctx)
                        self.assertTrue(writeback.finish(ctx, self.outcomes(role, spec, plan)[status]))
                        self.assertEqual(gql.calls, self.golden(role, status, resume))

    def test_plog_and_ledger(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        writeback.finish(ctx, self.outcomes("engineer", spec, plan)["done"])
        steps = [f"file:ENG-7-spec.md:{sha(SPEC_TEXT)}", f"file:ENG-7-plan.md:{sha(PLAN_TEXT)}", "comment",
                 f"attach:{PR}", "move:in_review"]
        self.assertEqual(self.lines(), [f"writeback {ID}: {s}" for s in steps])
        self.assertEqual(self.ledger(ctx), {SID: steps})

    def test_design_retitle_and_mapped_approve_line(self):
        gql = Gql()
        ctx = self.ctx("pm", gql=gql, target=None, project=PROJECT)
        writeback.finish(ctx, outcome("done", "Session Registry", "PRD for the registry.", url=PRD))
        self.assertEqual(gql.calls, expect(title="PRD: Session Registry", body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}",
                                           state="in_review", attach=PRD, attach_title="Session Registry"))
        self.assertEqual(self.ledger(ctx), {SID: ["retitle", "comment", f"attach:{PRD}", "move:in_review"]})


class FinishLedger(Base):
    def test_second_finish_skips_ledgered_steps(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        done = outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again), done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_stops_at_first_error_and_resumes_there(self):
        broken = Gql(fail=lambda name, v: False if name == "comment" and v["b"].startswith("Build ready") else None)
        ctx = self.ctx(gql=broken)
        spec, plan = self.files(ctx)
        done = outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])
        self.assertFalse(writeback.finish(ctx, done))
        self.assertEqual(broken.calls[-1], comment(f"Build ready: Ready.\n\n{PR}"))
        self.assertEqual(self.lines()[-1], f"writeback-error {ID}: comment: RuntimeError: commentCreate: success: false")
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again), done))
        self.assertEqual(again.calls, expect(body=f"Build ready: Ready.\n\n{PR}", state="in_review", attach=PR,
                                             attach_title="ENG-7: x"))

    def test_read_error_stops(self):
        gql = Gql(fail=lambda name, v: SystemExit("linear api error: down") if name == "read" else None)
        ctx = self.ctx(gql=gql)
        self.assertFalse(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
        self.assertEqual(gql.calls, [READ])
        self.assertEqual(self.lines(), [f"writeback-error {ID}: read: SystemExit: linear api error: down"])

    def test_other_sid_does_not_skip(self):
        ctx = self.ctx()
        self.write(ctx, "writeback.json", json.dumps({OTHER_SID: ["comment", "move:in_review"]}))
        gql = Gql()
        writeback.finish(replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted"))
        self.assertEqual(gql.calls, expect(body="Build failed: push not permitted", state="in_review"))
        self.assertEqual(self.ledger(ctx), {OTHER_SID: ["comment", "move:in_review"], SID: ["comment", "move:in_review"]})

    def test_unreadable_ledger_is_empty(self):
        for text in ("not json", json.dumps([1]), json.dumps({SID: "comment move:in_review"}), json.dumps({SID: [1]})):
            with self.subTest(text=text):
                gql = Gql()
                ctx = self.ctx(gql=gql)
                self.write(ctx, "writeback.json", text)
                writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
                self.assertEqual(gql.calls, expect(body="Build failed: push not permitted", state="in_review"))

    def test_fifo_ledger_does_not_block(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        os.mkfifo(os.path.join(ctx.workdir, "writeback.json"))
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertEqual(self.ledger(ctx), {SID: ["comment", "move:in_review"]})

    def test_symlinked_ledger_is_empty_and_replaced(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        elsewhere = os.path.join(self.tmp, "elsewhere.json")
        with open(elsewhere, "w") as f:
            f.write(json.dumps({SID: ["comment", "move:in_review"]}))
        os.symlink(elsewhere, os.path.join(ctx.workdir, "writeback.json"))
        writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
        self.assertEqual(gql.calls, expect(body="Build failed: push not permitted", state="in_review"))
        self.assertFalse(os.path.islink(os.path.join(ctx.workdir, "writeback.json")))
        with open(elsewhere) as f:
            self.assertEqual(json.load(f), {SID: ["comment", "move:in_review"]})

    def test_ledger_write_failure_leaves_no_temp(self):
        ctx = self.ctx()
        with mock.patch.object(drive.os, "replace", side_effect=OSError("disk full")):
            err = writeback._step(ctx, "comment", lambda: None)
        self.assertIsInstance(err, OSError)
        self.assertEqual(os.listdir(ctx.workdir), [])


class FinishSteps(Base):
    def test_attach_skipped_when_already_attached(self):
        gql = Gql(attachments=[PR])
        ctx = self.ctx(gql=gql)
        self.assertTrue(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
        self.assertEqual(gql.calls, expect(body=f"Build ready: Ready.\n\n{PR}", state="in_review"))
        self.assertIn(f"attach:{PR}", self.ledger(ctx)[SID])

    def test_already_linked_errors_count_as_attached(self):
        for text in ("url has already been linked to issue", "Duplicate attachment", "Unable to create issue attachment"):
            with self.subTest(text=text):
                err = SystemExit(f"linear api error: [{{'message': '{text}'}}]")
                gql = Gql(fail=lambda name, v: err if name == "attach" else None)
                ctx = self.ctx(gql=gql)
                self.assertTrue(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
                self.assertEqual(gql.calls[-1], move("in_review"))
                self.assertIn(f"attach:{PR}", self.ledger(ctx)[SID])

    def test_other_attach_error_stops(self):
        gql = Gql(fail=lambda name, v: SystemExit("linear api error: boom") if name == "attach" else None)
        ctx = self.ctx(gql=gql)
        self.assertFalse(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
        self.assertEqual(gql.calls[-1][0], "attach")
        self.assertEqual(self.lines()[-1], f"writeback-error {ID}: attach:{PR}: SystemExit: linear api error: boom")

    def test_state_already_target(self):
        gql = Gql(states=[STATES["in_review"]])
        ctx = self.ctx(gql=gql)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertEqual(gql.calls[-1], reread())
        self.assertEqual(self.lines()[-1], f"writeback {ID}: move:in_review")
        self.assertIn("move:in_review", self.ledger(ctx)[SID])

    def test_state_never_overrides_a_user_move(self):
        gql = Gql(states=[STATES["handoff"]])
        ctx = self.ctx(gql=gql)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertEqual(gql.calls[-1], reread())
        self.assertIn(f"writeback-skip {ID}: move to in_review: issue is {STATES['handoff']}", self.lines())
        self.assertIn("move:in_review", self.ledger(ctx)[SID])

    def test_subscribe_failures_noted_and_finish_continues(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == HUMANS[0] else SystemExit("linear api error: boom")
        gql = Gql(fail=fail)
        ctx = self.ctx(gql=gql)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        body = ("Build failed: push not permitted"
                "\n\nCould not subscribe ann@example.com: RuntimeError: issueSubscribe: success: false"
                "\n\nCould not subscribe bob@example.com: SystemExit: linear api error: boom")
        self.assertEqual(gql.calls, expect(body=body, state="in_review"))

    def test_rejected_spec_post_noted(self):
        err = SystemExit("linear api error: body too long")
        gql = Gql(fail=lambda name, v: err if name == "comment" and v["b"].startswith("**Spec**") else None)
        ctx = self.ctx(gql=gql)
        spec, plan = self.files(ctx)
        self.assertTrue(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])))
        body = f"Build ready: Ready.\n\n{PR}\n\nCould not post the Spec `ENG-7-spec.md`: SystemExit: linear api error: body too long"
        self.assertEqual(gql.calls, expect(files=[spec_comment(), plan_comment()], body=body, state="in_review",
                                           attach=PR, attach_title="ENG-7: x"))
        self.assertIn(f"writeback-error {ID}: file:ENG-7-spec.md:{sha(SPEC_TEXT)}: SystemExit: linear api error: body too long",
                      self.lines())

    def test_files_only_for_files_roles(self):
        gql = Gql()
        ctx = self.ctx("pm", gql=gql, target=None)
        spec, plan = self.files(ctx)
        writeback.finish(ctx, outcome("failed", "Session Registry", "Could not publish.", files=[spec, plan]))
        self.assertEqual(gql.calls, expect(body="Could not publish.", state="in_review"))


class FileRecheck(Base):
    def finish_with(self, ctx, path):
        gql = Gql()
        writeback.finish(replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted", files=[path]))
        posted = [c for c in gql.calls if c[0] == "comment"]
        self.assertEqual(len(posted), 1, posted)
        return posted[0][1]["b"]

    def assert_skipped(self, ctx, path, reason):
        name = os.path.basename(path)
        self.assertEqual(self.finish_with(ctx, path),
                         f"Build failed: push not permitted\n\nCould not post the file `{name}`: {reason}")

    def test_symlink(self):
        ctx = self.ctx()
        spec, _ = self.files(ctx)
        link = os.path.join(ctx.workdir, "link.md")
        os.symlink(spec, link)
        body = self.finish_with(ctx, link)
        self.assertTrue(body.startswith("Build failed: push not permitted\n\nCould not post the file `link.md`: OSError: "), body)

    def test_hard_link(self):
        ctx = self.ctx()
        spec, _ = self.files(ctx)
        os.link(spec, os.path.join(ctx.workdir, "copy.md"))
        self.assert_skipped(ctx, spec, "ValueError: not a single-link file")

    def test_not_md(self):
        ctx = self.ctx()
        self.assert_skipped(ctx, self.write(ctx, "notes.txt", "x"), "ValueError: not a .md file")

    def test_outside_workdir(self):
        ctx = self.ctx()
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("secret")
        self.assert_skipped(ctx, outside, "ValueError: not under the workdir")

    def test_outside_through_a_linked_dir(self):
        ctx = self.ctx()
        os.makedirs(os.path.join(self.tmp, "away"))
        with open(os.path.join(self.tmp, "away", "x.md"), "w") as f:
            f.write("secret")
        os.symlink(os.path.join(self.tmp, "away"), os.path.join(ctx.workdir, "away"))
        self.assert_skipped(ctx, os.path.join(ctx.workdir, "away", "x.md"), "ValueError: not under the workdir")

    def test_too_big(self):
        ctx = self.ctx()
        self.assert_skipped(ctx, self.write(ctx, "big.md", "x" * 1_000_001), "ValueError: over 1000000 bytes")

    def test_at_the_limit_posts(self):
        ctx = self.ctx()
        path = self.write(ctx, "big.md", "x" * 1_000_000)
        gql = Gql()
        writeback.finish(replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted", files=[path]))
        self.assertEqual(gql.calls[1], comment(f"**Spec** `big.md`\n\n---\n\n{'x' * 1_000_000}"))

    def test_fifo_does_not_block(self):
        ctx = self.ctx()
        fifo = os.path.join(ctx.workdir, "pipe.md")
        os.mkfifo(fifo)
        self.assert_skipped(ctx, fifo, "ValueError: not a regular file")

    def test_invalid_utf8_replaced(self):
        ctx = self.ctx()
        path = os.path.join(ctx.workdir, "spec.md")
        with open(path, "wb") as f:
            f.write(b"ok \xff")
        gql = Gql()
        writeback.finish(replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted", files=[path]))
        self.assertEqual(gql.calls[1], comment("**Spec** `spec.md`\n\n---\n\nok \ufffd"))


class UrlCheck(Base):
    def test_other_repo_dropped(self):
        evil = "https://github.com/ophis/agent-pm-evil/pull/1"
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=evil))
        self.assertEqual(gql.calls, expect(body="Build ready: Ready.", state="in_review"))
        self.assertEqual(self.lines()[0], f"writeback-skip {ID}: url not on ophis/agent-pm")

    def test_case_insensitive_match(self):
        gql = Gql()
        ctx = self.ctx(gql=gql, target=("Ophis", "Agent-PM"))
        writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR))
        self.assertEqual(gql.calls, expect(body=f"Build ready: Ready.\n\n{PR}", state="in_review", attach=PR,
                                           attach_title="ENG-7: x"))

    def test_no_target_drops_every_url(self):
        gql = Gql()
        ctx = self.ctx(gql=gql, target=None)
        writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted", url=TREE))
        self.assertEqual(gql.calls, expect(body="Build failed: push not permitted", state="in_review"))
        self.assertEqual(self.lines()[0], f"writeback-skip {ID}: url not on a target repo")


class Sink(Base):
    def test_start_once_per_sid(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        start = drive.Event("progress", text="first build of the PRD", name="start")
        writeback.sink(ctx)(start)
        writeback.sink(ctx)(start)
        self.assertEqual(gql.calls, [comment("Build started: first build of the PRD")])
        self.assertEqual(self.ledger(ctx), {SID: ["start"]})
        self.assertEqual(self.lines(), [f"writeback {ID}: start"])
        writeback.sink(replace(ctx, sid=OTHER_SID))(start)
        self.assertEqual(len(gql.calls), 2)

    def test_build_started_matches_the_cutoff(self):
        gql = Gql()
        writeback.sink(self.ctx(gql=gql))(drive.Event("progress", name="start"))
        self.assertEqual(gql.calls, [comment("Build started")])
        self.assertTrue(issues.BUILD_STARTED.match(gql.calls[0][1]["b"]))

    def test_start_leads(self):
        for role, body in (("researcher", "Research started: budget 2 rounds"), ("pm", "PRD started: budget 2 rounds")):
            with self.subTest(role=role):
                gql = Gql()
                writeback.sink(self.ctx(role, gql=gql))(drive.Event("progress", text="budget 2 rounds", name="start"))
                self.assertEqual(gql.calls, [comment(body)])

    def test_the_start_comment_is_the_roles_lead_and_the_runs_pick(self):
        gql = Gql()
        pick = "light-build: a template wording tweak, like TASK-226."
        writeback.sink(self.ctx("engineer", gql=gql))(drive.Event("progress", text=pick, name="start"))
        self.assertEqual(gql.calls, [comment(f"Build started: {pick}")])

    def test_ignores_text_and_outcome(self):
        gql = Gql()
        s = writeback.sink(self.ctx(gql=gql))
        s(drive.Event("text", text="hello"))
        s(drive.Event("outcome", outcome={"status": "done"}))
        self.assertEqual(gql.calls, [])

    def test_posts_every_other_mark_once_per_text(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        s = writeback.sink(ctx)
        s(drive.Event("progress", text="12 passing", name="tests"))
        s(drive.Event("progress", text="12 passing", name="tests"))
        s(drive.Event("progress", text="13 passing", name="tests"))
        self.assertEqual(gql.calls, [comment("Progress (tests): 12 passing"), comment("Progress (tests): 13 passing")])
        self.assertEqual(self.ledger(ctx), {SID: [f"progress:tests:{sha('12 passing')}", f"progress:tests:{sha('13 passing')}"]})

    def test_swallows_system_exit_and_retries_later(self):
        gql = Gql(fail=lambda name, v: SystemExit("linear api error: down"))
        ctx = self.ctx(gql=gql)
        start = drive.Event("progress", text="x", name="start")
        writeback.sink(ctx)(start)
        self.assertEqual(self.lines(), [f"writeback-error {ID}: start: SystemExit: linear api error: down"])
        ok = Gql()
        writeback.sink(replace(ctx, gql=ok))(start)
        self.assertEqual(ok.calls, [comment("Build started: x")])

    def test_signal_exit_passes_through(self):
        gql = Gql(fail=lambda name, v: SystemExit(129))
        ctx = self.ctx(gql=gql)
        with self.assertRaises(SystemExit) as cm:
            writeback.sink(ctx)(drive.Event("progress", text="x", name="start"))
        self.assertEqual(cm.exception.code, 129)
        self.assertFalse(os.path.exists(os.path.join(ctx.workdir, "writeback.json")))
        self.assertEqual(self.lines(), [])

    def test_never_raises(self):
        gql = Gql(fail=lambda name, v: RuntimeError("boom"))
        ctx = replace(self.ctx(gql=gql), plog=os.path.join(self.tmp, "missing", "p.log"))
        writeback.sink(ctx)(drive.Event("progress", text="x", name="start"))
        writeback.sink(replace(self.ctx(), role="nope"))(drive.Event("progress", text="x", name="start"))
        self.assertEqual(self.lines(), [f"writeback-error {ID}: sink: KeyError: 'nope'"])


class FinishNeverRaises(Base):
    def test_unknown_role_logged(self):
        self.assertFalse(writeback.finish(self.ctx("nope"), outcome("done", summary="x")))
        self.assertEqual(self.lines(), [f"writeback-error {ID}: finish: KeyError: 'nope'"])

    def test_signal_exit_stops_the_steps(self):
        for name in ("read", "subscribe", "comment", "attach"):
            with self.subTest(name=name):
                gql = Gql(fail=lambda n, v: SystemExit(143) if n == name else None)
                ctx = self.ctx(gql=gql)
                self.assertFalse(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(self.lines()[-1], f"writeback-error {ID}: finish: SystemExit: 143")
                self.assertNotIn("state", [c[0] for c in gql.calls])


HANDOFF = ("Handoff from PRD-3: https://linear.app/t/issue/PRD-3\n\n## Source\n- PRD: x\n\n"
           "## Instructions\nBuild it.\n\n## Comments\n* Ann:\n  > Repo: ophis/other")


def issue(description):
    return issues.Issue(UUID, ID, f"https://linear.app/t/issue/{ID}", "ENG: Session registry", description,
                        "2026-09-01T00:00:00.000Z", None, (), (), ())


class Bounce(Base):
    DONE = (STATES["done"], STATES["in_progress"])   # the source's state, then this issue's

    def src(self, team=board_ids.TEAM):
        return {"id": SRC_UUID, "team": {"id": team}}

    def text(self, reason, tail=""):
        return (f"Repo check failed: {reason}. To build it, move PRD-3 to Handoff again with a comment "
                f"`Repo: <owner>/<name>` naming the target repo.{tail}")

    def test_handoff_path(self):
        gql = Gql(issue=self.src(), states=self.DONE)
        reason = "ophis/x: no push permission"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), reason)
        t = self.text(reason)
        self.assertEqual(gql.calls, [("id", {"i": "PRD-3"}), ("unarchive", {"i": SRC_UUID}),
                                     sub(HUMANS[0], SRC_UUID), sub(HUMANS[1], SRC_UUID), comment(t, SRC_UUID),
                                     reread(SRC_UUID), move("in_review", SRC_UUID), comment(t), reread(), move("canceled")])
        self.assertEqual(self.lines(), [])

    def test_skipped_moves_logged(self):
        gql = Gql(issue=self.src(), states=(STATES["handoff"], STATES["todo"]))
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        t = self.text("r")
        self.assertEqual(gql.calls[-4:], [comment(t, SRC_UUID), reread(SRC_UUID), comment(t), reread()])
        self.assertEqual(self.lines(), [f"writeback-skip PRD-3: move to in_review: issue is {STATES['handoff']}",
                                        f"writeback-skip {ID}: move to canceled: issue is {STATES['todo']}"])

    def test_mapped_reason_names_project_repos(self):
        gql = Gql(issue=self.src())
        reason = "project mapping ophis/x: not found or no access (HTTP 404)"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), reason)
        self.assertIn(comment(self.text(reason, " Or fix ~/.agent-pm/orchestrator.local.toml's [project_repos] entry.")), gql.calls)

    def test_unarchive_failure_ignored(self):
        for err in (SystemExit("linear api error: not archived"), False):
            with self.subTest(err=err):
                gql = Gql(issue=self.src(), states=self.DONE, fail=lambda name, v: err if name == "unarchive" else None)
                writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
                self.assertEqual(gql.calls[-1], move("canceled"))

    def test_subscribe_failure_noted_move_made(self):
        gql = Gql(issue=self.src(), states=self.DONE,
                  fail=lambda name, v: SystemExit("linear api error: boom") if name == "subscribe" and v["e"] == HUMANS[1] else None)
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        t = self.text("r")
        self.assertIn(comment(t + "\n\nCould not subscribe bob@example.com: SystemExit: linear api error: boom", SRC_UUID),
                      gql.calls)
        self.assertEqual(gql.calls[-5:], [reread(SRC_UUID), move("in_review", SRC_UUID), comment(t), reread(), move("canceled")])

    def question(self, reason):
        return ("Question: repo check failed: " + reason
                + ". Fix the description's `Repo:` line, then move this issue back to Todo.")

    def test_not_a_handoff_question_path(self):
        gql = Gql()
        writeback.bounce(self.ctx(gql=gql), issue("Repo: nope\n\nBuild it."), "unreadable Repo line: 'nope'")
        self.assertEqual(gql.calls, [sub(HUMANS[0]), sub(HUMANS[1]), comment(self.question("unreadable Repo line: 'nope'")),
                                     reread(), move("in_review")])

    def test_src_in_another_team_question_path(self):
        gql = Gql(issue=self.src(team="00000000-0000-4000-8000-0000000000ff"))
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        self.assertEqual(gql.calls, [("id", {"i": "PRD-3"}), sub(HUMANS[0]), sub(HUMANS[1]), comment(self.question("r")),
                                     reread(), move("in_review")])

    def test_src_missing_question_path(self):
        gql = Gql(issue=None)
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        self.assertEqual(gql.calls[1:], [sub(HUMANS[0]), sub(HUMANS[1]), comment(self.question("r")), reread(),
                                         move("in_review")])

    def test_question_subscribe_failure_noted(self):
        gql = Gql(fail=lambda name, v: False if name == "subscribe" and v["e"] == HUMANS[0] else None)
        writeback.bounce(self.ctx(gql=gql), issue("x"), "r")
        self.assertEqual(gql.calls[-3:], [comment(self.question("r") + "\n\nCould not subscribe ann@example.com: "
                                                  "RuntimeError: issueSubscribe: success: false"), reread(), move("in_review")])

    def test_signal_exit_raises(self):
        for name in ("unarchive", "subscribe"):
            with self.subTest(name=name):
                gql = Gql(issue=self.src(), fail=lambda n, v: SystemExit(129) if n == name else None)
                with self.assertRaises(SystemExit):
                    writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
                self.assertEqual(gql.calls[-1][0], name)

    def test_other_failures_raise(self):
        gql = Gql(issue=self.src(), fail=lambda name, v: False if name == "comment" else None)
        with self.assertRaises(RuntimeError):
            writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        self.assertEqual(gql.calls[-1], comment(self.text("r"), SRC_UUID))
        gql = Gql(fail=lambda name, v: SystemExit("linear api error: x") if name == "state" else None)
        with self.assertRaises(SystemExit):
            writeback.bounce(self.ctx(gql=gql), issue("x"), "r")


if __name__ == "__main__":
    unittest.main()
