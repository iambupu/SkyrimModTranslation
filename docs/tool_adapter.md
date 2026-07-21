# Tool Adapter

## 为什么需要 Tool Adapter

LexTranslator、xTranslator 和可选的 ESP-ESM Translator 是本插件工作流的外部 GUI 工具，不是核心卖点。Tool Adapter 的作用是让主控 agent 优先通过 CLI/库适配器处理工作区内输入输出；只有 Codex adapter 额外拥有 GUI 自动化和 Computer Use fallback，并且同样必须把输入路径、输出路径和日志记录限制在当前工作区内。

工具优先级固定为：

```text
CLI/库适配器 > 可审计导出/导入 > GUI fallback > 人工 handoff
```

优先投入方向是 Mutagen、xEdit/SSEDump 安全包装器、PEX string tool 等可日志化、可重跑、可 hash 校验的 adapter。GUI 只在 decoder/CLI 缺失、格式不支持或必须由 GUI 写回工作区内副本时进入。

## 能力解析与 Adapter 选择

Game Profile 分别声明插件文本、PEX、BSA/BA2、外部字符串表和 loose text 的级别与 adapter id。调用方先按 `inventory`、`read`、`write` 或 `strict_complete` 解析资源能力，再从受控 Registry 取得入口；不得通过 `game_id`、`support_level` 或旧 `plugin_adapter` 别名选择代码分支。

`bethesda-string-tables` 提供 STRINGS/DLSTRINGS/ILSTRINGS 的 inventory、extract、apply 和 verify，并通过 AdapterResult、输入输出哈希和独立复核报告约束交付。Skyrim 与 Fallout 4 Profile 当前都将该能力声明为 `experimental_write`，真实 Mod、xEdit 和游戏内验收完成后才能分别提升；二者都不能仅凭字符串表证据完成 localized 插件交付，xTranslator GUI 输出也不能冒充受控 adapter 证据。

`unsupported` capability 可以不声明 adapter 和格式选项。已启用的插件文本 adapter 必须在 Registry 中提供统一的 Python `extract`/`apply` 入口合同；`run_plugin_translation_stage.py` 从 Registry 取得脚本名。内置 Bethesda plugin adapter 再通过 Profile 的 `extract_backend` 与 `localized_plugin_policy` 选择已注册行为，未知值必须阻断，不能回退到某个游戏实现。

严格 QA 只评估 final_mod 实际使用的能力，并交叉核对扫描清单、AdapterResult 与 provenance。Profile 和公共工作流报告不接受旧顶层能力字段；工具报告只记录本次调用所需的 adapter identity、options 和 hash 证据。

严格完成按“实际操作”判定：`inventory_only` 只认证 inventory，`read_only` 认证 read，`experimental_write` 的读取仍可用于严格交付，但其写回不可放行；只有 `stable` capability 的 write 可以通过严格完成。归档 loose override 因此要求受控读取证据和稳定的 `loose_text/write`，不要求归档 adapter 实现不存在的 apply。

## 主动调用边界

- 只允许针对当前工作区内的 `mod/`、`source/`、`work/`、`translated/`、`out/` 路径。
- 不访问当前 Game Profile 对应的真实游戏目录。
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

下列脚本名是当前内置 Registry 映射和共享文本中间件，不是跨游戏硬编码合同。插件/PEX 二进制操作必须先解析 capability 和 adapter id；Registry 返回其他 adapter 时使用其注册入口，未知映射直接阻断。

