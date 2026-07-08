# Decoder-First Workflow

## 目标

减少 LexTranslator/xTranslator GUI 和 Computer Use 的使用，把汉化流程改成：

1. 先用项目内脚本和 CLI/库解码器导出文本中间文件。
2. 主控 agent 使用模型能力翻译、规范化、校验这些中间文件。
3. 只有必须写回二进制且没有可靠 CLI 写回器时，才进入 LexTranslator/xTranslator GUI。
4. GUI 产生的结果仍必须保存到项目内 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。

最终组装采用直接替换模式：所有译文产物都要以 Skyrim Data 根相对路径覆盖原版文件副本。语言旁挂文件、XML/JSONL 对照表和 patch-only 文件默认只作为中间件，不能替代 `final_mod` 中同路径同名文件的覆盖结果。

BSA 内资源也遵循同一规则：默认不重新打包 BSA；已汉化内容按归档内原始相对路径生成 loose override，由 `final_mod/` 中同路径 loose file 覆盖归档内资源。只有人工测试证明 loose override 不加载或导致 Mod 问题时，才进入高风险 BSA packer adapter 方案。

## 是否可以不通过 GUI

可以，但分文件类型：

| 文件类型 | 无 GUI 可行性 | 推荐路径 | 限制 |
|---|---|---|---|
| `Interface/translations/*.txt` | 完全可行 | Agent 文本管线 | 保留 key、tab、行数、占位符 |
| `MCM/Config/*.json`、`*.ini` | 完全可行 | Agent 结构化解析 | 只翻译 `text/help/pageDisplayName/desc` 等可见字段 |
| `FOMOD/*.xml`、JSON、XML、CSV、TXT、MD | 完全可行 | Agent 结构化解析 | 保留标签、key、属性、占位符 |
| `.zip` | 完全可行 | `prepare_mod_workspace.py` | 只读解压到项目内并生成 inventory/route 报告 |
| `.bsa/.ba2` | 审计可行；解包需 adapter | 首选 `bethesda-structs` 只读审计；BSA 解包先接 `BsaFileExtractorPath` | 未配置审计库或解包器时只生成提取计划 |
| `.esp/.esm/.esl` | 可行但需要插件解析/写回适配器 | 配置 `PluginTextCliPath`、`XEditPath` 或 `MutagenCliPath` | Agent 不直接改插件二进制 |
| `.pex` | 当前可通过 Mutagen 适配器导出和写回项目内 PEX 副本 | 配置 `PexStringToolPath` | Agent 不直接 patch PEX；只写回已确认的指令字符串；逻辑字符串不翻译 |
| `.psc` | 只读提取可行 | Agent 只读字符串候选提取 | 不回写、不编译 |

## 新优先级

```text
CLI/库适配器 > 可审计导出/导入 > GUI fallback > 人工 handoff
```

具体执行时，纯文本和结构化文本仍优先走 agent 文本管线；需要解码或写回的文件优先走项目内 CLI/库 adapter。LexTranslator/xTranslator 只作为最后自动化兜底，不作为项目能力证明的中心。

Computer Use 不是解码优先路径。它是 Codex-only GUI fallback，只在必须操作 GUI 工具时使用。

## 配置入口

`config/tools.local.json` 支持：

```json
{
  "LexTranslatorPath": "请填写本机 LexTranslator.exe 路径",
  "XTranslatorPath": "请填写本机 xTranslator.exe 路径",
  "DecoderFirst": true,
  "AllowGuiFallback": true,
  "AllowLaunchGuiTools": false,
  "PreferredAutomationBackend": "decoder-first",
  "GuiAutomationPython": "python",
  "DecoderTools": {
    "PluginTextCliPath": "",
    "XEditPath": "",
    "SafeSseDumpWrapperPath": "scripts/invoke_ssedump_safe.py",
    "DotNetSdkPath": "tools/dotnet-sdk/dotnet.exe",
    "MutagenSourceDir": "",
    "MutagenCliPath": "scripts/invoke_mutagen_plugin_text_tool.py",
    "PexStringToolPath": "scripts/invoke_mutagen_pex_string_tool.py",
    "ChampollionSourceDir": "",
    "PexDecompilerPath": "",
    "BsaFileExtractorPath": "",
    "BsaExtractorPath": "",
    "Ba2ExtractorPath": "",
    "Archive7zPath": ""
  },
  "RequireProjectLocalInputOutput": true,
  "DefaultModInputRoot": "mod",
  "DefaultExtractedModRoot": "work/extracted_mods",
  "DefaultSourceRoot": "source",
  "DefaultWorkRoot": "work",
  "DefaultTranslatedRoot": "translated",
  "DefaultOutputRoot": "out",
  "ToolOutputRoots": [
    "translated/tool_outputs",
    "out/tool_outputs"
  ],
  "NeverTouchRealGameDirectory": true,
  "NeverTouchRealModManagerDirectory": true,
  "NeverTouchSteamGameDirectory": true,
  "NeverTouchAppDataGameConfig": true,
  "NeverTouchDocumentsMyGames": true
}
```

