---
name: lextranslator-gui-automation
description: Use only after routing selects LexTranslator GUI for Skyrim ESP, ESM, ESL, MCM, PEX, dictionary import, export, apply, or save into workspace-local tool_outputs. Do not use to decide translatable strings or edit binaries directly.
---

# LexTranslator GUI Automation

## 目标

只负责 LexTranslator 的工具操作：启动、连接窗口、打开工作区内文件、导入或应用工作区内翻译材料、保存到工作区内输出目录、记录日志。工具优先级由 `translation-task-router` 决定，翻译范围由文件类型 Skill 决定。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 进入 GUI fallback 后，Computer Use 是第一优先级，用于连接 LexTranslator 窗口、截图确认、点击、键盘输入和保存路径确认。
- pywinauto/UI Automation 是降级方案；只有 Computer Use 在当前会话不可用、无法识别窗口或操作失败时才使用。
- 输入输出路径必须在当前工作区内。
- 打开 Mod 原始文件时只能使用当前工作区 `mod/` 沙盒副本或工作区内工作副本。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- Codex 不直接修改插件或 PEX 二进制；二进制输出必须由 LexTranslator 生成。

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

1. 遵守 `docs/gui_automation_rules.md`。
2. 读取 `config/tools.local.json` 并校验 LexTranslator 路径存在。
3. 校验输入、导入材料和输出目录都在当前工作区内。
4. 优先用 Computer Use 启动或连接 LexTranslator。
5. 用 Computer Use 获取当前窗口截图；基于截图确认目标按钮、文件对话框和保存路径。
6. 只有 Computer Use 不可用或失败时，才记录原因并降级到 pywinauto/UI Automation。
7. 打开项目内输入文件。
8. 按上游任务要求导入或应用项目内翻译材料。
9. 保存或导出到项目内 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
10. 写入工具调用日志和 GUI 报告。
11. 交回 `qa-validation` 和对应文件类型 Skill 做结果判断。

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

## QA 检查

- 输入文件、导入材料和输出目录都在当前工作区内。
- 保存路径经过确认，且没有指向真实游戏目录、MO2/Vortex 或 `mod/` 原始文件。
- GUI 报告记录窗口、操作、输出路径和失败状态。
- 失败时有截图、blocked 日志和人工步骤。

## 完成标准

- 已读取 `config/tools.local.json` 并确认 LexTranslator 路径存在。
- 所有打开、导入、保存或导出的路径都在当前工作区内。
- 输出已保存到 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- `qa/tool_invocation_log.md` 和 `qa/lextranslator_gui_report.md` 已记录操作结果。
- 失败时已写入 blocked 状态、截图和人工操作清单，没有直接改写二进制。

## 失败处理

Computer Use 失败时先记录失败原因；如降级到 pywinauto/UI Automation，也必须记录降级原因。降级后仍失败时，保存截图到 `qa/gui_screenshots/`，生成失败日志和 `qa/manual_tool_steps.md`，在 `qa/tool_invocation_log.md` 标记工具阶段 blocked。不得回退到直接改写二进制。
