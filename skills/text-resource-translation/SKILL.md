---
name: text-resource-translation
description: Use for Skyrim Interface/translations/*.txt and workspace-local visible JSON, JSONL, XML, CSV, TXT, MD text resource translation that preserves structure and prepares same-path final_mod replacement overlays. Do not use for Meshes/Textures/FaceGenData XML resource metadata, ESP/ESM/ESL/PEX binary writeback, GUI automation, archive extraction, or final_mod assembly.
---

# Text Resource Translation

## 目标

翻译工作区内低到中风险文本资源副本，保留结构、key、标签、列名、行数、tab 分隔和占位符。本 Skill 负责文本资源规则，不处理插件二进制，不选择 GUI 工具优先级。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前工作区内。
- Mod 原始输入只允许来自当前工作区 `mod/` 沙盒或工作区内工作副本。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- 不覆盖 `mod/` 原始文件。
- 不直接修改插件或 PEX 二进制。

## 输入

- `Interface/translations/*.txt`
- `.json/.jsonl/.xml/.csv/.txt/.md` when the file is a visible text resource.
- 工具导出的文本中间文件。

## 输出

- `translated/interface/<ModName>/`
- `translated/text_assets/<ModName>/`
- `translated/final_mod/<ModName>/` 或 `translated/overlay/<ModName>/` 中的同路径同名暂存 overlay；`out/<ModName>/final_mod_overlay/` 只作为兼容旧暂存输入。这些目录只作为 final_mod 组装输入，最终交付只认 `out/<ModName>/汉化产出/final_mod/` 和 `_CHS.zip`。
- QA 报告。

## 推荐工具

- Codex Text Pipeline。
- 结构化解析器。
- `scripts/normalize_export.py`
- `scripts/split_jsonl.py`
- `scripts/new_model_review_packet.py`
- `scripts/proofread_translation.py`
- `scripts/validate_interface_translation.py`
- `scripts/validate_translation.py`
- `scripts/scan_placeholders.py`
- `scripts/validate_final_text_structure.py`
- `scripts/new_final_text_review_packet.py`

## Interface 特殊规则

- 解析 `$Key<TAB>Text`。
- 保留 key、tab 分隔、行数、控制符和变量。
- 只翻译 tab 后玩家可见文本。
- 输出到工作区译文或 final_mod overlay。
- final_mod overlay 默认使用原文件名直接替换，例如把译文写成 `Interface/translations/<Plugin>_english.txt` 的工作区 overlay，而不是只新增 `<Plugin>_chinese.txt`。
- `*_chinese.txt` 只有在 QA 已记录目标环境会加载该语言文件时，才可作为最终交付文件；否则只作为中间参考文件。
- 交付态 `Interface/translations/*.txt` 必须是 Skyrim 可加载的 UTF-16 LE BOM 文本；UTF-8/无 BOM 的中间文件不能直接当作 `final_mod` 完成品。
- 运行 `python scripts/validate_interface_translation.py`；该脚本只写 Markdown 报告，`--report-output-path` 必须使用 `.md` 后缀。

## 通用文本规则

- JSON：不翻译 key，只翻译明确玩家可见的 value。
- XML：不翻译 tag 和属性名；属性值只有明确玩家可见时才可翻译，路径、source、destination、文件名和 schema 不翻译。
- `Meshes/`、`Textures/`、`FaceGenData/` 下的 XML 默认是资源元数据或工具配置，不是玩家可见文本；必须原样保留，不进入自动翻译。
- CSV：不翻译 header，只翻译允许列。
- JSONL：保持记录数、字段名和 ID。
- TXT/MD：保留占位符、标签、换行和路径。

## 模型翻译要求

- 翻译内容必须由 Codex 模型完成，不允许把字典替换、正则替换或脚本校验当作完整翻译。
- 脚本只能负责提取、分批、格式保护和机械 QA。
- 批量输出前后都要由 Codex 模型抽查语义、风格和术语一致性。
- 对 UI/MCM 文本，模型校对应优先检查是否短、准、清楚。

## 必须保护

- JSON key、XML tag、CSV header。
- 文件名、路径、插件名、脚本名。
- `%s/%d/%f`、`{0}`、`{name}`、`$变量`。
- `<Alias=...>`、HTML/XML-like 标签、颜色/字体标签。
- `\n`、`\r\n` 和原始结构。

## QA 检查

- Interface 文件行数、key、tab 分隔和控制符不变。
- Interface 交付文件必须运行 `python scripts/audit_final_interface_translations.py --mod-name <ModName> --final-mod-dir out/<ModName>/汉化产出/final_mod`，确认 UTF-16 LE BOM、可解码、非空和 `$key<TAB>value` 结构。
- JSON/JSONL/XML/CSV 结构可解析，key/header/tag 不变。
- 资源 XML 如果进入 final_mod，必须和工作副本字节级一致；任何差异都作为误翻译或误改风险处理。
- 占位符、标签、换行和路径未丢失。
- 进入 final_mod 后运行 `python scripts/validate_final_text_structure.py`，确认同路径替换没有破坏 JSON key、XML tag/attribute name、INI section/key、CSV header、Interface key/tab/行数。
- 进入 final_mod 后运行 `scripts/new_final_text_review_packet.py`，把实际交付文本差异交给 Codex 模型做最终态语义和术语校对。
- 已运行 `scripts/proofread_translation.py` 或对应结构校对，并完成 Codex 模型校对。
- 未决术语已写入 `qa/unresolved_terms.md`。

## 完成标准

- 翻译结果已写入 `translated/interface/<ModName>/`、`translated/text_assets/<ModName>/` 或同路径同名的 final_mod overlay。
- 对应校验脚本已运行，结果写入 `qa/`。
- 如果目标是交付，overlay 已准备为直接替换原相对路径，而不是仅生成旁挂语言文件。
- 如果目标是交付，`qa/<ModName>.final_text_structure.md` 必须通过；XML 的 `name` 属性可翻译时不能被误判为 tag，但真实 tag 必须保持原样。
- 如果目标是交付且包含 `Interface/translations/*.txt`，`qa/<ModName>.final_interface_runtime.md` 必须通过且阻断/警告均为 0。
- 如果目标是交付，`qa/<ModName>.model_review.md` 必须明确覆盖 `qa/<ModName>.final_text_review_packet.md`。
- 格式不可解析或用途不明的字段未自动翻译。
- 输出可交给 `qa-validation` 或 `final-mod-assembly`。

## 失败处理

格式不可解析、字段用途不明或可能参与逻辑判断时停止自动翻译，保留原文并写入 QA。
