# TASK-67: role identity — plan

RESUME: phase=S9 worktree=/Users/francis/playground/agent-pm/work/TASK-67/worktrees/TASK-67-role-identity-per-role-config-and-linear branch=TASK-67-role-identity-per-role-config-and-linear base_ref=dd157f7631f98fd5a3c9070cef9c036d85ab8201 review_round=0 spec_file=docs/specs/2026-09-29-task-67-role-identity-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each agent session acts in Linear as its role's account (per-role config + Keychain service), dispatch stays by project, harness keeps its own key.

**Architecture:** `pipeline.py` validates new role/task/pipeline keys and exposes `Role`, `Run.key`, `harness_service()`, `user_id()`. `launch.py` checks the role's Keychain item (no `-w`) and exports `LINEAR_KEYCHAIN_SERVICE`. `router.py` treats role accounts as agents in the attempt-cap reset.

**Tech Stack:** Python 3.11+ stdlib (`tomllib`, `unittest`, `unittest.mock`), macOS `security`.

**Spec:** `docs/specs/2026-09-29-task-67-role-identity-design.md`

## Global Constraints

- Work only in the worktree `/Users/francis/playground/agent-pm/work/TASK-67/worktrees/TASK-67-role-identity-per-role-config-and-linear` on branch `TASK-67-role-identity-per-role-config-and-linear`; before any write assert `git -C <worktree> branch --show-current` prints that branch. Never touch `main`, never force-push, never merge.
- Follow the repo's `CLAUDE.md`: default to no code comments; terse docstrings; match surrounding idiom (`SystemExit` messages start with the file they blame, e.g. `roles/pm.toml: ...` or `pipeline.toml: ...`).
- Tests: `python3 -m unittest discover -s scripts/tests` — no network, Keychain or Claude. Every `security` call in tests is faked.
- No Linear API key ever appears in env vars, argv, the tmux command, logs or prompts. The launcher never runs `security ... -w`.
- `Reviewer:` stays in the prompt; `[projects]` in `pipeline.toml` keeps `role`, `task`, `prefix`, `next`.
- Commit messages: `TASK-67: <what>` + blank line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- After each task: `git -C /Users/francis/playground/agent-pm/work/TASK-67/worktrees/TASK-67-role-identity-per-role-config-and-linear push -u origin TASK-67-role-identity-per-role-config-and-linear`.

## Review Focus

- A `key` starting with `-` (would be read by `security` as a flag) → rejected by `KEY_RE` (test in Task 1).
- `human_members` listing a role account in different case → rejected (test in Task 1).
- `pipeline.toml` without `harness_key` → clear `pipeline.toml: harness_key ...` error (test in Task 1).
- Resume of a run whose role key vanished from the Keychain → `config-error`, exit 2, not transient (test in Task 3).
- Router run with `--project` → agent set still holds every role's account (test in Task 4).

---

### Task 1: role, task and pipeline config (FR-1, FR-2, FR-14 config)

**Files:**
- Modify: `scripts/pipeline.py` (`TOP_KEYS`, `ROLE_KEYS`, `TASK_KEYS`, new `KEY_RE`, `load_config`, new `Role`, `Run`, `_role`, `_task`, `registry`, `runnable`)
- Modify: `pipeline.toml`, `roles/researcher.toml`, `roles/pm.toml`, `roles/engineer.toml`, `tasks/product-design.toml`, `tasks/engineering.toml`
- Modify tests/fixtures: `scripts/tests/board_ids.py` (HEADER), `scripts/tests/test_pipeline.py`, `scripts/tests/test_launch.py` (REGISTRY only), `scripts/tests/test_router.py` (PD_RUNNABLE only)

**Interfaces:**
- Produces: `pipeline.KEY_RE`; `pipeline.Role(read_only: tuple, memory: str | None, tasks: tuple, account: str, key: str)` (frozen dataclass); `pipeline.registry(root=ROOT) -> ({name: Role}, {name: task dict})`; `pipeline.Run` gains trailing fields `key: str`, `account: str`; `cfg["harness_key"]` always present after `load_config`.

