# Game-scoped Translation Dictionary RAG

本页说明本地词典检索。文件名保留 `lextranslator_dictionary_rag.md` 以兼容既有引用，但索引不再只处理 LexTranslator 文本词典。

## 词典来源

主流程读取工作区 marker 的 `game_id`，再从对应 Game Profile 的 `glossary_sources` 选择带 `rag` consumer 的路径。它不会递归扫描整个 `glossary/`，因此 Fallout 4、Skyrim 和后续游戏的词典不会混用。

词典是强烈推荐的术语一致性辅助，不是翻译工作的必需输入。Profile 使用 `recommended: true` 表达这一点；路径缺失或目录为空时直接跳过，不阻断工作区初始化、翻译、QA 或交付。词典越完整，专名和既有译名越稳定。

当前格式：

| 格式 | 来源软件或用途 | RAG 处理 |
|---|---|---|
| `.md` | 人工基础词表、Mod 术语表 | 读取 English/简体中文表格 |
| `.txt/.csv/.dict` | LexTranslator 风格动态词典 | 读取原文/译文对 |
| `.sst` | xTranslator 原生词典 | 只读解码 SSU5、SSU8、SSU9 |
| `.eet` | ESP-ESM Translator 工程或数据库 | 只读解码 EET v2 |

SST/EET 不会被修改或转换。未知版本、字段越界、编码错误、记录重叠或 EET 记录数与文件头不一致时，该次索引构建失败；脚本不会用可打印字符串扫描来猜译文。总流程会把失败记录为词典警告并继续翻译，不会使用不可信的词典内容。

典型目录由 profile 自行声明，例如：

```text
glossary/lextranslator_dynamic_dictionaries/skyrim/
glossary/lextranslator_dynamic_dictionaries/fallout4/
glossary/sst/skyrim/
glossary/sst/fallout4/
glossary/eet/fallout4/
```

新增游戏时，应新增自己的 Game Profile 和独立词典目录，不要复用另一个游戏的 `glossary_sources`。

## 索引与报告

```text
work/glossary_rag/lextranslator_dynamic.sqlite
qa/lextranslator_dictionary_rag_index.md
qa/lextranslator_dictionary_rag_index.json
```

SQLite 元数据记录 `game_id`、词典源清单、源文件指纹和索引版本。即使工作区被错误切换游戏，只要游戏或源清单变化，旧索引也不会直接复用。

常规检查：

```powershell
python scripts\build_lextranslator_dictionary_rag_index.py
```

强制重建：

```powershell
python scripts\build_lextranslator_dictionary_rag_index.py --force
```

工作区不包含 `scripts/`。上述命令运行插件源脚本，并通过工作区 marker 或 `SKYRIM_CHS_WORKSPACE_ROOT` 把输出写回工作区。

## 检索与优先级

为当前 Mod 生成命中包：

```powershell
python scripts\build_external_glossary_matches.py --mod-name "<ModName>"
```

输出位于：

```text
work/glossary_matches/<ModName>/external_glossary_matches.jsonl
work/glossary_matches/<ModName>/external_glossary_matches.md
qa/<ModName>.external_glossary_matches.md
```

同一原文存在多个译文时，先出现的来源优先。默认顺序是 `mod_terms.md`、当前游戏基础词表，再按 Game Profile 中 `glossary_sources` 的顺序处理；Fallout 4 当前把简体中文 EET 放在 SST 前。命中项只是模型术语提示，不是自动替换规则，不能覆盖 FormID、EditorID、脚本名、路径、占位符或运行时 key。

`scripts/run_non_gui_translation_workflow.py` 会在翻译阶段前检查索引，并在有待翻译文本时生成当前 Mod 的小型命中包。
