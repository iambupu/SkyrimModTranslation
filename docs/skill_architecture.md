# Skill Architecture

## 控制分层

- Codex 负责准确和灵活的编排：理解当前任务、读取状态证据、选择下一步、决定低风险重试或安全停止。
- 状态机负责边界和证据：给出当前状态、最后成功阶段、全局入口脚本、阶段脚本、分步脚本、推荐动作、修复候选、停止条件和下一条命令。
- 脚本负责可复现动作：执行准备、抽取、受控写回、final_mod 组装、报告刷新和 QA 门禁，不承担语义判断。
- QA 负责是否允许推进：严格门禁、结构校验、覆盖率、模型审读和 final_mod 证据决定状态能否前进。

## 拆分原则

当前工程保留 13 个核心业务 Skill，外加 2 个工作流控制 Skill，避免一个 Mod 的流程被过细 Skill 切碎，同时让全局状态判断和 Codex agent 恢复协议从具体文件处理中分离出来。

- 策略状态 Skill 只负责读取 workflow policy/state、判断允许动作和下一条命令。
- Agent 编排 Skill 只负责 Codex 恢复循环：读阻断报告、分类失败、选择允许动作、记录尝试和安全停止。
- 总 Skill 只负责编排。
- 路由 Skill 只负责文件类型、风险等级、工具优先级和下游 Skill。
- GUI Skill 只负责 LexTranslator/xTranslator 的工具操作。
- 文件类型 Skill 只负责可翻译范围、保护内容、译文规则和 QA 要求。
- QA Skill 只负责校验和报告。
- Final Skill 只负责组装完整 Mod 目录。

## 职责边界

| 层级 | 负责 | 不负责 |
|---|---|---|
| `workflow-policy-and-state` | 读取 `workflow_policy.json`、`workflow_state.json`，判断当前阶段、允许动作、下一条命令 | 翻译、单文件路由、GUI 操作、final_mod 组装 |
| `workflow-agent-orchestration` | Codex agent 恢复协议、阻断分类、低风险自动修复候选、尝试日志、停止条件 | 直接翻译、绕过状态机、替代 QA、直接编辑二进制 |
| `skyrim-mod-translation-orchestrator` | 阶段编排、串联下游 Skill | 全局策略判断、字符串可翻译判断、GUI 操作细节、文件组装细节 |
| `translation-task-router` | 文件类型、风险、工具优先级、下游 Skill | 翻译具体内容、点击工具、写 final_mod |
| GUI Skill | 启动工具、打开项目内输入、导入、导出、保存、日志 | 决定工具优先级、决定字符串是否可翻译、直接改二进制 |
| 文件类型 Skill | 可翻译范围、保护内容、译文风格、QA 规则 | GUI 菜单步骤、工具优先级、最终目录组装 |
| `qa-validation` | 校验格式、占位符、残留、报告 | 翻译文本、操作 GUI、组装 Mod |
| `final-mod-assembly` | 复制项目内来源、叠加翻译和工具输出、生成 meta | 翻译、工具选择、文本质量判断 |

## 接手顺序

后续 Codex agent 接手时，默认不要从全项目扫描开始。先按下面顺序读取现成状态，再决定是否需要展开到具体 Skill：

1. 先读 `qa/workflow_state.json` 或 `qa/workflow_state.md`，确认每个 Mod 的 `state`、`last_success_stage`、`blocking_checks` 和 `next_command`。
2. 再读 `qa/workflow_health.md` 或 `qa/workflow_health.json`，确认核心脚本、Workflow Policy、Skill、final text/binary review packet、严格门禁和最终证据是否完整。
3. 再读 `qa/translation_readiness.md` 或 `qa/translation_readiness.json`，确认 `mod/` 输入、已知输出、项目级状态和下一条建议命令。
4. 如果项目级状态是 `ready_for_manual_test`，不要重新扫描和重跑翻译；按 Known Mod Outputs 逐个检查 `out/<ModName>/汉化产出/final_mod/` 和 `<ModName>_CHS.zip`，并安排人工游戏内测试。
5. 如果 Mod 还处于 `discovered` 到 `qa_passed` 之间的正常推进状态，优先执行 workflow_state/readiness 报告中的推荐命令；不要手动拼接分步脚本。
6. 如果状态是 `blocked`、`qa_failed` 或某个证据缺失，先使用 `workflow-agent-orchestration` 读取 `recommended_actions`、`repair_candidates`、`stop_conditions` 和阻断报告，再打开相关文件类型 Skill 做局部排错。

