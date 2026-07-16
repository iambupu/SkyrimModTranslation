---
name: mcm-translation
description: "用于按当前 Game Profile 处理 MCM 菜单、选项、帮助和页面可见文本。中文触发：MCM、菜单页面、设置界面、MCM Helper、SkyUI、Fallout 4 MCM。Use for profile-supported visible MCM text across Interface files, JSON, plugin exports, and PEX exports. Do not assume Skyrim encodings for Fallout 4, process generic assets, operate GUI, or translate script logic keys."
---

# MCM Translation Rules

## 目标

只定义 MCM 文本的来源识别规则、可翻译范围、保护内容和 QA 要求。本 Skill 不直接操作 GUI 工具，不决定 LexTranslator/xTranslator 优先级，不决定下游 Skill，不改写底层文件。

## 全局硬约束

- 继承 `translation-task-router` 的 Windows、工作区路径、`mod/` 输入和真实游戏目录隔离合同；本节只补充 MCM 限制。
- 不直接修改插件或 PEX 二进制。

## 来源识别规则

- MCM 是 container，不是单一 JSON 文件类型。先识别 `MCM/` 容器，再按 JSON、INI、TOML、TXT、Interface 文本、插件导出或 PEX 导出的实际格式处理。
- MCM 嵌套在 protected 或 F4SE 路径下时，不能覆盖外层 container。此时分别按 protected 原样复制或 F4SE 人工确认处理。
- `Interface/translations/*.txt` 中的 MCM 文本通常是低风险文本表。
- JSON/INI 使用 Agent Structured MCM Extractor。LexTranslator 仅作为 Codex 后备。
- TXT 使用 Agent Text Pipeline，并保留行结构、占位符、key、路径和内部标识。
- TOML 当前没有安全写回实现，只允许 manual review，不生成自动译文或写回产物。
- 工具导出的 MCM 字符串按导出格式保留 key、id 和字段结构。
- `.pex` 中的 MCM 文本只有确认玩家可见时才允许翻译。
- `.psc` 源码只允许只读提取字符串，不能回写或编译。
- 具体下游 Skill 和工具仍由 `translation-task-router` 决定。

## 可翻译内容

- 页面标题、选项显示名、按钮文本。
- 帮助说明、状态说明、玩家可见提示。
- 简短 UI 文本应短、准、清楚。
- `On`、`OK`、`Enabled` 等单词形式的 `label`、`title`、`text` 仍是可翻译候选；短或无空格本身不能作为标识符证据。

## 模型翻译要求

- MCM 文本必须由 agent 模型翻译或复核，重点检查短、准、清楚。
- 脚本只能提取候选、保留 key/id、检查占位符和控制符。
- 模型校对必须确认 page id、option id、StorageUtil key、JsonUtil key、setting key 没有被当成显示文本翻译。

## 必须保护

- page id、option id、state id。
- `OnPageReset(Page)`、`SetPageResetHandler` 或同类回调中用于匹配页面的标题字符串；这类字符串即使显示给玩家，也同时是 page id，不能写回译文。
- StorageUtil key、JsonUtil key、setting key。
- 脚本名、函数名、变量名、属性名。
- `$变量`、占位符、控制符、换行和标签。

## 输出要求

- 翻译结果进入 `translated/mcm/<ModName>/`、`translated/interface/<ModName>/` 或工具准备目录。
- MCM 可见文本候选抽取默认使用 `python scripts/extract_mcm_text.py --input-path <MCMDir> --mod-name <ModName>`，输出 `work/normalized/<ModName>/mcm_text_candidates.jsonl` 和 `qa/mcm_extraction_report.md`。
- 当前提取器直接支持 JSON 和 INI。TXT 由 Agent Text Pipeline 处理。TOML 只写入 manual review 记录，不得假装已有安全写回实现。
- 用于 `final_mod` 交付的 MCM 文本必须按原相对路径和原文件名准备 overlay；例如 Interface MCM 翻译默认覆盖原 `*_english.txt`，而不是只新增 `*_chinese.txt`。
- `Interface/translations/*.txt` 类型的 MCM overlay 必须使用当前 Game Profile 声明的运行时编码与结构，并保持 key、行数、tab 和控制符。Skyrim SE/AE 默认要求 UTF-16 LE BOM 与 `$key<TAB>value`；Fallout 4 不得沿用该规则猜测，必须由 profile 对应验证器放行。
- 未决或高风险字符串写入 `qa/mcm_review.md`。
- 来源类型、未决项和处理状态写入报告，便于编排层跟踪。

## QA 要求

- key/id 不变。
- 如果 MCM 文本来自 PEX，写回译表不得包含 `CMP_*` 比较指令中的字符串；这些字符串应标记为 protected 或保留空 target。
- `Interface/translations/*.txt` 交付态必须通过 `python scripts/audit_final_interface_translations.py --mod-name <ModName> --final-mod-dir out/<ModName>/汉化产出/final_mod`。
- 占位符和控制符保留。
- UI 文本短而明确。
- 已完成 agent 模型校对，并记录需要人工确认的 UI/脚本边界项。
- 生成 `qa/mcm_review.md`。

## 完成标准

- MCM 容器内的 JSON、INI、TOML、TXT 都已分类，且没有按单一 JSON 规则处理整个容器。
- `MCM/**/*.json`、`MCM/**/*.ini` 已由 Agent Structured MCM Extractor 处理，或明确记录了 Codex-only LexTranslator 后备结果。
- `MCM/**/*.txt` 已由 Agent Text Pipeline 处理，并保留原始结构。
- `MCM/**/*.toml` 已明确记录为 manual review，未生成自动译文或写回产物。
- 自动处理与 manual review 结果已分别记录，不能把 manual 状态计为翻译完成。
- 已判断其他 MCM 文本来源属于 Interface、工具导出、PEX 可见字符串或 PSC 只读候选。
- page id、option id、StorageUtil key、JsonUtil key 和脚本标识未被翻译。
- 玩家可见菜单文本已输出到项目内翻译目录或工具准备目录。
- 面向交付的输出已能被 final_mod 直接替换原文件加载。
- 如果 MCM 来源是 `Interface/translations/*.txt`，`qa/<ModName>.final_interface_runtime.md` 必须显示 `Blocking issues: 0`、`Warnings: 0`。
- `qa/mcm_review.md` 已记录来源、风险和待人工确认项。

## 失败处理

无法判断来源或是否为显示文本时，保持原文，写入 `qa/mcm_review.md` 或 `qa/unresolved_terms.md`，等待人工确认。