- [ ] **Step 1: Fixtures get the new required keys.**
  - `board_ids.py`: `HEADER = 'team = "%s"\nharness_key = "linear-api-key"\nstates = { %s }\n' % (...)` (keep the inline-table comment true).
  - `test_pipeline.py` `FILES`: `"roles/researcher.toml": RESEARCHER_ID`, `"roles/engineer.toml": 'read_only = ["~/playground/private_docs"]\n' + ENGINEER_ID` with
    `RESEARCHER_ID = 'tasks = ["deep-research"]\naccount = "r@x.com"\nkey = "k-researcher"\n'` and `ENGINEER_ID = 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = "k-engineer"\n'`. Every test that rewrites `roles/engineer.toml` (`test_unknown_keys`, `test_read_only_paths`, `test_repo_read_only`, the `memory()` helper) appends `ENGINEER_ID` to what it writes. `test_repo_read_only`'s second case (researcher's project given role engineer) needs `roles/engineer.toml` with `tasks = ["engineering", "deep-research"]` so it still reaches the `{repo}` check.
  - `test_launch.py` `REGISTRY`: researcher toml `'tasks = ["deep-research"]\naccount = "r@x.com"\nkey = "k-researcher"\n'`; engineer toml `'read_only = ["~/playground/private_docs"]\ntasks = ["engineering"]\naccount = "e@x.com"\nkey = "k-engineer"\n'`.
  - `test_router.py`: `PD_RUNNABLE = 'role = "pm"\ntask = "product-design"\n'` (the real registry: researcher cannot run product-design).
- [ ] **Step 2: Write failing tests in `test_pipeline.py`.**

```python
    def test_runs(self):  # replace the fields assertion and add identity
        ...
        self.assertEqual([f.name for f in dataclasses.fields(pipeline.Run)],
                         ["task_name", "task", "charter", "instructions", "memory", "read_only", "key", "account"])
        self.assertEqual((dr.key, dr.account, eng.key, eng.account), ("k-researcher", "r@x.com", "k-engineer", "e@x.com"))

    def test_role_identity_rejected(self):
        ro = 'read_only = ["~/playground/private_docs"]\n'
        cases = {
            "tasks": ro + 'account = "e@x.com"\nkey = "k-engineer"\n',
            "tasks ": ro + 'tasks = []\naccount = "e@x.com"\nkey = "k-engineer"\n',
            "tasks  ": ro + 'tasks = "engineering"\naccount = "e@x.com"\nkey = "k-engineer"\n',
            "account": ro + 'tasks = ["engineering"]\nkey = "k-engineer"\n',
            "account ": ro + 'tasks = ["engineering"]\naccount = ""\nkey = "k-engineer"\n',
            "key": ro + 'tasks = ["engineering"]\naccount = "e@x.com"\n',
            "key ": ro + 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = "-w"\n',
            "key  ": ro + 'tasks = ["engineering"]\naccount = "e@x.com"\nkey = "a b"\n',
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.write("roles/engineer.toml", text)
                self.rejects(label.strip(), prefix="roles/engineer.toml")

    def test_role_unknown_task(self):
        self.write("roles/engineer.toml", ENGINEER_ID.replace('["engineering"]', '["engineering", "nope"]'))
        self.rejects("nope", prefix="roles/engineer.toml")

    def test_role_key_shared(self):
        self.write("roles/engineer.toml", ENGINEER_ID.replace("k-engineer", "k-researcher"))
        self.rejects("k-researcher", prefix="roles/engineer.toml")

    def test_role_key_is_harness_key(self):
        self.write("roles/engineer.toml", ENGINEER_ID.replace("k-engineer", "linear-api-key"))
        self.rejects("harness_key", prefix="roles/engineer.toml")

    def test_role_account_is_human(self):
        self.rejects("human_members", prefix="roles/researcher.toml", text='human_members = ["R@X.com"]\n' + PROJECTS)

    def test_project_task_outside_role_tasks(self):
        self.write("roles/researcher.toml", RESEARCHER_ID.replace('["deep-research"]', '["engineering"]'))
        self.rejects("deep-research")

    def test_unused_role_validated(self):
        self.write("roles/pm.md", "")
        self.write("roles/pm.toml", 'tasks = ["deep-research"]\naccount = "p@x.com"\n')
        self.rejects("key", prefix="roles/pm.toml")

    def test_task_prefix(self):
        self.write("tasks/engineering.toml", ENGINEERING + 'prefix = "ENG"\n')
        self.assertEqual(self.runs()["eng"].task["prefix"], "ENG")
        for bad in ('""', "1"):
            with self.subTest(bad):
                self.write("tasks/engineering.toml", ENGINEERING + f"prefix = {bad}\n")
                self.rejects("prefix", prefix="tasks/engineering.toml")
```

  In `Config`:

