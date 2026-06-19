# Skyrim SE/AE Mod Agent 汉化工作流

这是一个 agent 驱动的 Skyrim SE/AE Mod 汉化工作流，本质上是由状态机约束、多个反馈循环驱动的自动化协作流程。它让 Codex 在受控项目目录内完成 Mod 输入准备、文本提取、翻译、工具写回、最终组装和 QA 检查，并把所有产物限制在当前项目内，方便回滚、复核和批量处理。

它不是 Mod 管理器，也不是自动发布工具。它不会访问真实 Skyrim、MO2、Vortex、Steam、AppData 或 `Documents/My Games` 目录；不会自动安装 Mod；不会把项目内静态 QA 当成游戏内实测。

## 项目依赖

### 必需环境

- Windows。
- Python 3。
- Codex，在本项目目录内作为汉化 agent 运行。

第一次使用先安装 Python 依赖：

```console
python -m pip install -r requirements.txt
```

当前 `requirements.txt` 只包含基础依赖：

| 依赖 | 用途 |
|---|---|
| `py7zr` | 优先处理 `.7z` 压缩包 |
| `bethesda-structs` | 只读审计 BSA/BA2 归档目录 |

### 可选工具

复杂 Mod 可能需要本机工具。工具路径写在：

```text
config/tools.local.json
```

参考模板是：

```text
config/tools.example.json
```

工具主页速查：

优先从工具作者主页、官方页面或可信项目页下载；不要从不明镜像站下载可执行文件。