检测命令：

```console
python .\scripts\detect_decoder_tools.py
```

报告输出：

```text
qa/decoder_tools_report.md
```

注意：xEdit/SSEDump 原始可执行文件不能仅因存在就视为 ready。未通过项目包装器指定 Data/master 路径时，它可能自动探测真实 Skyrim 目录；检测脚本会将其标记为 `requires-safe-wrapper`。

当前安全包装器：

```console
python .\scripts\invoke_ssedump_safe.py --plugin-path ".\work\extracted_mods\<ModName>\<Plugin>.esp" --data-path ".\work\extracted_mods\<ModName>" --output-path ".\source\plugin_dumps\<ModName>\<Plugin>.ssedump.txt" --report-path ".\qa\<Plugin>.ssedump_safe_report.md"
```

该包装器只读运行，并且：

- 所有路径必须在项目内。
- 禁止真实 Skyrim、Steam、MO2/Vortex、AppData、Documents/My Games 路径。
- 缺少项目内 master 时在启动前阻断。
- 不导入、不保存、不写回插件。

## 当前非 GUI 抽取入口

候选抽取：

```console
python .\scripts\extract_non_gui_candidates.py --mod-name <ModName>
```

输出：

```text
out/<ModName>/non_gui_exports/translation_candidates.jsonl
out/<ModName>/non_gui_exports/translation_candidates_unique.jsonl
out/<ModName>/non_gui_exports/protected_or_logic_strings.jsonl
out/<ModName>/non_gui_exports/manual_review_strings.jsonl
out/<ModName>/qa/non_gui_extraction_report.md
```

覆盖率审计：

```console
python .\scripts\audit_non_gui_coverage.py --mod-name <ModName>
```

输出：

```text
out/<ModName>/qa/non_gui_translation_coverage.md
out/<ModName>/qa/non_gui_remaining_gaps.jsonl
out/<ModName>/qa/non_gui_unverified_candidates.jsonl
```

常规接手和重跑优先使用非 GUI 总控入口，它会串联准备、构建、final_mod 校验、严格门禁和健康报告：

```console
python .\scripts\run_non_gui_translation_workflow.py --mod-name <ModName> --skip-prepare --workspace-path ".\work\extracted_mods\<ModName>"
```

该入口会在翻译阶段前运行 LexTranslator 风格动态词典索引刷新检查：如果当前工作区 `glossary/lextranslator_dynamic_dictionaries/` 目录及词表文件没有比 `work/glossary_rag/lextranslator_dynamic.sqlite` 更新，就复用现有索引；如果词典较新、索引缺失或索引版本变化，则重建索引。详情见 `docs/lextranslator_dictionary_rag.md`。

该入口以及严格门禁、状态刷新、健康检查都会使用 `work/.workflow.lock`，避免同一项目的报告和 `final_mod` 验证被并发运行互相覆盖。长流程会把用户可见状态写入 `.workflow/progress_card.*`，把开发者排查细节写入 `traces/latest.jsonl` 和 `traces/trace_summary.md`。

需要排查单个阶段时，再分步运行总门禁。进入 `final_mod` 交付前必须把候选抽取和覆盖率审计纳入总门禁：

```console
python .\scripts\run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete
```

总门禁之后生成接手总览：

```console
python .\scripts\test_workflow_health.py --mod-name <ModName> --run-strict-gate
```

输出：

```text
qa/workflow_state.md
qa/workflow_state.json
.workflow/progress_card.md
.workflow/progress_card.json
.workflow/progress_events.jsonl
.workflow/workflow_state.json
qa/workflow_timeline.md
qa/blockers.md
traces/latest.jsonl
traces/trace_summary.md
qa/workflow_health.md
qa/workflow_health.json
```

