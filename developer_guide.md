# 开发者指南

本文面向维护者和有技术经验的开发者，说明这个 Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化 Codex 插件如何维护、扩展和验证。

普通用户看 [USER_GUIDE.md](./USER_GUIDE.md)。高级用户看 [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md)。Codex/agent 接手流程见 [docs/codex_workflow.md](./docs/codex_workflow.md)。

## 怎么读

| 你要做什么 | 先看 |
|---|---|
| 理解整体架构 | 核心模型、仓库和工作区 |
| 改初始化或安装 | 安装和初始化入口、工具准备契约 |
| 改翻译流程 | 状态机、主流程入口、文件类型路由 |
| 新增文件类型 | 新增文件类型检查表 |
| 新增工具或 adapter | 工具 adapter 检查表 |
| 改 QA 或 ready 判定 | QA 和模型校对契约、验证清单 |
| 设计自动回归验证 | [docs/effect_regression_workflow.md](./docs/effect_regression_workflow.md) |
| 打源码包 | 发布工程源码包 |

## 核心模型

这个项目不是“直接改 Mod 文件”的脚本集合，而是一个受状态机约束的 Codex 插件工程。

核心分工：

| 层 | 负责 | 不负责 |
|---|---|---|
| Codex 模型 | 编排、翻译、解释阻断、模型校对 | 绕过证据、伪造完成、直接改二进制 |
| Skills | 告诉 Codex 任务边界、路由规则、文件类型规则 | 替代状态机或 Python 入口 |
| Python 脚本 | 解包、抽取、转换、调用受控工具、组装、写 QA 报告 | 做语义质量最终判断 |
| 状态机 | 记录阶段、阻断项、允许动作、下一步建议 | 直接执行翻译或工具操作 |
| QA 门禁 | 判断是否允许推进或人工测试 | 替代真实游戏测试 |

基本原则：

- 所有输入、输出、工具产物和报告都留在项目或工作区目录内。
- 文本管线优先，CLI/库解码器优先，GUI 只作为后备。
- 二进制只能由受控工具生成工作区内副本，Codex 不直接修改。
- `final_mod/` 必须是 Skyrim Data 根结构，`_CHS.zip` 是交付包。
- 项目内 QA 通过只表示可以进入人工游戏测试。

## 仓库和工作区

本仓库是插件源仓库，不是某个 Mod 的运行目录。

| 位置 | 内容 |
|---|---|
| 插件源仓库 | `.codex-plugin/`、`skills/`、`.codex/skills/`、`scripts/`、`adapters/`、`docs/`、配置模板、开发文档 |
| 汉化工作区 | `mod/`、`work/`、`source/`、`translated/`、`out/`、`qa/`、`glossary/`、`.workflow/`、`traces/`、`config/tools.local.json`、`.skyrim-chs-workspace.json` |

维护边界：

- 不要把 `scripts/`、`skills/`、`adapters/`、`.codex-plugin/` 或完整文档树复制进工作区。
- `glossary/` 可以作为工作区可编辑种子复制。
- 工作区命令应由插件源仓库脚本执行，并把输出写回工作区。
- `config/tools.local.json` 是本机配置，不应提交。

## 安装和初始化入口

这些入口只负责安装插件或创建工作区，不属于单个 Mod 的翻译阶段。

| 脚本 | 用途 |
|---|---|
| `scripts/install_codex_plugin.py` | 安装或刷新 Codex 插件入口，写入个人 marketplace，并在可用时调用 Codex CLI |
| `scripts/init_workspace.py` | 创建独立空工作区，写入 marker、目录、工具配置和初始报告 |
| `scripts/setup_workspace_tools.py` | 在工作区内准备工具；`auto` 安装安全非 GUI 工具，`manual` 只写报告 |

初始化目标必须是插件仓库外部的不存在路径或空目录。脚本必须拒绝：

- 插件仓库本身。
- 插件仓库内部目录。
- 已有文件。
- 非空目录。

工具准备模式：

| 模式 | 行为 |
|---|---|
| `--tool-setup auto` | 自动准备安全非 GUI 工具和依赖 |
| `--tool-setup manual` | 不下载工具，只写检测报告和人工配置提示 |
| `--tool-setup skip` | 只创建基础结构 |
| 默认 `ask` | 交互询问；非交互环境自动落到 `manual` |

`auto` 模式只能安装或准备项目可控的非 GUI 路径，例如工作区 Python 依赖、固定版本 .NET SDK、校验过的 BSAFileExtractor、Champollion 源码和 Mutagen adapter 构建。LexTranslator、xTranslator、SSEEdit/xEdit、B.A.E.、7-Zip 等 GUI 或系统级工具不得静默安装。

