# Tool Adapter

## 为什么需要 Tool Adapter

LexTranslator 和 xTranslator 是本插件工作流的外部 GUI 后备工具，不是核心卖点。Tool Adapter 的作用是让主控 agent 优先通过 CLI/库适配器处理工作区内输入输出；只有 Codex adapter 额外拥有 GUI 自动化和 Computer Use fallback，并且同样必须把输入路径、输出路径和日志记录限制在当前工作区内。

工具优先级固定为：

```text
CLI/库适配器 > 可审计导出/导入 > GUI fallback > 人工 handoff
```

优先投入方向是 Mutagen、xEdit/SSEDump 安全包装器、PEX string tool 等可日志化、可重跑、可 hash 校验的 adapter。GUI 只在 decoder/CLI 缺失、格式不支持或必须由 GUI 写回工作区内副本时进入。

## 主动调用边界

- 只允许针对当前工作区内的 `mod/`、`source/`、`work/`、`translated/`、`out/` 路径。
- 不访问真实 Skyrim 游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不访问 Steam 游戏安装目录。
- 不访问 AppData 或 Documents/My Games 下的配置目录。
- Agent 不直接修改 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`。
- 外部工具可以在 GUI 自动化控制下生成工作区内输出，但输出路径必须位于 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。

## GUI 无法完全自动化时

如果 LexTranslator 或 xTranslator 不支持可靠命令行参数，且 decoder/CLI 路径已经被证明不可用，Codex 可以使用 Computer Use 操作 GUI：打开工作区内输入、执行翻译/导出、保存到工作区 `tool_outputs`。GUI 自动化必须记录日志、保存前后路径和 QA 结果。

如果当前环境没有可用 GUI 自动化能力，或 GUI 状态无法可靠识别，该工具步骤必须标记为 blocked。人工接管可以作为临时处置，但不能算作全流程自动化完成。

## Decoder-first 适配器

GUI 之前必须优先尝试插件提供的 decoder 路径：

- ESP/ESM/ESL 只读导出：`scripts/export_esp_strings.py`
- ESP/ESM/ESL 翻译中间文件：`scripts/apply_plugin_translation_map.py`
- ESP/ESM/ESL Mutagen 写回：`scripts/invoke_mutagen_plugin_text_tool.py`
- PEX 指令字符串导出/工作区内副本写回：`scripts/invoke_mutagen_pex_string_tool.py`
- xEdit/SSEDump 上下文 dump：只能通过 `scripts/invoke_ssedump_safe.py`
- BSA 只读归档审计：首选 `scripts/new_bsa_archive_manifest.py` 调用 Python `bethesda-structs`，只生成目录、候选分类和 manifest 证据，不写归档。
- BSA 解包第一阶段：`DecoderTools.BsaFileExtractorPath` 指向 BSAFileExtractor 工具路径；实际调用必须通过 `scripts/invoke_bsa_file_extractor_safe.py`，wrapper 必须拒绝项目外输入，并且只能输出到 `work/archive_extracts/<ModName>/<ArchiveName>/`。
- BSA 汉化交付：默认不需要 packer；已汉化资源按归档内原始相对路径作为 loose override 进入 `final_mod/`。BSA packer adapter 只能作为人工测试证明 loose override 不可用后的高风险后续能力。
- BSA/BA2 loose override 门禁：`scripts/audit_archive_coverage.py` 会要求 manifest 中每个 `Risk=translatable` 条目在 `final_mod/` 同路径存在，或存在 `qa/<ModName>.archive_loose_override_exemptions.jsonl` 豁免记录。
- BA2 只读 inventory 可由 `bsa-archive-audit` / `bethesda-structs` 生成；materialization 只属于 `ba2-archive-audit`，通过 `scripts/invoke_ba2_extractor_safe.py` 的受控 protocol 在隔离 staging 中提取，并生成绑定源 BA2、adapter、limits 和预发布 payload snapshot 的 receipt/manifest/files hash 证据，再由 `scripts/verify_ba2_extraction.py` 独立验证。译文只以同路径 loose override 交付；BA2 不写回、不重打包。

只读导出和翻译表脚本只生成工作区内中间文件或报告，不保存插件。Mutagen ESP 写回脚本是受控适配器，只允许从 `work/extracted_mods/` 读取、从 `translated/` 读取翻译 JSONL，并写到 `out/`。Mutagen PEX 写回脚本只改 PEX 函数指令中的 `VariableType.String` 字面量，不改函数名、变量名、属性名、状态名、标识符、user flag 或 debug symbol。

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

脚本会检查输入路径是否在工作区内，检查工具路径是否存在，然后启动 LexTranslator 并记录 `qa/tool_invocation_log.md`。后续 GUI 自动化应把输出保存到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。

## xTranslator 示例

```console
python .\scripts\invoke_xtranslator.py --input-path .\mod\ExampleMod.esp --optional-mode "gui-automation"
```

脚本启动 xTranslator 后，GUI 自动化可以继续打开工作区内文件并将输出保存到工作区 `tool_outputs`。Agent 不直接改写插件；插件输出必须由 xTranslator/LexTranslator 产生并记录。

## 安全路径检查

新 Python 工具脚本必须内置工作区路径校验：相对路径先解析到工作区根目录，绝对路径必须位于工作区根目录内，输出路径允许不存在但父路径仍必须在工作区内。

GUI/工具包装统一使用 Python 入口并内置工作区路径校验；不再维护额外的 shell 包装层。
