# Computer Use / GUI Automation Rules

## 目标

为 LexTranslator 和 xTranslator 桌面 fallback 自动化提供共用安全规则。具体工具操作仍由 `lextranslator-gui-automation` 和 `xtranslator-gui-automation` Skill 执行。

## 规则

- Windows 10；GUI 启动流程统一走 Python 入口。
- Decoder/CLI 是翻译流程第一优先级；decoder 不可用本身不授权 GUI。只有当前 Game Profile 明确认可该 GUI 能力、`translation-task-router` 已选择对应 GUI Skill，且确实需要 GUI 写回工作区内副本时才能进入。
- 当前措辞泛化不代表 Fallout 4 GUI 路径已经认证。Fallout 4 localized plugin/STRINGS 固定 blocked，不得转入 LexTranslator/xTranslator 兜底。
- 进入 GUI fallback 后，Computer Use 是第一优先级，用于连接窗口、截图确认、点击、键盘输入和保存路径确认。
- pywinauto/UI Automation 是 GUI 降级方案；只有 Computer Use 在当前会话不可用、无法识别目标窗口或当前操作失败时才使用。
- Computer Use 可以基于当前窗口截图使用窗口相对坐标，但必须先截图确认目标控件。
- pywinauto/UI Automation 降级方案禁止默认使用固定屏幕坐标。
- 输入路径必须在当前工作区内。
- 输出路径必须在当前工作区内。
- 打开 Mod 原始文件时只能使用当前工作区 `mod/` 沙盒副本或工作区内工作副本。
- 不访问当前 Game Profile 对应的真实游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不直接修改插件或 PEX 二进制。

## 输出位置

- 常规工具输出：`out/<ModName>/tool_outputs/`。
- 兼容工具输出：`translated/tool_outputs/<ModName>/`。
- GUI 日志：`qa/tool_invocation_log.md`。
- 工具报告：`qa/lextranslator_gui_report.md` 或 `qa/xtranslator_gui_report.md`。
- 失败截图：`qa/gui_screenshots/`。
- 人工操作清单：`qa/manual_tool_steps.md`。

## 失败处理

Computer Use 不可用、窗口不可识别、控件找不到、保存路径不可验证或工具报错时，先记录 Computer Use 失败原因；如降级到 pywinauto/UI Automation，也必须记录降级原因。降级仍失败时立即停止工具阶段，保存截图到 `qa/gui_screenshots/`，记录窗口标题、进程名、AutomationId 或控件树摘要，生成失败日志和 `qa/manual_tool_steps.md`。同时在 `qa/tool_invocation_log.md` 标记阶段为 blocked。不得把 GUI 失败伪装成完成，不得回退到直接改写二进制。
