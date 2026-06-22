# 开发者指南

本文面向维护者和有技术经验的开发者，说明这个 Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化 Codex 插件的工作流设计理念、核心分层、状态机、扩展方式和最小验证要求。普通用户可以只看根目录 `README.md`；Codex/agent 接手流程单独见 `docs/codex_workflow.md`。

## 插件与工作区架构

本仓库是 `skyrim-mod-chs-translation` 插件源仓库，不是某个具体 Mod 的运行状态目录。插件源仓库提供 `.codex-plugin/plugin.json`、根目录 `skills/`、Python 脚本、配置模板、文档和 QA 规则。

实际汉化任务应运行在由 `python scripts/init_workspace.py <workspace>` 创建的独立工作区中。初始化目标必须是不存在的路径或插件仓库外部的空目录；脚本会拒绝插件仓库本身、插件仓库内部目录、已有文件和非空目录。工作区保存 `mod/`、`work/`、`qa/`、`out/`、`source/`、`translated/`、`glossary/`、`.skyrim-chs-workspace.json` 和 `config/tools.local.json`。

`.codex-plugin/`、`skills/`、`.codex/skills/`、`scripts/` 和完整文档树只属于插件源仓库，不复制进工作区。`glossary/` 是例外：初始化会把插件默认术语表复制为工作区可编辑种子，用户可在工作区加入新的词典。根目录 `skills/` 是插件运行 Skill 的唯一权威目录；`.codex/skills/` 只保留安装、使用和维护 meta Skill。工作区中的命令应运行插件源脚本的绝对路径或由状态报告给出的规范化命令，不要为了运行流程把 `scripts/` 复制进工作区。

## 设计理念

本插件的核心目标不是“尽快改出一个能看的文件”，而是建立可维护、可回滚、可复核的《上古卷轴5：天际》SE/AE Mod 简体中文汉化工程流程。

从架构上看，插件在工作区中运行时是一个由状态机约束、多个反馈循环驱动的 agent 工作流。状态机不直接执行全部流程，而是记录阶段、授权动作、证据要求、下一步建议和停止条件；agent 和 Python 入口在这些约束内执行具体动作。外层接手循环负责按 Mod、阶段和阻断状态持续推进；文件类型循环负责发现、路由、抽取、翻译和写回不同资源；QA 循环负责在每次组装或恢复后重新生成证据，并决定是否允许进入下一阶段。

设计原则：

- 所有输入、输出、工具产物和报告都必须留在项目目录内。
- 多个 loop 必须通过 `workflow_state`、`workflow_tasks`、readiness 和 QA 报告共享事实，不能绕过状态机直接推进。
- 文本处理优先，CLI/库解码器优先，GUI 只作为最后自动化后备。
- 模型负责语义翻译、编排和判断；脚本负责可复现动作；QA 负责推进门槛。
- 二进制文件只能由受控工具生成工作区内副本，Codex 不直接修改。
- `final_mod/` 必须像 Skyrim Data 根目录一样可检查，`_CHS.zip` 才是交付包。
- 静态 QA 通过只允许进入人工游戏测试，不等于游戏内测试通过。

## 控制分层

| 层 | 负责 | 不负责 |
|---|---|---|
| 模型编排层 | 读取状态、选择下一步、翻译和模型校对、解释阻断 | 绕过证据、伪造完成、直接改二进制 |
| 状态机 | 记录阶段、边界、证据、允许动作、推荐动作和停止条件 | 执行具体翻译或工具操作 |
| Python 脚本 | 解包、抽取、转换、写报告、调用受控工具、组装和校验 | 做语义质量最终判断 |
| QA 门禁 | 判断是否允许推进到下一阶段或人工测试 | 替代真实游戏内测试 |
| Skill | 给模型编排层提供任务路由、文件类型规则和执行边界 | 取代状态机或脚本入口 |

## 标准工作流

默认状态顺序来自 `config/workflow_policy.json`：

```text
discovered
-> extracted
-> routed
-> candidates_extracted
-> translated
-> tool_outputs_generated
-> final_mod_built
-> qa_passed
-> ready_for_manual_test
-> manual_tested
```

失败或暂停状态不是普通进度阶段：

```text
needs_input
blocked
qa_failed
```

典型处理路径：

