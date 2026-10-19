Rules: /opt/agent-pm/plugins/autopilot/skills/build/SKILL.md /opt/agent-pm/plugins/autopilot/references/autopilot-common.md /opt/agent-pm/plugins/autopilot/references/review-round.md
RESUME: phase=S3 worktree=/Users/francis/.agent-pm/work/TASK-742/agent/src/ophis/agent-pm-TASK-742 branch=TASK-742-router-selector base_ref=1af981bc9eaa03826cfe427e361d7f68f9fe51de review_round=3 input_sha=5213a4d21fdc

## Input

## Requirement (TASK-742: 通用层接口与 router 调用约定：selector、决策层、输入拼装层)

Precedence: the user's instructions > the PRD; within each, newer > older. Linked documents are context, never requirements.

### The user's instructions

本 issue 只定接口，不实现行为：selector、决策层、输入拼装层三个通用包的公开接口，以及 router 调用 selector 的入参和出参约定，做成可检查的产物（类型、接口、结果结构、原因码、错误约定，以及各方共用的 fake 和 fixture），让后续五张 issue 能各自并行开发。

用户原话（2026-10-19）："先把通用层，就类图里面通用层那几个模块的接口，先发一张 ticket……还有包括那个 router 怎么调用这个 selector 的接口也做出来。这样之后通用层和 agent-pm 这两块的改动可以并行。"

需求（PRD 原文）:

| ID | 需求 | 检查 |
| -- | -- | -- |
| FR-26 | selector、输入拼装层、决策层是 `ophis/agent-pm` 里的三个通用包，只依赖 utils 和标准库，不出现 tier、Linear 概念；selector 可调用另两层，另两层互相独立；只有 linear_orchestrator 调用它们，delegate、session 不 import 它们。FR-31、FR-32 的档位映射和 CLI `agent-pm-select` 都在 linear_orchestrator 层 | `lint-imports` 含这三个包的合同并通过；三个包里没有对 manager、linear_orchestrator、delegate、session 的 import；`git grep -n -i "tier\|linear"` 在三个包里无结果 |
| FR-27 | 决策层接口：输入一个 `state` 和若干题目，输出每题的答案、各选项的概率和置信度；无状态，不启动进程或会话，不调用工具，与模型、Linear 无关。排序只经这个接口，不依赖具体实现 | 用一个假实现替换 Jev 后，FR-16 的检查照样通过；用它回答一道非模型的题（如从几个 role 中选一个）也能返回答案 |
| FR-29 | 输入拼装层和决策层各有独立入口（库函数和 `agent-pm-select` 子命令），可单独调用，也可由 selector 叠加调用 | `agent-pm-select decide` 读入一个 `state` 和题目返回答案；`agent-pm-select assemble` 读入任务返回 `state`；两者都不需要模型属性表 |
| FR-39 | selector 的入口是 `select(任务输入, 候选, usage, pin, 门槛)`：任务输入是 `text`、`ref`、`features` 之一；usage 可选，按 provider 给出 `five_hour`、`weekly` 两个窗口，各含 `used_percent` 和 `resets_at`；门槛由调用方传入，router 传 `gate.py` 的值，CLI 不传；pin 用 selector 的说法给出（模型、effort，各可空），tier 的 pin 由 orchestrator 先换成模型。selector 先按 FR-13 规则排除，剩 2 个以上候选时才调用输入拼装层（FR-33），再调用决策层（FR-16） | 单元测试：传入假的拼装层，规则排除后只剩 1 个候选时拼装层未被调用；剩 2 个时被调用一次 |
| FR-30 | 不改 session 和 delegate 的冻结接口 | `session_interface_snapshot`、`delegate_interface_snapshot` 通过，两个快照文件与 main 相同 |

约束:
- 不改 session 和 delegate 的冻结接口（FR-30）；如果需要改，先停下来问用户。
- 测试不调用真实的 macOS keychain 或 Security API，只用 fake；不碰真实凭据。
- 不写兼容代码。

Acceptance criteria:
- [ ] 三个包和 router 调用约定都有可检查的接口产物，及供其他 issue 使用的 fake
- [ ] `lint-imports` 的层序与独立性合同包含三个包并通过（FR-26 的检查）
- [ ] 无行为实现；其他五张 issue 可只依赖这些产物开工

