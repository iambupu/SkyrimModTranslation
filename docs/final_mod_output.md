# Final Mod Output

## 目标

本项目最终输出不只是零散翻译文件，而是一个固定层级的汉化产出目录：

```text
out/<ModName>/
└─ 汉化产出/
   ├─ final_mod/
   ├─ intermediate/
   ├─ <ModName>_CHS.zip
   └─ package_report.md
```

final_mod 是完整 Mod，不是零散翻译补丁。`final_mod/` 用于人工检查和 MO2/Vortex 本地安装测试。`final_mod/meta/provenance.jsonl` 是逐文件产物溯源清单，记录每个交付文件的直接来源、来源 SHA256、最终 SHA256、transform、tool 和 QA 证据入口。`intermediate/` 汇总本次汉化相关的中间产出，且必须包含可检查的 `translation_text_dictionary/` 翻译文本词典。`<ModName>_CHS.zip` 是已打包好的汉化 Mod，文件名必须带 `_CHS` 后缀。

工作区 `glossary/` 中当前 Game Profile 声明的文本/SST/EET 词典和 `work/glossary_rag/` 索引只是翻译辅助，不是 `final_mod/` 的一部分，也不替代 `intermediate/translation_text_dictionary/`。RAG 命中包可以进入 `out/<ModName>/汉化产出/intermediate/` 作为参考证据，但原始词典不得打入 `_CHS.zip`。

自动化目标是：在当前工作区内自动完成扫描、路由、文本处理、GUI/CLI 工具操作、翻译产物覆盖、`final_mod` 组装、`intermediate` 汇总、CHS 包生成、manifest 生成和 QA 校验。真实游戏目录安装和 Codex 直接改写插件二进制不纳入自动化范围。

## 交付模式

默认交付模式是直接替换，而不是旁挂语言补丁文件。

