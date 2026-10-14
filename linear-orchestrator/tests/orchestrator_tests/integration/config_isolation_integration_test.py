"""Real Git and interpreter contracts in the test sandbox."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import GUARD, SHORT
from orchestrator_tests.case import ORCH_ROOT, PY, REPO, Case, child_env
from orchestrator_tests.config_fixtures import (
    CORE_TABLES,
    CORE_TOP,
    HTTPS,
    IMPORT_ALONE,
    PIPELINE,
    SID,
    SLUG,
    STEPS,
    ConfigFile,
    write,
)

from utils.tests.sandbox import clean_env, git


class Clones(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.dir = os.path.realpath(self.dir)

    def clone(self, name="clone", origin=HTTPS):
        path = os.path.join(self.dir, name)
        git("init", "-q", path)
        if origin:
            git("-C", path, "remote", "add", "origin", origin)
        return path

    def bad(self):
        dotgit_file = os.path.join(self.dir, "file")
        os.makedirs(dotgit_file)
        write(os.path.join(dotgit_file, ".git"), "gitdir: /elsewhere\n")
        linked = os.path.join(self.dir, "linked")
        os.makedirs(linked)
        os.symlink(os.path.join(self.clone("real"), ".git"), os.path.join(linked, ".git"))
        missing = os.path.join(self.dir, "missing")
        no_origin = self.clone("no-origin", origin="")
        gitlab = self.clone("gitlab", "https://gitlab.com/ophis/agent-pm.git")
        other = self.clone("other", "https://github.com/ophis/other.git")
        return [
            ("missing", missing, f"{missing} does not exist"),
            (".git a file", dotgit_file, f"{dotgit_file} is not a git clone"),
            (".git a symlink", linked, f"{linked} is not a git clone"),
            ("no origin", no_origin, f"{no_origin}: no origin URL naming host/owner/name"),
            (
                "non-github host",
                gitlab,
                "origin is gitlab.com/ophis/agent-pm, not github.com/ophis/agent-pm",
            ),
            (
                "other owner/name",
                other,
                "origin is github.com/ophis/other, not github.com/ophis/agent-pm",
            ),
        ]


class ClonePaths(Clones):
    def test_entries_stored_as_realpaths(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        real = os.path.join(home, "real")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.dir, "link"))
        git("init", "-q", real)
        git("-C", real, "remote", "add", "origin", HTTPS)
        cfg = self.load(
            f'work_dir = "{self.dir}/w"\n'
            + PIPELINE
            + f'[local_clones]\n"{SLUG}" = "{self.dir}/link"\n'
        )
        self.assertEqual(cfg.local_clones, {SLUG: real})


class CloneValidation(Clones):
    def test_matching_origin(self):
        for name, origin, slug in (
            ("https", HTTPS, SLUG),
            ("scp", "git@github.com:ophis/agent-pm.git", SLUG),
            ("bare", "https://github.com/ophis/agent-pm", SLUG),
            ("case", HTTPS, "OPHIS/Agent-PM"),
            ("origin case", "https://GitHub.com/Ophis/AGENT-PM.git", SLUG),
        ):
            with self.subTest(name):
                self.assertIsNone(config.clone_error(self.clone(name, origin), slug))

    def test_reasons(self):
        bad = self.bad() + [
            (repr(path), path, f"{path!r} has a non-printable character")
            for path in ("/x\ny", "/x\x85y", "/x\u2028y")
        ]
        for name, path, reason in bad:
            with self.subTest(name):
                self.assertEqual(config.clone_error(path, SLUG), reason)

    def test_run_failures_are_reasons(self):
        clone = self.clone()
        for error in (
            OSError("boom"),
            subprocess.TimeoutExpired(["git"], 10),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        ):

            def run(_argv, _timeout, *, error=error):
                raise error

            with self.subTest(type(error).__name__):
                self.assertEqual(config.clone_error(clone, SLUG, run=run), f"git: {error}")

    def test_git_runs_guarded(self):
        clone, calls = self.clone(), []

        def run(argv, timeout):
            calls.append((argv, timeout))
            return subprocess.CompletedProcess(argv, 0, HTTPS + "\n", "")

        self.assertIsNone(config.clone_error(clone, SLUG, run=run))
        self.assertEqual(
            calls, [(["git", *GUARD, "-C", clone, "remote", "get-url", "origin"], SHORT)]
        )


class RunnableClones(Clones):
    def runs(self, key, path):
        return self.load(PIPELINE + f'[local_clones]\n"{key}" = {json.dumps(path)}\n').roles

    def test_each_entry_is_checked_and_a_bad_one_names_its_key(self):
        self.assertEqual(list(self.runs(SLUG, self.clone())), ["researcher", "pm", "engineer"])
        for name, path, reason in self.bad():
            with self.subTest(name):
                self.fails(
                    f"linear-orchestrator/config.toml: local_clones.{SLUG}: {reason}",
                    PIPELINE + f'[local_clones]\n"{SLUG}" = {json.dumps(path)}\n',
                )

    def test_no_entries_runs_no_clone_checks(self):
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as git_calls:
            self.assertEqual(list(self.load(PIPELINE).roles), ["researcher", "pm", "engineer"])
        self.assertTrue(git_calls.call_args_list)
        self.assertTrue(
            all("rev-parse" in call.args[0] for call in git_calls.call_args_list),
            git_calls.call_args_list,
        )


class ConfigurationLoading(ConfigFile, Case):
    def test_importing_reads_no_config_file(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), "[[[ not toml")
        code = (
            "import json\nfrom agent_pm.linear_orchestrator.engine import config\n"
            "try:\n    config.load()\nexcept config.ConfigError as e:\n    print(json.dumps([e.msg, e.logs_dir]))"
        )
        res = subprocess.run(
            [*PY, "-c", code],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            env=child_env(None, ORCH_ROOT),
        )
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        msg, logs = json.loads(res.stdout)
        self.assertTrue(msg.startswith("linear-orchestrator/config.toml: "), msg)
        self.assertIsNone(logs)


class TeamDirectoryIsolation(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        team = tempfile.TemporaryDirectory(dir=REPO)
        self.addCleanup(team.cleanup)
        self.team = os.path.realpath(team.name)
        writer = (
            "# Writer\n\nYou write.\n\n## Tasks\n\nPick the task below that fits the input; unsure → `one-pager` "
            "(`<tasks>/one-pager.md`).\n\n- `one-pager` (`<tasks>/one-pager.md`): a brief; always; light.\n"
        )
        for rel, text in (
            ("tasks/one-pager.md", "# One-Pager\n" + STEPS),
            ("roles/writer.md", writer),
        ):
            path = os.path.join(self.team, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write(path, text)
        self.work = os.path.join(os.path.dirname(self.agent_pm), "work")

    def seen(self):
        cfg = self.load(PIPELINE)
        pm = cfg.roles["pm"]
        return pm, delegate.plan(
            pm.run,
            delegate.Spec(
                "x",
                self.work,
                runtime="claude",
                sid=SID,
                out=os.path.join(self.work, "out.md"),
                cwd=self.work,
            ),
            driver=delegate.configure(),
        )

    def test_team_dirs_change_no_orchestrator_run(self):
        before = self.seen()
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            CORE_TOP
            + f"team_dirs = {json.dumps([self.team])}\n"
            + CORE_TABLES
            + "[delegate.roles.writer]\ntier = 3\n",
        )
        listed = subprocess.run(
            [*PY, "-m", "agent_pm.delegate.cli", "run", "--list"],
            env=child_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("writer", listed.stdout)
        after = self.seen()
        self.assertEqual(after, before)
        self.assertNotIn("one-pager", after[1].prompt)
        self.assertFalse(os.path.exists(self.work))


class WorkDirectories(ConfigFile, Case):
    def test_set(self):
        home = os.path.realpath(os.path.dirname(self.agent_pm))
        self.assertEqual(
            self.load('work_dir = "~/d"\n' + PIPELINE).work_dir, os.path.join(home, "d")
        )
        for name in ("d-c", "c"):
            path = os.path.join(home, name)
            git("init", "-q", path)
            git(
                "-C",
                path,
                "remote",
                "add",
                "origin",
                f"https://github.com/ophis/{'x' if name == 'd-c' else 'y'}.git",
            )
        cfg = self.load(
            f'work_dir = "{home}/d"\n'
            + PIPELINE
            + f'[local_clones]\n"ophis/x" = "{home}/d-c"\n"ophis/y" = "{home}/c"\n'
        )
        self.assertEqual(cfg.work_dir, f"{home}/d")


class ExecutablePath(Case):
    def test_importing_reads_no_config_home_or_environment(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "AGENT_PM_PATH": "/before/bin"}
            res = subprocess.run(
                [*PY, "-c", IMPORT_ALONE],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                env=child_env(env, ORCH_ROOT),
            )
            self.assertEqual((res.returncode, res.stderr), (0, ""))
            self.assertEqual(
                json.loads(res.stdout),
                {
                    "read": [],
                    "kept": [],
                    "path": (
                        "/later/bin:/opt/homebrew/bin:/later/.local/bin:/usr/local/bin:/usr/bin:/bin"
                    ),
                },
            )
            self.assertEqual(os.listdir(home), [])


class PackagedDeployments(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        self.dir = os.path.realpath(self.dir)
        self.deployment = Path(self.dir).resolve() / "deployment"
        self.copy_packages(self.deployment)

    def copy_packages(self, deployment, *, editable=None):
        for project in ("linear-orchestrator", "delegate", "session", "utils"):
            package = project.replace("-", "_")
            target = deployment / project / "src" / "agent_pm" / package
            shutil.copytree(
                Path(REPO) / project / "src" / "agent_pm" / package,
                editable if project == "linear-orchestrator" and editable is not None else target,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            if project == "linear-orchestrator" and editable is not None:
                target.parent.mkdir(parents=True)
                target.symlink_to(editable, target_is_directory=True)
            source = Path(REPO) / project / "config.toml"
            if source.exists():
                shutil.copyfile(source, deployment / project / "config.toml")

    def environment(self):
        roots = [
            str(self.deployment / project / "src")
            for project in ("linear-orchestrator", "delegate", "session", "utils")
        ]
        return clean_env(os.environ, *roots)

    def child(self, code, *arguments):
        result = subprocess.run(
            [*PY, "-c", code, *map(str, arguments)],
            cwd=self.dir,
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        return json.loads(result.stdout)

    def discovery(self, work):
        return self.child(
            "import json, sys\n"
            "from agent_pm.linear_orchestrator.engine import config\n"
            "protected = ()\n"
            "try:\n"
            "    protected, protect = config.deployment_dirs()\n"
            "    work = config._work_dir({'work_dir': sys.argv[1]}, protected)\n"
            "except config.ConfigError as error:\n"
            "    print(json.dumps({'error': str(error), 'protected': protected}))\n"
            "else:\n"
            "    print(json.dumps({'work': work, 'protected': protected}))\n",
            work,
        )

    def package(self):
        return self.deployment / "linear-orchestrator" / "src" / "agent_pm" / "linear_orchestrator"

    def test_package_equal_containment_missing_sibling_and_symlink(self):
        package = self.package()
        alias = Path(self.dir) / "package-alias"
        alias.symlink_to(package, target_is_directory=True)
        for work in (package, package / "missing" / "state", package.parent, alias):
            with self.subTest(work=work):
                found = self.discovery(work)
                self.assertIn(str(package), found["protected"])
                self.assertIn("work_dir must neither", found["error"])
        sibling = self.deployment / "unrelated-state" / "missing"
        self.assertEqual(self.discovery(sibling)["work"], str(sibling))
        self.assertFalse(sibling.exists())

    def test_lower_package_dirs_are_rejected_as_work_dir(self):
        for project in ("delegate", "session", "utils"):
            package = self.deployment / project / "src" / "agent_pm" / project
            for work in (package, package / "missing", package.parent):
                with self.subTest(work=work):
                    found = self.discovery(work)
                    self.assertIn(str(package), found["protected"])
                    self.assertIn("work_dir must neither", found["error"])

    def test_lower_package_checkout_is_protected(self):
        checkout = self.deployment / "delegate"
        git("init", "-q", str(checkout))
        found = self.discovery(checkout / "state")
        self.assertIn(str(checkout), found["protected"])
        self.assertIn("work_dir must neither", found["error"])
        self.local(PIPELINE)
        protect = self.child(
            "import json\nfrom agent_pm.linear_orchestrator.engine import config\n"
            "print(json.dumps(config.load().protect))\n"
        )
        self.assertIn(str(checkout), protect)
        self.assertIn(str(self.package()), protect)
        for project in ("delegate", "session", "utils"):
            self.assertNotIn(str(self.deployment / project / "src" / "agent_pm" / project), protect)

    def test_deployment_checkout_and_nested_outer_checkout(self):
        git("init", "-q", str(self.deployment))
        inner = self.deployment / "linear-orchestrator"
        git("init", "-q", str(inner))
        found = self.discovery(inner / "state")
        self.assertIn(str(inner), found["protected"])
        self.assertIn(str(self.deployment), found["protected"])
        self.assertIn("work_dir must neither", found["error"])
        self.assertIn("work_dir must neither", self.discovery(self.deployment / "state")["error"])

    def test_linked_worktree_checkout_is_protected(self):
        main = Path(self.dir) / "main"
        git("init", "-q", str(main))
        git("-C", str(main), "commit", "--allow-empty", "-qm", "fixture")
        linked = Path(self.dir) / "linked"
        git("-C", str(main), "worktree", "add", "-qb", "linked", str(linked))
        self.copy_packages(linked)
        self.deployment = linked
        found = self.discovery(linked / "state")
        self.assertIn(str(linked), found["protected"])
        self.assertIn("work_dir must neither", found["error"])

    def test_symlinked_editable_package_finds_source_checkout(self):
        editable = Path(self.dir) / "editable"
        git("init", "-q", str(editable))
        source = editable / "src" / "agent_pm" / "linear_orchestrator"
        source.parent.mkdir(parents=True)
        self.deployment = Path(self.dir) / "editable-deployment"
        self.copy_packages(self.deployment, editable=source)
        found = self.discovery(editable / "state")
        self.assertIn(str(source), found["protected"])
        self.assertIn(str(editable), found["protected"])
        self.assertIn("work_dir must neither", found["error"])

    def test_discovery_error_rejects_unrelated_work_dir(self):
        (self.deployment / "linear-orchestrator" / ".git").write_text("invalid metadata\n")
        found = self.discovery(Path(self.dir) / "unrelated")
        self.assertIn("deployment checkout", found["error"])

    def test_load_preserves_deployment_protection_without_local_clone(self):
        git("init", "-q", str(self.deployment))
        clone = Path(self.dir) / "clone"
        git("init", "-q", str(clone))
        git("-C", str(clone), "remote", "add", "origin", HTTPS)
        self.local(PIPELINE + f'[local_clones]\n"{SLUG}" = {json.dumps(str(clone))}\n')
        found = self.child(
            "import json\nfrom agent_pm.linear_orchestrator.engine import config\n"
            "cfg = config.load()\n"
            "print(json.dumps({'protect': cfg.protect, 'clones': dict(cfg.local_clones)}))\n"
        )
        self.assertIn(str(self.package()), found["protect"])
        self.assertIn(str(self.deployment), found["protect"])
        self.assertNotIn(str(clone), found["protect"])
        for project in ("delegate", "session", "utils"):
            self.assertNotIn(
                str(self.deployment / project / "src" / "agent_pm" / project), found["protect"]
            )
        self.assertEqual(found["clones"], {SLUG: str(clone)})

    def test_default_gate_runs_outside_checkout_cwd(self):
        self.local(PIPELINE)
        gate = self.child(
            "import json\nfrom agent_pm.linear_orchestrator.engine import config\n"
            "print(json.dumps(config.load().roles['researcher'].run.gate))\n"
        )
        fake_bin = Path(self.dir) / "bin"
        fake_bin.mkdir()
        marker = Path(self.dir) / "usage-probe-ran"
        fake = fake_bin / "claude"
        fake.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).touch()\n"
            'print(\'{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{}}}\')\n'
        )
        fake.chmod(0o700)
        result = subprocess.run(
            shlex.split(gate),
            cwd=self.dir,
            env={**self.environment(), "AGENT_PM_PATH": str(fake_bin)},
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(marker.exists(), result.stdout)
        self.assertTrue(result.stdout.strip())