相关 Skill 同步点：

| 改动 | 同步文件 |
|---|---|
| 插件安装入口 | `.codex/skills/skyrim-mod-chs-install/SKILL.md` |
| 工作区使用入口 | `.codex/skills/skyrim-mod-chs-usage/SKILL.md` |
| 工作区工具准备 | `skills/workspace-tool-setup/SKILL.md` |
| 自然语言示例 | `README.md`、`USER_GUIDE.md`、`ADVANCED_USER_GUIDE.md` |

## 状态机

默认状态顺序来自 `config/workflow_policy.json`：

```text
discovered
-> extracted
-> routed
-> candidates_extracted
-> translated
-> tool_outputs_generated
-> final_mod_built
-> packaged
-> qa_passed
-> ready_for_manual_test
-> manual_tested
```

暂停或失败状态：

```text
needs_input
blocked
qa_failed
```

关键文件：

| 文件 | 用途 |
|---|---|
| `config/workflow_policy.json` | 状态顺序、允许入口、GUI fallback、恢复策略 |
| `config/workflow_state.schema.json` | 状态 JSON 结构契约 |
| `qa/workflow_state.json` | 机器可读权威状态 |
| `qa/workflow_state.md` | 人可读状态摘要 |
| `.workflow/progress_card.md` | 用户可见进度卡 |
| `.workflow/progress_card.json` | 结构化进度卡 |
| `.workflow/progress_events.jsonl` | 用户可见进度事件历史 |
| `.workflow/workflow_state.json` | 给进度卡消费的简化状态事实 |
| `qa/workflow_timeline.md` | 主阶段时间线 |
| `qa/blockers.md` | 当前阻断和下一步说明 |
| `traces/latest.jsonl` | 本地详细执行 trace |
| `traces/trace_summary.md` | 开发者排查摘要 |
| `qa/translation_readiness.json` | 项目级 ready 判断 |
| `qa/workflow_tasks.json` | 从状态派生的任务视图 |

不变量：

- `next_command` 不得指向未授权脚本。
- 可执行脚本必须来自 `workflow_policy.json` 授权面。
- 严格 QA 未过、provenance 缺失、覆盖率缺失、模型审读过期时，不得进入 `ready_for_manual_test`。
- `ready_for_manual_test` 不等于人工测试通过。

## 主流程入口

常用入口：

| 脚本 | 用途 |
|---|---|
| `scripts/run_translation_queue.py --mode prepare` | 准备 `mod/` 输入队列 |
| `scripts/run_non_gui_translation_workflow.py --mod-name "<ModName>"` | 单个 Mod 的常规非 GUI 主流程 |
| `scripts/run_non_gui_qa_gates.py --mod-name "<ModName>" --strict-complete` | 严格 QA 门禁 |
| `scripts/test_workflow_health.py` | 工作流健康检查 |

常用刷新：

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

`scripts/write_workflow_state.py` 会同时派生 `.workflow/progress_card.*`、`.workflow/progress_events.jsonl`、`.workflow/workflow_state.json`、`qa/workflow_timeline.md` 和 `qa/blockers.md`。用户进度只能从这些进度卡文件转述，不能从脚本 stdout 或 trace 推断。

同一时间不要并行跑多个主流程、严格门禁或状态刷新入口。项目使用 `work/.workflow.lock` 避免报告和输出互相覆盖。

## 文件类型路由

所有文件处理前都应通过 `translation-task-router` 或对应 Python 路由入口确认风险、工具和输出位置。

| 文件类型 | 默认策略 |
|---|---|
| Interface translations | 文本管线，保留 key、tab、行数和占位符 |
| MCM JSON/INI | 结构化文本管线，只翻译玩家可见字段 |
| JSON/XML/CSV/TXT/MD | 结构化解析，保护 key、tag、path 和占位符 |
| ESP/ESM/ESL | 导出文本，翻译中间表，由受控 Mutagen/xEdit adapter 写回工作区副本 |
| PEX | 优先 PEX adapter 导出可见字符串；写回只允许受控工具处理工作区内 PEX 副本 |
| PSC | 只读提取候选，不回写、不编译 |
| BSA/BA2 | 首选只读审计；BSA 可通过安全 wrapper 解包；默认 loose override，不重打包 |
| ZIP/7Z | 解包到项目内 `work/`，不修改原压缩包 |
| RAR | 默认生成提取建议，除非有明确安全流程 |

PEX 特别规则：

