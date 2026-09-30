# TASK-81: project → repo mapping — design

Source: TASK-81 (https://linear.app/ophis-workgroup/issue/TASK-81), handed off from the PRD TASK-65 (`private_docs/Product Design/2026-09-29-2333-TASK-65-project-repo-mapping.md`), FR-1–FR-7 and its non-functional requirements. The user's instructions: register Agent PM and Autopilot at launch (PRD open question 1), MISC unmapped; no other open question; a user-written `Repo:` line overrides the mapping.

Model: an Engineering issue's target repo is its `Repo:` line if it has one, else its Linear project's entry in `pipeline.toml`'s `[project_repos]`, else it bounces as today.

## Goals

1. **G1 — Config (FR-1, FR-2).** Optional top-level table `[project_repos]`, `"<Linear project id>" = "<owner>/<name>"`, validated by `load_config`. Shipped with Agent PM (`121166b1-191a-4461-bec4-42f1c2dc0ddd`) → `ophis/agent-pm` and Autopilot (`ae72ede7-67a6-469d-a959-8ea51ab71fb8`) → `ophis/claude-autopilot`.
2. **G2 — Resolve (FR-3, FR-4).** `eng.resolve` reads the issue's project id and takes the mapping from its caller; a `Repo:` line wins silently, the mapping is the fallback.
3. **G3 — Source visible (FR-5).** A mapped repo adds `(from project mapping)` to the prompt's `Repo check: OK` line.
4. **G4 — Mapping failures (FR-6).** Failures caused by the mapped repo itself get a `project mapping <owner>/<name>: ` reason; `tasks/engineering.md` step 2 bounces them with a request to fix `pipeline.toml`.
5. **G5 — Docs (FR-7).** README, CLAUDE.md, `tasks/engineering.md`, `pipeline.toml` comments.

## Non-goals

- The PM hand-off line (`tasks/product-design.md` step 8) stays as is; no mapping-aware PM prompt (PRD open question 2).
- A warning when a `Repo:` line differs from the mapping; several repos per project; checking project ids or repos against Linear/GitHub at load time; migrating issues; promote changes.
- Historical docs in `docs/specs/`.

## Decisions

- **Mapping travels as a dict argument.** `resolve(issue_id, gql, run, playground=PLAYGROUND, repos=None)`; `repos` is `{project id: "owner/name"}` (`None` = empty). The launcher passes `cfg["project_repos"]` from the config it already loaded; the `eng.py` CLI loads its own (`load_config`), so both resolve the same repo.
- **`Ok` gains `mapped: bool = False`** (last field, defaulted, so every existing `Ok(...)` stays valid). It is the only way the launcher learns the source.
- **Prefix wording.** Today's mapped-repo reasons already start with `<owner>/<name>: `; a mapped failure is `"project mapping " + <today's reason>`, e.g. `project mapping ophis/x: not found or no access (HTTP 404)`, so the reason starts with `project mapping <owner>/<name>: ` without repeating the repo.
- **Annotation position.** Right after the repo: `Repo check: OK ophis/agent-pm (from project mapping), clone …`.
- **CLI config errors exit 2.** A `SystemExit` from `load_config` in `eng.py` prints `eng.py: <message>` to stderr and exits 2 (refused), matching the documented exit codes.

## G1 — Config

`pipeline.py`:

- `OWNER` and `NAME` (the regexes) move from `eng.py` to `pipeline.py`, with `repo_slug(value)` → `(owner, name)` or `None`: the bare `owner/name` rule (fullmatch, name not `.`/`..`). `eng._one` uses it for its bare form; `load_config` and `resolve` use it for mapping values. No rule changes.
- `TOP_KEYS` gains `project_repos`.
- `load_config` sets `cfg["project_repos"]` to `{}` when absent and validates:
  - not a table → `SystemExit` `pipeline.toml: project_repos must be a table of "<Linear project id>" = "<owner>/<name>"`;
  - per entry, the key is a Linear project id (`UUID_RE`, lowercase as Linear returns it) and the value is a string accepted by `repo_slug` — the same rule as a bare `Repo: owner/name` (no URL, no empty value). A bad entry → `SystemExit` starting `pipeline.toml: project_repos.<key> ` and containing the value (`!r`).
  - Several projects may map to the same repo.

`pipeline.toml` gets, after `[roles.*]`:

```toml
# Target repo of an Engineering issue with no Repo: line, by its Linear project id; a Repo: line wins.
[project_repos]
"121166b1-191a-4461-bec4-42f1c2dc0ddd" = "ophis/agent-pm"           # Agent PM
"ae72ede7-67a6-469d-a959-8ea51ab71fb8" = "ophis/claude-autopilot"   # Autopilot
```

## G2 — Resolve (`scripts/eng.py`)

- `Q_ISSUE` adds `project { id }`.
- `parse_repo` is unchanged. `resolve`:
  - `parse_repo` returns a repo → use it (`mapped=False`), whatever the mapping says.
  - It returns an `Invalid` other than "no Repo: line" (unreadable value, several values) → return it, mapped or not.
  - It returns "no Repo: line …" and the issue's project id is in `repos` → use `repo_slug(repos[id])` (`mapped=True`; `None` → `Invalid` with the mapping prefix); else return today's `Invalid` unchanged. No project, a `project` that is not an object, or an `id` that is not a string counts as no project.
- The rest of `resolve` (access, clone, branch) is unchanged for both sources.
- `eng.main(argv, env, gql, run, out, err, config=CONFIG)` loads `config` after the `AGENT_PM_ISSUE` check and passes `repos=cfg["project_repos"]` to `resolve`.

## G3 — Launcher (`scripts/launch.py`)

- `repo_step` takes the mapping and calls `eng.resolve(a.issue, gql, run, repos=<cfg["project_repos"]>)`.
- `Repo check: OK <owner>/<name>` becomes `Repo check: OK <owner>/<name> (from project mapping)` when `r.mapped`; otherwise the tail is byte-identical to today. `Repo check failed:` lines carry the reason as today.

## G4 — Mapping failures

With `mapped=True`, these `Invalid` reasons are prefixed with `project mapping ` (one constant in `eng.py`, which the tests use): HTTP 403/404 from `gh api repos/<owner>/<name>`, no push permission, unsafe default branch name. Clone and branch failures (symlink/outside playground, not a git repo, origin URL differs, several/bad `<ID>-*` branches) and every `Transient` keep today's reason. A resumed launch still turns `Invalid` into `Transient` (unchanged).

`tasks/engineering.md` step 2, for a reason starting `project mapping `:

- Handoff-created → on the PRD issue comment the reason and ask the user to fix the project's entry in `pipeline.toml`'s `[project_repos]` (or add a `Repo:` line), then Handoff again; move it to In Review; on this issue comment the same and move it to Canceled.
- Otherwise → `Question:` with the reason, asking the user to fix the mapping (or add a `Repo:` line) and move the issue back to Todo; move it to In Review.

Other reasons keep today's two bullets.

## G5 — Docs

- `README.md`: "New work" — a direct engineer issue needs a `Repo:` line only when its project has no `[project_repos]` entry; "Approve" — a PRD's Handoff comment needs `Repo:` only when the project is unmapped, and a `Repo:` line always wins; "Configuration" — `pipeline.toml` lists `[project_repos]`.
- `CLAUDE.md`: the `eng.py` bullet — the repo comes from the issue's `Repo:` line, else its project's `[project_repos]` entry.
- `tasks/engineering.md`: Board "a bad `Repo:` line" → "a bad `Repo:` line or project mapping"; Inputs — the `Repo check: OK` line may end the repo with `(from project mapping)`, and `## Instructions` holds the `Repo:` line (optional in a mapped project); step 2 as in G4.
- `pipeline.toml`: the table's comment (G1).

## Testing

`python3 -m unittest discover -s scripts/tests` (no network, Keychain or Claude). Existing expectations stay, except `test_launch`'s `resolve` call assertion, which gains the `repos` argument. New:

- **pipeline:** absent table → `{}`; the real `pipeline.toml` loads with exactly the two shipped entries (MISC absent); two projects → one repo accepted; rejected with a message naming `project_repos.<key>` and the value: non-UUID key, URL value, `owner/.` and `owner/..`, empty string, non-string value, `owner` without name; non-table `project_repos` rejected; `project_repos` is not an unknown key for `runnable`.
- **eng.resolve:** mapped project + no `Repo:` line → `Ok` of the mapped repo, `mapped=True`; mapped + different `Repo:` line → the line's repo, `mapped=False`; mapped + unreadable `Repo:` line → `Invalid` (unreadable); unmapped project + no line → today's exact `Invalid`; issue without project (`project: null`) + no line → today's exact `Invalid`; mapped repo HTTP 404/403, no push, unsafe default → reason starts `project mapping ophis/<name>: `; mapped repo with a bad clone → today's reason (no prefix).
- **eng CLI:** passes the loaded config's mapping to `resolve`; a broken config → exit 2 with the message on stderr.
- **launch:** a mapped `Ok` → tail `Repo check: OK ophis/demo (from project mapping), clone …`; `repo_step` passes `cfg["project_repos"]` to `resolve`.

Done when: a mapped project's issue without `Repo:` resolves to the mapped repo; an unmapped one bounces with today's reason; a bad `[project_repos]` entry stops `load_config` naming the entry.
