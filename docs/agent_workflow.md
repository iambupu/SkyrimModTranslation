# Non-GUI Agent Workflow

本页定义所有入口共享的非 GUI 基础流程，并作为 opencode 和 Claude Code 的完整接手协议。[Codex 接手指南](./codex_workflow.md) 在此基础上增加 `qa/codex_handoff.json`、GUI/Computer Use 和按需 Codex 插件辅助，不建立第二套状态机。

## 接手顺序

如果用户只问当前进度，先读 `.workflow/progress_card.md`；必要时再读 `.workflow/workflow_state.json`，不要为进度查询刷新状态或展开完整 handoff。

opencode 和 Claude Code 接手工作区时按顺序读取：

1. `qa/agent_handoff.json`
2. `qa/codex_handoff.json`（兼容 fallback）
3. `qa/workflow_state.json`
4. `qa/workflow_tasks.json`
5. `qa/translation_readiness.json`
6. 插件源 `config/workflow_policy.json`
7. 插件源 `config/agent_capabilities.example.json`

`qa/agent_handoff.json` 是短摘要，不取代状态机、任务视图或 QA 报告。

## 断点恢复

`resume_checkpoint` 用于减少中断后的重复读取。恢复时先运行插件源脚本：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "<PluginRoot>"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "<WorkspaceRoot>"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py" --agent <opencode|claude-code> --check-freshness
```

`<PluginRoot>` 从工作区 marker 的 `plugin_root` 读取，`<WorkspaceRoot>` 是包含 `.skyrim-chs-workspace.json` 的工作区根目录。工作区不包含 `scripts/`；不要在未绑定工作区时从插件源根目录运行相对命令，否则脚本会把插件源误当成项目根。

返回码 `0` 表示现有 checkpoint 可以继续使用。返回码 `2` 只表示 checkpoint 不可继续使用，主控必须先读取命令输出 JSON 中的 `reasons[]`，不能把所有原因都当成普通状态变化：

- `snapshot_changed` 等实际输入、产物或核心 QA 快照变化：刷新 readiness、workflow state、workflow tasks、Codex handoff、agent handoff 和 adapter context。
- `checkpoint_snapshot_incomplete`、`evidence_ref_limit_exceeded`、`read_budget_exceeded`、`checkpoint_read_budget_exhausted`、reparse point 或其他 unsafe/limit 原因：fail closed，记录明确 blocker；不得反复刷新 handoff 试图绕过边界。

checkpoint 最多纳入 64 个 evidence refs，创建和校验共享 32 MiB 总读取预算；达到上限时必须阻断并缩小证据范围或由维护者修复输入结构，不能静默截断后继续。

checkpoint 只提供 `next_read_set`、`artifact_refs` 和过期快照，不负责选择或放行下一步。

`--agent` 必须与当前入口一致；不能让 Claude Code 复用 opencode checkpoint，反向也一样。

## 主控流程

opencode 和 Claude Code 是非 GUI 顶层主控，可以：

- 读取状态和 QA 报告；
- 执行 workflow policy 已授权的非 GUI Python 入口；
- 把独立 Mod、文件或资源 lane 分配给主控派生的子 agent；
- 汇总子 agent 结果；
- 串行刷新 readiness、state、tasks、handoff 和进度卡。

顶层主控不得直接领取 `qa/workflow_tasks.json` 子任务，也不得绕过 `workflow_state.json` 自行猜测下一阶段。

状态动作中的 `required_agent_capability` 是声明，不是状态刷新时的桌面探测。非 GUI handoff 发现 `gui:desktop` 时会把动作标为 `agent_capability_missing` 并设置 `handoff_target=codex`；该动作不得进入可执行 checkpoint。

运行 workflow、队列、严格门禁、健康检查、状态刷新或恢复命令后，主控必须重新读取 `.workflow/progress_card.md`，并把完整的 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]` Markdown 卡片作为用户可见正文输出。不要用脚本 stdout、trace 或自行概括的状态代替进度卡。

## 子 Agent 协议

只有主控分派的子 agent 才能领取任务：

```powershell
python scripts\claim_workflow_task.py --mod-name <ModName> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --mod-name <ModName> --resource-lock <ResourceLock> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --task-id <TaskId> --owner <SubagentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
```

子 agent 只能执行领取结果中的 `command`。并发批次结束后，由主控串行刷新全局状态；子 agent 不运行严格 QA、final_mod 组装或全局状态刷新。

正常并发使用 `workflow-subagent-orchestration`。进入 `blocked` 或 `qa_failed` 后，停止正常领取，切换到 `workflow-agent-orchestration` 处理恢复记录和安全停止。

## GUI Handoff

opencode 和 Claude Code 遇到下列步骤时必须停止：

- `gui:desktop` 锁；
- LexTranslator/xTranslator GUI 后备；
- Computer Use；
- pywinauto/UI Automation；
- 任何窗口或桌面坐标操作。

记录 `blocked` 和 `handoff_target=codex`，然后交给 Codex 继续。不要把人工操作记录成自动完成。

## 性能规则

以下辅助入口只在显式接手、手工检查或 CI 中运行，不挂到 Codex 默认流程：

- `validate_agent_capabilities.py`
- `list_agent_skills.py`
- `export_agent_context.py`
- `write_agent_handoff.py`
- `validate_claude_plugin_marketplace.py`

具体安装和配置分别见 [opencode Adapter](./opencode_adapter.md) 与 [Claude Code Adapter](./claude_code_adapter.md)。
