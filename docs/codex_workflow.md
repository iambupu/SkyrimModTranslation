# Codex Workflow

本页说明 Codex 如何使用 SkyrimModTranslation。普通用户先看
[README](../README.md)；开发者维护内部状态机时看
[developer_guide](../developer_guide.md)。

## 唯一公开入口

用户说“翻译这个 Mod”时，Codex 首次只调用：

```powershell
python scripts\smt.py --format json run <Mod路径> --game <game-id>
```

如果用户尚未说明游戏且没有有效工作区 marker，先用自然语言询问并等待
确认。新工作区没有默认游戏，不能根据 Mod 名、压缩包名或目录名猜测。

后续只使用：

```powershell
python scripts\smt.py --format json status
python scripts\smt.py --format json resume
python scripts\smt.py --format json doctor
python scripts\smt.py --format json output
```

Codex 必须读取 JSON 的 `outcome`、`workspace`、`mod_name`、`game_id`、
`workflow_state`、`next_action.kind`、`next_action.summary`、
`next_action.artifacts`、`progress_card` 和 `diagnostics`。不得仅凭退出码
判断流程失败。

## 继续动作

- `needs_agent_translation`：只处理 `next_action.artifacts` 指定的工作区内
  文本或审阅材料，完成后调用 `resume`。
- `needs_gui`：只有 Codex 可以按已授权动作进入 GUI/Computer Use 后备，
  完成后调用 `resume`。
- `needs_user_input`：向用户取得明确输入，再调用 `resume`。
- `ready_for_manual_test`：明确说明仍需游戏内测试，不能宣称正式完成。
- `completed`：必须来自有效的 `manual_tested` 状态投影。

询问进度时先调用公开 `status`，直接渲染返回的 `progress_card`。不要直接
读取 `.workflow/progress_card.*`、重新扫描项目或用 trace 猜测进度。

## 内部实现边界

公开 CLI 内部和已选中的运行期 Skill 可以读取
`qa/workflow_state.json`、`qa/workflow_tasks.json`、
`qa/codex_handoff.json`、readiness 与 workflow policy，并执行精确的低
风险任务。顶层 Codex 不自行组合 `init_workspace.py`、
`resume_workflow.py`、状态刷新、严格 QA 或 final_mod 脚本。

只有 Codex 明确派生的子智能体可以按
`workflow-subagent-orchestration` 领取并回写并行 lane。顶层 Codex
不把自己当成子任务执行器。失败恢复由
`workflow-agent-orchestration` 在现有状态机和 QA 证据内完成。

## 能力边界

Codex 是唯一可以处理 `gui:desktop`、LexTranslator/xTranslator 桌面
后备、Computer Use 和 pywinauto/UI Automation 的顶层 adapter。任何 GUI
动作仍须使用工作区内输入输出、受控 adapter、日志和 QA 证据；不能直接
修改真实游戏、MO2/Vortex 或原始 Mod 二进制。

共享工具由 `--tool-setup auto` 发布或复用到 Windows Local AppData 的
版本化托管仓库，工作区只保存派生绑定。缓存清理与完整卸载只在用户明确
要求时使用 `managed-tool-cache-maintenance`，不属于 `smt.py` 翻译子命令。
