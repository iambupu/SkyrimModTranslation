## ADDED Requirements

### Requirement: 现有工作流状态保持权威
CLI MUST 把 `qa/workflow_state.json`、`qa/workflow_tasks.json`、`.workflow/progress_card.md`、marker/session 作为 outcome 分类输入，并 MUST 仅以只读方式读取 `workflow_policy.json` 取得编排上限。公开 outcome MUST 只是单次命令投影，MUST NOT 写入 workflow state 或新增状态枚举。

#### Scenario: CLI 运行到需要翻译
- **WHEN** 权威 workflow state 为 `candidates_extracted` 且 tasks 等待 Agent 生成译文
- **THEN** CLI 投影 `needs_agent_translation`，但 workflow state 继续保持 `candidates_extracted`

#### Scenario: 状态查询不刷新
- **WHEN** 用户执行 `status`
- **THEN** CLI 原样读取最近快照和 progress card，并设置 `state_snapshot=true`、`refreshed_by_this_command=false`

### Requirement: canonical refresh 使用现有顺序
`run` 和 `resume` 的推进循环 MUST 复用 `workflow_refresh.CORE_REFRESH_STEPS`，按 readiness、workflow state、workflow tasks、Codex handoff 的权威顺序刷新，MUST NOT 在 `smt_cli.py` 维护第二套脚本列表。

#### Scenario: queue prepare 完成
- **WHEN** 新输入完成精确 queue prepare
- **THEN** CLI 按 CORE refresh 顺序生成新的 state/tasks/handoff 后再分类 outcome

### Requirement: 精确执行当前 Mod 的低风险任务
CLI MUST 从 tasks 中选择 session 当前 Mod 的 `executable=true`、`risk=low`、依赖满足、非 `gui:desktop` 任务，并 MUST 使用 `resume_workflow.py --mod-name <session.mod_name> --task-id <selected>` 精确执行。CLI MUST NOT 使用无法按 task/mod 精确过滤的 `run_workflow_tasks.py --limit 1` 代替。

#### Scenario: 工作区存在多个任务 lane
- **WHEN** tasks 中同时存在当前 session Mod 和其他 Mod 的可执行任务
- **THEN** CLI 只把当前 Mod 的精确 task_id 交给 `resume_workflow.py`

#### Scenario: 任务在执行前失效
- **WHEN** 精确任务已被其他执行器认领、完成或依赖失效
- **THEN** 内部入口不执行该任务，CLI 记录底层结果、刷新并重新分类，不选择工作区中的任意其他任务顶替

### Requirement: classify_outcome 支持继续状态
`classify_outcome()` SHALL 返回 `PublicOutcome | None`。当仍有当前 Mod 合法低风险自动任务且不存在更高优先级安全停止条件时 MUST 返回 `None`；只有达到稳定结果才返回 `completed`、`ready_for_manual_test`、`needs_gui`、`needs_agent_translation`、`needs_user_input` 或 `blocked`。

#### Scenario: 自动任务与后续 Agent 任务并存
- **WHEN** 当前既有可执行低风险任务，又有尚未就绪的 Agent 翻译任务
- **THEN** 分类返回 `None` 并先执行自动任务，不得过早返回 `needs_agent_translation`

#### Scenario: GUI 任务是下一稳定动作
- **WHEN** 不存在合法自动任务，下一任务要求 `gui:desktop`、持有该资源锁或要求 handoff 到 Codex
- **THEN** 分类返回 `needs_gui`

### Requirement: completed 和 ready 采用项目级一致性
`completed` MUST 要求 project 与当前 session Mod 都为 `manual_tested` 且 blocking checks 为空。`ready_for_manual_test` MUST 要求当前 Mod 为该状态、project 属于 `ready_for_manual_test|manual_tested`、当前 Mod 无 blocking checks 且不存在 global/project blocker。

