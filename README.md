# Skyrim SE/AE Mod Agent 汉化工作流

| ![Skyrim Mod CHS Translation logo](./logo.png) |
|:--:|

这是一个面向 Windows 环境的 Skyrim Mod 简体中文汉化 agent 工作流工程。它继续完整支持 Codex 插件调用，同时把 opencode 和 Claude Code 作为完整非 GUI adapter 接入同一套 Python 状态机、受控工具适配、工作区状态和 QA 报告。

项目目标是让 Skyrim Mod 汉化过程更可维护、可复核、可回滚：每个实际 Mod 汉化任务都在独立工作区中运行，输入、翻译中间件、工具输出、最终交付包和检查报告都保留在项目目录内，避免直接触碰真实游戏目录或 Mod 管理器目录。

## 项目解决什么问题

Skyrim Mod 汉化经常同时涉及插件文本、MCM、Interface 翻译文件、PEX 可见字符串、BSA/BA2 归档、JSON/XML/CSV/TXT 资源以及人工游戏内测试。本项目把这些步骤拆成可追踪的工程流程：

- 识别 Mod 输入和文件类型。
- 提取可翻译文本并保护 FormID、EditorID、脚本名、变量名、路径、文件名、结构 key 和占位符。
- 使用 agent 模型进行语义翻译和模型校对；Codex 是默认完整模型入口。
- 通过受控工具在工作区内生成插件或 PEX 副本，不直接改原始二进制。
- 组装 Skyrim Data 根结构的 `final_mod/` 和可测试的 `_CHS.zip`。
- 生成 QA、来源追踪、覆盖率和阻断报告，判断是否可以进入人工游戏测试。

## 核心设计

项目分为两类目录：

| 类型 | 说明 |
|---|---|
| 插件源仓库 | 保存插件元数据、Skills、Python 脚本、适配器源码、配置模板、文档和 QA 规则 |
| 汉化工作区 | 保存当前 Mod 输入、工具配置、术语表、中间产物、QA 报告和最终输出 |

插件源仓库不应该直接当作某个 Mod 的运行目录。实际汉化任务应初始化到插件仓库外部的独立工作区中。

## 多 Agent 与并发

工作流支持 Codex Plugin、opencode 和 Claude Code 三个一等入口。Codex 是默认插件入口，并且独占 GUI/Computer Use 能力；opencode 和 Claude Code 完整支持非 GUI workflow，遇到 GUI-only 步骤时必须 blocked 并 handoff 给 Codex。Gemini CLI 没有支持计划。

| Adapter | 支持级别 | 定位 |
|---|---|---|
| Codex Plugin | 完整支持，含 GUI | 默认插件入口、主控、GUI fallback |
| opencode | 完整非 GUI 支持 | CLI agent / 非 GUI 主控 |
| Claude Code | 完整非 GUI 支持，含 Claude Code marketplace | Claude Code plugin / CLI agent / 非 GUI 主控 |

工作流支持把可独立执行的低风险任务拆给多个子智能体。并发只来自 `qa/workflow_tasks.json` 这个派生任务视图，不能由人工直接编辑任务文件或绕过状态机。

| 并发范围 | 适合任务 | 边界 |
|---|---|---|
| 多 Mod lane | 不同 Mod 的准备、审计、候选抽取、低风险报告生成 | `resource_locks` 不重叠，且 `can_run_parallel=true` |
| 单个大型 Mod 的文件/资源 lane | 不同文本文件的解析、翻译分片、只读审计、模型校对分片 | 使用 `file:<ModName>:...` 或 `resource:<ModName>:...` 锁；同一文件 lane 串行 |
| 主控串行阶段 | 状态刷新、严格 QA、final_mod 组装、GUI 自动化、共享 glossary/RAG 重建 | 由主控统一运行，不分派给子智能体并发执行 |

主控智能体负责刷新 `workflow_state`、生成 `workflow_tasks`、分配 lane、汇总结果并重新输出进度卡。子智能体只能领取任务、执行返回的 `command`，再通过领取协议回写完成状态。

子智能体领取任务使用插件源脚本：

```powershell
python scripts\claim_workflow_task.py --mod-name <ModName> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --task-id <TaskId> --owner <SubagentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
```

opencode 和 Claude Code 是完整非 GUI adapter，不是子任务执行器；它们不直接领取 `workflow_tasks`。配置和边界见 [docs/agent_adapters.md](./docs/agent_adapters.md) 与 [docs/agent_compatibility.md](./docs/agent_compatibility.md)。

