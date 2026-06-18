# LexTranslator Dictionary Notes

- LexTranslator 可用于 AI 批量翻译和词典辅助。
- 本项目中的 glossary 文件先作为人类可读术语表。
- 不要臆造 LexTranslator 的真实词典格式。
- 如果用户提供 LexTranslator 导出的词典样例，再根据样例生成 `out/lex_dictionary/` 下的真实词典文件。
- 词典内容优先来自：
  1. `glossary/skyrim_cn_glossary.md`
  2. `glossary/mod_terms.md`
  3. `qa/unresolved_terms.md` 中经过用户确认的条目

## LexTranslator-style dynamic dictionaries

- `glossary/lextranslator_dynamic_dictionaries/` is the dynamic loading directory for high-priority Skyrim reference dictionaries in LexTranslator-style format.
- Put LexTranslator-style `.txt`, `.csv`, or `.dict` dictionaries in that directory; the index builder discovers them automatically.
- Do not merge the full dynamic dictionary into `skyrim_cn_glossary.md` or `mod_terms.md`.
- Build or refresh the local SQLite/FTS retrieval index with `python scripts/build_lextranslator_dictionary_rag_index.py --force`.
- Before translating a Mod, run `python scripts/build_external_glossary_matches.py --mod-name "<ModName>"` to retrieve a compact per-Mod match packet under `work/glossary_matches/<ModName>/` and `qa/<ModName>.external_glossary_matches.md`.
- The default index lives at `work/glossary_rag/lextranslator_dynamic.sqlite` and is rebuilt automatically when the source dictionary changes.
- Treat matched entries as terminology guidance, not blind replacement rules. Protected tokens, script names, file names, placeholders, and runtime keys still win.