#### Scenario: 当前 Mod ready 但额外 Mod 阻断项目
- **WHEN** session Mod A 为 `ready_for_manual_test`，但额外 Mod B 造成 project/global blocker
- **THEN** CLI MUST NOT 返回 `ready_for_manual_test`，而应按证据返回 `needs_user_input` 或 `blocked`

#### Scenario: 静态 QA 通过但未人工测试
- **WHEN** project/current Mod 为 `ready_for_manual_test` 且人工游戏测试尚未验证
- **THEN** CLI 返回 `ready_for_manual_test`，MUST NOT 返回 `completed`

#### Scenario: 人工测试有效
- **WHEN** canonical refresh 后 project/current Mod 都为 `manual_tested` 且无 blocker
- **THEN** CLI 返回 `completed`

### Requirement: 无进展和重试必须安全停止
推进循环 MUST 计算包含 project/current Mod state、blocking checks、pending/running/failed task_id、next action 类型和 evidence 的摘要。单次命令内同一 `(blocker,evidence)` 最多尝试 policy 规定的两次；跨命令 MUST 使用 last_attempt command/evidence、上次失败状态和摘要不变判定，MUST NOT 从通用 `retry_count` 推导 blocker 次数。

#### Scenario: 任务执行后状态不变
- **WHEN** 精确任务完成调用但刷新前后状态摘要相同
- **THEN** CLI 停止循环，返回 `blocked`、退出码 `3`，并报告 task_id 与 evidence

#### Scenario: 相同失败跨命令重现
- **WHEN** 当前 task command/evidence 与 last_attempt 相同、上次为 failed/blocked 且 blocker/摘要没有变化
- **THEN** CLI 不再次自动执行该任务，直接安全停止

#### Scenario: 达到最大步数
- **WHEN** 循环尚未得到稳定 outcome 但达到固定 max steps 或命令超时
- **THEN** CLI 停止，按原因返回普通阻断或超时 `124`，不得无限运行

### Requirement: 子进程输出和后代进程受监管
底层脚本 MUST 通过 `Popen` 增量读取，完整输出写 `.workflow/smt-cli.log`，内存最多保留尾部 200 行。Windows 子进程 MUST 加入 Process Group 和设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object；无法建立可靠监管时 MUST NOT 继续工作流。

#### Scenario: 底层任务超时
- **WHEN** 子进程超过命令超时
- **THEN** CLI 终止整个 Job 进程树、返回 `124`，并在 diagnostics 保留输出尾部

#### Scenario: 用户按 Ctrl+C
- **WHEN** 用户中断正在运行的底层任务
- **THEN** CLI 先发送 CTRL_BREAK，短暂等待后关闭 Job，确保后代退出并返回 `130`

#### Scenario: Job Object 分配失败
- **WHEN** 新进程无法加入 Job Object
- **THEN** CLI 立即终止已启动进程，必要时使用明确的 taskkill tree 兜底，并返回环境不可用 `5`

### Requirement: 进度卡是用户可见事实来源
文本结果 MUST 原样显示 `.workflow/progress_card.md`，JSON MUST 同时提供其相对路径和内容。CLI MUST NOT 用自写摘要替代进度卡或把当前没有自动任务描述成完成。

#### Scenario: run 在 Agent 翻译处暂停
- **WHEN** canonical refresh 生成 `candidates_extracted` 进度卡并分类为 `needs_agent_translation`
- **THEN** 文本输出显示原始卡片，JSON 返回相同内容、next_action 和候选 artifacts

### Requirement: 只读查询使用一致快照
`status` 和 `output` MUST 在短时共享工作区锁内读取相关文件。`output --open` MUST 在锁内验证路径并在释放后打开。共享锁超时 MUST 返回读取失败 `1` 和 `busy=true`，而不是读取可能撕裂的多文件状态。

#### Scenario: 状态刷新正在直接写多个文件
- **WHEN** `run/resume` 持有独占锁并依次写 state、progress card、timeline 和 blockers
- **THEN** 并发 `status/output` 不得看到这些文件的混合版本