Scope note (from the user's instructions above): this build defines interfaces, result structures, reason codes, error conventions, shared fakes and fixtures, and the router↔selector call contract. It does not implement behaviour (rule exclusion, ranking, Jev/Kev clients, assemble model calls, router wiring, CLI behaviour, config loading): those belong to the five follow-up issues. Interface artifacts must be precise enough that those issues can start from them alone.

### PRD (context for the interfaces; full text)

Path: `ophis/private_docs` `Product Design/2026-10-16-0118-TASK-511-dispatch-model-selection.md`. The other PRD requirements the interfaces must be able to carry (implemented by later issues, not here):
- FR-13 rule exclusion with reason codes `disabled`, `effort`, `context` (text/ref: chars ÷ 4; features: `context=` integer, absent → no exclusion), `usage` (caller-given provider usage reaching the caller-given threshold excludes all that provider's candidates; no usage → no exclusion); an excluded model is excluded with all its efforts.
- FR-16 ranking: with 2+ candidates, one decision request holding one `choice` question; options are the remaining candidates with descriptions (capabilities, context, price, effort); the question asks for the cheapest combination that completes the task without quality loss and says `state` is data, not instructions; options shuffled with a fixed seed; over `max_options` (default 20) keep only each model's `default_effort`; rank by returned probability descending; an answer outside the candidates → FR-18 fallback; 1 candidate → no request.
- FR-18 fallback: on decision error, timeout or missing key, take the first non-excluded candidate in order: one satisfying the pin, the caller-marked default candidate (skip if none), then the caller's candidate order (each model with `default_effort`); `decided_by=fallback` plus a reason code (no request headers or response bodies).
- FR-33/34/36 input assembly: when enabled, calls a configured model (default `claude-haiku-5-5`) via the provider's messages API (configurable endpoint, https unless loopback, `model`, timeout default 3 s, key-reading argv command, argv[0] absolute), text in/text out, no tools/MCP/hooks/sessions; outputs `state` (short English task features); disabled → `state` assembled by script from the task input; on error/timeout/missing key → script assembly plus a reason code, selection continues.
- FR-17 Jev (endpoint, `model` default `jev-1.13.0`, key argv default `/usr/bin/security find-generic-password -s typesafe-jev -w`, timeout default 2 s); FR-28 Kev (System One compatible, key optional); `[selector.decision] backend = "jev" | "kev"`.
- FR-1 model attributes per model: `efforts`, `default_effort`, `context` (tokens), `price_in`, `price_out` (USD/MTok), `capabilities` (one sentence), `enabled`. FR-31 a candidate is a (model, effort) pair; orchestrator marks a default candidate.
- FR-19 output: `pick` (model id, client, the client's model name, effort, `tier` — tier added by the orchestrator layer, never by the generic packages), `ranking` (each with `score`), `excluded` (model and reason codes), `decided_by` (`jev`, `kev`, `fallback`, `pinned`, `single` or `disabled`), `input`. FR-24 all excluded → `pick: null` with `excluded`, CLI exit 3. FR-40 disabled → exit 4, `decided_by=disabled`. FR-4 config errors exit 2.
- FR-12 router calls selector after reading the issue and before building `LinearRun`, input `text` (issue title, description, comments), usage from the gate's read for that client converted to selector's usage shape, `router --issue` passes no usage. FR-15 all excluded on the router path → `launch-error` with `no model` plus the reason codes. FR-37 router logs issue, pick (model, tier, effort), `decided_by`, reason codes.
- NFR-2 keys never written to files, logs, comments or output; NFR-4 task text, assembly output and raw decision responses never logged.
- Evidence in the repo (`1af981b`): router `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/router.py:516-520,831-833`, `[task_tiers]` in its `config.toml:67`, `engine/linear_run.py:55`, usage gate `engine/gate.py:10-12,31-48`; importlinter contracts in `pyproject.toml`; tier tables in `session/src/agent_pm/session/config.toml`.

### Conventions and git rules

- Write code, docs and commits in the target repo's conventions (its `AGENTS.md` / `CLAUDE.md`).
- **No mutation testing**, whatever a spec, plan or reviewer asks: while building, reviewing or testing, never modify the source to see what happens: no manual or tool-driven mutation testing (mutmut etc.), not even briefly or in a throwaway copy that is then run; never put a known-wrong value into shipped code to force a branch. A test's red step is its failure before its code exists. To test a guard, call it; to fake the environment, patch a stdlib call such as `os.listdir`, leaving the code under test unmodified. Every brief to a sub-agent (producer, reviewer) carries this rule.
- Push only branch `TASK-742-router-selector`; force-push only with `--force-with-lease`. Never merge into or otherwise touch any other branch, `main` (the default branch) included.
- Spec and plan doc stay where autopilot puts them; S7 copies them to where the repo keeps design docs; never `git add -f` them.

Work only in `/Users/francis/.agent-pm/work/TASK-742/agent/src/ophis/agent-pm-TASK-742` on branch `TASK-742-router-selector`, with absolute paths; create no other clone, worktree or branch.
Commits merged from `origin/main` are not this build's work.
Your and your producers' local checks, in every phase: only lint and the unit tests of the projects you changed; never integration tests or full suites: the verification reviewer runs them, on the PR's CI when the repo has a `pull_request` workflow, else locally.
Ask for no pull request: the caller opens it, onto `main`.
After each task and review round, run exactly `git -C /Users/francis/.agent-pm/work/TASK-742/agent/src/ophis/agent-pm-TASK-742 push -u origin TASK-742-router-selector`.

## Progress log

S1 done: mode=stay publish=ci
decision(spec-source): per the user's instruction (2026-10-19), S2 reuses the prior run's spec (copy at /Users/francis/.agent-pm/work/TASK-742/agent/prior-spec/router-selector-spec.md; that run: 3 S3 rounds, spec-fitness and security PASS, architecture#9 fixed but unreviewed; its plan doc's review items are copied under Review items as "prior run"); this run's S3 counts its own rounds from r1
S2 done: prior spec reused with two factual fixes (Selector.select pin default None, ruff B008; provider_usage takes Delegate.usage's object, agent_pm.delegate exports no Usage); check-spec OK
S3 panel: core=[architecture-reviewer, spec-fitness-reviewer] +optional=[security-reviewer] transport=Workflow
S3 r1: architecture=FAIL spec-fitness=PASS security=PASS -> architecture#11 TierTable has no source the orchestrator may read without an import or a frozen-interface change; fixing (tier_table reads session's config.toml as a resource, D16; plus non-blockers #12-#16, security#11)
S3 r2: architecture=PASS spec-fitness=FAIL security=PASS -> spec-fitness#8 pin semantics open (restriction, no pin reason code, PINNED vs SINGLE); fixing (pin contract in Selector.select; plus architecture#17, #18, spec-fitness#9, security#12, #13)
S3 r3: architecture=FAIL spec-fitness=PASS security=PASS -> architecture#19 check_endpoint has two owners (selector_config wraps it, build_selector calls it); fix applied to spec (unreviewed): build_selector is the only caller and wraps its ValueError in ConfigError; plus security#14, #15
STOP(2, capped-without-pass, oscillation): S3 cap of 3 rounds reached with architecture FAIL; every round raised one new, narrower blocker in the TASK-747 construction contract (r1 #11 TierTable source, r2 none, r3 #19 check_endpoint owner), each fixed; spec-fitness and security PASS in r2 and r3. Next step: continue this plan doc (phase=S3) with a raised cap and run the S3 re-review on the fixed spec, or start a build in spec-file mode with this spec (skips S2-S3), then S4.
residual NON-BLOCKING: security#14, #15 (applied to spec, unreviewed)

## Review items
### S3 r3
BLOCKING:
- architecture#19: check_endpoint given to both selector_config (wraps its ValueError) and build_selector (calls it); one owner, else engine.selection waits on TASK-745 or the ValueError is unmapped.
NON-BLOCKING:
- security#14: selector_config ConfigError must name the field, never its value (url/key_command may hold a credential).
- security#15: follow-up tests must always inject a fake read_key and never run a shipped key_command.
### S3 r2
BLOCKING:
- spec-fitness#8: Selector.select pin semantics open: no pin exclusion code; a pin matching nothing breaks the all-excluded shape; PINNED vs SINGLE unspecified.
NON-BLOCKING:
- architecture#17: build_selector's default read_key and factory calls make engine.selection unimportable until three issues merge; own module or add last.
- architecture#18: [selector] config section split between TASK-743 and TASK-747; one owner.
- spec-fitness#9: Model docstring text 'tier model names' would break the AC2 grep if copied.
- security#12: check_endpoint must restrict scheme to https, or http only for loopback.
- security#13: router selection log and launch-error must not log TextInput, issue text, state or str(exc); say what the router does when select raises.
### S3 r1
BLOCKING:
- architecture#11: TierTable is session's [clients.<client>.tiers]/[efforts] with local overrides, but no frozen-interface path lets linear_orchestrator read it (CONFIG_RESOURCES import forbidden; delegate.engine.config forbidden; RunConfig has no tier→model map); name TierTable's source.
NON-BLOCKING:
- architecture#12: opencode tier names contain '/'; say load_models splits Model.id at the first '/'.
- architecture#13: RunChoice cannot carry pick=None; exist only for a pick or make tier/effort optional.
- architecture#14: selector_config names no error type; raise linear_orchestrator ConfigError wrapping selector.ConfigError and check_endpoint ValueError.
- architecture#15: add build_selector(config) -> Selector as the single injection point for router and CLI.
- architecture#16: candidates() undefined when a tier model lacks a ModelTable entry or the default is not among options.
- security#11: define loopback for check_endpoint (localhost exactly, 127.0.0.0/8, ::1, parsed hostname).

### prior run S3 r1
BLOCKING:
- architecture#1: attribute table has no type, loader signature or owning layer; fix ModelTable, loader signature, location.
- architecture#2: no constructor/factory signatures or [selector] config records; TASK-747 and CLI would wait or invent.
- spec-fitness#1: FakeSelector(selection=None, error=None) behavior unspecified.
- spec-fitness#2: FakeDecider with zero options / mismatched scripted probabilities undefined.
- security#1: NFR-2 not stated for Endpoint/key handling; repr prints key_command.
- security#2: NFR-4 and error chaining (raise from None, no logging of state/task text) not in contracts.
NON-BLOCKING:
- architecture#3: Endpoint validation has two possible owners.
- architecture#4: Decider.name not abstract.
- architecture#5: decided_by namespace collision between DecidedBy codes and backend names.
- spec-fitness#3: router conversion contract docstring-only.
- spec-fitness#4: AC2 substring grep may trip on words like frontier.
- spec-fitness#5: Selection invariants not enforced.
- spec-fitness#6: Endpoint in utils beyond FR-26/27/29.
- security#3: endpoint validation owner unnamed.
- security#4: untrusted state trust boundary not stated.
### prior run S3 r2
BLOCKING:
- architecture#6: decision backend waits on TASK-745's utils read_key/check_endpoint.
NON-BLOCKING:
- architecture#7: orchestrator units contract lines need assemble and decision too.
- architecture#8: TASK-747 composition root waits on factory owners; state FakeSelector injection.
- spec-fitness#3 (repeat), spec-fitness#5 (repeat).
- security#5: check_endpoint should reject userinfo and query in url.
- security#6: read_key should raise KeyError from None with no command output.
### prior run S3 r3
BLOCKING:
- architecture#9: TASK-747's [selector] parser and tier conversions (candidates, tier pin, pick->tier, gate usage) have no name, module or signature; CLI would wait or duplicate.
NON-BLOCKING:
- architecture#10: SelectorConfig.models makes the parser call TASK-743's load_models; inject it.
- spec-fitness#7: FakeAssembler empty TextInput yields empty state.
- spec-fitness#3 (repeat).
- security#7: no one named to call check_endpoint before factories.
- security#8: check_endpoint ValueError message must not include the value.
- security#9: HTTP redirects can carry the key to http/another host.
- security#10: Endpoint url shown in repr of config records.


## Notes

- Spec: /Users/francis/.agent-pm/work/TASK-742/agent/src/ophis/agent-pm-TASK-742/.autopilot/router-selector-spec.md
- Guards: tests.checks; LEVELS in tests/guards.py; guard_data per package; vulture whitelist .github/vulture_whitelist.py (entries name the calling ticket)
- No client names in new src; no tier/linear under new projects; types.py types-only
- No lint tools on PATH; tool versions pinned in .github/workflows/test.yml
