# LexTranslator Dynamic Dictionary RAG

本项目支持把 LexTranslator 风格词表作为动态加载词典使用。词表不直接并入人工确认术语表，而是先构建本地检索索引，再按当前 Mod 的待翻译文本生成小型命中词表。

## 目录

动态词典目录位于初始化后的工作区内：

```text
glossary/lextranslator_dynamic_dictionaries/
```

把 LexTranslator 风格的 `.txt`、`.csv` 或 `.dict` 词表放入该目录。脚本会递归扫描目录和子目录中的词表文件，不写死单个词表文件名；用户新增词典应放在工作区 `glossary/`，不要为具体 Mod 修改插件源仓库里的默认种子。

索引文件：

```text
work/glossary_rag/lextranslator_dynamic.sqlite
```

索引报告：

```text
qa/lextranslator_dictionary_rag_index.md
qa/lextranslator_dictionary_rag_index.json
```

该脚本会写 Markdown 报告，并同步写出同名 `.json` 摘要；两者都由 `scripts/build_lextranslator_dictionary_rag_index.py` 生成，不是把 Markdown 内容写入 `.json` 文件。

## 刷新规则

构建索引前先比较：

- `glossary/lextranslator_dynamic_dictionaries/` 目录及其词表文件的最新修改时间
- `work/glossary_rag/lextranslator_dynamic.sqlite` 的修改时间

如果索引比动态词典目录更新，并且索引版本有效，直接复用索引。报告中会记录：

```text
Refresh decision: reused_index_current_by_mtime
```

只有以下情况才重建索引：

- 索引文件缺失
- 动态词典目录或词表文件比索引更新
- 索引版本变化
- 用户显式使用 `--force`

手动强制刷新：

```console
python scripts\build_lextranslator_dictionary_rag_index.py --force
```

常规刷新检查：

```console
python scripts\build_lextranslator_dictionary_rag_index.py
```

如果当前目录是工作区，工作区内不会有 `scripts/`。上面的命令表示运行插件源仓库中的同名 Python 脚本，并让脚本通过 `.skyrim-chs-workspace.json` 或 `SKYRIM_CHS_WORKSPACE_ROOT` 把输出写回当前工作区。

## 翻译前检索

为当前 Mod 生成词典命中包：

```console
python scripts\build_external_glossary_matches.py --mod-name "<ModName>"
```

输出：

```text
work/glossary_matches/<ModName>/external_glossary_matches.jsonl
work/glossary_matches/<ModName>/external_glossary_matches.md
qa/<ModName>.external_glossary_matches.md
```

命中词表只作为高优先级术语提示，不是自动替换表。遇到上下文冲突时，以当前 Mod 上下文和人工确认术语为准，并把不确定项记录到 `qa/unresolved_terms.md`。

## 工作流接入

插件提供的 `scripts/run_non_gui_translation_workflow.py` 会在翻译阶段前运行索引刷新检查。插件翻译阶段导出插件文本后，也会生成对应的词典命中包，方便填充工作区 `work/plugin_translation_maps/<ModName>/` 下的翻译映射。

## 优先级

术语判断优先级：

1. `glossary/mod_terms.md`
2. `glossary/skyrim_cn_glossary.md`
3. 动态词典 RAG 命中包
4. 当前 Mod 上下文和模型判断

动态词典命中项不得覆盖 FormID、EditorID、脚本名、变量名、路径、文件名、插件名、JSON/XML key、占位符或任何运行时逻辑 key。
