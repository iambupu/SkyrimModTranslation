---
name: mcm-translation
description: Use for Skyrim MCM visible text rules across Interface translations, JSON, ESP exports, and PEX exports including menu pages, option labels, help text, and settings UI. Do not use for generic text assets, GUI automation, or script logic keys.
---

# MCM Translation Rules

## 目标

只定义 MCM 文本的来源识别规则、可翻译范围、保护内容和 QA 要求。本 Skill 不直接操作 GUI 工具，不决定 LexTranslator/xTranslator 优先级，不决定下游 Skill，不改写底层文件。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前项目内。
- Mod 原始输入只允许来自当前项目 `mod/` 沙盒或项目内工作副本。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- 不直接修改插件或 PEX 二进制。

## 来源识别规则

- `Interface/translations/*.txt` 中的 MCM 文本通常是低风险文本表。
- 工具导出的 MCM 字符串按导出格式保留 key、id 和字段结构。
- `.pex` 中的 MCM 文本只有确认玩家可见时才允许翻译。
- `.psc` 源码只允许只读提取字符串，不能回写或编译。
- 具体下游 Skill 和工具仍由 `translation-task-router` 决定。

## 可翻译内容

- 页面标题、选项显示名、按钮文本。
- 帮助说明、状态说明、玩家可见提示。
- 简短 UI 文本应短、准、清楚。

## 模型翻译要求

- MCM 文本必须由 Codex 模型翻译或复核，重点检查短、准、清楚。
- 脚本只能提取候选、保留 key/id、检查占位符和控制符。
- 模型校对必须确认 page id、option id、StorageUtil key、JsonUtil key、setting key 没有被当成显示文本翻译。

## 必须保护

- page id、option id、state id。
- StorageUtil key、JsonUtil key、setting key。
- 脚本名、函数名、变量名、属性名。
- `$变量`、占位符、控制符、换行和标签。

## 输出要求

- 翻译结果进入 `translated/mcm/<ModName>/`、`translated/interface/<ModName>/` 或工具准备目录。
- MCM 可见文本候选抽取默认使用 `python scripts/extract_mcm_text.py --input-path <MCMDir> --mod-name <ModName>`，输出 `work/normalized/<ModName>/mcm_text_candidates.jsonl` 和 `qa/mcm_extraction_report.md`。
- 用于 `final_mod` 交付的 MCM 文本必须按原相对路径和原文件名准备 overlay；例如 Interface MCM 翻译默认覆盖原 `*_english.txt`，而不是只新增 `*_chinese.txt`。
- 未决或高风险字符串写入 `qa/mcm_review.md`。
- 来源类型、未决项和处理状态写入报告，便于编排层跟踪。

## QA 要求

- key/id 不变。
- 占位符和控制符保留。
- UI 文本短而明确。
- 已完成 Codex 模型校对，并记录需要人工确认的 UI/脚本边界项。
- 生成 `qa/mcm_review.md`。

## 完成标准

- 已判断 MCM 文本来源属于 Interface、工具导出、PEX 可见字符串或 PSC 只读候选。
- page id、option id、StorageUtil key、JsonUtil key 和脚本标识未被翻译。
- 玩家可见菜单文本已输出到项目内翻译目录或工具准备目录。
- 面向交付的输出已能被 final_mod 直接替换原文件加载。
- `qa/mcm_review.md` 已记录来源、风险和待人工确认项。

## 失败处理

无法判断来源或是否为显示文本时，保持原文，写入 `qa/mcm_review.md` 或 `qa/unresolved_terms.md`，等待人工确认。
