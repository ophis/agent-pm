"""delegate/engine/container.py: the input's Reference `Repo:` as a github.com owner/name, a local
clone's from its origin read with GUARD; AGENT_PM_USERS; the Docker Host test.
"""

import json
import os
import subprocess

from agent_pm.delegate import ConfigError
from agent_pm.delegate.engine import container
from agent_pm.utils import GUARD

from delegate.tests.plans import Base


def origin(url, code=0):
    calls = []

    def run(argv, _timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, url, "fatal: no remote")

    return run, calls


def refuse(argv, _timeout):
    raise AssertionError(f"ran {argv}")


class ReferenceRepo(Base):
    def test_only_the_leading_reference_blocks_repo_line_counts(self):
        for text, want in (
            ("Reference: TASK-1\nRepo: ophis/agent-pm\nCheckout: TASK-1\n\nBody", "ophis/agent-pm"),
            ("\n\nReference: TASK-1\nRepo: github.com/Ophis/x.y\n", "Ophis/x.y"),
            ("Reference: TASK-1\nRepo: https://github.com/ophis/agent-pm.git\n", "ophis/agent-pm"),
            ("Reference: TASK-1\nTitle: t\n\nRepo: ophis/other\n", None),
            ("Reference: TASK-1\nRepo: ophis/a\n\n## Brief\nRepo: ophis/other\n", "ophis/a"),
            ("Brief\nRepo: ophis/agent-pm\n", None),
            ("Repo: ophis/agent-pm\n", None),
            ("Reference: TASK-1\nRepo: ghe.example.com/ophis/agent-pm\n", None),
            ("Reference: TASK-1\nRepo: not a repo\n", None),
            ("Reference: TASK-1\nRepo: ophis/a\nRepo: ophis/a\n", "ophis/a"),
            ("", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(container.reference_repo(text, run=refuse), want)

    def test_two_different_repo_lines_are_refused(self):
        with self.assertRaises(ConfigError) as cm:
            container.reference_repo("Reference: T\nRepo: ophis/a\nRepo: ophis/b\n", run=refuse)
        self.assertIn("Repo:", str(cm.exception))

    def test_a_local_clone_is_named_by_its_origin_read_with_guard(self):
        clone = os.path.join(self.tmp, "clone")
        os.mkdir(clone)
        for url, want in (
            ("git@github.com:ophis/agent-pm.git\n", "ophis/agent-pm"),
            ("https://github.com/ophis/agent-pm\n", "ophis/agent-pm"),
            ("git@ghe.example.com:ophis/agent-pm.git\n", None),
            ("/srv/plain.git\n", None),
        ):
            with self.subTest(url=url):
                run, calls = origin(url)
                got = container.reference_repo(f"Reference: T\nRepo: {clone}\n", run=run)
                self.assertEqual(got, want)
                self.assertEqual(
                    calls, [["git", *GUARD, "-C", clone, "remote", "get-url", "origin"]]
                )

    def test_a_local_path_without_an_origin_is_none(self):
        run, _ = origin("", code=2)
        self.assertIsNone(container.reference_repo("Reference: T\nRepo: ~/gone\n", run=run))


class UsersEnv(Base):
    def test_the_users_without_bots_as_a_json_array(self):
        got = container.users_env(["octocat", "agent-pm-bot[bot]", 'q"uote'])
        self.assertEqual(got, {"AGENT_PM_USERS": '["octocat", "q\\"uote"]'})
        self.assertEqual(json.loads(got["AGENT_PM_USERS"]), ["octocat", 'q"uote'])
        for users in ("octocat", [1]):
            with self.subTest(users=users), self.assertRaises(ConfigError):
                container.users_env(users)

    def test_a_run_whose_reference_names_two_repos_is_refused_before_any_write(self):
        why = self.refused(input="Reference: T\nRepo: ophis/a\nRepo: ophis/b\n\nResearch X.")
        self.assertIn("Repo:", why)
        self.assertFalse(os.path.exists(self.work))


class OnDocker(Base):
    def test_the_agents_host_is_docker_by_its_implementation(self):
        hosts = {
            "default": "tmux",
            "tmux": {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"},
            "box": {"implementation": "agent_pm.session.hosts.docker.host:DockerHost"},
        }
        for host, want in ((None, False), ("tmux", False), ("box", True), ("absent", False)):
            with self.subTest(host=host):
                self.assertIs(container.on_docker({"hosts": hosts}, host), want)
        self.assertTrue(container.on_docker({"hosts": hosts | {"default": "box"}}, None))
        with self.assertRaises(ConfigError):
            container.on_docker({"hosts": {"default": "x"}}, None)