效率提升取决于可拆分任务占比。多个小 Mod 或大型 Mod 的多个独立文本文件越多，吞吐越接近 `max-workers`；但 final_mod 组装、严格 QA、GUI 自动化和状态刷新仍是串行瓶颈，不能承诺全流程线性提速。

## 交付物

每个完成到可测试阶段的 Mod 会在工作区内生成：

```text
out/<ModName>/汉化产出/
```

主要内容：

| 输出 | 用途 |
|---|---|
| `final_mod/` | 完整 Skyrim Mod Data 根结构，便于人工检查 |
| `<ModName>_CHS.zip` | 打包好的汉化包，便于手动导入 MO2/Vortex 测试 |
| `intermediate/` | 工具输出、overlay、patch、审计等中间产物 |

工作区还会生成这些状态和排查入口：

| 输出 | 用途 |
|---|---|
| `qa/` | 状态、检查、阻断原因和人工测试辅助报告 |
| `.workflow/` | 用户可见进度卡和结构化进度状态 |
| `traces/` | 本地执行追踪和开发者排查摘要 |

项目内 QA 通过只表示可以进入人工游戏测试，不表示已经在真实游戏中验证通过。

## 适合谁使用

用户指南全部放在根目录：

| 读者 | 目标 | 入口 |
|---|---|---|
| 普通用户 | 把 Mod 放进工作区，拿到可测试的 `_CHS.zip` | [USER_GUIDE.md](./USER_GUIDE.md) |
| 高级用户 | 配置工具、理解暂停原因、查看 QA、判断能否测试 | [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md) |
| 开发者用户 | 维护插件、脚本、Skills、适配器和 QA 门禁 | [developer_guide.md](./developer_guide.md) |

普通用户不需要阅读 `docs/`、`scripts/`、`skills/`、`adapters/` 或 `.codex-plugin/`。这些目录主要给高级排查和开发维护使用。

## 最短入口

如果你只是想开始一次汉化，先看 [USER_GUIDE.md](./USER_GUIDE.md)。最短流程是：

```text
安装插件 -> 创建工作区 -> 把 Mod 放进 mod/ -> 让 agent 翻译 mod -> 查看 out/<ModName>/汉化产出/
```

## Codex repo marketplace 安装

从 GitHub `master` 分支安装：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

查看、刷新或卸载：

```powershell
codex plugin list --marketplace skyrim-mod-chs --available --json
codex plugin marketplace upgrade skyrim-mod-chs
codex plugin remove skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

## Claude Code marketplace 安装

Claude Code 使用自己的 marketplace 和 `/plugin` 命令，不复用 Codex marketplace：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

本仓库的 Claude Code marketplace 只暴露非 GUI Skills。LexTranslator/xTranslator GUI、Computer Use 和桌面自动化仍然是 Codex-only。维护说明见 [docs/claude_code_marketplace.md](./docs/claude_code_marketplace.md)。

## opencode CLI 使用

opencode 没有单独的 marketplace 安装流程。本项目把 opencode 当作完整非 GUI 顶层 adapter：它读取同一套工作区状态、root `skills/`、Python workflow 脚本和 QA 报告，但不执行 GUI、Computer Use、LexTranslator/xTranslator 桌面自动化，也不直接领取 `qa/workflow_tasks.json` 子任务。

一键初始化并启动 opencode：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

没有安装 uv 时继续使用 Python：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

该入口会在目标工作区缺失时先调用 `init_workspace.py` 创建工作区，然后写入 `opencode.json`、`.opencode/AGENTS.md`、`.opencode/agents/skyrim-chs.md`、`.opencode/commands/skyrim-chs-*.md` 和 `.opencode/skyrim-chs-opencode.json`，刷新 agent handoff，并导出 `qa/agent_context_prompts/latest.opencode.context.md`。默认会启动 opencode TUI；只生成配置和上下文时使用：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --no-launch
```

如果要让 opencode 执行一次非交互 run，而不是进入 TUI：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --launch-mode run --auto
```

`uv` 只是易用性增强；所有入口仍保留 `python ...` 形式，且 Codex 默认翻译热路径不会依赖 uv。工作区自动工具准备在检测到 uv 时会优先用 `uv venv` 和 `uv pip install` 创建 `tools/python-venv/`，检测不到或失败时回退到 `venv + pip`。仓库根目录的 `uv.lock` 是受跟踪的依赖锁文件，用于让 `uv run` 在不同机器上更稳定。

在插件源仓库中也可单独检查 opencode adapter 能力和可见 Skill：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
```