只有 `workflow_state`、`workflow_health` 和 `translation_readiness` 缺失、过期或互相矛盾时，才回到总控 Skill 重新梳理流程。

`workflow_policy.json` 的授权面分三层：`always_allowed_scripts` 用于日志和状态刷新，`allowed_entrypoint_scripts` 用于总控/队列/严格门禁/健康检查，`allowed_leaf_scripts` 用于 QA、adapter 和局部恢复分步动作。`workflow_state.json` 中的 `allowed_scripts` 是这三层与当前阶段脚本的合并结果；`next_command` 不得指向一个未授权脚本。

## 权威路由

`.codex/skills/translation-task-router` 是当前权威路由 Skill。

非 GUI 路径优先级高于 GUI 工具：

- ESP/ESM/ESL：优先 Decoder CLI/library pipeline 和项目内 Mutagen 适配器；LexTranslator/xTranslator 只作为 GUI 后备。
- PEX 可见字符串：优先 `PexStringToolPath` / Mutagen PEX 适配器导出和写回项目内 PEX 副本；LexTranslator/xTranslator PapyrusPex 只作为后备。
- MCM：独立 Interface 文本和 JSON/INI 优先 Codex 结构化文本管线；只有需要工具处理时才进入 GUI 后备。

工具优先级只允许由 `translation-task-router` 维护：

1. Codex Text Pipeline 和项目内 decoder/CLI 是默认首选：低风险文本直接处理，ESP/PEX 通过受控 Mutagen 等适配器导出、翻译、写回项目内工具输出。
2. LexTranslator 只在 decoder/CLI 不可用、格式不支持或 QA 失败后作为 GUI fallback，用于项目内输入和项目内输出。
3. xTranslator 用于 GUI fallback 精修、查漏、对照、复杂导入和 LexTranslator 失败后的 PapyrusPex 后备。

## Codex 查找原则

Skill 首先服务 Codex 自动发现和执行。`SKILL.md` 的 `description` 应该前置触发词、文件扩展名、工具名和排除条件；不要依赖正文里的“什么时候用”来帮助首次匹配。

当前项目以 `.codex/skills/` 为权威 Skill 目录。这个目录直接服务 Codex 检索和执行，项目根不再保留第二套 `skills/` 镜像，避免后续 agent 在两个来源之间二次探索或读取过期规则。
如果发现有人重新创建了根目录 `skills/`，应先比对并迁移到 `.codex/skills/`，再删除根目录镜像；不得让两套 Skill 同时存在。

推荐写法：

```text
Use for Skyrim Interface/translations/*.txt and JSON text resources. Do not use for ESP/PEX binary writeback or GUI automation.
```

不推荐写法：

```text
Translate Skyrim mods.
```

## 当前核心 Skill

| 类别 | Skill |
|---|---|
| 策略状态 | `workflow-policy-and-state` |
| Agent 编排恢复 | `workflow-agent-orchestration` |
| 编排 | `skyrim-mod-translation-orchestrator` |
| 路由 | `translation-task-router` |
| 输入准备 | `mod-input-preparation` |
| BSA/BA2 归档审计 | `bsa-archive-audit` |
| 文本资源 | `text-resource-translation` |
| MCM | `mcm-translation` |
| 插件 | `esp-esm-esl-translation` |
| PEX/PSC | `pex-visible-strings-translation` |
| GUI | `lextranslator-gui-automation` |
| GUI | `xtranslator-gui-automation` |
| 术语 | `glossary-management` |
| QA | `qa-validation` |
| 组装 | `final-mod-assembly` |

