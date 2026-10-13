"""recovery test behavior."""

import os
import shlex
import unittest
from functools import partial

from agent_pm.session.engine.types import (
    Access,
    SessionStatus,
)

from session.tests.unit.adapters.claude.fixtures import SID, Base, ClaudeCommon, tui_launch


class TranscriptTest(Base):
    def test_projects_dir_named_by_the_mangled_cwd(self):
        self.assertEqual(
            ClaudeCommon({}).transcript("/agent-pm-absent/a.b_c d-é", SID),
            os.path.join(self.config_dir, "projects", "-agent-pm-absent-a-b-c-d--", f"{SID}.jsonl"),
        )

    def test_the_cwds_realpath(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.work, link)
        common = ClaudeCommon({})
        self.assertEqual(common.transcript(link, SID), common.transcript(self.work, SID))
        self.assertNotIn("link", common.transcript(link, SID))

    def test_the_config_dir_from_the_config_env_then_the_process_env_then_home(self):
        given = {"env": {"CLAUDE_CONFIG_DIR": "/given"}}
        self.assertEqual(
            ClaudeCommon(given).transcript("/w", SID), f"/given/projects/-w/{SID}.jsonl"
        )
        self.assertEqual(ClaudeCommon(given).config_dir(), "/given")
        self.assertEqual(ClaudeCommon({}).config_dir(), self.config_dir)
        del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertEqual(
            ClaudeCommon({}).transcript("/w", SID),
            os.path.join(self.home, ".claude", "projects", "-w", f"{SID}.jsonl"),
        )

    def test_the_launch_transcript_is_in_its_cwd(self):
        cwd = os.path.join(self.tmp, "cwd")
        common = ClaudeCommon({})
        self.assertEqual(self.launch().transcript, common.transcript(self.work, SID))
        self.assertEqual(
            self.launch(access=Access(cwd=cwd)).transcript, common.transcript(cwd, SID)
        )


class ResumeTest(Base):
    def test_the_command_quoted(self):
        self.assertEqual(
            ClaudeCommon({}).resume("/w d", SID), f"cd '/w d' && claude --resume {SID}"
        )

    def test_each_granted_dir_is_added(self):
        self.assertEqual(
            ClaudeCommon({}).resume("/w", SID, ("/o x", "/p")),
            f"cd /w && claude --resume {SID} --add-dir '/o x' --add-dir /p",
        )

    def test_the_launch_resume_adds_the_granted_dirs_never_the_workdir(self):
        cwd = os.path.join(self.tmp, "cwd")
        q = shlex.quote
        self.assertEqual(self.launch().resume, f"cd {q(self.work)} && claude --resume {SID}")
        for launch in (self.launch, partial(tui_launch, self.work)):
            with self.subTest(launch=launch):
                self.assertEqual(
                    launch(access=Access(cwd=cwd)).resume, f"cd {q(cwd)} && claude --resume {SID}"
                )
                self.assertEqual(
                    launch(access=Access(cwd=cwd, dirs=(self.work, "/d"))).resume,
                    f"cd {q(cwd)} && claude --resume {SID} --add-dir {q(self.work)} --add-dir /d",
                )

    def test_the_launch_resume_assigns_the_session_env_quoted(self):
        env = {"PYTHONPATH": "/a b;c", "S": "s"}
        resume = self.launch(access=Access(env=env)).resume
        self.assertEqual(
            resume,
            f"cd {shlex.quote(self.work)} && PYTHONPATH='/a b;c' S=s claude --resume {SID}",
        )
        self.assertEqual(shlex.split(resume)[3:6], ["PYTHONPATH=/a b;c", "S=s", "claude"])


class SessionStatusTest(Base):
    def setUp(self):
        super().setUp()
        self.common = ClaudeCommon({})
        self.path = self.common.transcript(self.work, SID)
        self.resume = f"cd {shlex.quote(self.work)} && claude --resume {SID}"

    def write(self, path, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        os.utime(path, (mtime, mtime))

    def status(self, sid=SID) -> SessionStatus:
        return self.common.session_status(sid, self.work)

    def test_a_transcript_is_resumable_with_its_mtime(self):
        self.write(self.path, 1000)
        self.assertEqual(self.status(), SessionStatus("yes", 1000.0, self.resume))

    def test_the_newest_file_under_the_session_dir_wins(self):
        self.write(self.path, 1000)
        sub = self.path[: -len(".jsonl")]
        self.write(os.path.join(sub, "subagents", "old.jsonl"), 500)
        self.assertEqual(self.status().last_active, 1000.0)
        self.write(os.path.join(sub, "subagents", "workflows", "r", "journal.jsonl"), 2000)
        self.write(os.path.join(sub, "subagents", "mid.jsonl"), 1500)
        self.assertEqual(self.status(), SessionStatus("yes", 2000.0, self.resume))

    def test_a_missing_transcript_is_not_resumable(self):
        self.assertEqual(self.status(), SessionStatus("no", None, self.resume))
        os.makedirs(os.path.dirname(os.path.dirname(self.path)))
        with open(os.path.dirname(self.path), "w", encoding="utf-8"):
            pass
        self.assertEqual(self.status().resumable, "no")

    @unittest.skipIf(os.geteuid() == 0, "root reads a 000 dir")
    def test_an_unreadable_project_dir_is_unknown(self):
        self.write(self.path, 1000)
        project = os.path.dirname(self.path)
        os.chmod(project, 0)
        self.addCleanup(os.chmod, project, 0o700)
        self.assertEqual(self.status(), SessionStatus("unknown", None, self.resume))

    def test_a_transcript_that_is_no_regular_file_is_unknown(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        self.assertEqual(self.status().resumable, "unknown")
        os.remove(self.path)
        target = os.path.join(self.tmp, "target.jsonl")
        self.write(target, 1000)
        os.symlink(target, self.path)
        self.assertEqual(self.status().resumable, "unknown")

    def test_a_sid_that_is_no_uuid_is_not_resumable(self):
        for sid in ("abc", SID.upper(), "../" + SID):
            with self.subTest(sid=sid):
                self.assertEqual(self.status(sid), SessionStatus("no", None, ""))