底层手工流程是先把插件源和工作区路径显式传给 Python 入口，再导出 opencode 的低上下文接手包：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent opencode --output qa/agent_context_prompts/latest.opencode.context.md
```

然后在 opencode 中使用生成的 `qa/agent_context_prompts/latest.opencode.context.md` 作为接手上下文，并遵守 `agents/opencode/prompt.md` 的边界。opencode 应优先读取 `qa/agent_handoff.json`，缺失时再读 `qa/codex_handoff.json`；如果下一步需要 GUI-only 能力，必须记录 blocked，并设置 `handoff_target=codex`。详细说明见 [docs/opencode_adapter.md](./docs/opencode_adapter.md) 和 [docs/agent_adapters.md](./docs/agent_adapters.md)。

## 安全边界

- 只读取当前工作区内的 `mod/` 输入。
- 只把产物写入工作区内的 `work/`、`source/`、`translated/`、`out/`、`qa/`、`.workflow/` 和 `traces/`。
- 不访问真实 Skyrim、MO2、Vortex、Steam、AppData 或 `Documents/My Games` 目录。
- 不自动安装或启用 Mod。
- 不直接修改原始 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`、`.dll`、`.exe`。
- 需要二进制写回时，只能通过受控工具在工作区内生成副本。

## CI checks

GitHub Actions 当前在 push / pull request 上只做仓库级基础检查：repository structure、plugin manifest、skills metadata、workflow policy references、Python compile、workflow task parallelism unittest，以及 Windows repo smoke check。手动 `workflow_dispatch` 可运行第一版 effect-regression fixture。CI 入口不会启动 GUI 工具，不读取真实 Skyrim、MO2、Vortex、Steam、AppData 或用户 Documents 目录，也不会调用外部翻译 API。

当前 CI 不覆盖 GUI tools、in-game validation、real mod translation quality 或 external translator APIs。真实游戏加载、MCM 显示、任务/对话/菜单效果和翻译质量仍需要工作区 QA 与人工游戏内测试确认。

全自动项目内效果回归验证的分层设计见 [docs/effect_regression_workflow.md](./docs/effect_regression_workflow.md)。该流程只证明可控 fixture、final_mod、QA 报告链和进度状态没有回归，不替代真实游戏测试。当前第一版可通过 `python scripts/run_effect_regression.py --all --ci` 本地运行，也可在 GitHub Actions `workflow_dispatch` 中手动触发。

## 当前限制

复杂 Mod 可能需要额外工具路径、GUI 工具保存确认、人工审查或游戏内测试。尤其是 ESP/ESM/ESL、PEX、MCM、BSA/BA2 和 GUI 写回场景，是否能自动推进取决于当前工作区的工具配置、输入结构和 QA 结果。

公开发布或长期使用前，还需要确认 Mod 作者授权、平台规则、真实加载环境、MCM 注册、任务/对话/菜单/提示显示，以及 `_CHS.zip` 是否对应最新 QA 报告。没有人工游戏测试，不建议公开发布。

## 仓库地址

- Gitee: [https://gitee.com/iambupu/SkyrimModTranslation](https://gitee.com/iambupu/SkyrimModTranslation)，方便中国用户访问和使用
- GitHub: [https://github.com/iambupu/SkyrimModTranslation](https://github.com/iambupu/SkyrimModTranslation)

## 简单依赖声明

本项目面向 Windows 环境，需要 Python 3 和至少一个受支持 agent 入口；推荐安装 uv 以获得更简单的 `uv run` 启动方式和更快的工作区依赖准备。Codex 提供完整插件和 GUI fallback；opencode 与 Claude Code 支持非 GUI workflow。普通文本、压缩包和部分归档可以走项目内自动流程；复杂 ESP/ESM/ESL、PEX、MCM、BSA/BA2 或 GUI 写回场景，可能需要用户自行安装并配置 LexTranslator、xTranslator、.NET SDK、SSEEdit/xEdit、BSAFileExtractor、B.A.E. 或 7-Zip 等外部工具。具体工具是否必需，以当前工作区的检测报告和 agent 提示为准。
