# Non-GUI Agent Workflow

本页定义 opencode 和 Claude Code 的公开非 GUI 使用合同。它们与 Codex
共享同一个 `smt.py` 控制器、工作区状态机和 QA 门禁，不建立第二套顶层
推进流程。

## 顶层入口

首次翻译只运行：

```powershell
python scripts\smt.py --format json run <Mod路径> --game <game-id>
```

后续只运行：

```powershell
python scripts\smt.py --format json status
python scripts\smt.py --format json resume
python scripts\smt.py --format json doctor
python scripts\smt.py --format json output
```

顶层 adapter 读取单个 JSON 结果中的 `outcome`、`workspace`、`mod_name`、
`game_id`、`workflow_state`、`next_action`、`progress_card` 和
`diagnostics`。非零退出码可以表示预期暂停，不能单独解释为底层失败。

顶层 adapter 不直接读取 `qa/agent_handoff.json`、`qa/workflow_state.json`
或 `qa/workflow_tasks.json` 来选择命令，也不自行调用初始化、刷新、任务
领取、恢复、严格 QA 或 final_mod 底层脚本。公开 CLI 内部和被状态机授权
的运行期 Skill 可以读取这些文件；它们仍是内部权威证据，而不是第二个
用户入口。

## Agent 动作

当 `outcome=needs_agent_translation` 时，只处理
`next_action.artifacts` 指向的工作区内文本或审阅材料，完成后调用
`resume`。当结果需要用户输入时，取得明确答案后再调用 `resume`。

opencode 和 Claude Code 没有 GUI 能力。遇到 `needs_gui`、`gui:desktop`、
LexTranslator/xTranslator、Computer Use、pywinauto 或 UI Automation 时，
必须安全停止并交给 Codex，不能把人工操作记录成自动完成。

进度查询只使用公开 `status` 返回的 `progress_card`。不要直接读取
`.workflow/progress_card.*`，不要用 trace 或自行概括的状态代替公开卡片。

## 内部并发边界

只有顶层主控明确派生的子智能体，才可以按
`workflow-subagent-orchestration` 领取 `qa/workflow_tasks.json` 中的并行
lane。领取、执行和完成回写都属于内部运行协议，不是 opencode 或 Claude
Code 顶层入口的常规命令。

进入 `blocked` 或 `qa_failed` 后，内部实现使用
`workflow-agent-orchestration` 记录恢复尝试和安全停止。所有内部动作仍
受 `workflow_policy.json`、资源锁、二进制边界和严格 QA 约束。

## Adapter 配置

opencode 的本地插件和 Claude Code marketplace 只提供环境、Skill 发现和
非 GUI 能力声明；它们不能改变公开 CLI、状态机或 GUI 边界。安装与验证
分别见 [opencode Adapter](./opencode_adapter.md) 和
[Claude Code Adapter](./claude_code_adapter.md)。
