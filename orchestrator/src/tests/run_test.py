import dataclasses
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
from board_ids import STATES  # noqa: E402
from run_fixtures import (CONFIG, ENGINEER, ID, KEY, RESEARCHER, SID, UUID, Base as RunBase, Gql,  # noqa: E402
                          forwarded, node)
from attended_test import Tmux  # noqa: E402
import config  # noqa: E402
import attended  # noqa: E402
import router  # noqa: E402
import run  # noqa: E402
import sessions  # noqa: E402
import writeback  # noqa: E402
import clients  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402

PR = "https://github.com/Ophis/Agent-PM/pull/7"
DONE = {"status": "done", "title": "TASK-7: Session registry", "summary": "Opened the PR.", "url": PR}
NOW = "2026-10-04T10:00:00+08:00"
CLOSE = attended.close


class Proc:
    def __init__(self, lines, rc, stderr=()):
        self.stdout, self.rc, self.stderr, self.killed = lines, rc, stderr, False

    def wait(self):
        return self.rc

    def kill(self):
        self.killed = True


def said(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def reported(rd, *lines):
    """Appends to the agent run's report channel, as core's report.py does."""
    with open(os.path.join(rd, compose.CHANNEL), "a") as f:
        f.writelines(json.dumps(line) + "\n" for line in lines)


def outcome(data):
    return {"kind": "outcome", "outcome": data}


class Base(RunBase):
    """run_fixtures' Base with a fake claude (popen) for the inner."""
    def setUp(self):
        super().setUp()
        self.popen_calls, self.proc = [], None
        self.lines, self.rc, self.claude_stderr = [], 0, []

    def popen(self, argv, **kw):
        self.popen_calls.append((argv, kw))
        self.proc = Proc(self.lines, self.rc, self.claude_stderr)
        return self.proc

    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run.main(argv, gql=self.gql, popen=self.popen, root=self.root)
        self.err = err.getvalue()
        return rc


class Inner(Base):
    def setUp(self):
        super().setUp()
        self.handlers, self.tmux = {}, Tmux()
        for p in (mock.patch.object(signal, "signal", lambda s, h: self.handlers.__setitem__(s, h)),
                  mock.patch.object(sessions, "now", return_value=NOW),
                  mock.patch.object(attended, "close", lambda ident, **kw: CLOSE(ident, proc=self.tmux, **kw))):
            p.start()
            self.addCleanup(p.stop)

    def inner(self, assignee=ENGINEER, task=None, mode="new", target="Ophis/Agent-PM", uuid=UUID, extra=()):
        return self.main(["--uuid", uuid, *(["--target", target] if target else []), *forwarded(assignee, task, mode),
                          *extra, "--input=Do it.\n"])

    def rec(self):
        return sessions.base(sid=SID, workdir=self.rd, started_at=NOW)

    def harness(self, rc):
        """The session start and end posts: harness account, bounded by sessions.LIMIT."""
        self.assertEqual({t for (_, service, _), t in zip(self.gql.calls, self.gql.timeouts) if service is None},
                         {sessions.LIMIT})
        find = ("find", None, {"i": ID, "p": f"Run {SID} · "})
        return [find, ("comment", None, {"i": ID, "b": sessions.body(self.rec())}),
                find, ("comment", None, {"i": ID, "b": sessions.body(self.rec(), rc, NOW)})]

    def test_build_run(self):
        """No --task: the prompt has the run pick its task, its start report naming the pick reaches the start
        comment."""
        pick = "light-build: a template wording tweak, like TASK-226."

        def lines():
            reported(self.rd, {"kind": "progress", "name": "start", "text": pick})
            yield said("Working on it")
            reported(self.rd, outcome(DONE))
        self.lines = lines()
        self.claude_stderr = ["claude: warning\n"]
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.said(), [
            f"run launch TASK-7 mode=new sid={SID}",
            f"writeback step TASK-7 sid={SID} step=start",
            f"run end TASK-7 sid={SID} exit=0",
            f"writeback step TASK-7 sid={SID} step=comment",
            f"writeback step TASK-7 sid={SID} step=attach:{PR}",
            f"writeback step TASK-7 sid={SID} step=move:in_review"])
        self.assertEqual(os.listdir(os.path.join(self.root, "logs")), ["orchestrator.jsonl"])
        posts = self.harness(0)
        self.assertEqual(self.gql.calls, [
            *posts[:2],
            ("comment", KEY, {"i": UUID, "b": f"Build started: {pick}"}),
            ("read", KEY, {"i": UUID}),
            ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
            ("comment", KEY, {"i": UUID, "b": "Build ready: Opened the PR."}),
            ("attach", KEY, {"i": UUID, "u": PR, "t": "TASK-7: Session registry"}),
            ("reread", KEY, {"i": UUID}),
            ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
            *posts[2:]])
        (argv, kw), = self.popen_calls
        self.assertEqual((argv[:2], argv[3:5], kw["cwd"]), (["claude", "-p"], ["--session-id", SID], self.rd))
        self.assertIn(f"\n- `<Workdir>`: `{self.rd}`\n", argv[2])
        self.assertTrue(argv[2].endswith("\n# Input\n\nDo it.\n"), argv[2][-200:])
        self.assertIn("**Your task**: pick it from your charter's Tasks section", argv[2])
        events = self.events()
        self.assertEqual([e["text"] for e in events if e["kind"] == "stderr"], ["claude: warning"])
        self.assertEqual([e["kind"] for e in events if e["kind"] != "stderr"],
                         ["input", "session", "progress", "outcome", "result", "end"])
        self.assertEqual(kw["stderr"], subprocess.PIPE)
        self.assertIn("Working on it", self.err)
        self.assertEqual(os.environ["PATH"], config.PATH)

    def test_no_outcome_leaves_the_issue(self):
        self.lines = [said("Working on it")]
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.said()[-2:], [f"run end TASK-7 sid={SID} exit=0",
                                            "run no-outcome TASK-7 error=the agent run returned no outcome"])
        self.assertEqual(self.gql.calls, self.harness(0))

    def test_nonzero_exit(self):
        def lines():
            reported(self.rd, outcome(DONE))
            yield from ()
        self.lines, self.rc = lines(), 1
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.said()[-2:], [f"run end TASK-7 sid={SID} exit=1", "run no-outcome TASK-7 error=the client exited 1"])
        self.assertFalse(os.path.exists(self.runs))
        self.assertEqual(self.gql.calls, self.harness(1))

    def test_sighup_kills_claude(self):
        def lines():
            yield said("Working on it")
            self.handlers[signal.SIGHUP](signal.SIGHUP, None)
            yield said("never")
        self.lines = lines()
        self.assertEqual(self.inner(), 0)
        self.assertTrue(self.proc.killed)
        self.assertEqual(self.said(), [f"run launch TASK-7 mode=new sid={SID}", f"run end TASK-7 sid={SID} exit=129",
                                       "run no-outcome TASK-7 error=no result"])
        self.assertEqual(self.gql.calls, self.harness(129))
        self.assertIn("interrupted", self.gql.calls[-1][2]["b"])
        self.assertEqual(set(self.handlers), {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.assertRaises(SystemExit) as cm:
                self.handlers[sig](sig, None)
            self.assertEqual(cm.exception.code, code)

    def test_tui_runs_as_headless_does_but_for_runner_layout_and_session_naming(self):
        seen = []
        for extra in ((), ("--runner=tui", "--split=below", "--split-from=dev", "--opener=w0t0p0:ABC", "--events=/x/ev.log")):
            self.gql = Gql(node())
            with mock.patch.object(drive, "start", return_value=drive.Result(0, drive.Outcome(**DONE))) as start:
                self.assertEqual(self.inner(extra=extra), 0)
            os.remove(os.path.join(self.rd, writeback.LEDGER))
            ((launch, r, params), kw), = start.call_args_list
            name = launch.interactive[launch.interactive.index("--name") + 1]
            seen.append(([kw.pop(k) for k in ("runner", "layout", "events")] + [params.prefix, name],
                         (dataclasses.replace(launch, interactive=[]), r, dataclasses.replace(params, prefix=None)),
                         [s.__qualname__ for s in kw.pop("sinks")], sorted(kw), self.gql.calls))
        (h_own, *headless), (t_own, *tui) = seen
        self.assertEqual(h_own, ["headless", None, None, None, f"engineer-{SID[:8]}"])
        self.assertEqual(t_own, ["tui", drive.Layout("below", "dev", "w0t0p0:ABC"), "/x/ev.log", "engineer-TASK-7",
                                 f"engineer-TASK-7-{SID[:8]}"])
        self.assertEqual(tui, headless)
        self.assertIn(("state", KEY, {"i": UUID, "s": STATES["in_review"]}), headless[-1])
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs", "tui")))

    def test_the_input_text_reaches_drive_and_the_deliverable_goes_to_out_md(self):
        with mock.patch.object(drive, "start", return_value=drive.Result(0, None)) as start:
            self.assertEqual(self.inner(), 0)
        ((_, _, params), _), = start.call_args_list
        self.assertEqual((params.input, params.out, params.workdir),
                         ("Do it.\n", os.path.join(self.rd, "out.md"), self.rd))

    def test_signals_are_unblocked_once_their_handlers_are_in(self):
        seen = []

        def sigmask(how, sigs):
            seen.append((how, sigs, set(self.handlers)))
        with mock.patch.object(signal, "pthread_sigmask", sigmask), \
                mock.patch.object(drive, "start", return_value=drive.Result(0, None)):
            self.assertEqual(self.inner(), 0)
        self.assertEqual(seen, [(signal.SIG_UNBLOCK, drive.SIGNALS, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})])

    def test_close_runs_before_every_run(self):
        mine = ["engineer-TASK-7-aaaaaaaa", "pm-TASK-7-bbbbbbbb"]
        other = ["engineer-TASK-70-cccccccc", "agent-pm-engineer-TASK-7"]
        for extra in ((), ("--runner=tui",)):
            with self.subTest(extra=extra):
                self.tmux = Tmux(live=[*mine, *other])
                seen = []
                self.gql = Gql(node(), lambda name, v: seen.append(list(self.tmux.live)))
                with mock.patch.object(drive, "start", return_value=drive.Result(0, None)):
                    self.assertEqual(self.inner(extra=extra), 0)
                self.assertEqual(self.said()[-5:-2], [f"run launch TASK-7 mode=new sid={SID}",
                                                      *(f"run tui-closed TASK-7 session={n}" for n in mine)])
                self.assertEqual(self.tmux.tmux_calls(), ["list-sessions", "kill-session", "kill-session"])
                self.assertEqual(seen[0], other)

    def test_close_results_become_tui_lines(self):
        closed = [attended.Closed("closed", "e-TASK-7-aaaaaaaa"),
                  attended.Closed("error", "e-TASK-7-bbbbbbbb", "TuiError: boom"),
                  attended.Closed("error", None, "TuiError: tmux: nope")]
        with mock.patch.object(attended, "close", return_value=closed), \
                mock.patch.object(drive, "start", return_value=drive.Result(0, None)):
            self.assertEqual(self.inner(), 0)
        self.assertEqual(self.said()[1:4], ["run tui-closed TASK-7 session=e-TASK-7-aaaaaaaa",
                                            "run tui-error TASK-7 session=e-TASK-7-bbbbbbbb msg=TuiError: boom",
                                            "run tui-error TASK-7 msg=TuiError: tmux: nope"])

    def test_bad_layout_exits_2(self):
        cases = [(("--runner=tui", "--split=left"), "layout split 'left': want one of right, below"),
                 (("--runner=tui", "--split-from=a:b"), "layout split_from 'a:b': want [A-Za-z0-9_-]+"),
                 (("--runner=tui", "--opener=a b"), "layout opener 'a b': want a tmux session name or an iTerm2 session id"),
                 (("--runner=tui", "--opener=w0:a:b"),
                  "layout opener 'w0:a:b': want a tmux session name or an iTerm2 session id")]
        for extra, msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(self.inner(extra=extra), 2)
                self.assertEqual(self.err, f"run.py: {msg}\n")
        self.assertEqual((self.gql.calls, self.popen_calls, os.path.exists(self.runs)), ([], [], False))

    def test_a_task_reaches_drive_and_the_prompt_names_it(self):
        self.assertEqual(self.inner(task="light-build"), 0)
        (argv, _), = self.popen_calls
        self.assertIn("**Your task**: `light-build`. Read only that task's file", argv[2])

    def test_a_researcher_run_has_the_brake(self):
        self.assertEqual(self.inner(RESEARCHER, target=None), 0)
        (argv, _), = self.popen_calls
        gate = f"python3 {shlex.quote(self.root)}/orchestrator/src/router.py --brake"
        self.assertIn(f"Bash({gate})", argv)
        self.assertIn(f"the gate is `{gate}`", argv[2])
        dirs = [d for flag, d in zip(argv, argv[1:]) if flag == "--add-dir"]
        self.assertIn(os.path.join(self.root, "core", "team", "tasks"), dirs)
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=new sid={SID}")

    def events(self):
        return [json.loads(line) for line in self.read(os.path.join(self.rd, "run.jsonl")).splitlines()]

    def test_resume(self):
        self.write(os.path.join(self.rd, "run.jsonl"), "".join(json.dumps(e) + "\n" for e in (
            {"ts": "t", "kind": "session", "sid": SID, "cwd": self.rd, "project": False},
            {"ts": "t", "kind": "progress", "name": "start", "text": "x"})))
        self.assertEqual(self.inner(mode="resume"), 0)
        (argv, _), = self.popen_calls
        self.assertEqual((argv[3:5], argv[2].startswith(compose.RESUME)), (["--resume", SID], True))
        self.assertEqual([e["kind"] for e in self.events()],
                         ["session", "progress", "input", "session", "result", "end"])
        self.assertEqual(self.said()[0], f"run launch TASK-7 mode=resume sid={SID}")

    def test_an_overlay_cwd_runs_the_agent_there_and_its_resume_stays_there(self):
        there, later = os.path.join(self.tmp, "data repo"), os.path.join(self.tmp, "later")
        os.makedirs(there)
        os.makedirs(later)
        for cwd, mode in ((there, "new"), (later, "resume")):
            self.write(os.path.join(self.root, "orchestrator", "config.toml"),
                       CONFIG + f'[core.roles.engineer]\ncwd = "{cwd}"\n')
            self.popen_calls, self.gql = [], Gql(node())
            self.assertEqual(self.inner(mode=mode), 0)
            (argv, kw), = self.popen_calls
            self.assertEqual((kw["cwd"], argv[argv.index("--add-dir") + 1]), (there, self.rd))
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
            self.assertIn(f"\n- `<Workdir>`: `{self.rd}`\n", argv[2])
            self.assertTrue(argv[2].endswith("\n# Input\n\nDo it.\n"))
            self.assertIn(f"drive.py: cwd {there} is not in trusted_dirs", self.err)
            entry = drive.session(self.rd, SID)
            self.assertEqual((entry["cwd"], entry["project"]), (there, False))
            self.assertIn(f"cd '{there}' && claude --resume {SID} --add-dir '{self.rd}'", self.gql.calls[1][2]["b"])
        self.assertEqual(config.transcript(ID, SID, self.projects), clients.claude.transcript(there, SID, self.projects))

    def test_a_failed_session_comment_is_a_registry_error(self):
        self.gql = Gql(node(), lambda name, v: RuntimeError("down") if name == "find" else None)
        self.lines = [said("Working on it")]
        self.assertEqual(self.inner(), 0)
        self.assertEqual([x for x in self.said() if "registry-error" in x],
                         [f"run registry-error TASK-7 sid={SID} error=RuntimeError: down"] * 2)

    def test_run_error(self):
        def popen(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "claude")
        self.popen = popen
        self.assertEqual(self.inner(), 0)
        self.assertEqual(self.said()[1:], [
            "run run-error TASK-7 error=FileNotFoundError: [Errno 2] No such file or directory: 'claude'",
            f"run end TASK-7 sid={SID} exit=1", "run no-outcome TASK-7 error=no result"])
        self.assertEqual(self.gql.calls, self.harness(1))

    def test_a_role_or_task_error_logs_config_error_and_end(self):
        cases = [(dict(assignee="x@y.com"), "'x@y.com' is not a role account"),
                 (dict(assignee=RESEARCHER, task="build"),
                  "task 'build' is not one of researcher's tasks (deep-research, light-research)")]
        for kw, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.inner(**kw), 1)
                self.assertEqual(self.said()[-2:], [f"run config-error TASK-7 reason={reason}",
                                                    f"run end TASK-7 sid={SID} exit=1"])
                self.assertEqual(self.err, "")
                self.assertFalse(os.path.exists(self.runs))
        self.assertEqual((self.gql.calls, self.popen_calls), ([], []))

    def test_config_error_writes_no_runs_line(self):
        os.remove(os.path.join(self.root, "orchestrator", "config.toml"))
        self.write(os.path.join(self.root, "orchestrator", "config.toml"), "team = 1\n")
        self.assertEqual(self.inner(), 1)
        error, end = self.said()
        self.assertTrue(error.startswith("run config-error TASK-7 reason=orchestrator/config.toml: missing harness_key"), error)
        self.assertEqual(end, f"run end TASK-7 sid={SID} exit=1")
        self.assertFalse(os.path.exists(self.runs))

    def test_bad_uuid_or_target(self):
        for kw in (dict(uuid="nope"), dict(target="a/b/c")):
            with self.subTest(kw=kw):
                self.assertEqual(self.inner(**kw), 2)
                self.assertTrue(self.err.startswith("run.py: bad issue uuid or target: "), self.err)
        self.assertEqual((self.gql.calls, self.popen_calls, os.path.exists(self.runs)), ([], [], False))