这些报告集中列出状态机、用户进度、开发者 trace、核心脚本、`skills/`、final text/binary review packet、模型校对、严格门禁和 `final_mod` 证据。后续 agent 如果只回答进度，应先读 `.workflow/progress_card.md`；如果选择下一步动作，应优先读取 `qa/workflow_state.json`，再读健康和 readiness 报告，避免重复探索分散的 QA 文件；脚本化接手优先读 JSON。

该门禁会重跑候选抽取和覆盖率审计，并要求：

- `Coverage missing: 0`
- `Coverage unverified: 0`
- `Strict complete mode: True`
- `Archive files checked` 和 `Archives missing evidence` 已记录；存在 BSA/BA2 时 `Archives missing evidence` 必须为 0
- `qa/<ModName>.final_text_structure.md` 已记录，且 `Blocking issues: 0`、`Warnings: 0`
- `qa/<ModName>.final_text_review_packet.md` 已记录，且 `qa/<ModName>.model_review.md` 明确覆盖该 packet
- `qa/<ModName>.final_binary_review_packet.md` 已记录，且 `Protected review items: 0`、`Export failures: 0`，并由 `qa/<ModName>.model_review.md` 明确覆盖
- 缺失插件译表、缺失 PEX 译表、候选覆盖率为 0 或任何 warning 都会阻断完成判定
- `qa/<ModName>.model_review.md` 由 agent 模型完成，且不早于最新译文输入
- `qa/final_mod_validation.md` 确认为 `direct-replacement-final-mod`

如果当前 `final_mod` 中已经存在经验证的项目内翻译结果，但对应覆盖层或 `tool_outputs` 缺失，可以恢复为可重建输入：

```console
python .\scripts\recover_final_mod_overlays.py --mod-name <ModName> --force
```

该脚本只复制项目内 `final_mod` 与 `work/extracted_mods` 的差异：

- 文本差异进入 `translated/final_mod/<ModName>/` 暂存 overlay；它只是 final_mod 组装输入，不是最终交付目录。
- 二进制差异进入 `out/<ModName>/tool_outputs/`。
- `.backup`、`meta/` 和压缩包会跳过。
- 二进制只做 byte-for-byte 复制；这不是非 GUI 写回器，也不是发布授权证明。

## ESP/ESM/ESL 的无 GUI 目标

目标不是让 agent 直接编辑插件，而是引入受控适配器：

1. 从项目内插件副本导出 `source/plugin_exports/<ModName>/*.jsonl`。
2. agent 模型翻译到 `translated/plugin_text/<ModName>/*.jsonl`。
3. 校验 FormID、EditorID、Record Type、占位符和换行。
4. 由受控 CLI/库适配器把译文写入项目内插件副本。
5. 输出到 `out/<ModName>/tool_outputs/<Plugin>.esp`。
6. 再由 `python scripts/build_final_mod.py` 复制进 `out/<ModName>/汉化产出/final_mod/<Plugin>.esp`，直接替换原插件副本。

没有受控 CLI 写回器时，仍然只能把 GUI 工具作为写回阶段的兜底。

### 当前已落地的 ESP 只读导出

不依赖真实 Skyrim 目录、不加载 master、不写回插件的轻量导出器：

```console
python .\scripts\export_esp_strings.py --plugin-path "work\extracted_mods\<ModName>\<Plugin>.esp" --mod-name "<ModName>"
```

输出：

```text
source/plugin_exports/<ModName>/<Plugin>.esp_strings.jsonl
qa/<Plugin>.esp_export_report.md
```

当前导出器会保留：

- Plugin 文件名
- Record Type
- FormID
- EditorID
- Group path
- Subrecord Type
- 原文、译文占位字段
- candidate/protected/review 风险分类

候选译文可以通过翻译表套用到中间文件：

```console
python .\scripts\apply_plugin_translation_map.py --export-path "source\plugin_exports\<ModName>\<Plugin>.esp_strings.jsonl" --translation-map-path "work\plugin_translation_maps\<ModName>\<Plugin>.translation_map.json" --mod-name "<ModName>"
```

输出：

```text
translated/plugin_exports/<ModName>/<Plugin>.esp_strings.zh.jsonl
qa/<Plugin>.esp_strings.translation_map_report.md
```

该步骤只生成翻译中间文件，不生成已汉化 ESP。真正写回仍需要 Mutagen/xEdit 受控适配器。

