---
name: esp-esm-esl-translation
description: Use for Skyrim .esp/.esm/.esl plugin text export translation rules, protected fields, terminology, and QA. Do not use for GUI automation, direct binary editing, PEX, Interface txt, or final_mod assembly.
---

# ESP/ESM/ESL Translation Rules

## 目标

只定义插件文本的可翻译范围、保护内容、译文风格和 QA 要求。本 Skill 不选择具体工具，不描述 GUI 操作步骤，不直接编辑插件二进制。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前项目内。
- Mod 原始输入只允许来自当前项目 `mod/` 沙盒或项目内工作副本。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- 不直接修改 `.esp/.esm/.esl`。
- 不覆盖 `mod/` 原始插件。

## 可翻译内容

- 玩家可见物品名、法术名、能力名、效果名。
- 任务标题、任务阶段、任务目标。
- 对话、Message、Book、Note、Terminal-like 文本。
- UI 显示描述、帮助文本、菜单文本。
- 工具导出的明确 `source -> target` 文本字段。

## 模型翻译要求

- ESP/ESM/ESL 译文必须由 Codex 模型基于上下文生成或复核；脚本只能导出、套用译表和做机械校验。
- 写回插件前必须有模型校对记录，检查语义、语气、术语一致性、是否误翻 protected 内容。
- `scripts/proofread_translation.py` 是机械门禁，不能替代模型校对。

## 必须保护

- FormID、EditorID、Record Type、Plugin Name。
- 脚本名、变量名、路径、文件名。
- 条件、结构字段、内部 key、排序或索引用字符串。
- `%s/%d/%f`、`{0}`、`{name}`、`<Alias=...>`、HTML/XML/颜色/字体标签。
- `\n`、`\r\n` 和原始换行结构。

## 输出要求

- Codex 只能处理 decoder/工具导出的文本中间文件。
- 优先使用 `python scripts/export_esp_strings.py --plugin-path <project-local-plugin> --mod-name <ModName>` 只读导出结构化文本，例如 `source/plugin_exports/<ModName>/*.jsonl`。
- 准备给工具导入的译文放入 `translated/`、`translated/lextranslator_ready/<ModName>/` 或 `translated/xtranslator_ready/<ModName>/`。
- 如果译文先以 source-to-target JSON map 形式生成，使用 `python scripts/apply_plugin_translation_map.py` 合成为 `translated/plugin_exports/<ModName>/*.zh.jsonl`。
- 完整非 GUI 插件阶段使用 `python scripts/run_plugin_translation_stage.py --mod-name <ModName> --workspace-path <workspace>`，它会导出候选、生成缺失译表模板、应用译表、调用 Mutagen 写回 `out/<ModName>/tool_outputs/` 并验证输出。
- 插件写回使用 `python scripts/invoke_mutagen_plugin_text_tool.py`，只能读取 `work/extracted_mods/` 和 `translated/`，只能写入 `out/` 和 `qa/`。
- 插件写回后必须重新用 `export_esp_strings.py --allow-generated-plugin` 反读 `out/<ModName>/tool_outputs/`，并把输出 JSONL 交给 `verify_plugin_output.py --output-export-jsonl-path`；不要只靠二进制字节搜索判断译文是否写入。
- decoder/工具生成的插件输出只能进入 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- 未决术语写入 `qa/unresolved_terms.md`。

## QA 要求

- 校验 ID、字段名、记录类型不变。
- 校验占位符、标签和换行不丢失。
- 运行 `scripts/proofread_translation.py` 后，再生成/填写 Codex 模型校对报告。
- decoder/工具输出进入 final_mod 前必须验证哈希变化、译文命中和英文残留。
- 验证压缩记录、CELL 覆盖或中文标点时，以结构化反读结果为准；字节探针只能作为辅助证据。
- 工具输出进入 final_mod 后，必须运行 `scripts/new_final_binary_review_packet.py` 反读最终 ESP/ESM/ESL 文本；任何 master、FormID、EditorID、MAST、EDID 等 protected 字符串变化都必须阻断或由模型明确解释。
- 高风险插件输出必须记录人工抽查和游戏内测试待办。

## 完成标准

- 只处理 decoder/工具导出的文本中间文件，未直接修改 `.esp/.esm/.esl`。
- 可翻译字段、保护字段和未决术语已分别记录。
- 译文准备文件已写入项目内 `translated/` 或工具准备目录。
- 相关 QA 报告已写入 `qa/`，包括 final_mod 二进制反读校对包，阻断问题未被标记为完成。
- decoder/工具生成的插件输出如需进入 final_mod，已交给 `qa-validation` 继续处理；只有 decoder 不可用时才进入 GUI fallback。

## 失败处理

工具导出格式不明、字段用途不明或文本可能参与逻辑判断时，不翻译，写入 QA 报告并要求人工确认。