- 没有可用 PEX 译表时，先生成 `work/normalized/<ModName>/pex_visible_strings/<Script>.translation.template.jsonl` 并阻断。
- 填好的译表保存为同目录 `<Script>.translation.jsonl`。
- 下一次总控运行再生成受控 PEX tool output。

## final_mod 交付契约

最终目录：

```text
out/<ModName>/汉化产出/final_mod/
```

交付压缩包：

```text
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

必须满足：

- `final_mod/` 保持 Skyrim Data 根结构。
- 默认直接替换同路径同名文件。
- `final_mod/meta/provenance.jsonl` 覆盖每个 `final_mod` 文件。
- `validate_final_mod.py` 中 missing provenance、hash mismatch 和 sidecar overlay 问题不能放行。
- BSA 内已汉化资源默认以同路径 loose override 进入 `final_mod/`，原 BSA 原样复制，不默认重打包。

## QA 和模型校对契约

项目内完成判定至少需要：

- final_mod 已构建。
- provenance 覆盖完整且 hash 一致。
- 非 GUI 候选覆盖率 `Missing` 和 `Unverified` 为 0。
- final text structure 无 blocking issue 和 warning。
- final text review packet 已生成。
- final binary review packet 已生成，且 protected/export 问题为 0。
- `qa/<ModName>.model_review.md` 由 Codex 模型校对完成，并覆盖最新 final text/binary review packet。
- `run_non_gui_qa_gates.py --strict-complete` 通过。

重建 `final_mod/` 或重写工具输出后，固定顺序是：

```text
build_final_mod
-> final text/binary review packet
-> final review quality
-> 模型校对
-> run_non_gui_qa_gates.py --strict-complete
```

旧模型校对不得在 packet 或 hash 变化后继续放行。

## GUI fallback 契约

GUI 工具只在以下情况进入：

- decoder/CLI 不可用。
- 文件格式不被非 GUI 流程支持。
- 受控写回必须由 GUI 完成。
- decoder/CLI QA 失败且确认 GUI 是合理后备。

进入 GUI 后必须满足：

- 输入路径在项目内。
- 输出路径在项目内 `tool_outputs`。
- 操作日志和报告写入 `qa/`。
- 能自动保存才算完成。
- 无法自动保存时必须标记 `blocked`。

Computer Use 可以操作窗口，但必须先截图确认目标控件。pywinauto/UI Automation 只能作为降级方案，不能默认使用固定屏幕坐标。

## 简单 RAG 模块

RAG 模块是项目内的轻量术语检索层，不是外部向量数据库、联网检索服务或自动翻译器。它用于把 LexTranslator 风格动态词典中可能相关的术语筛出来，生成当前 Mod 可复核的术语提示包。

数据来源：

| 来源 | 作用 |
|---|---|
| `glossary/mod_terms.md` | 当前工作区和具体 Mod 的人工确认术语，优先级最高 |
| `glossary/skyrim_cn_glossary.md` | Skyrim 常用中文术语参考种子 |
| `glossary/lextranslator_dynamic_dictionaries/` | 用户新增的 LexTranslator 风格动态词典 |
| `work/glossary_rag/lextranslator_dynamic.sqlite` | 项目内 SQLite 检索索引 |

基础入口：

```console
python scripts/build_lextranslator_dictionary_rag_index.py
python scripts/build_external_glossary_matches.py --mod-name "<ModName>"
```

边界：

- 命中包只是术语提示，不是自动替换表。
- 不能覆盖人工确认术语、禁翻项、结构 key、路径、占位符或运行时逻辑 key。
- 不能把字典替换冒充完整翻译。
- 输出必须留在项目内 `work/` 和 `qa/`。

## 新增文件类型检查表

新增文件类型时，至少同步：

| 位置 | 需要更新 |
|---|---|
| `skills/translation-task-router/SKILL.md` | 风险等级、推荐工具、输出目录、是否允许 Codex 直接处理 |
| 对应文件类型 Skill | 可翻译范围、保护项、QA 要求 |
| `scripts/route_translation_task.py` | 路由和报告输出 |
| 抽取或转换脚本 | 可复现生成 `source/`、`translated/` 或 `tool_outputs` |
| QA 脚本 | 结构、占位符、覆盖率和 final_mod 检查 |
| 专题文档 | `docs/decoder_first_workflow.md` 或相关文件类型文档 |

## 新增工具 adapter 检查表

新增 adapter 时必须保持项目边界：

- 输入来自 `mod/`、`work/`、`source/` 或 `translated/`。
- 输出进入 `translated/tool_outputs/`、`out/<ModName>/tool_outputs/` 或 QA 报告目录。
- 不访问真实游戏、MO2/Vortex、Steam、AppData 或 `Documents/My Games`。
- 不覆盖 `mod/` 原始输入。
- 二进制改写必须由工具完成，项目流程只复制工具输出。
- adapter 写可审计报告，记录输入、输出、hash、工具和阻断原因。

同步点：

| 位置 | 需要更新 |
|---|---|
| `config/tools.example.json` | 新工具路径字段 |
| `scripts/detect_decoder_tools.py` | 可用性检测和报告 |
| `config/workflow_policy.json` | 授权脚本 |
| 对应 Skill | 何时使用、何时 blocked |
| QA 脚本 | 输出验证和 final_mod 覆盖判断 |
| 专题文档 | `docs/tool_adapter.md` 和相关 workflow 文档 |

## 新增或调整状态机检查表

状态机变更必须同步：

| 改动 | 必须同步 |
|---|---|
| 新增阶段或调整顺序 | `config/workflow_policy.json`、`scripts/write_workflow_state.py`、`scripts/workflow_progress.py` |
| 新增入口脚本 | `allowed_entrypoint_scripts` 或阶段 `allowed_scripts` |
| 新增分步脚本 | `allowed_leaf_scripts` 和对应文档/Skill |
| 新增 ready 前证据 | `scripts/audit_translation_readiness.py`、`scripts/write_workflow_state.py`、`scripts/run_non_gui_qa_gates.py` |
| 新增状态字段 | `config/workflow_state.schema.json`、`scripts/write_workflow_state.py` |
| 新增用户可见进度字段或文案 | `scripts/workflow_progress.py`、`docs/codex_workflow.md`、`workflow-policy-and-state` Skill |
| 新增 trace span 或追踪字段 | `scripts/workflow_trace.py`、长流程入口脚本、`docs/codex_workflow.md` |
| 新增 final_mod 证据 | `scripts/validate_final_mod.py`、`scripts/build_final_mod.py`、`qa-validation` Skill |

## 最小验证清单

改文档：

```console
git diff --check -- README.md USER_GUIDE.md ADVANCED_USER_GUIDE.md developer_guide.md docs/codex_workflow.md
```

改 JSON 配置：

```console
python -m json.tool config/workflow_policy.json
python -m json.tool config/workflow_state.schema.json
```

改 Python 脚本：

```console
python -m py_compile scripts/<ChangedScript>.py
```

改状态机或任务逻辑：

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py
python scripts/write_workflow_tasks.py
```

