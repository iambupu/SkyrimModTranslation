# LexTranslator Workflow

本页是 Codex 的 LexTranslator GUI 后备合同。具体窗口操作由 `lextranslator-gui-automation` Skill 执行；本页只定义进入条件、输入输出、证据和停止条件。

## 触发条件

只有以下条件同时成立时才能进入 LexTranslator：

1. `translation-task-router` 已选择 LexTranslator GUI fallback；
2. 当前 Game Profile 明确认可该 GUI 路径；
3. decoder/CLI 不可用、格式不支持，或必须由 GUI 工具写回工作区副本；
4. 当前主控是 Codex。

通用说明不构成 string-table GUI 认证。Skyrim/Fallout 4 STRINGS 与 Fallout 4 localized plugin 固定 blocked，不得因为 decoder 失败转入 GUI。

## 必读输入

- 当前工作区 `.skyrim-chs-workspace.json` 和 Game Profile；
- Router 输出和对应文件类型 Skill；
- `config/tools.local.json` 中的 LexTranslator 路径；
- `qa/decoder_tools_report.md` 或相关 decoder 失败证据；
- 待处理的工作区内副本和已有 QA 报告。

LexTranslator 可执行文件路径只能来自工作区本地配置。路径缺失、不可访问或指向工作区外输入时必须停止。

## 词典准备

进入 GUI 前按 [LexTranslator Dictionary RAG](./lextranslator_dictionary_rag.md) 准备当前 Game Profile 的词典索引和 Mod 命中包。本页只使用该结果；不重复定义索引、SST/EET 解码或检索命令。命中包只提供术语提示，不代表 GUI 已执行，也不能替代模型翻译或 QA。

## 执行动作

1. 优先使用 Computer Use，先截图确认目标窗口和控件。
2. 确认源语言、目标语言、输入副本和输出路径。
3. 先用一个工作区内样本验证导入、导出和保存路径，再处理同类输入。
4. 只处理 Router 允许的玩家可见文本；不修改脚本逻辑、`.pex` 或 `.psc`。
5. 存在 `Interface/translations/*.txt` 时优先走独立文本流程，不碰 PEX。
6. 输出前抽样检查占位符、控制符、术语和未翻译项。

Computer Use 不可用、无法识别窗口或操作失败时，记录原因后才允许降级到项目 pywinauto/UIA 入口：

```powershell
python .\scripts\invoke_lextranslator_gui.py --input-path ".\out\<ModName>\tool_outputs\<PluginName>.esp" --mode inspect
python .\scripts\invoke_lextranslator_gui.py --input-path ".\out\<ModName>\tool_outputs\<PluginName>.esp" --mode open
```

`inspect` 只检查窗口；`open` 只加载工作区输入。二者都不代表翻译、应用或保存完成。降级脚本不得默认使用固定屏幕坐标。

## 输出与证据

原始导出、翻译中间文件、最终导入文件和 QA 记录必须分别保留。二进制输入先复制到受控工具输出目录，LexTranslator 生成的结果只能进入：

```text
translated/tool_outputs/<ModName>/
out/<ModName>/tool_outputs/
```

Agent 不得绕过 LexTranslator 直接保存插件。工具调用必须写日志，输出必须经过对应插件或 PEX 验证脚本和后续严格 QA。

## 停止条件

- Router 或 Game Profile 未授权；
- Skyrim/Fallout 4 STRINGS 家族，或 Fallout 4 localized plugin；
- 输入或输出路径无法确认在当前工作区内；
- GUI 保存目标无法确认；
- Computer Use 与 pywinauto/UIA 降级均失败；
- 当前适配器只完成窗口检查或文件加载；
- 工具无法把输出保存到工作区 `tool_outputs`。

停止时写 blocked 报告和失败原因。人工临时保存只能作为记录，不能算自动化完成。