1. `mod/` 下发现输入。
2. 解包到 `work/extracted_mods/<ModName>/`。
3. 路由每类文件，判断是否允许模型编排层直接处理。
4. 抽取可翻译候选到 `source/`、`work/` 或 `out/<ModName>/non_gui_exports/`。
5. 模型生成并校对译文，写入 `translated/`。
6. 需要二进制写回时，由 Mutagen、PEX adapter、LexTranslator 或 xTranslator 生成项目内 tool output。
7. `build_final_mod.py` 组装 `out/<ModName>/汉化产出/final_mod/` 和 `_CHS.zip`。
8. final text/binary review packet、模型校对、final_mod 校验和严格 QA 门禁共同判断是否可以人工测试。
9. 用户在真实游戏环境中测试并回填人工测试结果。

## 关键状态文件

| 文件 | 用途 |
|---|---|
| `qa/workflow_state.json` | 机器可读的权威状态、阻断项、推荐动作、允许脚本 |
| `qa/workflow_state.md` | 人可读状态摘要 |
| `qa/translation_readiness.json` | 项目级 ready 判断、输入输出汇总 |
| `qa/workflow_tasks.json` | 从状态派生的可领取任务视图 |
| `config/workflow_policy.json` | 状态机、允许入口、GUI fallback 和恢复策略 |
| `config/workflow_state.schema.json` | 状态文件结构契约 |

Agent 接手专用的 `qa/codex_handoff.json`、`qa/workflow_agent_runs.jsonl` 和恢复循环规则不在本文展开，见 `docs/codex_workflow.md`。

## 脚本入口分层

### 总入口

| 脚本 | 用途 |
|---|---|
| `scripts/run_translation_queue.py` | 准备 `mod/` 输入队列 |
| `scripts/run_non_gui_translation_workflow.py` | 某个 Mod 的常规非 GUI 主流程 |
| `scripts/run_non_gui_qa_gates.py` | 严格 QA 门禁 |
| `scripts/test_workflow_health.py` | 工作流健康检查 |

Agent 恢复和并行任务调度入口不作为开发者常规入口展开，见 `docs/codex_workflow.md`。

### 常用状态刷新

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
```

### 工具检测

```console
python scripts/detect_decoder_tools.py
python scripts/validate_tools_config.py
```

### 交付验证

```console
python scripts/validate_final_mod.py --mod-name "<ModName>"
python scripts/validate_final_text_structure.py --mod-name "<ModName>"
python scripts/run_non_gui_qa_gates.py --mod-name "<ModName>" --strict-complete
```

## 文件类型处理策略

| 文件类型 | 默认策略 |
|---|---|
| Interface translations | 文本管线，保留 key、tab、行数和占位符 |
| MCM JSON/INI | 结构化文本管线，只翻译玩家可见字段 |
| JSON/XML/CSV/TXT/MD | 结构化解析和文本管线，保护 key、tag、path 和占位符 |
| ESP/ESM/ESL | 导出文本、翻译中间表、由受控 Mutagen/xEdit adapter 写回工作区内副本 |
| PEX | 优先 PEX adapter 导出/写回可见字符串；不直接 patch PEX，不改 PSC |
| PSC | 只读提取候选，不回写、不编译 |
| BSA/BA2 | 首选只读审计；BSA 可通过安全 wrapper 解包；默认 loose override，不重打包 |
| ZIP/7Z | 解包到项目内 `work/`；不修改原压缩包 |
| RAR | 默认生成提取建议，除非后续添加明确安全流程 |

所有文件处理前应通过 `translation-task-router` 或对应 Python 路由入口确认风险和输出位置。

## 简单 RAG 模块

本项目的 RAG 模块是项目内的轻量术语检索层，不是外部向量数据库、联网检索服务或自动翻译器。它的目标是在翻译前把 LexTranslator 风格动态词典中可能相关的术语筛出来，生成当前 Mod 可复核的术语提示包。

数据来源：

| 来源 | 作用 |
|---|---|
| `glossary/mod_terms.md` | 当前工作区和具体 Mod 的人工确认术语，优先级最高 |
| `glossary/skyrim_cn_glossary.md` | 工作区内的 Skyrim 常用中文术语参考种子 |
| `glossary/lextranslator_dynamic_dictionaries/` | 工作区内 LexTranslator 风格动态词典来源目录，允许用户新增词典文件或子目录 |
| `work/glossary_rag/lextranslator_dynamic.sqlite` | 动态词典的项目内 SQLite 检索索引 |

基础入口：

```console
python scripts/build_lextranslator_dictionary_rag_index.py
python scripts/build_external_glossary_matches.py --mod-name "<ModName>"
```

输出证据：

```text
qa/lextranslator_dictionary_rag_index.md
qa/lextranslator_dictionary_rag_index.json
work/glossary_matches/<ModName>/external_glossary_matches.jsonl
work/glossary_matches/<ModName>/external_glossary_matches.md
qa/<ModName>.external_glossary_matches.md
```

工作方式：

- 索引构建脚本会比较动态词典目录、词表文件和 SQLite 索引的修改时间。
- 索引缺失、词典更新、索引版本变化或显式 `--force` 时才重建。
- `build_external_glossary_matches.py` 根据当前 Mod 的待翻译文本生成小型命中包。
- `run_non_gui_translation_workflow.py` 会在翻译阶段前刷新索引检查，插件文本导出后也可生成对应命中包。
- 命中包只作为术语提示和人工复核材料，不是自动替换表。

边界规则：

- RAG 命中不能覆盖 `glossary/mod_terms.md` 中的人工确认术语。
- RAG 命中不能覆盖 FormID、EditorID、脚本名、变量名、路径、文件名、插件名、JSON/XML key、占位符或运行时逻辑 key。
- RAG 模块不能把字典替换冒充完整翻译；语义翻译和最终校对仍由 Codex 模型完成。
- RAG 输出必须留在项目内 `work/` 和 `qa/`，不能访问真实游戏目录或外部隐私数据。

扩展该模块时，优先保持确定性和可审计性：新增词典格式应先扩展解析脚本和报告字段，再同步 `docs/lextranslator_dictionary_rag.md`、`glossary/lex_dictionary_notes.md`、相关 QA 证据和工作流文档。除非用户明确要求，不要把这个模块升级成联网检索、外部 embedding 服务或不可复现的黑盒流程。

## final_mod 交付契约

最终目录必须是：

```text
out/<ModName>/汉化产出/final_mod/
```

交付压缩包必须是：

```text
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