```python
    def test_harness_key_required(self):
        for text in (BASE.replace('harness_key = "linear-api-key"\n', ""),
                     BASE.replace('harness_key = "linear-api-key"', 'harness_key = "-a"'),
                     BASE.replace('harness_key = "linear-api-key"', "harness_key = 1")):
            with self.subTest(text[:80]):
                with self.assertRaises(SystemExit) as cm:
                    self.load(text)
                self.assertIn("pipeline.toml: harness_key", str(cm.exception.code))
        self.assertEqual(self.load(BASE)["harness_key"], "linear-api-key")
```

  In `RealConfig.test_three_runs` also assert `{k: (r.key, r.account) for k, r in runs.items()}` equals researcher `("linear-api-key-researcher", "frank.agent.w+researcher@gmail.com")`, pm `("linear-api-key-pm", "frank.agent.w+pm@gmail.com")`, engineer `("linear-api-key-engineer", "frank.agent.w+engineer@gmail.com")` by the same project ids, and `pipeline.load_config()["harness_key"] == "linear-api-key"`; and `registry()` tasks `product-design`/`engineering`/`deep-research` have `prefix` `"PRD"`/`"ENG"`/absent.

- [ ] **Step 3: Run** `python3 -m unittest discover -s scripts/tests -k role -k prefix -k harness -k runs` → FAIL.
- [ ] **Step 4: Implement in `scripts/pipeline.py`.**

```python
TOP_KEYS = {"team", "states", "human_members", "harness_key", "projects"}
ROLE_KEYS = {"read_only", "memory", "tasks", "account", "key"}
TASK_KEYS = {"model", "effort", "add_dirs", "repo_from_issue", "allowed_tools", "prefix"}
# A Keychain service name; it reaches `security` argv and the tmux command line.
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
```

  `load_config`, right after `_check_ids(cfg)`:

```python
    hk = cfg.get("harness_key")
    if not isinstance(hk, str) or not KEY_RE.fullmatch(hk):
        raise SystemExit(f"pipeline.toml: harness_key must be a Keychain service name: {hk!r}")
```

```python
@dataclass(frozen=True)
class Role:
    """roles/<role>.toml, validated; key is a Keychain service name, never the secret."""
    read_only: tuple
    memory: str | None
    tasks: tuple
    account: str
    key: str
```

  `Run` gains `key: str` and `account: str` after `read_only` (docstring: key is the role's Keychain service name). `_role` keeps its read_only/memory checks first (unchanged messages), no early `return`, then:

```python
    tasks, account, key = r.get("tasks"), r.get("account"), r.get("key")
    if not isinstance(tasks, list) or not tasks or not all(isinstance(t, str) and t for t in tasks):
        raise SystemExit(f"{where}: tasks must be a non-empty list of task names: {tasks!r}")
    if not isinstance(account, str) or not account:
        raise SystemExit(f"{where}: account must be the role's Linear email: {account!r}")
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise SystemExit(f"{where}: key must be a Keychain service name: {key!r}")
    return Role(tuple(read_only), memory, tuple(tasks), account, key)
```

  `_task`: `if "prefix" in t and (not isinstance(t["prefix"], str) or not t["prefix"]): raise SystemExit(f"{where}: prefix must be a non-empty string")`.

  `registry` (docstring `{name: Role}`), after validating tasks:

```python
    owner = {}
    for name, r in roles.items():
        if unknown := [t for t in r.tasks if t not in tasks]:
            raise SystemExit(f"roles/{name}.toml: tasks {', '.join(unknown)} have no tasks/<task>.md + .toml pair")
        if r.key in owner:
            raise SystemExit(f"roles/{name}.toml: key {r.key!r} is also roles/{owner[r.key]}.toml's")
        owner[r.key] = name
```

  `runnable`, after `registry(root)`:

```python
    humans = {e.lower() for e in cfg.get("human_members") or []}
    for name, r in roles.items():
        if r.key == cfg["harness_key"]:
            raise SystemExit(f"roles/{name}.toml: key {r.key!r} is pipeline.toml's harness_key")
        if r.account.lower() in humans:
            raise SystemExit(f"roles/{name}.toml: account {r.account!r} is in pipeline.toml's human_members")
```

  and in the project loop, after the unknown role/task checks: `r = roles[role]`; `if task not in r.tasks: raise SystemExit(f"pipeline.toml: {name!r} task {task!r} is not in roles/{role}.toml tasks")`; build `Run(task, t, charter, instructions, r.memory, r.read_only, r.key, r.account)`.

- [ ] **Step 5: Real config.** `pipeline.toml` after `human_members`: `harness_key = "linear-api-key"   # Keychain service of the harness account's Linear API key (router, promote, prune)`. Role tomls per the spec's G1 table (comments on researcher's three lines as in the spec, other roles no comments); engineer keeps `read_only`. `tasks/product-design.toml`: `prefix = "PRD"   # title prefix of this task's issues`; `tasks/engineering.toml`: `prefix = "ENG"`.
- [ ] **Step 6: Run** the full suite `python3 -m unittest discover -s scripts/tests` → all PASS.
- [ ] **Step 7: Commit** `TASK-67: per-role tasks, account and key; task prefix; harness_key` and push.