确认 `.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`qa/workflow_timeline.md` 和 `qa/blockers.md` 与 `qa/workflow_state.json` 一致。

改 final_mod、QA 或工具输出逻辑：

```console
python scripts/run_non_gui_qa_gates.py --mod-name "<ModName>" --strict-complete
```

改插件结构：

```console
python "%USERPROFILE%\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" .
```

## 发布工程源码包

项目源码发布包由 Git 跟踪文件生成，不包含 ignored 输出和未跟踪本地文件：

```console
python scripts/package_project_release.py --version "<Version>" --dry-run
python scripts/package_project_release.py --version "<Version>"
```

`--dry-run` 会列出将被排除的非 ignored 未跟踪文件。正式打包时如果仍存在这类文件，脚本默认阻断，防止生产脚本、Skill、adapter 或文档漏进源码包。

输出位于：

```text
out/project_packages/
```

## 维护建议

- 不要新增 Bash、WSL 或 Linux shell 包装层；主流程统一使用 Python。
- 不要把深层开发说明塞回根 README。
- 不要用字符串替换冒充翻译。
- 不要让 GUI fallback 成为默认路径。
- 不要把人工临时操作写成自动化完成。
- 不要提交真实插件二进制、压缩包、真实游戏目录或本机私有配置。

## 相关文档

- [AGENTS.md](./AGENTS.md)
- [docs/codex_workflow.md](./docs/codex_workflow.md)
- [docs/decoder_first_workflow.md](./docs/decoder_first_workflow.md)
- [docs/tool_adapter.md](./docs/tool_adapter.md)
- [docs/skill_architecture.md](./docs/skill_architecture.md)
- [docs/translation_proofreading_workflow.md](./docs/translation_proofreading_workflow.md)
- [docs/effect_regression_workflow.md](./docs/effect_regression_workflow.md)
- [skills/workflow-policy-and-state/SKILL.md](./skills/workflow-policy-and-state/SKILL.md)
- [skills/workflow-agent-orchestration/SKILL.md](./skills/workflow-agent-orchestration/SKILL.md)
