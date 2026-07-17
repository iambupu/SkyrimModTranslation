# xTranslator Workflow

本页是 Codex 的 xTranslator GUI 后备合同。具体窗口操作由 `xtranslator-gui-automation` Skill 执行；本页只定义进入条件、受保护内容、输出证据和停止条件。

## 触发条件

只有 Router 明确选择 xTranslator、当前 Game Profile 认证该 GUI 路径且当前主控为 Codex 时才能进入。适用范围包括精修、查漏、对照、复杂导入和受控 PapyrusPex 后备。

通用说明不构成 string-table GUI 认证。当前 Skyrim 与 Fallout 4 的 STRINGS 家族都固定 blocked；Fallout 4 localized plugin 同样 blocked。decoder 失败本身不授权 GUI。

## 必读输入

- 当前工作区 marker 和 Game Profile；
- Router 输出与对应文件类型 Skill；
- `config/tools.local.json` 中的 xTranslator 路径；
- 工作区内插件或 PEX 副本；
- 已有译表、术语和 QA 报告。

如果 xTranslator 配置中出现真实游戏、Steam、MO2/Vortex、AppData 或 `Documents/My Games` 路径，Agent 仍只能打开和保存工作区内副本。

## 受保护内容

PapyrusPex 只提取玩家可见字符串。不得翻译函数名、变量名、属性名、状态名、事件名、StorageUtil key、JsonUtil key 或任何参与脚本判断的字符串。Agent 不直接修改 `.pex`，也不编译 `.psc`。

## 执行动作

1. 优先使用 Computer Use，并先截图确认窗口、输入文件和保存目标。
2. 每次只处理一个插件或一个资源 lane，避免输出混淆。
3. 只打开工作区内副本，不围绕 `mod/` 原始二进制执行保存操作。
4. 可以导出文本供 Agent 分析，但译文必须经过对应文件类型 Skill 和模型校对。
5. 保存前检查未翻译项、占位符、FormID/EditorID、路径和受保护脚本标识符。

Computer Use 不可用或失败时，按 `xtranslator-gui-automation` Skill 规定的 pywinauto/UIA 路径降级；不得默认使用固定屏幕坐标。

## 输出与 QA

保存或导出目标只能位于：

```text
out/<ModName>/tool_outputs/
translated/tool_outputs/<ModName>/
```

Agent 不得绕过 xTranslator 直接保存插件。插件输出运行：

```powershell
python scripts\verify_plugin_output.py
```

PEX 输出运行：

```powershell
python scripts\verify_pex_output.py
```

验证通过后仍需进入 final review、严格 QA 和人工游戏测试。玩家尚未提供游戏内结果时，不得把实机验证描述成已完成。

## 停止条件

- Router 或 Game Profile 未授权；
- Skyrim/Fallout 4 STRINGS 家族，或 Fallout 4 localized plugin；
- 输入不是工作区副本；
- 保存路径无法确认在 `tool_outputs` 内；
- Computer Use 与降级 GUI 自动化均失败；
- 只完成窗口打开、文件加载或文本查看；
- 输出验证失败或 PEX 受保护内容无法确认。

停止时写 blocked 报告和工具日志，并说明缺失的自动化或人工测试条件；不得把人工操作伪装成自动完成。