### Task 2: harness key by service (FR-14)

**Files:** Modify `scripts/pipeline.py` (`linear_gql`, new `harness_service`); Test `scripts/tests/test_pipeline.py`.

**Interfaces:** Consumes `cfg["harness_key"]` (Task 1). Produces `pipeline.harness_service() -> str` (cached).

- [ ] **Step 1: Failing tests** (new class; imports `io`, `types.SimpleNamespace`):

```python
class LinearGql(unittest.TestCase):
    def test_harness_key_by_service_only(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(stdout="secret\n")
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"data": {"viewer": {"id": "v"}}}')
        with mock.patch.object(pipeline, "harness_service", return_value="svc-h"), \
                mock.patch.object(pipeline.subprocess, "run", run), \
                mock.patch.object(pipeline.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.assertEqual(pipeline.linear_gql("query { viewer { id } }"), {"viewer": {"id": "v"}})
        self.assertEqual(calls, [["security", "find-generic-password", "-s", "svc-h", "-w"]])
        self.assertEqual(urlopen.call_args[0][0].get_header("Authorization"), "secret")

    def test_harness_service_reads_pipeline_toml(self):
        pipeline.harness_service.cache_clear()
        self.addCleanup(pipeline.harness_service.cache_clear)
        self.assertEqual(pipeline.harness_service(), "linear-api-key")
```

- [ ] **Step 2: Run** `python3 -m unittest discover -s scripts/tests -k LinearGql` → FAIL.
- [ ] **Step 3: Implement** (`import functools`):

```python
@functools.cache
def harness_service():
    """Keychain service of the harness account's Linear key: pipeline.toml's harness_key."""
    return load_config()["harness_key"]


def linear_gql(query, **variables):
    key = subprocess.run(["security", "find-generic-password", "-s", harness_service(), "-w"],
                         capture_output=True, text=True, check=True).stdout.strip()
    ...unchanged
```

- [ ] **Step 4: Run** full suite → PASS. **Step 5: Commit** `TASK-67: harness key by pipeline.toml service` and push.

### Task 3: launcher sets the session's key service (FR-13)

**Files:** Modify `scripts/launch.py` (docstring, new `has_key`, `main`); Test `scripts/tests/test_launch.py`.

**Interfaces:** Consumes `Run.key` (Task 1). Produces `launch.has_key(service) -> bool`; `launch.main(..., keychain=has_key)`.

- [ ] **Step 1: Test harness.** In `setUp`: `self.checked, self.missing = [], set()`. `run_launch(self, *argv, keychain=None)` passes `keychain=keychain or (lambda s: self.checked.append(s) or s not in self.missing)` to `launch.main`.
- [ ] **Step 2: Failing tests.**