交付规则：

- `final_mod/` 保持 Skyrim Data 根结构。
- 默认直接替换同路径同名文件。
- `Interface/translations/*_chinese.txt`、XML/JSONL 对照表、词典和 patch-only 文件默认只是中间件。
- `final_mod/meta/provenance.jsonl` 必须覆盖每个 `final_mod` 文件。
- `validate_final_mod.py` 中 missing provenance、hash mismatch 和 sidecar overlay 问题不能放行。
- BSA 内已汉化资源默认以同路径 loose override 进入 `final_mod/`，原 BSA 原样复制，不默认重打包。

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

Computer Use 可以操作窗口，但必须先截图确认目标控件。项目内 pywinauto/UI Automation 只能作为降级方案，不能默认使用固定屏幕坐标。

## 新增文件类型

新增文件类型时，至少同步：

| 位置 | 需要更新 |
|---|---|
| `skills/translation-task-router/SKILL.md` | 风险等级、推荐工具、输出目录和是否允许模型编排层直接处理 |
| 对应文件类型 Skill | 可翻译范围、保护项、QA 要求 |
| `scripts/route_translation_task.py` | 路由和报告输出 |
| 抽取或转换脚本 | 可复现生成 `source/`、`translated/` 或 `tool_outputs` |
| QA 脚本 | 结构、占位符、覆盖率和 final_mod 检查 |
| 文档 | `docs/decoder_first_workflow.md` 或相关专题文档 |

## 新增工具 adapter

新增 adapter 时必须保持项目边界：

- 输入必须来自 `mod/`、`work/`、`source/` 或 `translated/`。
- 输出必须进入 `translated/tool_outputs/`、`out/<ModName>/tool_outputs/` 或 QA 报告目录。
- 不能访问真实游戏、MO2/Vortex、Steam、AppData 或 `Documents/My Games`。
- 不能覆盖 `mod/` 原始输入。
- 二进制改写必须由工具完成，项目流程只复制工具输出。
- adapter 必须写可审计报告，记录输入、输出、hash、工具和阻断原因。

需要同步：

| 位置 | 需要更新 |
|---|---|
| `config/tools.example.json` | 新工具路径字段 |
| `scripts/detect_decoder_tools.py` | 可用性检测和报告 |
| `config/workflow_policy.json` | `allowed_leaf_scripts` 或阶段 `allowed_scripts` |
| 对应 Skill | 何时使用、何时 blocked |
| QA 脚本 | 输出验证和 final_mod 覆盖判断 |
| 文档 | `docs/tool_adapter.md` 和相关 workflow 文档 |