### 当前已落地的 Mutagen ESP 写回适配器

项目内 Mutagen adapter：

```console
python .\scripts\invoke_mutagen_plugin_text_tool.py --input-plugin-path "work\extracted_mods\<ModName>\<Plugin>.esp" --translation-jsonl-path "translated\plugin_exports\<ModName>\<Plugin>.esp_strings.zh.jsonl" --output-plugin-path "out\<ModName>\tool_outputs\<Plugin>.esp" --report-path "qa\<Plugin>.mutagen_write.md"
```

边界：

- 输入插件必须位于 `work/extracted_mods/`。
- 翻译 JSONL 必须位于 `translated/`。
- 输出插件必须位于 `out/`。
- 通过项目内 `tools/dotnet-sdk/dotnet.exe` 构建/运行。
- 使用 Mutagen 写回，不由 Codex 直接 patch 二进制。
- 写出时使用 UTF-8，避免中文变成 `?`。
- 写出时使用原插件 header master 顺序，避免 `MAST` 顺序漂移。
- 保留原 master 列表，不自动精简依赖。

验证命令：

```console
python .\scripts\verify_plugin_output.py --original-plugin-path "work\extracted_mods\<ModName>\<Plugin>.esp" --output-plugin-path "out\<ModName>\tool_outputs\<Plugin>.esp" --translation-jsonl-path "translated\plugin_exports\<ModName>\<Plugin>.esp_strings.zh.jsonl" --warn-only
```

## PEX 的无 GUI 目标

PEX 分两层：

1. 提取和判断可见字符串：优先使用 `python scripts/invoke_mutagen_pex_string_tool.py --mode Export` 导出 PEX 函数指令中的 `VariableType.String` 字符串。
2. 写回 PEX：使用 `python scripts/invoke_mutagen_pex_string_tool.py --mode Apply` 按已确认 JSONL 译表写入项目内 PEX 副本。

导出示例：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Export --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-jsonl-path "source\pex_exports\<ModName>\<Script>.pex_strings.jsonl" --report-path "qa\<Script>.pex_export_report.md"
```

写回示例：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Apply --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --output-pex-path "out\<ModName>\tool_outputs\Scripts\<Script>.pex" --report-path "qa\<Script>.mutagen_pex_write.md"
```

`python scripts/build_final_mod.py` 会把该工具输出覆盖到 `out/<ModName>/汉化产出/final_mod/Scripts/<Script>.pex`，而不是额外生成旁挂脚本补丁文件。

final_mod 交付前还要反读 ESP/PEX 实际输出：

```console
python .\scripts\new_final_binary_review_packet.py --mod-name "<ModName>" --workspace-path "work\extracted_mods\<ModName>" --final-mod-dir "out\<ModName>\汉化产出\final_mod"
```

输出：

```text
qa/<ModName>.final_binary_review_packet.md
qa/<ModName>.final_binary_review_items.jsonl
```

该包必须由 agent 模型在 `qa/<ModName>.model_review.md` 中明确校对。`Protected review items` 或 `Export failures` 非 0 时不能通过完整汉化门禁。

不可做：

- 不直接二进制 patch `.pex`。
- 不反编译 `.pex` 后自动改 `.psc` 并编译。
- 不翻译脚本 key、事件名、函数名、变量名、逻辑判断字符串。
- 不写回 `VariableType.Identifier`，不改对象名、属性名、状态名、函数名、调试符号和 user flag。

## QA 门槛

- decoder 报告：`qa/decoder_tools_report.md`
- 路由报告：`qa/routing_report.md`
- 候选抽取报告：`out/<ModName>/qa/non_gui_extraction_report.md`
- 覆盖率报告：`out/<ModName>/qa/non_gui_translation_coverage.md`
- 归档覆盖报告：`qa/<ModName>.archive_coverage.md`
- 归档内容 manifest：`out/<ModName>/archive_audits/<ArchiveName>/manifest.json`
- final_mod 文本结构：`qa/<ModName>.final_text_structure.md`
- final_mod 交付态文本校对包：`qa/<ModName>.final_text_review_packet.md`
- final_mod 交付态文本条目：`qa/<ModName>.final_text_review_items.jsonl`
- 翻译校验：`qa/validation_errors.md`
- 译文校对：`qa/translation_proofread.md` 或按 Mod 命名的 `qa/<ModName>.translation_proofread.md`
- PEX 风险：`qa/pex_risk_report.md`
- 插件输出验证：`qa/plugin_output_verification.md`
- final_mod 验证：`qa/final_mod_validation.md`
- 总门禁报告：`qa/<ModName>.non_gui_qa_gates.md`，最终交付使用 `--strict-complete`

