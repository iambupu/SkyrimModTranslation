---
name: lextranslator-gui-automation
description: "用于 Codex-only LexTranslator GUI 自动化后备。中文触发：LexTranslator、打开 Lex、导入导出、GUI 写回、保存到 tool_outputs。Use only after the current Game Profile and router explicitly select a certified LexTranslator fallback for workspace-local import/export/apply/save. Neutral wording does not certify Fallout 4 GUI support. Do not select translatable strings, access real game paths, or edit binaries directly."
---

# LexTranslator GUI Automation

## 目标

只负责 LexTranslator 的工具操作：启动、连接窗口、打开工作区内文件、导入或应用工作区内翻译材料、保存到工作区内输出目录、记录日志。工具优先级由 `translation-task-router` 决定，翻译范围由文件类型 Skill 决定。

## 全局硬约束

- LexTranslator 桌面执行仅支持 Windows，启动与降级适配器都走插件源 Python 入口。
- 桌面安全、路径隔离、Computer Use/UIA 顺序和失败证据只以 `docs/gui_automation_rules.md` 为准。LexTranslator 的附加条件是当前 Profile 已认证，且二进制结果必须由该工具生成到工作区；Fallout 4 不因中性措辞自动获得认证。

## 输入

- 项目内插件副本、PEX 副本或工具导出文本。
- 项目内翻译对、词典或导入材料。
- 目标输出目录：`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。

## 输出

- LexTranslator 生成的工作区内文件。
- `qa/tool_invocation_log.md`。
- `qa/lextranslator_gui_report.md`。

## 推荐工具

- Computer Use (`@电脑`)
- `scripts/invoke_lextranslator_gui.py`
- `scripts/automate-lextranslator-gui.py`
- `docs/gui_automation_rules.md`
- Python `pywinauto` 降级适配器

## 具体流程

1. 按 `docs/gui_automation_rules.md` 完成授权、路径、可执行文件、窗口、截图和降级前置检查。
2. 打开项目内输入，按上游任务导入或应用翻译材料。
3. 保存到允许的 `tool_outputs` 目录，并写 `qa/lextranslator_gui_report.md`。
4. 把结果交回 `qa-validation` 和对应文件类型 Skill；窗口启动或文件加载不算完成。

## PEX 工作区内副本写回规则

- PEX 输入必须是工作区内副本，例如 `out/<ModName>/tool_outputs/Scripts/<ScriptName>.pex`。
- PEX 可见字符串翻译材料必须来自项目内，例如 `translated/lextranslator_ready/<ModName>/*pex*.jsonl`。
- LexTranslator 中执行 PEX/STRINGS 翻译时，必须点击 `STRINGS(...)` 右侧的蓝色播放按钮；不要点击其右侧齿轮设置按钮。
- LexTranslator 保存后的 PEX 仍只能留在项目内 tool_outputs。
- 不得打开或保存真实游戏目录、真实 MO2/Vortex 目录或 `mod/` 原始 PEX。
- 详细执行清单见 `docs/pex_visible_strings_writeback.md`。

## 禁止事项

- 不决定字符串是否可翻译。
- 不决定 LexTranslator 与 xTranslator 的优先级。
- 不写真实游戏目录或真实 Mod 管理器目录。
- Computer Use 可以使用当前窗口截图中的窗口相对坐标，但必须先确认目标控件。
- pywinauto/UI Automation 降级方案不默认固定屏幕坐标点击。
- 不跳过保存路径确认。
- 不把 GUI 失败伪装成完成。

## 完成标准

- 使用 `docs/gui_automation_rules.md` 的共同完成/失败标准；LexTranslator 额外要求 `qa/lextranslator_gui_report.md` 绑定实际输出和操作证据。

## 失败处理

统一执行 `docs/gui_automation_rules.md` 的失败处理。本 Skill 只补充 LexTranslator 的窗口、操作和输出证据到 `qa/lextranslator_gui_report.md`。