## 新增或调整状态机阶段

状态机变更必须同步：

| 改动 | 必须同步的位置 |
|---|---|
| 新增阶段或调整顺序 | `config/workflow_policy.json` 的 `state_order`、`states`，以及 `scripts/write_workflow_state.py` |
| 新增入口脚本 | `allowed_entrypoint_scripts` 或阶段 `allowed_scripts` |
| 新增分步脚本 | `allowed_leaf_scripts` 和对应文档/Skill |
| 新增 ready 前证据 | `scripts/audit_translation_readiness.py`、`scripts/write_workflow_state.py`、`scripts/run_non_gui_qa_gates.py` |
| 新增状态字段 | `config/workflow_state.schema.json`、`scripts/write_workflow_state.py` |
| 新增 final_mod 证据 | `scripts/validate_final_mod.py`、`scripts/build_final_mod.py`、`qa-validation` Skill |

不变量：

- `qa/workflow_state.json` 的 `next_command` 不得指向未授权脚本。
- `allowed_scripts` 必须由策略文件授权面合并而来。
- 缺少 final_mod provenance、严格 QA 未过、覆盖率缺失、模型审读缺失或包校验不一致时，不能进入 `ready_for_manual_test`。
- `ready_for_manual_test` 只表示可以人工测试，不表示人工测试已通过。

## QA 和模型校对契约

项目内完成判定至少需要：

- final_mod 已构建。
- `final_mod/meta/provenance.jsonl` 覆盖完整且 hash 一致。
- 非 GUI 候选覆盖率 `Missing` 和 `Unverified` 为 0。
- final text structure 无 blocking issue 和 warning。
- final text review packet 已生成。
- final binary review packet 已生成，且 protected/export 问题为 0。
- `qa/<ModName>.model_review.md` 由模型校对完成，并覆盖最新 final text/binary review packet。
- `run_non_gui_qa_gates.py --strict-complete` 通过。

重建 `final_mod/` 或重写工具输出后，应按固定顺序：

```text
build_final_mod
-> final text/binary review packet
-> final review quality
-> 模型校对
-> run_non_gui_qa_gates.py --strict-complete
```

旧模型校对不得在 packet 或 hash 变化后继续放行。

## 最小验证清单

改文档：

```console
git diff --check -- README.md developer_guide.md docs/codex_workflow.md
```

改 JSON 配置：

```console
python -m json.tool config/workflow_policy.json
python -m json.tool config/workflow_state.schema.json
```

改状态机或任务逻辑：

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py
python scripts/write_workflow_tasks.py
```

改 Python 脚本：

```console
python -m py_compile scripts/<ChangedScript>.py
```

改 Skill：

```console
python C:\Users\bupuy\.codex\skills\.system\skill-creator\scripts\quick_validate.py .\.codex\skills\<SkillName>
```

改 final_mod、QA 或工具输出逻辑：

```console
python scripts/run_non_gui_qa_gates.py --mod-name "<ModName>" --strict-complete
```

## 发布工程源码包

项目源码发布包由 Git 跟踪文件生成，不包含 ignored 输出和未跟踪本地文件：

```console
python scripts/package_project_release.py --version "<Version>" --dry-run
python scripts/package_project_release.py --version "<Version>"
```

输出位于：

```text
out/project_packages/
```

对应 manifest 会记录 archive hash、文件数量、Git commit、dirty 状态和是否仅包含 Git 跟踪文件。

## 维护建议

- 不要新增 Bash、WSL 或 Linux shell 包装层；主流程统一使用 Python。
- 不要把深层开发说明塞回根 README。
- 不要用字符串替换冒充翻译。
- 不要让 GUI fallback 成为默认路径。
- 不要把人工临时操作写成自动化完成。
- 不要提交真实插件二进制、压缩包、真实游戏目录或本机私有配置。

相关文档：

- `AGENTS.md`
- `docs/codex_workflow.md`
- `docs/decoder_first_workflow.md`
- `docs/tool_adapter.md`
- `docs/skill_architecture.md`
- `docs/translation_proofreading_workflow.md`
- `skills/workflow-policy-and-state/SKILL.md`
- `skills/workflow-agent-orchestration/SKILL.md`