```python
    def test_session_gets_role_key_service(self):
        self.assertEqual(self.run_launch(*self.args()), 0)
        self.assertEqual(self.checked, ["k-researcher"])
        self.assertIn("LINEAR_KEYCHAIN_SERVICE=k-researcher", self.exports())

    def test_missing_role_key_is_config_error(self):
        self.missing = {"k-researcher"}
        self.make_transcript()
        for mode in ("new", "resume"):
            with self.subTest(mode):
                self.calls = []
                self.assertEqual(self.run_launch(*self.args(mode)), 2)
                self.assertEqual(self.calls, [])
                self.assertRegex(self.plog().splitlines()[-1],
                                 r"^\S+ \S+ config-error TASK-1: no Keychain item for role key k-researcher$")
                self.assertIn("config-error TASK-1", self.err)

    def test_has_key_never_reads_the_secret(self):
        calls = []
        def run(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=44)
        with mock.patch.object(launch.subprocess, "run", run):
            self.assertFalse(launch.has_key("k-x"))
        self.assertEqual(calls, [(["security", "find-generic-password", "-s", "k-x"],
                                  {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})])

    def test_no_key_in_command_env_or_log(self):
        secret, security = "lin_api_SENTINEL", []
        def run(cmd, **kw):
            if cmd[0] != "security":
                raise AssertionError(cmd)
            security.append(cmd)
            return SimpleNamespace(returncode=0, stdout=secret if "-w" in cmd else "", stderr="")
        for project, key in (("p-dr", "k-researcher"), ("p-eng", "k-engineer")):
            with self.subTest(project):
                self.calls = []
                with mock.patch.object(launch.subprocess, "run", run), \
                        mock.patch.object(eng, "resolve", return_value=eng.Invalid("x")):
                    self.assertEqual(self.run_launch(*self.args(project=project), keychain=launch.has_key), 0)
                (cmd,) = self.calls
                self.assertFalse([part for part in cmd if secret in part])
                self.assertIn(f"LINEAR_KEYCHAIN_SERVICE={key}", self.exports())
                self.assertNotIn("LINEAR_API_KEY", cmd[9])
        self.assertEqual(sorted(c[-1] for c in security), ["k-engineer", "k-researcher"])
        self.assertFalse([c for c in security if "-w" in c])
        for f in os.listdir(os.path.join(self.logs, "projects")):
            with open(os.path.join(self.logs, "projects", f)) as fh:
                self.assertNotIn(secret, fh.read())
```

  (`SimpleNamespace` from `types`.) Existing exact-string tests stay unchanged: the prompt does not change.
- [ ] **Step 3: Run** `python3 -m unittest discover -s scripts/tests -k key` → FAIL.
- [ ] **Step 4: Implement.**

```python
def has_key(service):
    """True when the Keychain has an item for service; the secret is never read (no -w)."""
    return subprocess.run(["security", "find-generic-password", "-s", service],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
```

  `main(..., root=ROOT, keychain=has_key)`; right after the resume transcript check:

```python
    if not keychain(job.key):
        return fail(plog, a.issue, "config-error", f"no Keychain item for role key {job.key}", 2)
```

  tmux script env: `script(a, cmd, {**ENV, "LINEAR_KEYCHAIN_SERVICE": job.key, **env}, plog, runs)`. Module docstring: config error covers "a role memory overlapping the issue's repo, or a role key missing from the Keychain".
- [ ] **Step 5: Run** full suite → PASS. **Step 6: Commit** `TASK-67: launcher points the session at its role's Keychain service` and push.

### Task 4: role accounts are agents in the attempt cap (spec G5)

**Files:** Modify `scripts/pipeline.py` (new `Q_USER`, `user_id`; `reviewer` uses it), `scripts/router.py` (`Board.__init__`, `last_move`); Test `scripts/tests/test_router.py`.

**Interfaces:** Consumes `pipeline.registry()` → `{name: Role}` (Task 1). Produces `pipeline.user_id(gql, email) -> str | None`; `Board.agents: set[str]`.

- [ ] **Step 1: Fake Linear knows role accounts.** In `test_router.py`: `ROLE_IDS = {r.account: f"role:{n}" for n, r in pipeline.registry()[0].items()}`; `FakeLinear.__init__` gets `self.unknown = set()`; the users branch returns `{"users": {"nodes": [{"id": uid}] if (uid := {"me@x.com": USER, **ROLE_IDS}.get(v["e"])) and v["e"] not in self.unknown else []}}`.
- [ ] **Step 2: Failing tests** (in `Plan`, next to the cap tests):

