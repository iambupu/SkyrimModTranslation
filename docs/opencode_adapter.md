# opencode Adapter

本页供 Agent 配置或接手 opencode 非 GUI 顶层入口。opencode 可以读取工作区状态、运行项目 Python 脚本并写 QA 报告；不能操作桌面窗口，也不能替 Codex 执行 GUI 后备。

## 触发条件

仅在以下情况使用本页：

- 用户明确要求使用或初始化 opencode；
- 已有 opencode 工作区需要刷新配置、上下文或 handoff；
- 需要诊断 opencode 本地插件或 Skill 发现结果。

普通汉化推进使用 [Non-GUI Agent Workflow](./agent_workflow.md)，不要重复初始化已有工作区。

## 前置检查

1. 确认命令从插件源仓库运行。
2. 确认目标是不存在的路径、插件仓库外的空目录，或已有有效 marker 的工作区。
3. 新工作区必须由用户明确选择游戏；未指定时先用自然语言询问，不能根据 Mod 名猜测。
4. 已有工作区从 `.skyrim-chs-workspace.json` 读取插件源路径和游戏身份。

## 初始化动作

用户确认 Skyrim SE/AE 后：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --game skyrim-se
```

用户确认 Fallout 4 Experimental 后，将游戏参数改为 `--game fallout4`。没有 uv 时使用标准 Python：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --game skyrim-se
```

只生成配置和上下文，不启动 TUI：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --game skyrim-se --no-launch
```

非交互执行一次：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --game skyrim-se --launch-mode run --auto
```

`uv` 只是可选启动方式。缺失或失败时必须回退到 `python`、`venv` 和 `pip`。

## 配置合同

初始化器必须增量保留已有 `opencode.json` 字段和 `.opencode/AGENTS.md` 用户内容，并生成：

- `.opencode/plugins/skyrim-chs.js`：只注入 `SKYRIM_CHS_PLUGIN_ROOT`、`SKYRIM_CHS_WORKSPACE_ROOT`、`OPENCODE_CONFIG_DIR` 和恢复提示；
- `.opencode/skills/<SkillName>/SKILL.md`：只提供 Skill 发现指针，不复制第二套正文；
- `qa/agent_context_prompts/latest.opencode.context.md`；
- readiness、workflow state、workflow tasks、Codex handoff 和 agent handoff。

本地插件不会提供 GUI、Computer Use 或 Codex 插件能力。

## 验证证据

从插件源运行：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
```

Agent 只有在配置文件、context、handoff 和 Skill 发现结果均存在且校验通过后，才能报告初始化成功。

## 断点恢复

通用 checkpoint 检查、环境变量和接手顺序见 [Non-GUI Agent Workflow 的断点恢复](./agent_workflow.md#断点恢复)。opencode 发现 checkpoint 过期时，重新运行 `init_opencode.py <Workspace> --no-launch`，由初始化器刷新状态链、agent handoff 和 opencode context；不要在本页维护另一套手工导出流程。

## 停止条件

- 目标游戏未确认或 marker 冲突；
- 工作区路径不合规；
- 本地插件、Skill 指针、context 或 handoff 生成失败；
- capability 校验失败；
- 当前任务需要 LexTranslator、xTranslator、Computer Use 或其他桌面 GUI。

GUI 任务记录 `blocked` 和 `handoff_target=codex`。opencode 顶层入口不领取 `qa/workflow_tasks.json` 子任务；领取只属于主控分派的子 Agent。
