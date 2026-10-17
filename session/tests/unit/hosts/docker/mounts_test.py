"""hosts/docker/mounts.py: a role run told from a crew worker and the workspace volume names;
daemon.py: the account home, endpoint and docker command line.
"""

import json
import os
import re
from unittest import mock

from agent_pm.session.engine.types import ConfigError
from agent_pm.session.hosts.docker import daemon, mounts

from session.tests.case import Case

HOME = "/Users/example"


class RoleRunTest(Case):
    INTENT = {"role": "pm", "delegate_config": "[delegate]\nusers = []\n"}

    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.agent_pm)

    def run_dir(self, workdir, text=None, *, agent="agent"):
        """`workdir`'s agent dir, its run/container-intent.json holding `text` (INTENT)."""
        for sub in (agent, "run"):
            os.makedirs(os.path.join(workdir, sub), exist_ok=True)
        with open(os.path.join(workdir, mounts.INTENT), "w") as f:
            f.write(json.dumps(self.INTENT) if text is None else text)
        return os.path.join(workdir, agent)

    def test_an_agent_dir_under_a_root_beside_its_intent_is_a_role_run(self):
        for root in (self.root, os.path.realpath(os.path.join(self.home, "work"))):
            with self.subTest(root=root):
                cwd = self.run_dir(os.path.join(root, "work", "TASK-1"))
                self.assertEqual(mounts.role_run(cwd, [self.root, root]), self.INTENT)

    def test_anything_else_is_a_crew_worker(self):
        elsewhere = os.path.realpath(os.path.join(self.home, "src", "x"))
        linked = os.path.join(self.root, "adhoc", "linked")
        os.makedirs(os.path.join(linked, "agent"))
        os.symlink(os.path.join(elsewhere, "run"), os.path.join(linked, "run"))
        file_link = os.path.join(self.root, "adhoc", "file")
        os.makedirs(os.path.join(file_link, "run"))
        os.makedirs(os.path.join(file_link, "agent"))
        os.symlink(os.path.join(elsewhere, mounts.INTENT), os.path.join(file_link, mounts.INTENT))
        for cwd in (
            self.run_dir(elsewhere),
            self.run_dir(os.path.join(self.root, "adhoc", "y"), agent="src"),
            os.path.join(linked, "agent"),
            os.path.join(file_link, "agent"),
            os.path.join(self.root, "adhoc", "none", "agent"),
        ):
            with self.subTest(cwd=cwd):
                self.assertIsNone(mounts.role_run(cwd, [self.root]))

    def test_an_intent_that_is_not_one_is_refused(self):
        for text in ("{", "[]", '{"delegate_config": 1}', " " * (mounts.INTENT_MAX + 1)):
            with self.subTest(text=text[:20]):
                cwd = self.run_dir(os.path.join(self.root, "adhoc", "x"), text)
                with self.assertRaisesRegex(ConfigError, "is not a run intent"):
                    mounts.role_run(cwd, [self.root])

    def test_volumes(self):
        self.assertEqual(mounts.crew_volume("worker_1"), "agent-pm-crew-worker_1")
        for cwd, label in (
            ("/w/TASK-1/agent", "TASK-1"),
            ("/w/a b!/agent", "a_b_"),
            ("/agent", "run"),
        ):
            with self.subTest(cwd=cwd):
                name = mounts.run_volume(cwd)
                self.assertRegex(name, rf"^agent-pm-run-{re.escape(label)}-[0-9a-f]{{8}}$")
                self.assertTrue(mounts.RUN_VOLUME.fullmatch(name))
        self.assertNotEqual(mounts.run_volume("/w/T/agent"), mounts.run_volume("/v/T/agent"))


class PathsTest(Case):
    def test_account_home_state_dir_endpoint_and_docker_call(self):
        with mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir="/Users/acct")):
            self.assertEqual(daemon.account_home(), "/Users/acct")
            self.assertEqual(daemon.endpoint(), "unix:///Users/acct/.docker/run/docker.sock")
            env = {"DOCKER_HOST": "tcp://x", "DOCKER_CONTEXT": "c", "DOCKER_CONFIG": "/c", "K": "v"}
            with mock.patch.dict(os.environ, env):
                argv, called = daemon.docker_call("/bin/docker", "ps", "-a")
        self.assertEqual(
            argv,
            ["/bin/docker", "--host", "unix:///Users/acct/.docker/run/docker.sock", "ps", "-a"],
        )
        self.assertEqual(called["K"], "v")
        self.assertFalse({"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"} & set(called))
        self.assertEqual(mounts.state_dir("w"), os.path.join(self.agent_pm, "docker-state", "w"))