- ESP/ESM/ESL 只读导出：`scripts/export_esp_strings.py`
- ESP/ESM/ESL 翻译中间文件：`scripts/apply_plugin_translation_map.py`
- ESP/ESM/ESL 当前内置 `mutagen-bethesda-plugin` 写回：`scripts/invoke_mutagen_plugin_text_tool.py`
- Skyrim 与 Fallout 4 的 `.esl` 或带 light trait 插件按 `experimental_write` 处理；任何插件 Apply/Verify 都必须确认当前插件和全部 masters 的 master-style map。`.esp/.esm` master 需要工作区内 header，或由路径、SHA256 和 Small flag 绑定的 schema v2 manifest 提供证据；未知、陈旧或冲突证据分别以稳定错误码阻断。只读 inventory/export 不要求补齐该写回证据。
- 完整插件阶段会先尝试从插件同目录和 `work/master_context/` 的游戏/Mod 分区查找 master，生成按插件相对路径区分的 schema v2 manifest。预检失败不阻断只读 export；发现写回候选后才在翻译前记录当前插件的 `master_style_preflight_blocked`，不得拖到 Apply 才失败。没有写回候选时不要求补齐 master-style 证据；直接 adapter 调用仍可显式传入 `--master-style-manifest`。
- STRINGS/DLSTRINGS/ILSTRINGS：`scripts/invoke_bethesda_string_table_tool.py` 提供 Inventory、Export、Apply 和 Verify；修改后的表只从受控 `tool_outputs/Strings/` 交付。
- Localized plugin 联合交付：`scripts/invoke_bethesda_localized_delivery.py` 绑定插件锚点、引用覆盖、语言、组件 AdapterResult、输入输出 hash 和 composite receipt；generic plugin/string-table 路径不能单独满足该能力。
- PEX 当前内置 `mutagen-pex` 指令参数导出/工作区内副本写回：`scripts/invoke_mutagen_pex_string_tool.py`。Fallout 4 写回只接受版本化 API 注册表证明为玩家可见的直接调用参数；文本形态和模型判断不能提高权限。
- xEdit/SSEDump 上下文 dump：只能通过 `scripts/invoke_ssedump_safe.py`
- BSA 只读归档审计：首选 `scripts/new_bsa_archive_manifest.py` 调用 Python `bethesda-structs`，只生成目录、候选分类和 manifest 证据，不写归档。
- BSA 解包第一阶段：`DecoderTools.BsaFileExtractorPath` 指向 BSAFileExtractor 工具路径；实际调用必须通过 `scripts/invoke_bsa_file_extractor_safe.py`，wrapper 必须拒绝项目外输入，并且只能输出到 `work/archive_extracts/<ModName>/<ArchiveName>/`。调用必须传入 `--adapter-result-path qa/<ModName>.<ArchiveName>.bsa_extract.adapter_result.json`，由 wrapper 在同一次提取中生成 extraction-backed manifest、files JSONL、QA 报告和 AdapterResult。单独运行 `new_archive_audit_manifest.py` 不能建立严格门禁所需的 AdapterResult lineage。
- BSA 汉化交付：默认不需要 packer；已汉化资源按归档内原始相对路径作为 loose override 进入 `final_mod/`。BSA packer adapter 只能作为人工测试证明 loose override 不可用后的高风险后续能力。
- BSA/BA2 loose override 门禁：`scripts/audit_archive_coverage.py` 会要求 manifest 中每个 `Risk=translatable` 条目在 `final_mod/` 同路径存在，或存在 `qa/<ModName>.archive_loose_override_exemptions.jsonl` 豁免记录。
- BA2 只读 inventory 和 materialization 都由 `ba2-archive-audit` 编排。`scripts/invoke_ba2_extractor_safe.py` 可用项目内置读取器选择性 materialize Fallout 4 GNRL；DX10 只做 inventory，完整提取可转受控外部 adapter。两条路径都先执行 limits/timeout/disk preflight，在 staging 中生成绑定源 BA2、adapter、limits 和 payload snapshot 的 receipt/manifest/files hash，再由 `scripts/verify_ba2_extraction.py` 独立验证并事务式发布。译文只以同路径 loose override 交付；BA2 不写回、不重打包。

只读导出和翻译表脚本只生成工作区内中间文件或报告，不保存插件。插件 JSONL 的可写候选由受控 Mutagen exporter 和 `PluginFieldContract` 共同限定；宽泛的 TES4 解析结果只用于发现，固定标记为不可写回。Mutagen ESP 写回脚本只允许从 `work/extracted_mods/` 读取、从 `translated/` 读取 schema v2 JSONL，并写到 `out/`。Mutagen PEX 写回脚本只改受控合同允许的直接 `VariableType.String` 字面量；Fallout 4 还要求精确调用位置语义授权，不改函数名、变量名、属性名、状态名、标识符、动态参数、user flag 或 debug symbol。

## 配置方式

1. 在 `config/tools.local.json` 填入本机 LexTranslator 和 xTranslator 路径；需要记录 EET4 时填写可选的 `EspEsmTranslatorPath`。
2. 保持 `AllowLaunchGuiTools` 为 `true` 才允许脚本启动 GUI。
3. 运行 `python scripts/validate_tools_config.py` 校验路径。

`config/tools.local.json` 已加入 `.gitignore`，不要提交。真实工具路径只允许保存在本机配置里；仓库文档和示例只能使用占位路径或 `config/tools.example.json`。

示例配置：

```json
{
  "LexTranslatorPath": "C:\\Path\\To\\LexTranslator.exe",
  "XTranslatorPath": "C:\\Path\\To\\xTranslator.exe",
  "EspEsmTranslatorPath": "C:\\Path\\To\\EET4.exe",
  "AllowLaunchGuiTools": true
}
```

EET4 目前没有项目受控写回 adapter。`.eet` 进入 RAG 时由 `scripts/glossary_binary_formats.py` 只读解码，不会启动 EET4；配置可执行文件路径不构成自动写回授权。

## LexTranslator 示例

```powershell
python .\scripts\invoke_lextranslator.py --input-path .\source\lextranslator_exports\example.jsonl --optional-mode "gui-automation"
```

脚本会检查输入路径是否在工作区内，检查工具路径是否存在，然后启动 LexTranslator 并记录 `qa/tool_invocation_log.md`。后续 GUI 自动化应把输出保存到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。

## xTranslator 示例

```powershell
python .\scripts\invoke_xtranslator.py --input-path .\mod\ExampleMod.esp --optional-mode "gui-automation"
```

脚本启动 xTranslator 后，GUI 自动化可以继续打开工作区内文件并将输出保存到工作区 `tool_outputs`。Agent 不直接改写插件；插件输出必须由 xTranslator/LexTranslator 产生并记录。

## 安全路径检查

新 Python 工具脚本必须内置工作区路径校验：相对路径先解析到工作区根目录，绝对路径必须位于工作区根目录内，输出路径允许不存在但父路径仍必须在工作区内。

GUI/工具包装统一使用 Python 入口并内置工作区路径校验；不再维护额外的 shell 包装层。