```python
    def test_attempt_cap_not_reset_by_role_account(self):
        pm = ROLE_IDS["frank.agent.w+pm@gmail.com"]  # not the runnable project's role
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": pm, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")

    def test_attempt_cap_reset_by_other_member(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": "member", "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "resume TASK-1 d 1 https://linear.app/x/TASK-1 Deep Research")

    def test_unknown_role_account_fails_loud(self):
        fake = FakeLinear([])
        fake.unknown = {"frank.agent.w+engineer@gmail.com"}
        with self.assertRaises(SystemExit) as e:
            self.run_main(fake, "--plan")
        self.assertIn("role accounts not found in Linear: frank.agent.w+engineer@gmail.com", str(e.exception))

    def test_agents_cover_every_role_under_project_filter(self):
        board = router.Board(FakeLinear([]), [], self.tdir, NOW, True, pipeline.load_config(self.config), only="p-dr")
        self.assertEqual(board.agents, {ME, *ROLE_IDS.values()})
```

  (Adjust `Board(...)` args to its actual signature `Board(gql, entries, tdir, now, dry, cfg, only=None)`.)
- [ ] **Step 3: Run** `python3 -m unittest discover -s scripts/tests -k role_account -k other_member -k agents_cover` → FAIL.
- [ ] **Step 4: Implement.** `pipeline.py`:

```python
Q_USER = "query($e: String!) { users(filter: { email: { eqIgnoreCase: $e } }) { nodes { id } } }"


def user_id(gql, email):
    """Linear user id of an email (case-insensitive), or None."""
    nodes = gql(Q_USER, e=email)["users"]["nodes"]
    return nodes[0]["id"] if nodes else None
```

  `reviewer` keeps its docstring and error, using `uid = user_id(gql, emails[0])`. `router.py` imports `registry, user_id`; in `Board.__init__` after `self.reviewer = ...`:

```python
        ids = {r.account: user_id(gql, r.account) for r in registry()[0].values()}
        if missing := sorted(a for a, i in ids.items() if i is None):
            raise SystemExit(f"role accounts not found in Linear: {', '.join(missing)}")
        self.agents = {self.me, *ids.values()}
```

  `last_move` docstring: "(by_user: by someone other than the harness or a role account)"; condition `n["actorId"] and n["actorId"] not in self.agents`.
- [ ] **Step 5: Run** full suite → PASS (`test_promote` still passes: same `users(filter` query and `e` variable). **Step 6: Commit** `TASK-67: role accounts never reset the attempt cap` and push.

### Task 5: rules and docs (FR-22, FR-23)

**Files:** Modify `roles/principles.md`, `README.md`, `CLAUDE.md`.

- [ ] **Step 1: `roles/principles.md`.** Replace the "Linear access" bullet with: "- Linear access: the `linear` skill. The launcher points it at your role's key (`LINEAR_KEYCHAIN_SERVICE`), so `viewer` is your role account." Replace the "Comments by a user …" bullet with: "- Comments by a user whose email is in the prompt's `Humans:` list (`human_members`) are the user's. Everything the agent accounts do (comments, moves, edits) is the agent's, never the user's: the role accounts (yours and the other roles') and the harness account `frank.agent.w@gmail.com` (router, promote, prune)." In the Handoff bullet, "written by the agent account" → "written by the harness account".
- [ ] **Step 2: `README.md`.** Setup: requirements mention the `linear` skill honouring `LINEAR_KEYCHAIN_SERVICE`; the `security add-generic-password ... -s linear-api-key -w` line's comment becomes "harness account's Linear API key; the only item under pipeline.toml's harness_key". Add after the Setup code block:

