from tests.integration.core import fake_codex
from tests.integration.core.live_tmux import wait

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

FAKE = os.path.abspath(fake_codex.__file__)
EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop", "Interrupt")
COMMON = {"session_id", "transcript_path", "cwd", "hook_event_name", "model", "permission_mode"}
HASH = re.compile(r"sha256:[0-9a-f]{64}")
INIT = [{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-pm", "version": "0"},
                                                     "capabilities": {"experimentalApi": True}}},
        {"method": "initialized"}]
CORE = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "core"))
DRIVE = os.path.join(CORE, "src", "drive.py")
SID = "12345678-1234-1234-1234-123456789abc"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"
DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class CodexIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        # Recovery authority cannot live in Codex's implicitly writable temporary directories.
        homes = os.path.join(os.path.dirname(CORE), "work", "fake-codex-homes")
        os.makedirs(homes, mode=0o700, exist_ok=True)
        if os.path.realpath(homes) != homes:
            raise RuntimeError("fake Codex HOME namespace must not contain symlinks")
        home = tempfile.TemporaryDirectory(prefix="home-", dir=homes)
        self.addCleanup(home.cleanup)
        self.home = home.name
        self.work, bin_dir = (os.path.join(self.root, d) for d in ("work", "bin"))
        for path in (self.work, bin_dir):
            os.mkdir(path)
        self.out = os.path.join(self.work, "out.md")
        self.scenario = os.path.join(self.root, "scenario.json")
        self.log = os.path.join(self.root, "codex-call.json")
        self.env = {"PATH": fake_codex.install(bin_dir), "HOME": self.home, "PYTHONUTF8": "1",
                    fake_codex.ENV: self.scenario}

    def drive(self, steps, *extra, **scenario):
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "log": self.log, **scenario}, f)
        argv = [sys.executable, DRIVE, "--client", "codex", "--role", "dummy-tester", "--task", "echo",
                "--input", "Hello.", "--out", self.out, "--workdir", self.work, "--sid", SID, *extra]
        return subprocess.run(argv, cwd=self.work, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def events(self):
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_fixture_home_is_ignored_and_owned_cleanup_removes_it(self):
        result = subprocess.run(["git", "check-ignore", self.home], cwd=os.path.dirname(CORE),
                                capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isdir(self.home))
        self.doCleanups()
        self.assertFalse(os.path.exists(self.home))

    def test_reports_start_and_valid_outcome_through_channel(self):
        res = self.drive([{"kind": "progress", "name": "start", "text": "echoing"},
                          "Codex says hello.", {"kind": "outcome", "outcome": DONE}])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Progress (start): echoing", res.stderr)
        self.assertIn("Codex says hello.", res.stderr)
        self.assertIn("status done", res.stderr)
        with open(self.out) as f:
            self.assertEqual(f.read(), "Hello.\n")
        self.assertEqual([e["kind"] for e in self.events()],
                         ["input", "session", "client_session", "progress", "outcome", "result", "end"])
        with open(self.log) as f:
            call = json.load(f)
        self.assertTrue(call["stdin_eof"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.work))
        self.assertEqual(call["argv"][0:2], ["exec", "--json"])
        self.assertEqual(call["argv"][call["argv"].index("-s") + 1], "workspace-write")
        self.assertIn("--skip-git-repo-check", call["argv"])
        self.assertIn('approvals_reviewer="auto_review"', call["argv"])
        self.assertIn('approval_policy="on-request"', call["argv"])
        self.assertIn("# Input\n\nHello.", call["prompt"])
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"], [CLIENT_ID])

    def test_two_turns_resume_the_same_native_session_and_keep_previous_progress(self):
        first = self.drive([{"kind": "progress", "name": "start", "text": "first turn"},
                            {"kind": "outcome", "outcome": DONE}])
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.drive([{"kind": "progress", "name": "review", "text": "second turn"},
                             {"kind": "outcome", "outcome": DONE}], "--resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        with open(self.log) as f:
            call = json.load(f)
        self.assertEqual(call["resume_id"], CLIENT_ID)
        self.assertLess(call["argv"].index("-s"), call["argv"].index("resume"))
        self.assertEqual([e["text"] for e in self.events() if e["kind"] == "progress"],
                         ["first turn", "second turn"])
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"],
                         [CLIENT_ID, CLIENT_ID])

    def test_interrupted_first_turn_resumes_same_run_and_keeps_progress(self):
        first = self.drive([{"kind": "progress", "name": "start", "text": "before interruption"}])
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertIn("the agent run returned no outcome", first.stderr)
        self.assertFalse(any(e["kind"] == "outcome" for e in self.events()))
        self.assertFalse(os.path.exists(self.out))
        second = self.drive([{"kind": "progress", "name": "review", "text": "recovered"},
                             {"kind": "outcome", "outcome": DONE}], "--resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        with open(self.log) as f:
            self.assertEqual(json.load(f)["resume_id"], CLIENT_ID)
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"],
                         [CLIENT_ID, CLIENT_ID])
        self.assertEqual([e["text"] for e in self.events() if e["kind"] == "progress"],
                         ["before interruption", "recovered"])
        outcomes = [e for e in self.events() if e["kind"] == "outcome"]
        self.assertEqual(len(outcomes), 1)
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])

    def test_missing_native_file_fails_before_fake_spawn(self):
        first = self.drive([{"kind": "outcome", "outcome": DONE}])
        self.assertEqual(first.returncode, 0, first.stderr)
        os.unlink(self.log)
        codex_home = os.path.join(self.home, ".codex", "sessions")
        for root, _, files in os.walk(codex_home):
            for name in files:
                os.unlink(os.path.join(root, name))
        second = self.drive([], "--resume")
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("missing Codex session file", second.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_missing_mapping_fails_before_fake_spawn(self):
        res = self.drive([], "--resume")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("missing Codex recovery state", res.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_different_native_id_on_resume_is_rejected(self):
        first = self.drive([{"kind": "outcome", "outcome": DONE}])
        self.assertEqual(first.returncode, 0, first.stderr)
        changed = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        second = self.drive([{"kind": "outcome", "outcome": DONE}], "--resume", thread_id=changed)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already bound to a different client ID", second.stderr)
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"], [CLIENT_ID])

    def test_missing_outcome_is_not_inferred_from_final_reply(self):
        reply = json.dumps(DONE)
        res = self.drive([{"kind": "progress", "name": "start", "text": "working"}, reply])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(reply, res.stderr)
        self.assertIn("the agent run returned no outcome", res.stderr)
        self.assertEqual(self.events()[-2]["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))

    def test_invalid_report_fails_validation(self):
        invalid = {k: v for k, v in DONE.items() if k != "deliverable"}
        res = self.drive([{"kind": "progress", "name": "start", "text": "working"},
                          {"kind": "outcome", "outcome": invalid}])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("invalid outcome: done with a local destination must carry the deliverable", res.stderr)
        self.assertIn("invalid outcome", self.events()[-2]["error"])
        self.assertFalse(os.path.exists(self.out))

    def test_stderr_is_readable_and_recorded(self):
        res = self.drive([{"kind": "stderr", "text": "Codex diagnostic"},
                          {"kind": "progress", "name": "start", "text": "working"},
                          {"kind": "outcome", "outcome": DONE}])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Codex diagnostic", res.stderr)
        self.assertIn("Codex diagnostic", [e["text"] for e in self.events() if e["kind"] == "stderr"])

    def test_tui_rejected_before_fake_codex_starts(self):
        res = self.drive([], "--runner", "tui")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("CodexClient has no tui command", res.stderr)
        self.assertFalse(os.path.exists(self.log))


class Fake(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home, self.codex_home, self.proj, self.bin = (os.path.join(self.root, d)
                                                           for d in ("home", "codex-home", "proj", "bin"))
        for d in (self.home, self.codex_home, self.proj, self.bin):
            os.mkdir(d)
        self.scenario, self.log, self.hooklog = (os.path.join(self.root, f)
                                                 for f in ("scenario.json", "log.jsonl", "hooks.jsonl"))
        self.record = f"cat >> {shlex.quote(self.hooklog)}; echo >> {shlex.quote(self.hooklog)}"
        self.env = {"PATH": fake_codex.install(self.bin), "HOME": self.home, "PYTHONUTF8": "1",
                    "CODEX_HOME": self.codex_home, fake_codex.ENV: self.scenario}
        self.plain = {**self.env, "NO_COLOR": "1"}
        self.scene()

    def scene(self, **scenario) -> None:
        with open(self.scenario, "w") as f:
            json.dump({"log": self.log, **scenario}, f)

    def hook_args(self, *events: str) -> list[str]:
        args = []
        for event in events:
            args += ["-c", f'hooks.{event}=[{{hooks=[{{type="command",command={json.dumps(self.record)},timeout=10}}]}}]']
        return args

    @staticmethod
    def state(hashes: dict) -> list[str]:
        body = ",".join(f"{json.dumps(k)}={{trusted_hash={json.dumps(h)}}}" for k, h in hashes.items())
        return ["-c", f"hooks.state={{{body}}}"]

    def rpc(self, *args: str, messages=None) -> tuple[subprocess.CompletedProcess, list[dict]]:
        if messages is None:
            messages = [*INIT, {"id": 2, "method": "hooks/list", "params": {"cwds": [self.proj]}}]
        res = subprocess.run([sys.executable, FAKE, "app-server", *args], cwd=self.proj, env=self.env,
                             input="".join(json.dumps(m) + "\n" for m in messages),
                             capture_output=True, text=True, timeout=30)
        return res, [json.loads(line) for line in res.stdout.splitlines()]

    def answer(self, lines: list[dict], ident: int) -> dict:
        (line,) = [line for line in lines if line.get("id") == ident]
        return line

    def listed(self, *args: str) -> list[dict]:
        res, lines = self.rpc(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        (entry,) = self.answer(lines, 2)["result"]["data"]
        return entry["hooks"]

    def trusted(self, *events: str) -> list[str]:
        args = self.hook_args(*events)
        return [*args, *self.state({h["key"]: h["currentHash"] for h in self.listed(*args)})]

    def run_tui(self, *args: str, stdin: str = "", env=None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, FAKE, *args], cwd=self.proj, env=env or self.plain, input=stdin,
                              capture_output=True, text=True, timeout=60)

    def popen(self, *args: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, FAKE, *args], cwd=self.proj, env=self.plain, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    def lines(self, path: str) -> list[dict]:
        try:
            with open(path) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def fired(self) -> list[dict]:
        return self.lines(self.hooklog)

    def started(self) -> int:
        return sum(line.get("argv", ["app-server"])[:1] != ["app-server"] for line in self.lines(self.log))


class Interactive(Fake):
    def test_unknown_option_or_resume_without_an_id_exits_2(self):
        for args in (["--bogus"], ["resume"]):
            self.assertEqual(self.run_tui(*args).returncode, 2, args)

    def test_turns_from_the_prompt_then_stdin_lines(self):
        self.scene(steps=["First."], turns=[["Second."]], exit=3)
        res = self.run_tui(*self.trusted("UserPromptSubmit"), "--", "-hello", stdin="line1\nline2\n")
        self.assertEqual((res.returncode, res.stdout), (3, "First.\nSecond.\n"), res.stderr)
        self.assertEqual([p["prompt"] for p in self.fired()], ["-hello", "line1", "line2"])

    def test_a_fresh_run_gets_a_new_uuid_and_its_rollout(self):
        args = self.trusted("SessionStart")
        for _ in range(2):
            res = self.run_tui(*args, "--", "hi")
            self.assertEqual(res.returncode, 0, res.stderr)
        first, second = self.fired()
        self.assertNotEqual(first["session_id"], second["session_id"])
        for p in (first, second):
            self.assertEqual(str(uuid.UUID(p["session_id"])), p["session_id"])
            self.assertEqual(p["source"], "startup")
            self.assertTrue(p["transcript_path"].startswith(self.codex_home + os.sep))
            self.assertTrue(os.path.isfile(p["transcript_path"]))

    def test_session_start_waits_for_the_first_turn_then_payloads_follow(self):
        self.scene(steps=["Done it."])
        cd = os.path.join(self.root, "link")
        os.symlink(self.proj, cd)
        proc = self.popen("--no-daemon", "-C", cd, *self.trusted(*EVENTS))
        wait(self.started)
        time.sleep(0.5)
        self.assertIsNone(proc.poll())
        self.assertEqual(self.fired(), [])
        out, err = proc.communicate("go\n", timeout=30)
        self.assertEqual((proc.returncode, out), (0, "Done it.\n"), err)
        start, submit, stop = self.fired()
        self.assertEqual([p["hook_event_name"] for p in (start, submit, stop)],
                         ["SessionStart", "UserPromptSubmit", "Stop"])
        for p in (start, submit, stop):
            self.assertLessEqual(COMMON, set(p))
            self.assertEqual((p["cwd"], p["session_id"]), (cd, start["session_id"]))
        self.assertEqual(start["source"], "startup")
        self.assertNotIn("turn_id", start)
        self.assertEqual(submit["prompt"], "go")
        self.assertEqual(stop["turn_id"], submit["turn_id"])
        self.assertEqual(stop["last_assistant_message"], "Done it.")
        self.assertIs(stop["stop_hook_active"], False)

    def test_a_turn_without_text_stops_with_a_null_message(self):
        res = self.run_tui(*self.trusted("Stop"), "--", "hi")
        self.assertEqual(res.returncode, 0, res.stderr)
        (stop,) = self.fired()
        self.assertIsNone(stop["last_assistant_message"])

    def test_resume_keeps_the_session_and_reports_source_resume(self):
        args = self.trusted("SessionStart")
        self.assertEqual(self.run_tui(*args, "--", "first").returncode, 0)
        (first,) = self.fired()
        res = self.run_tui("resume", first["session_id"], "-C", self.proj, "--no-daemon", *args, stdin="again\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        second = self.fired()[1]
        self.assertEqual((second["session_id"], second["source"], second["transcript_path"]),
                         (first["session_id"], "resume", first["transcript_path"]))

    def test_resume_of_an_unknown_session_exits_1(self):
        res = self.run_tui("resume", str(uuid.uuid4()), "--", "hi")
        self.assertEqual(res.returncode, 1)
        self.assertIn("no session", res.stderr)

    def test_untrusted_or_modified_hooks_show_the_review_and_wait(self):
        args = self.hook_args("UserPromptSubmit", "Stop")
        submit, stop = self.listed(*args)
        for state, count in (([], "2 hooks are new or changed."),
                             (self.state({submit["key"]: submit["currentHash"], stop["key"]: "sha256:" + "0" * 64}),
                              "1 hook is new or changed.")):
            self.scene(exit=4)
            runs = self.started()
            proc = self.popen(*args, *state, "--", "hi")
            wait(lambda: self.started() > runs)
            time.sleep(0.5)
            self.assertIsNone(proc.poll())
            out, err = proc.communicate("a line\n", timeout=30)
            self.assertEqual(proc.returncode, 4, err)
            self.assertIn("Hooks need review", out)
            self.assertIn(count, out)
            self.assertEqual(self.fired(), [])

    def test_hooks_off_run_none_and_need_no_review(self):
        self.scene(steps=["Hi."])
        for off in (["--disable", "hooks"], ["-c", "features.hooks=false"]):
            res = self.run_tui(*self.hook_args("Stop"), "--enable", "other", *off, "--", "hi")
            self.assertEqual((res.returncode, res.stdout), (0, "Hi.\n"), res.stderr)
            self.assertEqual(self.fired(), [])

    def test_permission_and_sub_agent_steps(self):
        agent = str(uuid.uuid4())
        self.scene(steps=[{"kind": "permission"},
                          {"kind": "permission", "tool_name": "apply_patch", "tool_input": {"patch": "x"},
                           "agent_id": agent, "agent_type": "explorer"}, "Done."])
        res = self.run_tui(*self.trusted("UserPromptSubmit", "PermissionRequest", "Stop"), "--", "hi")
        self.assertEqual(res.returncode, 0, res.stderr)
        submit, root, child, stop = self.fired()
        self.assertEqual([p["hook_event_name"] for p in (root, child)], ["PermissionRequest"] * 2)
        for p in (root, child):
            self.assertLessEqual(COMMON, set(p))
            self.assertEqual((p["turn_id"], p["session_id"]), (submit["turn_id"], submit["session_id"]))
        self.assertEqual(root["tool_name"], "Bash")
        self.assertIsInstance(root["tool_input"], dict)
        self.assertNotIn("agent_id", root)
        self.assertEqual((child["tool_name"], child["tool_input"], child["agent_id"], child["agent_type"]),
                         ("apply_patch", {"patch": "x"}, agent, "explorer"))
        self.assertEqual(stop["last_assistant_message"], "Done.")

    def test_interrupt_ends_the_turn_without_stop(self):
        self.scene(steps=["Partial.", {"kind": "interrupt"}, "Never."], turns=[["Next."]])
        res = self.run_tui(*self.trusted("UserPromptSubmit", "Stop", "Interrupt"), "--", "hi", stdin="more\n")
        self.assertEqual((res.returncode, res.stdout), (0, "Partial.\nNext.\n"), res.stderr)
        first, interrupt, second, stop = self.fired()
        self.assertEqual([p["hook_event_name"] for p in (first, interrupt, second, stop)],
                         ["UserPromptSubmit", "Interrupt", "UserPromptSubmit", "Stop"])
        self.assertLessEqual(COMMON, set(interrupt))
        self.assertEqual(interrupt["turn_id"], first["turn_id"])
        self.assertEqual((stop["turn_id"], stop["last_assistant_message"]), (second["turn_id"], "Next."))

    def test_a_users_own_hook_runs_when_trusted_and_needs_review_when_not(self):
        self.scene(user_hook={"event": "Stop", "command": self.record, "trustStatus": "trusted"})
        res = self.run_tui("--", "hi")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([p["hook_event_name"] for p in self.fired()], ["Stop"])
        self.scene(user_hook={"trustStatus": "modified"})
        res = self.run_tui("--", "hi")
        self.assertIn("Hooks need review", res.stdout)

    def test_logs_argv_and_selected_env(self):
        env = {**self.env, "NO_COLOR": "", "CODEX_THREAD_ID": "t", "UNRELATED": "u"}
        argv = ["--no-daemon", "-C", self.proj, "-c", 'tui.notification_method="bel"', "-m", "m", "--", "hi"]
        self.assertEqual(self.run_tui(*argv, env=env).returncode, 0)
        (line,) = self.lines(self.log)
        self.assertIsInstance(line["pid"], int)
        self.assertEqual((line["argv"], line["cwd"]), (argv, self.proj))
        self.assertEqual({k: line["env"].get(k) for k in ("NO_COLOR", "CODEX_THREAD_ID", "CODEX_HOME", "UNRELATED")},
                         {"NO_COLOR": "", "CODEX_THREAD_ID": "t", "CODEX_HOME": self.codex_home, "UNRELATED": None})
        self.assertNotIn("PAGER", line["env"])

    def test_colored_unless_no_color_set_and_not_0(self):
        self.scene(steps=["Hi."])
        for value, colored in ((None, True), ("", False), ("1", False), ("0", True)):
            env = dict(self.env) if value is None else {**self.env, "NO_COLOR": value}
            res = self.run_tui("--", "hi", env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Hi.", res.stdout)
            self.assertEqual("\x1b[" in res.stdout, colored, value)

    def test_hooks_run_in_the_session_cwd_and_their_results_are_logged(self):
        self.scene(steps=["Hi."])
        pwd = os.path.join(self.root, "pwd")
        args = ["-c", f'hooks.Stop=[{{hooks=[{{type="command",command={json.dumps(f"pwd > {pwd}; echo out")}}}]}}]']
        (stop,) = self.listed(*args)
        res = self.run_tui("-C", self.root, *args, *self.state({stop["key"]: stop["currentHash"]}), "--", "hi")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(pwd) as f:
            self.assertEqual(os.path.realpath(f.read().strip()), self.root)
        (ran,) = [line for line in self.lines(self.log) if "hook" in line]
        self.assertEqual((ran["hook"], ran["key"], ran["exit"], ran["stdout"]), ("Stop", stop["key"], 0, "out\n"))
        self.assertEqual(ran["payload"]["hook_event_name"], "Stop")


class AppServer(Fake):
    def test_hooks_list_after_initialize_with_notifications_interleaved(self):
        res, lines = self.rpc(*self.hook_args(*EVENTS))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(any("id" not in line and "method" in line for line in lines))
        self.assertIn("result", self.answer(lines, 1))
        (entry,) = self.answer(lines, 2)["result"]["data"]
        self.assertEqual((entry["cwd"], entry["errors"]), (self.proj, []))
        hooks = entry["hooks"]
        self.assertEqual([h["eventName"] for h in hooks],
                         ["sessionStart", "userPromptSubmit", "permissionRequest", "stop", "interrupt"])
        snakes = ["session_start", "user_prompt_submit", "permission_request", "stop", "interrupt"]
        for h, snake in zip(hooks, snakes):
            self.assertEqual(h["key"], f"/<session-flags>/config.toml:{snake}:0:0")
            self.assertEqual((h["source"], h["handlerType"], h["command"], h["enabled"], h["isManaged"],
                              h["trustStatus"]), ("sessionFlags", "command", self.record, True, False, "untrusted"))
            self.assertRegex(h["currentHash"], HASH)
        self.assertEqual([h["timeoutSec"] for h in hooks], [10, 10, 10, 10, 3])

    def test_state_trusts_the_listed_hash_only(self):
        args = self.hook_args("SessionStart", "Stop")
        start, stop = self.listed(*args)
        self.assertNotEqual(start["currentHash"], stop["currentHash"])
        trusted = self.state({start["key"]: start["currentHash"], stop["key"]: "sha256:" + "0" * 64})
        self.assertEqual([h["trustStatus"] for h in self.listed(*args, *trusted)], ["trusted", "modified"])
        disabled = ["-c", f"hooks.state={{{json.dumps(stop['key'])}={{enabled=false}}}}"]
        self.assertEqual([h["enabled"] for h in self.listed(*args, *disabled)], [True, False])

    def test_hooks_off_list_nothing(self):
        args = self.hook_args("Stop")
        for off in (["-c", "features.hooks=false"], ["--disable", "hooks"]):
            self.assertEqual(self.listed(*args, *off), [])
        self.assertEqual(len(self.listed(*args, "--disable", "hooks", "--enable", "hooks")), 1)

    def test_errors_for_each_cwd(self):
        errors = [{"path": "/x/hooks.json", "message": "bad"}]
        self.scene(errors=errors)
        cwds = [self.proj, self.root]
        res, lines = self.rpc(messages=[*INIT, {"id": 2, "method": "hooks/list", "params": {"cwds": cwds}}])
        self.assertEqual(res.returncode, 0, res.stderr)
        data = self.answer(lines, 2)["result"]["data"]
        self.assertEqual([(d["cwd"], d["errors"]) for d in data], [(c, errors) for c in cwds])

    def test_a_users_own_hook_with_its_trust_status(self):
        for status in ("untrusted", "modified", "trusted"):
            self.scene(user_hook={"trustStatus": status})
            ours, theirs = self.listed(*self.hook_args("Stop"))
            self.assertEqual(ours["source"], "sessionFlags")
            self.assertEqual((theirs["source"], theirs["trustStatus"], theirs["enabled"], theirs["isManaged"]),
                             ("user", status, True, False))
            self.assertTrue(theirs["key"].startswith(self.codex_home + os.sep))
            self.assertRegex(theirs["currentHash"], HASH)

    def test_only_config_and_feature_options(self):
        for args in (["-p", "x"], ["--profile", "x"], ["-C", self.proj], ["--bogus"]):
            res, _ = self.rpc(*args, messages=[])
            self.assertEqual(res.returncode, 2, args)

    def test_requests_before_initialize_and_unknown_methods_fail(self):
        res, lines = self.rpc(messages=[{"id": 7, "method": "hooks/list", "params": {"cwds": [self.proj]}},
                                        *INIT, {"id": 8, "method": "thread/start", "params": {}}])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("error", self.answer(lines, 7))
        self.assertIn("result", self.answer(lines, 1))
        self.assertIn("error", self.answer(lines, 8))

    def test_logs_argv_and_each_message(self):
        res, _ = self.rpc("--enable", "hooks")
        self.assertEqual(res.returncode, 0, res.stderr)
        start, *received = self.lines(self.log)
        self.assertEqual(start["argv"], ["app-server", "--enable", "hooks"])
        self.assertEqual([r["received"]["method"] for r in received], ["initialize", "initialized", "hooks/list"])


if __name__ == "__main__":
    unittest.main()
