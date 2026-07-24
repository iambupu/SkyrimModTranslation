# opencode Adapter

opencode 是 Windows 上的非 GUI 顶层 adapter。它可以安装本地配置和发现
本仓库的非 GUI Skills，但实际翻译推进与其他入口共享唯一公开
`smt.py` 控制器。

## 配置

仅在用户明确要求安装或刷新 opencode 适配器时，从插件源运行：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --game skyrim-se
```

Fallout 4 使用 `--game fallout4`。没有有效 marker 且用户未说明游戏时，
先用自然语言询问；不能猜测。`uv run` 只是可选启动方式。

初始化器增量保留已有 `opencode.json` 与 `.opencode/AGENTS.md` 用户内容，
并生成环境注入、本地命令和指向插件源 `skills/` 的轻量发现文件。它不会
复制运行 Skill 正文，也不会增加 GUI 能力。

## 使用

顶层 opencode 遵循
[Non-GUI Agent Workflow](./agent_workflow.md)，只调用公开
`smt.py --format json run|status|resume|doctor|output`。它不直接读取
workflow state/tasks 选择底层命令，也不领取子任务。

生成的 `/skyrim-chs-status` 和 `/skyrim-chs-resume` 分别调用公开
`status` 与 `resume`。本地 context/handoff 文件只用于显式适配器诊断，
不能覆盖公开命令结果。

## 验证

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
```

遇到 `needs_gui`、LexTranslator/xTranslator、Computer Use、
pywinauto/UI Automation 或 `gui:desktop` 时，安全停止并交给 Codex。
