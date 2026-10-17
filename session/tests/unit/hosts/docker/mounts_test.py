"""hosts/docker/mounts.py: the cwd rule Session checks before any broker call and the crew volume
name; daemon.py: the account home, endpoint and docker command line.
"""

import os
from unittest import mock

from agent_pm.session.engine.types import ConfigError
from agent_pm.session.hosts.docker import daemon, mounts

from session.tests.case import Case

HOME = "/Users/example"


class CwdTest(Case):
    def test_a_cwd_outside_the_agent_pm_dir_passes_the_broker_judges_the_rest(self):
        for cwd in (f"{HOME}/src/p", HOME, "/tmp/p", f"{HOME}/.agent-pmx", f"{HOME}/.ssh"):
            with self.subTest(cwd=cwd):
                mounts.check_cwd(cwd, HOME)

    def test_role_run_cwd_names_unsupported_role_runs(self):
        for cwd in (f"{HOME}/.agent-pm", f"{HOME}/.agent-pm/adhoc/x/agent"):
            with (
                self.subTest(cwd=cwd),
                self.assertRaisesRegex(
                    ConfigError,
                    f"^Docker cwd {cwd}: role runs are not supported on the Docker Host yet$",
                ),
            ):
                mounts.check_cwd(cwd, HOME)

    def test_crew_volume(self):
        self.assertEqual(mounts.crew_volume("worker_1"), "agent-pm-crew-worker_1")


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
