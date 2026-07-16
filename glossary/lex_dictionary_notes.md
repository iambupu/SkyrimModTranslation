# Translation Dictionary Notes

- 当前工作区只使用 marker/Game Profile 指定的游戏词典，不扫描其他游戏目录。
- 人工确认顺序为 `glossary/mod_terms.md`、当前游戏基础词表、profile 声明的外部词典。
- LexTranslator 风格 `.txt/.csv/.dict`、xTranslator `.sst`、ESP-ESM Translator `.eet` 都可以进入本地 RAG。
- SST/EET 只读解码，不修改原文件；xTranslator 或 EET4 的原生加载、保存能力与 RAG 分开。
- 不把外部词典全量合并到基础词表或 `mod_terms.md`。

构建或刷新索引：

```powershell
python scripts\build_lextranslator_dictionary_rag_index.py
```

为当前 Mod 生成小型命中包：

```powershell
python scripts\build_external_glossary_matches.py --mod-name "<ModName>"
```

索引位于 `work/glossary_rag/lextranslator_dynamic.sqlite`。它绑定当前 `game_id` 和词典源清单；词典、索引版本、游戏或源清单变化时自动重建。命中结果只用于术语提示，不能覆盖受保护标识符、路径、占位符或运行时 key。
