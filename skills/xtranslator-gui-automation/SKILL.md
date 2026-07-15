---
name: xtranslator-gui-automation
description: "用于 Codex-only xTranslator GUI 后备、精修和 Skyrim STRINGS 受控流程。中文触发：xTranslator、STRINGS、DLSTRINGS、ILSTRINGS、查漏、精修、插件导入导出、PapyrusPex、GUI 后备。Use only after the current Game Profile and router explicitly select this real Skill with workspace-local paths. Skyrim string tables route here; Fallout 4 string tables remain blocked/manual-review. Do not access real game paths or edit binaries directly."
---

# xTranslator GUI Automation

## 目标

只负责 xTranslator 的工具操作：启动、连接窗口、打开工作区内文件、导入、导出、查漏、保存和记录日志。工具优先级由 `translation-task-router` 决定，翻译范围由文件类型 Skill 决定。

## 全局硬约束

- xTranslator 自动化只在 Windows 运行，并由项目 Python 入口启动受控适配器。
- 不在本 Skill 重述桌面通用规则；执行前完整加载 `docs/gui_automation_rules.md`。xTranslator 还要求 Profile 明确授权，工具自身生成工作区二进制输出，并继续阻断未经认证的 Fallout 4 GUI 路径。

## 输入

- 工作区内插件副本、PEX 副本或导出文件。
- 工作区内 XML、字典、批处理脚本或翻译材料。
- 当前 Game Profile 为 xTranslator 声明的 `glossary/sst/<game>/` 原生 SST 词典；RAG 读取 SST 不代表 GUI 已加载该词典。
- 目标输出目录：`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。

## 输出

- xTranslator 生成的工作区内文件。
- `qa/tool_invocation_log.md`。
- `qa/xtranslator_gui_report.md`。

## 推荐工具

- Computer Use (`@电脑`)
- `scripts/invoke_xtranslator.py`
- `docs/gui_automation_rules.md`
- Python `pywinauto` 降级适配器

## 具体流程

1. 按 `docs/gui_automation_rules.md` 完成授权、路径、可执行文件、窗口、截图和降级前置检查。
2. 需要 SST 时先确认词典目录属于当前 Game Profile，不得加载其他游戏目录。
3. 打开工作区输入，按上游任务执行导入、导出、查漏或 PapyrusPex 后备操作。
4. 保存到允许的 `tool_outputs` 目录并写 `qa/xtranslator_gui_report.md`，再交回 `qa-validation`。

## 后备定位

- 对 Skyrim `.strings/.dlstrings/.ilstrings`，本 Skill 是 router 指定的 Codex-only 受控 GUI 路径；不得把二进制 string table 当普通文本处理。非 Codex 顶层 adapter 必须 blocked 并交回 Codex。
- 对 ESP/ESM/ESL，xTranslator 默认是 LexTranslator 后的精修、查漏和验证工具。
- 对 PEX，xTranslator PapyrusPex 默认是 LexTranslator 失败或不可用时的后备工具。
- 已知项目特定操作记录若存在，应保存在本地 `qa/xtranslator_operation_record.md`；继续同一模组时先读取，避免重复探索。

## 禁止事项

- 不决定字符串是否可翻译。
- 不决定主工具优先级。
- 不直接写真实游戏 Data 或真实 MO2/Vortex。
- 不覆盖 `mod/` 原始插件或 PEX。
- Computer Use 可以使用当前窗口截图中的窗口相对坐标，但必须先确认目标控件。
- pywinauto/UI Automation 降级方案不默认固定屏幕坐标点击。
- 不跳过保存路径确认。

## 完成标准

- 使用 `docs/gui_automation_rules.md` 的共同完成/失败标准；xTranslator 额外要求 `qa/xtranslator_gui_report.md` 绑定实际输出和操作证据。

## 失败处理

统一执行 `docs/gui_automation_rules.md` 的失败处理。本 Skill 只补充 xTranslator 的窗口、操作和输出证据到 `qa/xtranslator_gui_report.md`。