## 文件路线

| 文件类型 | 风险 | 推荐 Skill | 主工具 | 后备/验证 |
|---|---|---|---|---|
| Interface TXT | 低 | `text-resource-translation` | Codex Text Pipeline | LexTranslator |
| JSONL / CSV / XML / TXT / MD | 低到中 | `text-resource-translation` | Codex Text Pipeline | 无 |
| MCM | 中 | `mcm-translation` | Codex Structured MCM Extractor | LexTranslator / xTranslator fallback |
| ZIP | 中 | `mod-input-preparation` | 项目内只读解压 | 无 |
| BSA | 中 | `bsa-archive-audit` | `bethesda-structs` 只读审计 | BSAFileExtractor 安全 wrapper；汉化内容默认 loose override，不重打包 |
| BA2 | 中 | `bsa-archive-audit` | `bethesda-structs` 只读审计 | 明确 BA2 adapter 前不解包；汉化内容默认 loose override |
| RAR / 7Z | 中 | `mod-input-preparation` | 提取计划或 7z 项目内解包 | 明确工具流程 |
| PEX | 高 | `pex-visible-strings-translation` | PexStringToolPath decoder/rewriter | LexTranslator / xTranslator PapyrusPex fallback |
| PSC | 高 | `pex-visible-strings-translation` | Codex 只读分析 | 无 |
| ESP / ESM / ESL | 高 | `esp-esm-esl-translation` | Decoder CLI/library pipeline | LexTranslator / xTranslator GUI fallback |
| Final Mod 组装 | 中 | `final-mod-assembly` | Codex 文件组装脚本 | final_mod 校验 |

## 任务路线

| 用户说法或任务意图 | 先读 Skill |
|---|---|
| “现在到哪一步了”“下一步能做什么”“状态机”“workflow_state” | `workflow-policy-and-state` |
| “自动编排重试”“QA 失败后怎么恢复”“根据 recommended_actions 继续”“记录 workflow_agent_runs” | `workflow-agent-orchestration` |
| “执行全流程”“试翻译这个 Mod”“构建最终汉化 Mod” | `skyrim-mod-translation-orchestrator` |
| “这个文件该怎么处理”“选择工具”“路由一下” | `translation-task-router` |
| “mod 里有压缩包”“先解压”“扫描输入” | `mod-input-preparation` |
| “BSA/BA2 审计”“BSAFileExtractor”“解包 .bsa”“归档 manifest” | `bsa-archive-audit` |
| “翻译 Interface/translations”“翻译 JSON/XML/CSV/TXT” | `text-resource-translation` |
| “MCM 菜单/选项/帮助文本” | `mcm-translation` |
| “ESP/ESM/ESL 插件文本” | `esp-esm-esl-translation` |
| “PEX 可见字符串”“PSC 只读提取” | `pex-visible-strings-translation` |
| “用 LexTranslator GUI” | `lextranslator-gui-automation` |
| “用 xTranslator GUI”“查漏/精修/PapyrusPex 后备” | `xtranslator-gui-automation` |
| “术语表/未决术语/专有名词” | `glossary-management` |
| “LexTranslator 动态词典/RAG 索引/词库命中包” | `glossary-management` |
| “跑校验/检查占位符/final_mod 校验” | `qa-validation` |
| “输出完整 Mod 目录/final_mod” | `final-mod-assembly` |

## 最小 Skill 组合

