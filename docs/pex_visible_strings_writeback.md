# PEX Visible Strings Writeback

## 目标

把已确认的 PEX 玩家可见字符串译文写入项目内 PEX 副本。首选非 GUI 路径：`scripts/invoke_mutagen_pex_string_tool.py`。LexTranslator 和 xTranslator PapyrusPex 只作为 GUI fallback。

Codex 仍不直接 patch `.pex` 字节，不改 `.psc`，不编译脚本。

`adapters/SkyrimPexStringTool` 是项目内受控 PEX 字符串工具源码。它使用 Mutagen 解析 PEX，并且只处理函数指令中的 `VariableType.String`。所有 Python 入口都必须先做项目路径校验。

## 工具边界

- `Apply` 输入 PEX 只能来自 `work/extracted_mods/`。
- `Export` 可以读取 `work/extracted_mods/`、`out/` 或 `translated/tool_outputs/`，用于反读验证工作区内输出。
- 译表只能来自 `translated/` 或 `work/normalized/`。
- 输出 PEX 只能进入 `out/` 或 `translated/tool_outputs/`。
- 不访问真实游戏、Steam、MO2/Vortex、AppData 或 Documents/My Games 路径。

## 本地 Mutagen 补丁

如果本地使用项目内 Mutagen 源码构建 PEX 写回工具，必须确认 `tools/Mutagen/Mutagen.Bethesda.Core/Pex/Extensions/BinaryWriterExtensions.cs` 写入字符串长度时使用 UTF-8 字节数：

```csharp
bw.Write((ushort) bytes.Length);
```

原因：Mutagen PEX writer 使用 UTF-8 写字符串字节，中文是多字节字符；如果长度字段使用 `s.Length` 字符数，输出 PEX 可能无法被 Mutagen 重新解析。工具更新或重新拉取 Mutagen 后必须重新确认该补丁。

## 输入输出约定

已确认译表：

```text
translated/lextranslator_ready/<ModName>/<Script>_strings.jsonl
```

项目内 PEX 输入：

```text
work/extracted_mods/<ModName>/Scripts/<Script>.pex
```

项目内 PEX 写回目标：

```text
out/<ModName>/tool_outputs/Scripts/<Script>.pex
```

## 固定边界

- 不打开真实游戏目录、真实 MO2/Vortex 目录、Steam 目录或 AppData/Documents 游戏配置目录。
- 不覆盖 `mod/` 原始 PEX。
- 不直接编辑 `.pex`、不反编译后回写、不断言脚本逻辑。
- 只处理已经确认的玩家可见字符串。
- 不确定是否参与脚本逻辑的字符串保持原文。

## 非 GUI 首选流程

1. 路由 PEX：

```console
python .\scripts\route_translation_task.py --file-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex"
```

2. 导出 PEX 指令字符串：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Export --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-jsonl-path "source\pex_exports\<ModName>\<Script>.pex_strings.jsonl" --report-path "qa\<Script>.pex_export_report.md"
```

3. 只翻译已确认可见字符串；逻辑 key、比较字符串、路径、文件名、函数名、变量名不翻译。

4. Dry-run 写回：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Apply --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --output-pex-path "out\<ModName>\pex_mutagen_test_outputs\Scripts\<Script>.pex" --report-path "qa\<Script>.mutagen_pex_dry_run.md" --dry-run
```

5. 测试写回并验证：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Apply --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --output-pex-path "out\<ModName>\pex_mutagen_test_outputs\Scripts\<Script>.pex" --report-path "qa\<Script>.mutagen_pex_write_test.md"
python .\scripts\verify_pex_output.py --original-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-pex-path "out\<ModName>\pex_mutagen_test_outputs\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --report-output-path "qa\<Script>.pex_output_verification_test.md" --warn-only
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Export --input-pex-path "out\<ModName>\pex_mutagen_test_outputs\Scripts\<Script>.pex" --output-jsonl-path "source\pex_exports\<ModName>\<Script>.mutagen_test.pex_strings.jsonl" --report-path "qa\<Script>.pex_export_test_output_report.md"
```

6. 正式写入项目内工具输出：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Apply --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --output-pex-path "out\<ModName>\tool_outputs\Scripts\<Script>.pex" --report-path "qa\<Script>.mutagen_pex_write_official.md"
```

7. 重建并验证 final_mod：

```console
python .\scripts\build_final_mod.py --mod-name "<ModName>" --source-mod-dir "work\extracted_mods\<ModName>" --force
python .\scripts\validate_final_mod.py --final-mod-dir "out\<ModName>\汉化产出\final_mod"
python .\scripts\verify_pex_output.py --original-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-pex-path "out\<ModName>\汉化产出\final_mod\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --report-output-path "qa\<Script>.pex_output_verification_final.md" --warn-only
```

验收要求：

- `verify_pex_output.py` 必须确认译文字节探针命中。
- `invoke_mutagen_pex_string_tool.py --mode Export` 必须能重新反读输出 PEX。
- 输出 PEX 仍可解析后，才允许进入 `final_mod` 组装。

## Mutagen PEX 写回范围

`python scripts/invoke_mutagen_pex_string_tool.py --mode Apply` 只替换：

- PEX 函数指令参数中的 `VariableType.String`
- JSONL 中 `Source` 精确匹配的字符串
- `Result` 非空且与原文不同的译文

它不会替换：

- `VariableType.Identifier`
- 函数名、变量名、属性名、状态名、事件名
- StorageUtil/JsonUtil key 等逻辑 key，除非已经出现在确认译表且作为 `VariableType.String` 指令参数匹配
- source file name、user flag、debug symbol

## GUI fallback

仅当 `PexStringToolPath` 不可用、Mutagen PEX QA 失败或某个 PEX 格式不被适配器支持时，才使用 GUI。

LexTranslator 优先，xTranslator PapyrusPex 后备。GUI 只能打开项目内 PEX 副本，输出必须保存到：

```text
out/<ModName>/tool_outputs/Scripts/
translated/tool_outputs/<ModName>/Scripts/
```

GUI 只打开或只检查窗口不算完成，必须有保存路径、工具日志和 QA 报告。

最终仍需要人工抽查和游戏内测试；玩家尚未提供真实游戏测试结果和证据时，不属于 Codex 项目内校对完成范围。
