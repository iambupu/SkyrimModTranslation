# Tool Adapter

## 为什么需要 Tool Adapter

LexTranslator 和 xTranslator 是本项目的外部 GUI 工具。Tool Adapter 的作用是让 Codex 能主动判断是否应该启动工具，并通过 CLI、GUI 自动化或 Computer Use 将输入路径、输出路径和日志记录限制在当前项目内。

## 主动调用边界

- 只允许针对当前项目内的 `mod/`、`source/`、`work/`、`translated/`、`out/` 路径。
- 不访问真实 Skyrim 游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不访问 Steam 游戏安装目录。
- 不访问 AppData 或 Documents/My Games 下的配置目录。
- Codex 不直接修改 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`。
- 外部工具可以在 GUI 自动化控制下生成项目内输出，但输出路径必须位于 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。

## GUI 无法完全自动化时

如果 LexTranslator 或 xTranslator 不支持可靠命令行参数，Codex 可以使用 Computer Use 操作 GUI：打开项目内输入、执行翻译/导出、保存到项目内 `tool_outputs`。GUI 自动化必须记录日志、保存前后路径和 QA 结果。

如果当前环境没有可用 GUI 自动化能力，或 GUI 状态无法可靠识别，该工具步骤必须标记为 blocked。人工接管可以作为临时处置，但不能算作全流程自动化完成。

## Decoder-first 适配器

GUI 之前必须优先尝试项目内 decoder 路径：

- ESP/ESM/ESL 只读导出：`scripts/export_esp_strings.py`
- ESP/ESM/ESL 翻译中间文件：`scripts/apply_plugin_translation_map.py`
- ESP/ESM/ESL Mutagen 写回：`scripts/invoke_mutagen_plugin_text_tool.py`
- PEX 指令字符串导出/项目内副本写回：`scripts/invoke_mutagen_pex_string_tool.py`
- xEdit/SSEDump 上下文 dump：只能通过 `scripts/invoke_ssedump_safe.py`

只读导出和翻译表脚本只生成项目内中间文件或报告，不保存插件。Mutagen ESP 写回脚本是受控适配器，只允许从 `work/extracted_mods/` 读取、从 `translated/` 读取翻译 JSONL，并写到 `out/`。Mutagen PEX 写回脚本只改 PEX 函数指令中的 `VariableType.String` 字面量，不改函数名、变量名、属性名、状态名、标识符、user flag 或 debug symbol。

## 配置方式

1. 在 `config/tools.local.json` 填入本机 LexTranslator 和 xTranslator 路径。
2. 保持 `AllowLaunchGuiTools` 为 `true` 才允许脚本启动 GUI。
3. 运行 `python scripts/validate_tools_config.py` 校验路径。

`config/tools.local.json` 已加入 `.gitignore`，不要提交。真实工具路径只允许保存在本机配置里；仓库文档和示例只能使用占位路径或 `config/tools.example.json`。

示例配置：

```json
{
  "LexTranslatorPath": "C:\\Path\\To\\LexTranslator.exe",
  "XTranslatorPath": "C:\\Path\\To\\xTranslator.exe",
  "AllowLaunchGuiTools": true
}
```

## LexTranslator 示例

```console
python .\scripts\invoke_lextranslator.py --input-path .\source\lextranslator_exports\example.jsonl --optional-mode "gui-automation"
```

脚本会检查输入路径是否在项目内，检查工具路径是否存在，然后启动 LexTranslator 并记录 `qa/tool_invocation_log.md`。后续 GUI 自动化应把输出保存到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。

## xTranslator 示例

```console
python .\scripts\invoke_xtranslator.py --input-path .\mod\ExampleMod.esp --optional-mode "gui-automation"
```

脚本启动 xTranslator 后，GUI 自动化可以继续打开项目内文件并将输出保存到项目内 `tool_outputs`。Codex 不直接改写插件；插件输出必须由 xTranslator/LexTranslator 产生并记录。

## 安全路径检查

新 Python 工具脚本必须内置项目路径校验：相对路径先解析到项目根目录，绝对路径必须位于项目根目录内，输出路径允许不存在但父路径仍必须在项目内。

GUI/工具包装统一使用 Python 入口并内置项目路径校验；不再维护额外的 shell 包装层。