- 文本资源译文必须按原始 Data 相对路径和原文件名写入 overlay，例如覆盖 `Interface/translations/<Plugin>_english.txt` 的 `final_mod` 副本。
- BSA/BA2 内提取出的可翻译资源完成汉化后，按归档内 Data 相对路径写入 loose overlay，并由 `final_mod/` 中同路径 loose file 覆盖归档内资源。原归档只原样复制；BSA 默认不重打包，BA2 当前禁止重打包。BA2 来源还必须保留受控 extraction receipt、manifest、entry hash 和独立验证证据。
- 当前 Game Profile 允许写回的 ESP/ESM/ESL 和 PEX 译文，必须先由项目内受控适配器写入 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/` 的同路径同名副本，再由 `python scripts/build_final_mod.py` 覆盖原路径。Fallout 4 `.esl`、带 light trait 的插件和 localized 插件不具备该资格。
- `*_chinese.txt`、xTranslator XML、LexTranslator JSONL、DSD patch 等默认是中间文件；除非已证明游戏会加载该路径，否则不能作为最终交付的唯一翻译文件。
- `meta/manifest.json` 必须记录 `DeliveryMode = direct-replacement-final-mod`、替换了哪些原文件、哪些只是新增 overlay。
- `meta/provenance.jsonl` 必须覆盖 `final_mod/` 中除自身以外的每个文件；自身使用 `status = self-referential` 记录。来源文件必须仍位于项目内，来源 hash 和最终 hash 必须能复核。
- `python scripts/validate_final_mod.py` 会统计 `Language sidecar files` 和 `Language sidecar overlays`。如果新增 overlay 是 `Interface/translations/*_chinese.txt` 这类旁挂语言文件，会作为阻断错误处理。

## 目录结构

最终结构中，`final_mod/` 应保持当前 Game Profile 的 Data 根结构。下面只展示两个 Profile 共有的典型目录；Profile 专用目录不写成虚构的二选一路径：

```text
out/<ModName>/汉化产出/final_mod/
  <PluginName>.esp
  <PluginName>.esm
  <PluginName>.esl
  Interface/
  Scripts/
  Meshes/
  Textures/
  Sound/
  Fomod/
  meta/
    manifest.json
    build_report.md
    qa_report.md
    provenance.jsonl
    source_files.md
out/<ModName>/汉化产出/intermediate/
  README.md
  translation_text_dictionary/
    manifest.json
    translation_dictionary.jsonl
    translation_dictionary.md
    raw_sources/
  tool_outputs/
  final_mod_overlay/   # staging overlay source mirrored into intermediate; not a final delivery root
  xtranslator_import/
  dsd_patch/
  lex_dictionary/
  archive_audits/
  qa/
out/<ModName>/汉化产出/<ModName>_CHS.zip
out/<ModName>/汉化产出/package_report.md
```

Skyrim 工作区可以按原始 Mod 结构保留 `SKSE/`、`Seq/` 等目录；Fallout 4 工作区可以保留 `F4SE/`、`Materials/`、`Music/`、`Video/`、`Vis/`、`Seq/` 等目录。实际目录必须来自工作区 Mod 副本并属于当前 Game Profile，不得为了匹配示例主动创建空目录。

`translation_text_dictionary/` 是必备中间产出，不是最终游戏加载文件。它用于人工复查和后续 agent 接手，必须能展示已翻译文本的 `source -> target` 对照：

- `translation_dictionary.jsonl` 是完整、机器可读的统一译表；按译文条目保留上下文，不因相同 `source -> target` 重复出现而丢行。
- `translation_dictionary.md` 是人工可读预览。
- `raw_sources/` 镜像生成词典所用的项目内 JSONL/XML 译表来源，例如插件导出译表、PEX 可见字符串译表、xTranslator XML 或 LexTranslator JSONL。
- `manifest.json` 必须记录 `TranslatedEntryCount` 和 `SourceFileCount`。

`meta/provenance.jsonl` 是最终交付的逐文件来源账本，每行是一个 JSON 对象，例如：

```jsonl
{"file":"final_mod/Interface/translations/foo_english.txt","source":"translated/final_mod/Foo/Interface/translations/foo_english.txt","source_sha256":"...","file_sha256":"...","transform":"text-resource-translation","tool":"Agent Text Pipeline","generated_by":"build_final_mod.py","status":"assembled","qa_evidence":["qa/final_mod_validation.md"]}
{"file":"final_mod/foo.esp","source":"out/Foo/tool_outputs/foo.esp","source_sha256":"...","file_sha256":"...","transform":"controlled-tool-output","tool":"MutagenAdapter/LexTranslator/xTranslator","generated_by":"build_final_mod.py","status":"assembled","qa_evidence":["qa/final_mod_validation.md"]}
```

这个文件不替代模型校对或游戏内测试；它证明的是“这个交付文件从哪个项目内来源来、经过哪个受控阶段、当前 hash 是什么”。`qa/final_mod_validation.md` 会把该账本校验为项目内 QA 证据。

## 构建

```powershell
python .\scripts\build_final_mod.py --mod-name <ModName>
```

`SourceModDir` 应优先使用 `work/extracted_mods/<ModName>/` 中已经解压好的 Mod Data 根目录。`mod/` 下的项目内 `.zip` 和 `.7z` 必须先通过 `mod-input-preparation` 只读解压到 `work/extracted_mods/<ModName>/`，再进入 final_mod 组装；`.7z` 优先使用 Python `py7zr`，不可用时回退到 `DecoderTools.Archive7zPath`。两者都不可用时写阻断报告。`.rar` 默认只生成提取建议。

如果输出目录已存在且非空，需要显式使用：

```powershell
python .\scripts\build_final_mod.py --mod-name <ModName> --force
```

## 校验

```powershell
python .\scripts\validate_final_mod.py --final-mod-dir .\out\<ModName>\汉化产出\final_mod\
python .\scripts\validate_chs_package.py --mod-name <ModName>
```

校验报告写入 `qa/final_mod_validation.md`。

报告里的 `Delivery` 段必须能证明：

- `Delivery mode: direct-replacement-final-mod`
- `FinalModDir: out/<ModName>/汉化产出/final_mod/`
- `Intermediate output dir` 存在。
- `Translation text dictionary` 存在，且 `Translation dictionary entries` 大于 0。
- `Packaged CHS mod` 存在且文件名以 `_CHS.zip` 结尾。
- `Direct replacement files` 大于 0，除非该 Mod 确认没有任何翻译覆盖。
- `Language sidecar overlays: 0`

报告里的 `Provenance` 段必须能证明：

- `Provenance entries` 与 `meta/manifest.json` 的 `ProvenanceEntryCount` 一致。
- `Missing provenance rows: 0`
- `Final file SHA256 mismatches: 0`
- `Source SHA256 mismatches: 0`

`qa/<ModName>.chs_package_validation.md` 必须证明：

- `_CHS.zip` 中没有绝对路径、`..` 或重复条目。
- `_CHS.zip` 的文件列表与 `final_mod/` 完全一致。
- 每个同名文件的 SHA256 与 `final_mod/` 完全一致。
- 同级 `intermediate/translation_text_dictionary/translation_dictionary.jsonl` 存在、非空、JSONL 有效，且翻译条目数与 `manifest.json` 一致。该词典不会打进 `_CHS.zip`，但缺失或为空时 CHS 包校验必须失败。

项目级交付还必须生成：

- `qa/project_completion_audit.md`：证明所有 known Mod outputs 的项目内静态交付证据完整。
- `qa/<ModName>.chs_package_validation.md`：证明人工安装测试的 CHS 包与通过 QA 的 `final_mod/` 是同一份内容。
- `qa/manual_game_test_plan.md`：列出每个 CHS 包的真实游戏/MO2/Vortex 人工验证步骤。
- `qa/manual_game_test_results.template.json`：按当前 CHS 包 SHA256 和 final_mod manifest SHA256 生成可填写的人工测试结果模板。
- `qa/manual_game_test_results_validation.md`：验证人工填写结果是否覆盖全部检查项并匹配当前输出。
- `qa/translation_goal_compliance.md`：把校对工作流目标拆成严格校对、全文件覆盖、无漏汉化、无语义质量阻断和玩家实机外部验证边界。每个 Mod 行必须显示翻译文本词典条目数和 `Final review quality` 状态，便于接手时直接确认 `intermediate/translation_text_dictionary/` 与最终反读质量都已通过。玩家尚未提交真实游戏测试结果时，该报告必须把玩家实机验证标为 `out_of_scope_for_proofreading_workflow`，不能因此阻断项目内校对工作流 `complete`。

## 清理

```powershell
python .\scripts\clean_final_mod.py --final-mod-dir .\out\<ModName>\汉化产出\final_mod\
```

使用 `--force` 可跳过确认。该脚本只允许清理 `out/<ModName>/汉化产出/final_mod/`。

## 安全边界

- 不访问当前 Game Profile 对应的真实游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不自动安装。
- 只在项目内生成 `<ModName>_CHS.zip`；不复制到真实 Mod 管理器目录，不声明可公开再分发。
- 不修改插件、BSA、BA2、PEX、DLL、EXE 等二进制文件。
- 不重打包 BSA。除非人工测试证明同路径 loose override 不加载或导致 Mod 问题，并且后续新增受控 BSA packer adapter、manifest、hash 校验和 QA 证据，否则 BSA 始终原样保留。
- 不重打包 BA2。Fallout 4 BA2 materialization 只能读取受控 adapter 生成且通过独立验证的 receipt、manifest 和 entry hash；交付只使用同路径 loose override，源 BA2 的 hash 必须保持不变。
- 归档 manifest 中 `Risk=translatable` 的 BSA/BA2 条目必须在 `final_mod/` 中有同路径 loose override；确认为无需交付的条目必须写入 `qa/<ModName>.archive_loose_override_exemptions.jsonl`，不能静默缺失。
- Materials、Meshes、Textures、Sound、Music、Video、Vis、Seq 下的资源默认受保护，只能从工作区 `mod/` 原样复制。source SHA256 与 final SHA256 必须相同，provenance transform 必须是 `original-copy`。
- SWF、GFX、DLL、EXE 同样只能从工作区 `mod/` 原样复制。不得反编译后回写，也不得由宽泛的 Tool Adapter 二进制条款替换。
- `tool_outputs` 只允许当前 Game Profile 明确开放写回的插件或 PEX。输出必须保持原相对路径和原文件名，并替换 `final_mod` 中已有的同路径文件。
- Fallout 4 `.esl`、带 light trait 的插件、localized 插件和 STRINGS 家族不得作为受控写回进入 `final_mod`。
- 翻译文件默认覆盖原相对路径；旁挂语言补丁文件不能替代原文件覆盖，除非已在 QA 中记录加载依据。
- `meta/provenance.jsonl` 只能引用项目内来源；不得把真实游戏目录、真实 MO2/Vortex 目录或 AppData 路径写成合法来源。
- final_mod 中不允许残留 `.zip`、`.rar`、`.7z`。
