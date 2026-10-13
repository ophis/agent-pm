import os
import re
import sys

from agent_pm.delegate import PYTHONPATH
from agent_pm.linear_orchestrator.engine import config, inputs, target
from agent_pm.linear_orchestrator.engine.types import SERVICE_ERRORS, Invalid, Transient
from agent_pm.linear_orchestrator.linear import issues, sessions, writeback
from agent_pm.linear_orchestrator.linear.linear import ISSUE_ID, log, one_line
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError

RUN = ("-P", "-m", "agent_pm.linear_orchestrator.engine.run")
SHARED = ("issue", "project", "assignee", "sid", "task", "mode", "runtime")


def fail(ident, kind, reason, rc):
    log("router", kind, ident, reason=reason)
    return rc


class Launch:
    def __init__(self, args, cfg, gql, run, keychain):
        self.args, self.cfg = args, cfg
        self.gql, self.run, self.keychain = gql, run, keychain

    def validate(self):
        a, cfg = self.args, self.cfg
        if not re.fullmatch(ISSUE_ID, a.issue) or not config.UUID_RE.fullmatch(a.sid):
            print(f"router.py: bad issue or session id: {a.issue} {a.sid}", file=sys.stderr)
            return 2
        role = config.role_for(cfg.roles, a.assignee)
        if role is None:
            return fail(a.issue, "config-error", f"{a.assignee!r} is not a role account", 2)
        tasks, key = (cfg.roles[role].tasks, cfg.roles[role].key)
        if a.task is not None and a.task not in tasks:
            why = f"task {a.task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
            return fail(a.issue, "config-error", why, 2)
        if not self.keychain(key):
            return fail(a.issue, "config-error", f"no Keychain item for role key {key}", 2)
        if a.mode == "resume":
            try:
                st = sessions.status(cfg.run_dir(a.issue), a.runtime, a.sid, driver=cfg.driver)
                why = None if st.resumable == "yes" else st.resumable
            except (SessionConfigError, DriverError, OSError, ValueError, RuntimeError) as e:
                why = one_line(e)
            if why:
                return fail(a.issue, "transient", f"session {a.sid} not resumable ({why})", 3)
        return role

    def execute(self):
        a, cfg = self.args, self.cfg
        os.environ["PATH"] = config.path()
        role = self.validate()
        if isinstance(role, int):
            return role
        self.role = role
        self.build = config.TASKS[role].kind == "build"
        try:
            issue = issues.read_issue(self.gql, a.issue)
        except SERVICE_ERRORS as e:
            return fail(a.issue, "transient", f"Linear: {one_line(e)}", 3)
        ctx = writeback.context(
            cfg,
            self.role,
            self.gql,
            ident=a.issue,
            issue_id=issue.id,
            sid=a.sid,
            resume=a.mode == "resume",
            project=a.project,
            target=None,
        )
        repo, result = self.target(issue, ctx)
        if result is not None:
            return result
        return self.detach(issue, ctx, repo)

    def target(self, issue, ctx):
        a, cfg = self.args, self.cfg
        if self.build:
            try:
                repo = target.check(
                    issue, cfg.project_repos, run=self.run, work=cfg.runs_dir, roles=cfg.roles
                )
            except SERVICE_ERRORS as e:
                return (None, fail(a.issue, "transient", f"repo check: {one_line(e)}", 3))
            if isinstance(repo, Transient) or (isinstance(repo, Invalid) and a.mode == "resume"):
                return (None, fail(a.issue, "transient", repo.reason, 3))
            if isinstance(repo, Invalid):
                try:
                    writeback.bounce(ctx, issue, repo.reason)
                except SERVICE_ERRORS as e:
                    return (None, fail(a.issue, "transient", f"bounce: {one_line(e)}", 3))
                log("router", "bounce", a.issue, reason=repo.reason)
                return (None, 0)
        else:
            repo = target.research_repo(issue, cfg.project_repos)
        if repo is not None:
            repo = target.with_clone(
                repo, a.issue, cfg.local_clones, work=cfg.runs_dir, run=self.run
            )
        return repo, None

    def detach(self, issue, ctx, repo):
        a, cfg = self.args, self.cfg
        rd = cfg.run_dir(a.issue)
        try:
            sources = inputs.gather(issue, self.role, cfg.docs, run=self.run)
        except SERVICE_ERRORS as e:
            return fail(a.issue, "transient", f"docs: {one_line(e)}", 3)
        try:
            os.makedirs(rd, exist_ok=True)
            text = inputs.render(
                issue, self.role, sources, humans=ctx.humans, target=repo, docs=cfg.docs
            ).replace("\x00", "")
            text = text.encode("utf-8", "replace").decode()
        except SERVICE_ERRORS as e:
            log("router", "launch-error", a.issue, error=f"input: {one_line(e)}")
            return 1
        try:
            cfg.driver.detach(
                config.session(self.role, a.issue),
                [
                    sys.executable,
                    *RUN,
                    "--uuid",
                    issue.id,
                    *(
                        ["--target", f"{repo.owner}/{repo.name}"]
                        if self.build and repo is not None
                        else []
                    ),
                    *(f"--{k}={v}" for k in SHARED if (v := getattr(a, k)) is not None),
                    f"--input={text}",
                ],
                cwd=rd,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        dict.fromkeys(
                            os.path.abspath(path)
                            for path in (
                                *PYTHONPATH.split(os.pathsep),
                                *os.environ.get("PYTHONPATH", "").split(os.pathsep),
                            )
                            if path
                        )
                    ),
                },
            )
        except (SessionConfigError, DriverError) as e:
            log("router", "launch-error", a.issue, error=one_line(e))
            return 1
        return 0
