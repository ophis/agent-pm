"""hosts/docker/mounts.py: the runtime mount rules Session checks before any broker call;
daemon.py: the account home, endpoint and docker command line.
"""

import os
from unittest import mock

from agent_pm.session.engine.types import ConfigError
from agent_pm.session.hosts.docker import daemon, mounts

from session.tests.case import Case
from session.tests.unit.hosts.docker.support import cases

LEFT_TO_THE_BROKER = {
    "runtime-deny-Users_example_claude.json",
    "runtime-deny-Users_example_codex.json",
}


def check(context):
    mounts.check_cwd(context["cwd_realpath"], context["mount_roots"], context["home"])


class CwdTest(Case):
    def test_every_runtime_mount_fixture_is_refused(self):
        found = [*cases("runtime-mount-"), *cases("runtime-deny-")]
        self.assertGreater(len(found), 10)
        for case in found:
            with self.subTest(name=case["file"]):
                context = case["context"]
                if case["file"] in LEFT_TO_THE_BROKER:
                    check(context)
                    continue
                with self.assertRaises(ConfigError) as raised:
                    check(context)
                self.assertIn(context["cwd_realpath"], str(raised.exception))

    def test_a_cwd_inside_a_root_and_clear_of_denied_paths_passes(self):
        for cwd in ("/Users/example/src", "/Users/example/src/p", "/Users/example/src/.ssh-notes"):
            with self.subTest(cwd=cwd):
                mounts.check_cwd(cwd, ["/Users/example/src"], "/Users/example")

    def test_each_rule_is_named(self):
        home = "/Users/example"
        for cwd, roots, want in (
            ("/Users/example/src/p", [], "mount_roots is empty"),
            ("/Users/example/src/p", ["/Users"], "the home or one of its ancestors"),
            ("/tmp/p", ["/Users/example/src"], "outside every mount root"),
            ("/Users/example", ["/Users/example/src", "/Users/example"], "the home"),
            ("/Users/example/.ssh/k", ["/Users/example/.ssh"], "~/.ssh"),
            ("/Users/example/Library", ["/Users/example/Library"], "~/Library"),
        ):
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ConfigError, want):
                mounts.check_cwd(cwd, roots, home)

    def test_role_run_cwd_names_unsupported_role_runs(self):
        (case,) = cases("runtime-deny-Users_example_agent-pm_x")
        with self.assertRaisesRegex(
            ConfigError, "role runs are not supported on the Docker Host yet"
        ):
            check(case["context"])


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