class Cli(Base):
    """run.py's arguments, checked before the inner starts."""
    def argv(self):
        return ["--uuid", UUID, "--target", "Ophis/Agent-PM", *forwarded(), "--input", "Do it."]

    def test_runner_headless_is_the_default(self):
        seen = []
        for extra in ((), ("--runner", "headless")):
            with mock.patch.object(run, "inner", side_effect=lambda a, **kw: seen.append((vars(a), kw)) or 0):
                self.assertEqual(self.main(self.argv() + list(extra)), 0)
        self.assertEqual(seen[1], seen[0])
        self.assertEqual((seen[0][0]["runner"], seen[0][1]["layout"]), ("headless", None))
        self.assertEqual(self.err, "")

    def test_tui_options_need_the_tui_runner(self):
        events = os.path.join(self.tmp, "events.log")
        for extra in (["--split", "right"], ["--split-from", "dev"], ["--runner", "headless", "--split", "below"],
                      ["--events", events], ["--opener", "mine"]):
            with self.subTest(extra=extra):
                self.assertEqual(self.main(self.argv() + extra), 2)
                self.assertEqual(self.err, "run.py: --split, --split-from, --opener and --events need --runner tui\n")
        self.assertEqual((self.gql.calls, self.popen_calls), ([], []))
        self.assertFalse(os.path.exists(events))
        self.assertFalse(os.path.exists(self.runs))

    def test_argparse_errors(self):
        argv = self.argv()
        for bad in (argv[:-1], argv[:-2], argv[2:], argv + ["--k", "1"], argv + ["--inner"], argv + ["--tui"]):
            with self.subTest(argv=bad), self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                run.main(bad)
            self.assertEqual(cm.exception.code, 2)

    def test_bad_issue_or_session_id(self):
        for bad in (["--issue", "task-7"], ["--sid", "not-a-sid"], ["--sid", "z" * 36]):
            with self.subTest(bad=bad):
                argv = ["--uuid", UUID, "--issue", ID, "--project", "p", "--assignee", ENGINEER, "--sid", SID,
                        "--task", "build", "--mode", "new", "--input", "Do it."]
                argv[argv.index(bad[0]) + 1] = bad[1]
                self.assertEqual(self.main(argv), 2)
                issue, sid = ("task-7", SID) if bad[0] == "--issue" else (ID, bad[1])
                self.assertEqual(self.err, f"run.py: bad issue or session id: {issue} {sid}\n")
        self.assertEqual((self.gql.calls, self.popen_calls), ([], []))


class Helpers(unittest.TestCase):
    def test_router_launches_this_file(self):
        self.assertEqual(router.RUN, os.path.abspath(run.__file__))


if __name__ == "__main__":
    unittest.main()
