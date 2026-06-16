---
name: xtranslator-gui-automation
description: Use only after routing selects xTranslator GUI for Skyrim plugin refinement, missing-text review, validation, import/export, save, or PapyrusPex fallback using project-local paths. Do not use as primary tool unless routed, decide translatable strings, or edit binaries directly.
---

# xTranslator GUI Automation

## 目标

只负责 xTranslator 的工具操作：启动、连接窗口、打开项目内文件、导入、导出、查漏、保存和记录日志。工具优先级由 `translation-task-router` 决定，翻译范围由文件类型 Skill 决定。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 进入 GUI fallback 后，Computer Use 是第一优先级，用于连接 xTranslator 窗口、截图确认、点击、键盘输入和保存路径确认。
- pywinauto/UI Automation 是降级方案；只有 Computer Use 在当前会话不可用、无法识别窗口或操作失败时才使用。
- 输入输出路径必须在当前项目内。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- Codex 不直接修改插件或 PEX 二进制；二进制输出必须由 xTranslator 生成。

## 输入

- 项目内插件副本、PEX 副本或导出文件。
- 项目内 XML、字典、批处理脚本或翻译材料。
- 目标输出目录：`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。

## 输出

- xTranslator 生成的项目内文件。
- `qa/tool_invocation_log.md`。
- `qa/xtranslator_gui_report.md`。

## 推荐工具

- Computer Use (`@电脑`)
- `scripts/invoke_xtranslator.py`
- `docs/gui_automation_rules.md`
- Python `pywinauto` 降级适配器

## 具体流程

1. 遵守 `docs/gui_automation_rules.md`。
2. 读取 `config/tools.local.json` 并校验 xTranslator 路径存在。
3. 校验输入、导入材料和输出目录都在当前项目内。
4. 优先用 Computer Use 启动或连接 xTranslator。
5. 用 Computer Use 获取当前窗口截图；基于截图确认目标菜单、对话框、按钮和保存路径。
6. 只有 Computer Use 不可用或失败时，才记录原因并降级到 pywinauto/UI Automation。
7. 打开项目内输入文件。
8. 按上游任务要求执行导入、导出、查漏、PapyrusPex 处理或保存。
9. 保存或导出到项目内 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
10. 写入工具调用日志和 GUI 报告。
11. 交回 `qa-validation` 和对应文件类型 Skill 做结果判断。

## 后备定位

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

## QA 检查

- 输入文件、导入材料和输出目录都在当前项目内。
- 保存路径经过确认，且没有指向真实游戏目录、MO2/Vortex 或 `mod/` 原始文件。
- GUI 报告记录窗口、操作、输出路径和失败状态。
- 失败时有截图、blocked 日志和人工步骤。

## 完成标准

- 已读取 `config/tools.local.json` 并确认 xTranslator 路径存在。
- 所有打开、导入、保存或导出的路径都在当前项目内。
- 输出已保存到 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- `qa/tool_invocation_log.md` 和 `qa/xtranslator_gui_report.md` 已记录操作结果。
- 失败时已写入 blocked 状态、截图和人工操作清单，没有直接改写二进制。

## 失败处理

Computer Use 失败时先记录失败原因；如降级到 pywinauto/UI Automation，也必须记录降级原因。降级后仍失败时，保存截图到 `qa/gui_screenshots/`，生成失败日志和 `qa/manual_tool_steps.md`，在 `qa/tool_invocation_log.md` 标记工具阶段 blocked。不得回退到直接改写二进制。