任何 decoder/CLI 写回工具都必须证明：

- 输入路径在项目内。
- 输出路径在项目内。
- 不访问真实 Skyrim、MO2/Vortex、Steam、AppData、Documents/My Games。
- 不覆盖 `mod/` 原始文件。
- 二进制写回由受控工具完成，不由 Codex 直接改。

## 译文校对门槛

写回 ESP/PEX 之前，先对翻译中间文件运行：

```console
python .\scripts\proofread_translation.py --input-path "translated\plugin_exports\<ModName>\<Plugin>.esp_strings.zh.jsonl" --input-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --report-output-path "qa\<ModName>.translation_proofread.md" --issues-jsonl-path "qa\<ModName>.translation_proofread_issues.jsonl"
```

校对脚本只读输入并写 QA 报告，检查：

- protected/key/path/filename/FormID 是否被误翻。
- `%s`、`{0}`、`<Token.Name>`、`$变量`、`\n` 等占位符和控制符是否丢失。
- candidate 译文是否为空。
- 译文是否残留非 allowlist 英文。
- 是否出现现代网络口语或不适合游戏本地化的表达。

机械校对不能代替 agent 模型校对。译文生成、语义校对、风格修正和过度翻译风险判断必须由 agent 模型完成，并在 `qa/<ModName>.model_review.md` 留下结论。

## final_mod 文本结构门槛

进入完整 Mod 交付前，必须比较工作区内工作副本和 `out/<ModName>/汉化产出/final_mod/` 的文本结构：

```console
python .\scripts\validate_final_text_structure.py --mod-name <ModName>
```

该校验只读运行，检查：

- `Interface/translations/*.txt` 的行数、tab 分隔、key 和占位符。
- JSON/JSONL 的 key、记录数、数组长度、非字符串值和受保护路径/ID 值。
- XML 的真实 tag、attribute name、受保护路径属性和占位符；可见的 `name` 属性可以翻译，但不能误改 tag。
- INI 的 section/key 和受保护路径值。
- CSV header 和行数。
- `Source/scripts/*.psc` 是否仍是原样副本。

`python .\scripts\run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete` 会自动调用此校验；任何 warning 在严格完成模式下都会阻断交付。

## final_mod 交付态模型校对门槛

中间译表校对不能证明最终 Mod 目录里的文本一定正确。进入完整交付前，还要从工作副本和 `final_mod` 的差异生成实际交付文本包：

```console
python .\scripts\new_final_text_review_packet.py --mod-name <ModName>
```

输出：

```text
qa/<ModName>.final_text_review_packet.md
qa/<ModName>.final_text_review_items.jsonl
```

该包只包含 final_mod 中实际变化的文本值，例如 Interface 行、MCM JSON 字符串、FOMOD 可见 XML 文本和 INI 值。Agent 模型必须审查该包，并在 `qa/<ModName>.model_review.md` 中明确引用它。严格门禁会阻断以下情况：

- final text review packet 生成失败。
- protected-review 条目未处理。
- model review 没有提到该 packet。
- model review 早于最新译文输入或 final text review packet。

## 二进制输出验证

ESP/ESM/ESL：

```console
python .\scripts\verify_plugin_output.py --original-plugin-path ".\work\extracted_mods\<ModName>\<Plugin>.esp" --output-plugin-path ".\out\<ModName>\汉化产出\final_mod\<Plugin>.esp" --translation-xml-path ".\translated\xtranslator_ready\<ModName>\<Plugin>_english_chinese.xml" --warn-only
```

PEX：

```console
python .\scripts\verify_pex_output.py --original-pex-path ".\work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-pex-path ".\out\<ModName>\汉化产出\final_mod\Scripts\<Script>.pex" --translation-jsonl-path ".\translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --warn-only
```

这些验证是字节探针，不替代游戏内测试。`source gone but expected target not directly found` 表示原文未残留，但还需要更强解码器或游戏内检查确认最终显示文本。

## 当前状态

当前项目已经具备完整无 GUI 文本管线、MCM 管线、FOMOD 文本修复、PSC 只读候选抽取、ESP 只读导出、ESP JSONL 翻译中间文件、Mutagen ESP 写回适配器和 final_mod 覆盖率审计。