```markdown
### Role accounts

Each role in `roles/*.toml` acts in Linear as its own `account`, with its API key in the Keychain under `key`:

1. Linear → Settings → Members: invite the `account` (a Gmail plus-alias of the harness account, e.g. `frank.agent.w+pm@gmail.com`).
2. Open the invite in a private window and choose **Continue with email**; Continue with Google signs in as the harness account.
3. Signed in as the role account, create a personal API key in its account settings.
4. `security add-generic-password -s <key> -a <account> -w` and paste the key at the prompt.

The launcher never reads the key: it checks the item exists and sets `LINEAR_KEYCHAIN_SERVICE=<key>` for the run's `linear` skill. A missing item stops that role's runs with a `config-error` line in its project log.
```

  Configuration bullets: `pipeline.toml` adds "`harness_key`, the Keychain service of the harness account's key"; `roles/` `.toml` lists `tasks` (first is the default), `account`, `key`, `read_only`, `memory`; `tasks/` `.toml` adds an optional title `prefix`.
- [ ] **Step 3: `CLAUDE.md`.** `launch.py` bullet: add "It checks the role's Keychain item (`security find-generic-password -s <key>`, no `-w`; missing → `config-error`, exit 2) and exports `LINEAR_KEYCHAIN_SERVICE=<key>`, so the run acts as the role's Linear account; it never reads a key." `pipeline.py` bullet: "shared Linear GraphQL client (the harness account's key, from the Keychain service `harness_key` in `pipeline.toml`), config loading and validation (`load_config`, `registry`, `runnable`)". Add an Architecture bullet: "Identity: router, promote and prune act as the harness account `frank.agent.w@gmail.com`; each run as its role's `account` (`roles/<role>.toml`). Only Keychain service names travel in env vars, argv, logs and prompts, never keys." Roles bullet: "A runnable project has `role` + `task` in `pipeline.toml`; the task must be in the role's `tasks`; each is an `.md` + `.toml` pair."
- [ ] **Step 4:** `grep -rn "agent account\|-a frank.agent.w" roles tasks README.md CLAUDE.md` shows no stale claim that sessions use the harness account. **Step 5: Commit** `TASK-67: docs and principles for role accounts` and push.

## Verification (S6)

- `python3 -m unittest discover -s scripts/tests` — all pass.
- `grep -n '"-w"' scripts/launch.py` — none.
- Real Keychain presence (no `-w`): `security find-generic-password -s linear-api-key-<role> >/dev/null` for researcher, pm, engineer, and `-s linear-api-key`.
- Real config loads: `python3 -c` → `pipeline.runnable(pipeline.load_config())` gives each run its role's key/account.

## Progress

- decision(FR-15): no code change - the prompt already passes the resolved task file; Reviewer: stays until PR-B; dissent: none
- decision(attempt cap): role accounts join the agent set in Router's by_user check (G5) over leaving it to PR-C's FR-11 - a role session's own move to Todo would otherwise reset the cap forever, breaking "runs as before"; dissent: none
- decision(project task in role tasks): runnable() rejects a project task outside its role's tasks - gives `tasks` its meaning while dispatch is by project; dissent: none
- decision(harness key source): linear_gql reads harness_key from pipeline.toml per call over threading cfg through every caller - no caller signature changes; dissent: none
- S3 panel: core=[architecture,spec-fitness] +optional=[security] transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS security=PASS -> converged; folded non-blockers: harness_service cached, harness_key/account checks in runnable, agents from all roles via user_id eqIgnoreCase, KEY_RE, account not in human_members, residual-risk note
- S4: plan written, 5 tasks (config, harness key, launcher, router agents, docs); execution=subagent-driven-development
- S5: 5 tasks done via SDD (65e8cd3 config, 62ea949 harness key, 9a1885d launcher, 26e13ea router agents, d6df89a+f2ec8e9 docs); per-task reviews clean
- S6: 274 tests OK; Keychain items present (no -w); real runnable() gives each run its role key/account; linear_gql by service -> viewer frank.agent.w@gmail.com; role accounts resolve in Linear; each role service authenticates as its account; promote --dry-run OK
- S7 panel: core=[correctness,requirement-fidelity,doc] +optional=[test,code-quality] +security(roster, unmatched by selector; keys are the change's core risk) dropped=[architecture (reviewed in S3), performance (marginal)] transport=Workflow
- S7 r0: correctness=PASS requirement-fidelity=PASS doc=PASS test=PASS code-quality=PASS security=PASS -> converged
- S8: skipped (commits kept, per run instructions)
- Residual non-blocking: harness_key service must hold exactly one Keychain item (documented, not checked at runtime); has_key lets an OSError from `security` crash instead of config-error; one unresolvable role account stops every router tick (fail loud, intended); two roles may share an `account`; README "tasks (first is the default)" describes behavior used only from PR-C; README harness_key comment and "its project log" wording; CLAUDE.md Identity bullet overlaps the launch.py bullet; Run.account unused outside tests; router reuses the name `missing`; Board parses the registry twice; RESEARCHER_ID/ENGINEER_ID duplicated across test modules; long Run docstring and launch.py tmux line; dense FakeLinear users branch.
