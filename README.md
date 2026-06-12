# Skyrim SE/AE Mod 自动化汉化工程

本项目用于管理 Skyrim Special Edition / Anniversary Edition Mod 汉化流程。目标是让翻译流程可维护、可回滚、可批量处理。

Codex 只负责文本工程、术语表、翻译中间文件、校验脚本、格式转换脚本和文档；LexTranslator 与 xTranslator 的最终导入、回写、保存插件和复制到 MO2/Vortex 测试由用户手动执行。

## 推荐工作流

1. 手动把待翻译 Mod 复制到当前项目的 `mod/` 目录。
2. 使用 LexTranslator 或 xTranslator 打开 `mod/` 副本中的插件或导出文本。
3. 把导出的文本文件放入 `source/lextranslator_exports/` 或 `source/xtranslator_exports/`。
4. Codex 进行格式整理、分批、翻译辅助、术语统一和校验。
5. 输出译文到 `translated/` 或 `out/`。
6. 用户手动把结果导入 LexTranslator 或 xTranslator。
7. 用户手动保存 Patch 或翻译输出。
8. 用户手动复制到 MO2/Vortex 测试。
9. 把游戏内发现的问题记录到 `qa/review_notes.md`。
10. 修正后提交 Git。

## 安全说明

- 本项目不直接修改原始 ESP/ESM/ESL。
- 本项目不访问真实游戏目录。
- `mod/` 是沙盒副本，不是游戏实际加载目录。
- 最终回写和游戏加载由用户手动完成。
- `config/tools.example.json` 里的路径只是示例，脚本不得自动访问这些路径。

## 工具分工

- LexTranslator：主力 AI 批量翻译、词典辅助、MCM/PEX/ESP/ESM 文本处理。
- xTranslator：精修、查漏、对照和回写。
- Codex：文本工程、术语整理、分批、格式转换、占位符检查和 QA。

## 目录结构

- `AGENTS.md`：Codex 项目边界和操作规则。
- `config/tools.example.json`：工具路径示例，不作为自动访问依据。
- `mod/`：唯一允许读取和分析的 Mod 沙盒输入目录。
- `glossary/`：Skyrim 通用术语、当前 Mod 术语和 LexTranslator 词典备注。
- `source/lextranslator_exports/`：LexTranslator 导出文本。
- `source/xtranslator_exports/`：xTranslator 导出文本。
- `source/raw/`：其他来源的原始文本副本。
- `work/batches/`：拆分后的翻译批次。
- `work/normalized/`：规范化 JSONL 中间文件。
- `translated/lextranslator_ready/`：准备导入 LexTranslator 的译文。
- `translated/xtranslator_ready/`：准备导入 xTranslator 的译文。
- `qa/`：校验错误、审校记录和未决术语。
- `out/lex_dictionary/`：LexTranslator 词典输出。
- `out/xtranslator_import/`：xTranslator 导入输出。
- `out/dsd_patch/`：DSD Patch 文本结构输出。
- `scripts/`：PowerShell 工程脚本。
- `docs/`：规则和工作流文档。
- `samples/`：示例导出和术语文件。

## 添加一个新的 Mod 翻译任务

1. 手动把 Mod 副本放入 `mod/`。
2. 在 `glossary/mod_terms.md` 写入 Mod 名称、插件名、版本和翻译状态。
3. 用 LexTranslator 或 xTranslator 手动导出文本到 `source/` 对应目录。
4. 运行规范化脚本：

```powershell
.\scripts\normalize-export.ps1 -InputPath .\source\lextranslator_exports\<export>.jsonl
```

5. 运行拆分脚本：

```powershell
.\scripts\split-jsonl.ps1 -InputPath .\work\normalized\<export>.normalized.jsonl
```

6. 翻译 `work/batches/` 中的批次，并把结果放入 `translated/`。
7. 运行校验脚本：

```powershell
.\scripts\validate-translation.ps1 -SourcePath .\work\normalized\<export>.normalized.jsonl -TranslatedPath .\translated\xtranslator_ready\<export>.zh-CN.jsonl
```

8. 将未决术语写入 `qa/unresolved_terms.md`，将测试问题写入 `qa/review_notes.md`。