当前 BSA/BA2 的方向是先审计、后解包：

- MCM、Interface、FOMOD、JSON/XML/TXT 可以不使用 GUI。
- ESP 可通过项目内导出器和 Mutagen adapter 完成当前已覆盖记录类型的非 GUI 写回。
- PEX 可通过 Mutagen PEX adapter 导出函数指令字符串，并把已确认译文写回项目内 PEX 副本；仍需人工抽查和游戏内测试可见性。
- BSA 由 `bsa-archive-audit` 处理：首选 Python `bethesda-structs` 读取归档目录、分类可翻译候选并生成 manifest 证据；它不写归档。
- BSA 解包第一阶段接 `BsaFileExtractorPath`，必须通过 `scripts/invoke_bsa_file_extractor_safe.py` 限定输入和输出目录；BA2 解包仍需要单独 adapter。
- BSA 内完成汉化的 Interface、MCM、JSON/XML/TXT、PEX 工具输出等资源默认进入 `translated/final_mod/<ModName>/` 或 `out/<ModName>/tool_outputs/`，再作为同路径 loose override 组装进 `final_mod/`；原 BSA 仍原样复制。

## BSA/BA2 归档审计证据

如果 Mod 含有 BSA/BA2，严格完成模式要求有项目内内容审计证据。BSA 首选路径是 `bsa-archive-audit` 使用 `bethesda-structs` 只读解析归档目录，生成可翻译候选分类和 manifest；如果需要实际展开 BSA 内容，再由 `scripts/invoke_bsa_file_extractor_safe.py` 调用 `BsaFileExtractorPath` 配置的工具，并解到 `work/archive_extracts/<ModName>/<ArchiveName>/`。当前项目不默认解包 BA2；解包器未配置时，只能阻断完整交付，不能假装归档内没有文本。

BSA 默认先生成只读 manifest，不为证明归档存在而解包：

```console
python .\scripts\new_bsa_archive_manifest.py --mod-name <ModName> --archive-path "work\extracted_mods\<ModName>\<Archive>.bsa"
```

只有当归档内容必须物化到 `work/` 下时，才通过安全 wrapper 解包，然后生成 extraction-backed manifest：

```console
python .\scripts\new_archive_audit_manifest.py --mod-name <ModName> --archive-path "work\extracted_mods\<ModName>\<Archive>.bsa" --extracted-dir "work\archive_extracts\<ModName>\<Archive>" --force
```

输出：

```text
out/<ModName>/archive_audits/<ArchiveName>/manifest.json
out/<ModName>/archive_audits/<ArchiveName>/files.jsonl
qa/<ModName>.<ArchiveName>.archive_audit_manifest.md
```

manifest 会把归档内资源分为：

- `translatable`：Interface 翻译表、JSON/XML/INI/TXT 等可直接进入文本管线。
- `decoder-required`：PEX、STRINGS/DLSTRINGS/ILSTRINGS 等还需要对应 decoder。
- `manual-review`：SWF/GFX 或 PSC 等不能自动改写的内容。

## BSA loose override 交付策略

默认交付：

```text
final_mod/
├─ Example.bsa                         # 原样复制
└─ Interface/translations/foo_english.txt  # 从 BSA 提取、汉化后的同路径 loose override
```

规则：

- 不把已汉化资源重新写回原 BSA。
- 不自动重打包 BSA。
- 同路径 loose override 必须保留归档内原始相对路径和原文件名。
- QA 必须证明原 BSA 未被修改，且 loose override 进入 `final_mod/` 并有 provenance/hash 证据。
- `scripts/audit_archive_coverage.py` 会检查 `archive_audits` manifest 中每个 `Risk=translatable` 项是否在 `final_mod/` 中存在同路径 loose override；没有 loose file 时，必须在 `qa/<ModName>.archive_loose_override_exemptions.jsonl` 写入明确豁免记录。
- 豁免记录是 JSONL，每行至少包含 `Archive`、`RelativePath`、`Status`、`Reason`、`Reviewer`；`Status` 只能是 `accepted`、`approved` 或 `exempted`，可选 `EvidencePath` 必须指向项目内存在文件。
- 只有玩家人工测试发现 loose override 不加载或导致 Mod 问题时，才记录 `bsa_repack_required_without_adapter` 或进入后续受控 BSA packer adapter。