| 工具 | 主页 | 本项目主要用途 |
|---|---|---|
| LexTranslator / Lexicon AI Translator | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/143056) / [GitHub](https://github.com/YD525/YDSkyrimToolR) | GUI 后备；插件、PEX、MCM 或翻译字典 |
| xTranslator | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/134) / [GitHub](https://github.com/MGuffin/xTranslator) | GUI 后备；精修、查漏、复杂导入或 PapyrusPex 后备 |
| Mutagen | [GitHub](https://github.com/Mutagen-Modding/Mutagen) | ESP/ESM/ESL 文本导出、写回和验证；PEX 可见字符串适配器 |
| .NET SDK | [Microsoft .NET 下载页](https://dotnet.microsoft.com/en-us/download) | 运行或构建 Mutagen 相关适配器 |
| SSEEdit / xEdit | [xEdit 主页](https://tes5edit.github.io/) / [GitHub](https://github.com/tes5edit/tes5edit) / [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/164) | 插件文本辅助导出、对照检查和安全 dump 包装器 |
| Champollion | [GitHub](https://github.com/Orvid/Champollion) | PEX/PSC 只读分析或后备解码 |
| bethesda-structs | [PyPI](https://pypi.org/project/bethesda-structs/) / [文档](https://bethesda-structs.readthedocs.io/) | BSA/BA2 只读归档目录读取和 manifest 证据 |
| BSAFileExtractor | [GitHub](https://github.com/Sw4T/BSAFileExtractor) | 通过项目安全包装器把 BSA 内容物化到 `work/archive_extracts/` |
| B.A.E. - Bethesda Archive Extractor | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/974) | BA2/BSA 人工提取参考；默认不作为本项目自动解包入口 |
| 7-Zip | [官方主页](https://www.7-zip.org/) | `.7z` 解包后备；首选 Python `py7zr` |
| py7zr | [PyPI](https://pypi.org/project/py7zr/) / [文档](https://py7zr.readthedocs.io/) | Python 内部 `.7z` 解包 |

检查工具配置：

```console
python scripts/detect_decoder_tools.py
```

报告会写到：

```text
qa/decoder_tools_report.md
```

### Codex 能力和增强插件

本项目可以配合 Codex 内置能力和可选插件提升 GUI 工具操作、复杂流程接手、复核和恢复能力，但这些能力不能替代项目内规则、状态机、Python 脚本或 QA 门禁。

Codex 内置能力：

| 能力 | 来源 | 适合用途 | 边界 |
|---|---|---|---|
| Computer Use | OpenAI bundled Codex 能力 | 操作 LexTranslator、xTranslator 等 GUI 工具；截图确认窗口、控件和保存位置 | 只作为 CLI/解码器不可用或必须 GUI 写回时的兜底；操作前应截图确认目标控件；输出必须保存到项目内 `tool_outputs` |
| Browser / Chrome | OpenAI bundled Codex 能力 | 查看工具主页、官方文档、下载页、问题排查资料 | 不直接改变项目输出；下载或执行外部工具前仍要遵守 `config/tools.local.json` 和项目路径边界 |

OpenAI curated/remote 能力：

| 能力 | 来源 | 适合用途 | 边界 |
|---|---|---|---|
| Data Analytics | OpenAI curated/remote Codex 能力 | 批量 Mod 队列状态、QA 通过/失败分布、blocked 原因分类、覆盖率、provenance、archive loose override 和发布前状态汇总 | 只能做项目内 QA/队列/覆盖率数据的表格、图表、报告或 dashboard 展示；不能替代 QA 脚本、状态机判定或人工游戏内测试 |

可选第三方增强插件：

| 插件 | 维护方 | 适合用途 | 边界 |
|---|---|---|---|
| AgentOps | 第三方 AgentOps / [boshu2/agentops](https://github.com/boshu2/agentops) | `qa_failed`、`blocked`、多次重试失败、严格 QA 前复核、发布前复核、批量队列诊断、多报告或 manifest 并行审计 | 只能做编排、复核、失败归因、恢复建议和接手摘要；不能直接翻译、修改二进制、绕过 Skill、绕过 QA 或覆盖 `workflow_policy.json` |

如果 Codex 决定使用 AgentOps，它应该先明确说明使用目的和边界。即使使用了 AgentOps，仍然必须刷新 `qa/translation_readiness.json`、`qa/workflow_state.json`、`qa/workflow_tasks.json` 和 `qa/codex_handoff.json`，并把恢复尝试记录到 `qa/workflow_agent_runs.jsonl`。

如果 Codex 决定使用 Data Analytics，它应该先说明读取哪些项目内报告、采用什么指标口径，以及输出是表格、图表、报告还是 dashboard。Data Analytics 的结论只用于帮助理解状态，最终是否能推进仍以项目 QA 报告、`workflow_state.json` 和人工测试为准。

如果你不确定本机是否已经装好工具或插件，可以直接让 Codex 帮你检查和配置：

```text
检查这个项目需要哪些工具还没配置
```

```text
帮我检查 LexTranslator、xTranslator、Mutagen 和 BSA 工具路径
```

```text
如果这个任务需要 AgentOps 或 Data Analytics，请帮我安装或启用
```

Codex 可以读取项目内 `config/tools.example.json` 和 `config/tools.local.json`，运行工具检测脚本，并告诉你哪些路径需要填写。对于 LexTranslator、xTranslator、7-Zip、.NET SDK 等本机程序，Codex 可以给出官方下载页和配置位置；下载安装和授权确认仍由你决定。对于 Codex 插件或连接器，Codex 会在当前环境支持时发起安装/启用请求，并说明用途和边界。

## 直接使用

普通用户只需要关心三个目录：

```text
mod/    放待汉化 Mod
out/    查看汉化输出
qa/     查看状态、阻断原因和检查报告
```

### 1. 放入 Mod

把要汉化的 Mod 压缩包或文件夹放进：

```text
mod/
```

`mod/` 是项目内沙盒副本，不是真实游戏目录。不要把真实 MO2/Vortex 目录当作输入。

### 2. 让 Codex 处理

在 Codex 里输入：

```text
翻译 mod
```

如果 `mod/` 里有多个 Mod，可以指定名称：

```text
翻译 <ModName> 这个 mod
```

Codex 会按项目规则扫描、解包、路由、翻译、组装和检查。遇到不能安全自动完成的步骤，会标记为 `blocked` 并说明原因。

### 3. 查看输出

每个 Mod 的交付目录是：

```text
out/<ModName>/汉化产出/
```

常见内容：

| 路径 | 用途 |
|---|---|
| `final_mod/` | 完整汉化 Mod 目录，适合人工检查文件结构 |
| `<ModName>_CHS.zip` | 打包好的汉化包，适合手动导入 MO2/Vortex 测试 |
| `intermediate/` | 中间产物，一般不用看 |
| `package_report.md` | 打包记录 |

### 4. 手动进游戏测试

项目内 QA 通过只表示：

```text
项目内的翻译、组装、来源追踪和静态检查允许进入人工游戏测试。
```

它不表示已经在游戏里通过。Skyrim 的真实加载顺序、脚本触发、MCM 注册、任务/对话显示和 Mod 冲突仍然需要你在自己的游戏环境里测试。

## Codex 会做什么

- 只读取当前项目内的 `mod/` 输入。
- 把工作产物写入 `work/`、`source/`、`translated/`、`out/` 和 `qa/`。
- 判断文件类型和风险，优先走文本管线和 CLI/库解码器。
- 保护 FormID、EditorID、脚本名、变量名、路径、文件名、JSON key、XML tag 和占位符。
- 对需要二进制写回的插件或 PEX，只调用受控工具生成项目内副本。
- 组装 `final_mod/` 和 `_CHS.zip`。
- 写入 QA、状态、来源追踪和阻断报告。

## Codex 不会做什么

- 不访问真实 Skyrim、MO2、Vortex、Steam、AppData 或 `Documents/My Games`。
- 不自动安装或启用 Mod。
- 不直接修改原始 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`、`.dll`、`.exe`。
- 不直接修改 `.psc` 源码并重新编译。
- 不覆盖 `mod/` 下的原始输入。
- 不把 GUI 工具“已打开”说成“已保存完成”。
- 不把项目内 QA 结果当成游戏内实测结果。

## 如果 Codex 说 blocked

`blocked` 是安全暂停，不是失败。它表示当前步骤缺少足够证据，继续自动推进可能损坏输出或伪造完成状态。

常见原因：

- 缺少本机工具路径。
- 压缩包或归档暂时没有可用安全解包器。
- GUI 工具无法自动保存到项目内目录。
- 插件或 PEX 文本风险较高，需要人工确认。
- QA 发现漏译、占位符损坏、结构错误、来源缺失或输出不一致。
- 已经到达需要人工游戏测试的阶段。

你可以直接问：

```text
说明现在卡在哪里
```

或：

```text
继续处理 blocked 的问题
```

普通用户不需要自己判断内部状态文件。Codex 会读取项目内报告并说明下一步。

## 判断能不能测试

优先看：

```text
qa/translation_goal_compliance.md
qa/workflow_state.md
qa/translation_readiness.md
```

如果状态是 `ready_for_manual_test`，说明可以拿 `_CHS.zip` 或 `final_mod/` 去你的 MO2/Vortex 里手动测试。

生成测试计划：

```console
python scripts/new_manual_game_test_plan.py --mod-name "<ModName>"
```

生成测试结果模板：

```console
python scripts/new_manual_game_test_results_template.py --mod-name "<ModName>"
```

## 常用对话

```text
现在这个项目应该怎么继续？
```

```text
检查工具配置有没有问题
```

```text
翻译 mod，如果遇到问题就记下来
```

```text
检查现在有哪些 mod 已经 ready
```

```text
重新跑 <ModName> 的 QA
```

```text
说明 <ModName> 能不能进游戏测试
```

```text
继续处理 blocked 的问题
```

## 手动命令速查

普通用户可以跳过这一节，直接让 Codex 执行。这里的命令只给想自己触发流程的人。

准备 `mod/` 输入队列：

```console
python scripts/run_translation_queue.py --mode prepare
```

运行某个 Mod 的非 GUI 主流程：

```console
python scripts/run_non_gui_translation_workflow.py --mod-name "<ModName>"
```

刷新用户可读状态报告：

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
```

运行严格 QA 门禁：

```console
python scripts/run_non_gui_qa_gates.py --mod-name "<ModName>" --strict-complete
```

同一时间不要并行跑多个主流程、严格门禁或状态刷新入口。项目会使用 `work/.workflow.lock` 避免报告和输出互相覆盖。

## 输出目录速查

```text
mod/                         待汉化 Mod 输入
work/                        解包和临时工作区
source/                      提取出的源文本
translated/                  翻译后的中间文本和 overlay
out/<ModName>/汉化产出/       final_mod 和 _CHS.zip
qa/                          检查报告、状态报告、问题记录
glossary/                    术语表和 LexTranslator 风格动态词典
config/                      工具路径和流程配置
tools/                       项目内工具依赖
docs/                        设计说明和维护文档
scripts/                     Python 自动化脚本
```

## 文档入口

| 想了解 | 文档 |
|---|---|
| 普通使用 | 本 README |
| 开发者指南、状态机和二次开发 | `developer_guide.md` |
| 非 GUI 优先工作流 | `docs/decoder_first_workflow.md` |
| final_mod 输出结构 | `docs/final_mod_output.md` |
| 工具适配器和本地工具配置 | `docs/tool_adapter.md` |
| GUI 操作边界 | `docs/gui_automation_rules.md` |
| 翻译规则 | `docs/translation_rules.md` |
| 模型校对和严格 QA | `docs/translation_proofreading_workflow.md` |

Agent/Codex 接手文档单独放在 `docs/codex_workflow.md`，普通用户不需要阅读。

## 发布前提醒

公开发布或长期使用前，你还需要确认：

- Mod 作者是否允许翻译和再发布。
- Nexus 或其他平台的发布规则。
- 你的真实游戏环境是否有冲突。
- MCM、脚本、任务、对话、菜单和提示是否在游戏内正常显示。
- `_CHS.zip` 是否对应最新 `final_mod/` 和 QA 报告。

没有人工游戏测试，不建议公开发布。
