---
name: esp-esm-esl-translation
description: "用于按 Game Profile 处理 Bethesda ESP/ESM/ESL 文本导出、白名单字段和受控写回边界。中文触发：ESP/ESM/ESL、插件文本、FormID、EditorID、localized、STRINGS、Fallout 4 插件。Skyrim uses the Skyrim adapter; Fallout 4 permits non-localized whitelist fields with Fallout4Mod reparse invariants, while localized plugins/STRINGS are blocked. Do not operate GUI, edit binaries directly, process PEX, or assemble final_mod."
---

# ESP/ESM/ESL Translation Rules

## 目标

只定义插件文本的可翻译范围、保护内容、译文风格和 QA 要求。本 Skill 不选择具体工具，不描述 GUI 操作步骤，不直接编辑插件二进制。

## 全局硬约束

- 继承 `translation-task-router` 的 Windows、工作区路径、`mod/` 输入和真实游戏目录隔离合同；本节只补充插件文件限制。
- 游戏身份和 adapter 只取工作区 marker/Game Profile，不按 Mod 名猜测。
- 不直接修改 `.esp/.esm/.esl`。
- 不覆盖 `mod/` 原始插件。

## Profile 分支

- 先解析当前 Game Profile 的 `capabilities.plugin_text` 级别、adapter id 和 options，再检查 localized/string-table 能力。Registry 未实现该 adapter、版本不兼容或必要 capability 关闭时必须 blocked；禁止读取旧顶层 adapter 字段，也禁止把未知游戏或未知 adapter 归入 Skyrim 分支。
- Skyrim SE/AE 与 Fallout 4 共用 `mutagen-bethesda-plugin` 受控入口；具体 Mutagen release、字段合同和能力级别来自当前 Game Profile。
- Fallout 4 仅允许 non-localized 插件的 profile 白名单字段。写回后必须用 `Fallout4Mod` 反解析，并通过 C# 原始二进制不变量：目标 subrecord occurrence 的 source/target 精确匹配；其余 record header、flags、subrecord 类型/顺序/索引和非目标 payload bytes 不变。只允许目标 record data-size 与祖先 GRUP size 变化。
- Fallout 4 localized plugin 或外部 `STRINGS`、`DLSTRINGS`、`ILSTRINGS` 家族一经检测即 `blocked`；不得用 Skyrim adapter、旁挂文本或 GUI 文案绕过。
- adapter、profile version 或 game metadata 与工作区不一致时 fail closed，旧报告不得复用。
- 后续新增游戏时，只在其 Game Profile、受控 adapter、不变量、路由和回归样本同时存在后开放对应插件能力；仅新增 game id 或 CLI 选项不能放行。
- Game Profile 中的 EET 可由 RAG 解析器只读提取原文/译文；这不等于允许 EET4 写回插件。`EspEsmTranslatorPath` 目前只是可选 GUI 工具配置，未经过受控 adapter 路由时不得用于自动写回。

## 可翻译内容

- 玩家可见物品名、法术名、能力名、效果名。
- 任务标题、任务阶段、任务目标。
- 对话、Message、Book、Note、Terminal-like 文本。
- UI 显示描述、帮助文本、菜单文本。
- 工具导出的明确 `source -> target` 文本字段。

## 模型翻译要求

- ESP/ESM/ESL 译文必须由 agent 模型基于上下文生成或复核；脚本只能导出、套用译表和做机械校验。
- 写回插件前必须有模型校对记录，检查语义、语气、术语一致性、是否误翻 protected 内容。
- `scripts/proofread_translation.py` 是机械门禁，不能替代模型校对。

## 必须保护

- FormID、EditorID、Record Type、Plugin Name。
- 脚本名、变量名、路径、文件名。
- 条件、结构字段、内部 key、排序或索引用字符串。
- `%s/%d/%f`、`{0}`、`{name}`、`<Alias=...>`、HTML/XML/颜色/字体标签。
- `\n`、`\r\n` 和原始换行结构。

## 输出要求

- Agent 只能处理 decoder/工具导出的文本中间文件。
- 优先使用 `python scripts/export_esp_strings.py --plugin-path <workspace-local-plugin> --mod-name <ModName>` 只读导出结构化文本，例如 `source/plugin_exports/<ModName>/*.jsonl`。
- 准备给工具导入的译文放入 `translated/`、`translated/lextranslator_ready/<ModName>/` 或 `translated/xtranslator_ready/<ModName>/`。
- 如果译文先以 source-to-target JSON map 形式生成，使用 `python scripts/apply_plugin_translation_map.py` 合成为 `translated/plugin_exports/<ModName>/*.zh.jsonl`。
- 完整非 GUI 插件阶段使用 `python scripts/run_plugin_translation_stage.py --mod-name <ModName> --workspace-path <workspace>`，它会导出候选、生成缺失译表模板、应用译表、调用 Mutagen 写回 `out/<ModName>/tool_outputs/` 并验证输出。
- 插件写回使用 `python scripts/invoke_mutagen_plugin_text_tool.py`，只能读取 `work/extracted_mods/` 和 `translated/`，只能写入 `out/` 和 `qa/`。
- 插件写回后必须重新用 `export_esp_strings.py --allow-generated-plugin` 反读 `out/<ModName>/tool_outputs/`，并把输出 JSONL、Mutagen writeback report 和 `--require-translation-evidence` 一起交给 `verify_plugin_output.py`。strict 模式不得使用 `--warn-only`；不要只靠二进制字节搜索判断译文是否写入。
- decoder/工具生成的插件输出只能进入 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- 未决术语写入 `qa/unresolved_terms.md`。

## QA 要求

- 校验 ID、字段名、记录类型不变。
- 校验占位符、标签和换行不丢失。
- 运行 `scripts/proofread_translation.py` 后，再生成/填写 agent 模型校对报告。
- decoder/工具输出进入 final_mod 前必须验证哈希变化、译文命中和英文残留。
- 验证压缩记录、CELL 覆盖或中文标点时，以结构化反读结果为准；字节探针只能作为辅助证据。
- 工具输出进入 final_mod 后，必须运行 `scripts/new_final_binary_review_packet.py` 反读最终 ESP/ESM/ESL 文本；任何 master、FormID、EditorID、MAST、EDID 等 protected 字符串变化都必须阻断或由模型明确解释。
- 高风险插件输出必须记录人工抽查和游戏内测试待办。

## 完成标准

- 只处理 decoder/工具导出的文本中间文件，未直接修改 `.esp/.esm/.esl`。
- 可翻译字段、保护字段和未决术语已分别记录。
- 译文准备文件已写入工作区 `translated/` 或工具准备目录。
- 相关 QA 报告已写入 `qa/`，包括 final_mod 二进制反读校对包，阻断问题未被标记为完成。
- decoder/工具生成的插件输出如需进入 final_mod，已交给 `qa-validation` 继续处理；只有 decoder 不可用时才进入 GUI fallback。

## 失败处理

工具导出格式不明、字段用途不明或文本可能参与逻辑判断时，不翻译，写入 QA 报告并要求人工确认。