| 场景 | 必读 | 按需追加 | 不要先读 |
|---|---|---|---|
| 新 Mod 或常规全流程 | `workflow-policy-and-state`、`skyrim-mod-translation-orchestrator` | `translation-task-router`、被路由命中的文件类型 Skill、`qa-validation`、`final-mod-assembly` | GUI Skill，除非路由进入 fallback |
| 判断单个文件怎么处理 | `translation-task-router` | 被路由命中的文件类型 Skill | 总控、QA、final_mod 组装 |
| 已有 ready 输出，准备人工测试 | `qa/workflow_state.md`、`qa/workflow_health.md`、`qa/translation_readiness.md` | `final-mod-assembly`，仅当 final_mod 结构有疑问 | 文件类型 Skill、GUI Skill |
| GUI 工具失败或需要 fallback | 路由结果、对应 GUI Skill | 对应文件类型 Skill、`qa-validation` | 其他 GUI Skill |
| 术语冲突或未决名词 | `glossary-management` | 对应文件类型 Skill | GUI Skill、final_mod 组装 |
| QA 未通过 | `workflow-agent-orchestration`、`qa-validation` | 失败报告对应的文件类型 Skill 或 `final-mod-assembly` | 重新扫描整个 `mod/` |

`mcm-translation` 和 `text-resource-translation` 可以在同一任务中同时使用：前者判断 MCM UI 语义和脚本 key 边界，后者保护 Interface/JSON/XML/CSV/TXT 等文件结构。它们不是互相替代关系。

不要为了减少 Skill 数量把这两个合并；合并会让脚本逻辑 key 和结构保护边界更难审计。

## 完成判定

一个自动化阶段只有在以下条件同时满足时才算完成：

- 输入和输出路径都在当前项目内。
- 工具日志或脚本报告存在。
- QA 报告存在且没有阻断项。
- final_mod 由项目内来源和项目内工具输出组装。
- `qa/workflow_state.json` 已记录阶段推进和下一条允许命令。
- Codex agent 恢复尝试已写入 `qa/workflow_agent_runs.jsonl`，当且仅当本轮执行了自动修复、重试或 blocked handoff。

人工临时保存可以作为记录，但不能算作全流程自动化完成，除非后续被受控工具适配器复现并写入项目内 `tool_outputs`。

## 防重复探索

后续 agent 只有在下面情况才重新扫描或重跑准备阶段：

- `qa/workflow_health.md` 或 `qa/translation_readiness.md` 缺失。
- `qa/workflow_state.md` 或 `qa/workflow_state.json` 缺失。
- `workflow_state`、`workflow_health` 和 `translation_readiness` 状态互相矛盾。
- 用户明确要求重新处理某个 Mod。
- `mod/` 新增了 readiness 报告未记录的输入。
- QA 报告指出某个证据文件缺失或过期。

其他情况下，先使用报告中的 `Known Mod Outputs`、`Next recommended action` 和 Evidence 表。不要重新枚举所有 QA 文件来猜当前状态。

## 已合并或降级

- `mod-sandbox-inventory` + generic archive handoff -> `mod-input-preparation`；`.bsa/.ba2` 归档审计已重新拆为 `bsa-archive-audit`，BA2 只读审计不等于解包支持。
- `interface-translation` + `text-asset-translation` -> `text-resource-translation`。
- `psc-string-extraction` -> 并入 `pex-visible-strings-translation`。
- `gui-automation-core` -> `docs/gui_automation_rules.md`。
- `dsd-patch-generation` -> 并入文本资源和 final_mod 组装流程；历史试验记录可放在 `qa/dsd_patch_generation.md`。

## PEX 写回边界

Codex 可以准备 PEX 可见字符串翻译材料，也可以通过受控 `PexStringToolPath` / Mutagen PEX 适配器写回项目内 PEX 副本，但不能直接改写 `.pex`。LexTranslator/xTranslator 仅作为后备工具路径：

```text
out/<ModName>/tool_outputs/Scripts/<ScriptName>.pex
```

详细流程见 `docs/pex_visible_strings_writeback.md`。
