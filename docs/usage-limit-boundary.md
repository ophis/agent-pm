# TASK-848 接口边界

**阻塞**：实现需要扩展 Delegate（委派层）的冻结接口；按要求停在实现前。本文记录原因与最小调整建议。

## 范围

来源：[TASK-848](https://linear.app/ophis-workgroup/issue/TASK-848/按-client-的-usage-limit-开关off-时开工不查用量) 与本次会话的补充要求。

- session 的每个 client 配置 `usage_limit = "on"/"off"`，默认 `"on"`。
- off：router 开工不读用量，忽略该 client 的未知用量、耗尽额度及已有额度暂停记录；其他 client 照旧。
- researcher 的 deep-research brake（深度研究刹车）共用此开关：on 保持当前 80% 阈值，off 跳过；移除独立的 `gate` 配置键及 orchestrator overlay（编排器覆盖配置）。
- 测试只用 fake（替身），不接触真实凭据或 client API；不做 mutation testing（变异测试）。

issue 网页不可访问；`issue read TASK-848 --full --output …` 因本机配置校验失败：`core: document roles must publish to one github.com repo and branch`。尚未重读“补充要求（第 1 轮）”原文；补充范围依据用户在本次会话的说明。

## 源码证据

| 边界 | 当前源码 | 影响 |
|---|---|---|
| 配置归属 | `delegate/src/agent_pm/delegate/engine/config.py` 通过 session、delegate 的有序资源读取有效配置；`AGENTS.md` 规定 “Orchestration reaches session only through Delegate” | 编排器直接用资源字符串读取 session 配置会绕开该边界，也会重复解释配置层次。 |
| 策略传递 | `Delegate.resolve()` 返回 `RunConfig`；`tests/delegate_interface.json` 冻结它的字段，未含 `usage_limit`；`Delegate.usage()` 只返回用量 | 现有接口没有在探测前取得有效 client 开关的途径。 |
| 开工暂停 | `orchestrator/src/agent_pm/orchestrator/engine/gate.py` 的 `Gate.blocked()` 先查已有 `limit`，再调用 `_probe()` | 仅在 Delegate 的用量读取内部跳过探针，不能绕过已有暂停；返回伪造的可用额度也不能满足要求。 |
| 研究刹车 | `orchestrator/src/agent_pm/orchestrator/engine/router.py` 的 `brake()` 总会调用 `read_usage()`，按 `BRAKE_5H = 80` 判断 | 它也需要在探测前取得相同开关，并使用运行中实际 client。 |
| gate 配置 | `orchestrator/src/agent_pm/orchestrator/config.toml` 设置 researcher 的 `gate`；`delegate/.../composer/run.py` 接受此键；`RunConfig.gate` 是冻结字段 | 移除配置键不等于必须删除公开字段；保留派生字段可以避免额外接口变更，不能保留旧键兼容路径。 |

## 最小调整建议

允许仅扩展 `tests/delegate_interface.json`：在 `RunConfig` 增加 `usage_limit: bool = True`，由 Delegate 根据最终 client 的 session 配置解析；编排器通过现有 `resolve()` 取得策略。`tests/session_interface.json` 保持冻结。

获准后，开工与 brake 均先解析实际 client 的策略：off 直接放行，on 保持现有阈值和异常行为。移除 `gate` 的配置支持与编排器配置项，`RunConfig.gate` 只作为内部派生的提示内容保留。测试覆盖 client 选择、已有暂停、on/缺省、off 不调用探针，以及 brake 的 80% 阈值。

## 当前交付

两个独立只读评估均认为需要上述接口例外。尚未修改产品代码、配置或冻结接口，也未运行产品测试；此 PR（拉取请求）只供审阅阻塞原因，不能视为功能交付。
