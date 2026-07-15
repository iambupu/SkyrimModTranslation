---
name: glossary-management
description: "用于按当前 Game Profile 维护汉化术语、SST/EET/TXT 只读 RAG 词典和译名一致性。中文触发：术语表、译名统一、未决术语、glossary、mod_terms、动态词典、SST、EET、角色名怎么翻。Use for game-scoped glossary sources, workspace Mod terms, binary dictionary retrieval, and unresolved proper nouns. Do not mix games, operate GUI, edit dictionaries/binaries, or assemble final_mod."
---

# Glossary Management

## 目标

维护术语一致性，管理工作区内的 `glossary/` 和 `qa/unresolved_terms.md`。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前工作区内。
- Mod 原始输入只允许来自当前工作区 `mod/` 沙盒。
- 不访问任何真实游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不直接修改插件二进制。

## 触发条件

- 开始新 Mod。
- 批量翻译前后。
- 出现专有名词、不确定译名或术语冲突。

## 输入

- 当前 Game Profile 的 `glossary_sources`；只读取带有 `rag` consumer 的源。
- 工作区 `glossary/mod_terms.md`
- 当前游戏目录下的 Markdown、LexTranslator 风格 TXT/CSV/DICT、xTranslator SST 和 ESP-ESM Translator EET 词典。
- 用户新增的工作区词典文件或子目录。
- 翻译任务文本。
- QA 未决术语。

## 输出

- 更新后的工作区 glossary。
- `work/glossary_rag/lextranslator_dynamic.sqlite` 当前游戏专用 RAG 索引。
- `work/glossary_matches/<ModName>/external_glossary_matches.jsonl`。
- `qa/<ModName>.external_glossary_matches.md`。
- `qa/unresolved_terms.md`。

## 推荐工具

- Agent Text Pipeline。
- Agent 模型术语判断。
- `python scripts/build_lextranslator_dictionary_rag_index.py`
- `python scripts/build_external_glossary_matches.py --mod-name "<ModName>"`

## 具体流程

1. 优先查 `mod_terms.md`。
2. 再按 Game Profile 中 `glossary_sources` 的声明顺序读取当前游戏词典；不得扫描整个 `glossary/`，不得回退到其他游戏目录。
3. TXT/CSV/DICT 作为 LexTranslator 风格文本词典解析；SST 按 SSU5/SSU8/SSU9 只读解析，EET 按 EET v2 只读解析。未知版本、记录越界、编码错误或 EET 记录数不一致时停止构建，不做字符串猜测。
4. 索引元数据必须绑定当前 `game_id` 和本次词典源清单。只有源更新、索引缺失、索引版本/游戏/源清单变化或用户要求 `--force` 时才重建。
5. 翻译前为当前 Mod 生成命中词表；命中项作为高优先级术语提示，不作为自动替换规则。
6. 结合当前任务上下文。
7. 使用 agent 模型判断术语是否应翻译、保留英文、音译或意译。
8. 不确定项写入 `qa/unresolved_terms.md`。
9. 用户确认或上下文充分后再进入 `mod_terms.md`。

具体 Mod 的术语改动只写入工作区 `glossary/`。插件源仓库的 `glossary/` 只维护默认种子，除非任务明确是维护插件默认词库。

## 禁止事项

- 不硬翻不确定专有名词。
- 不把 FormID、EditorID、脚本名、路径、文件名当术语翻译。
- 不把动态词表全量内容直接复制进当前 Profile 的静态 glossary 或 `mod_terms.md`；静态 glossary 路径必须来自 Game Profile，不能硬编码某个游戏。
- 不修改、转换或覆盖 SST/EET；ESP-ESM Translator/xTranslator 的原生词典用途与 RAG 只读用途分开。
- 不把 RAG 命中项当成已确认人工术语；上下文冲突时必须记录未决项。

## QA 检查

- 术语一致性。
- 未决术语有上下文。
- 暂定和确认状态分开。
- 当前 Game Profile 的任一 RAG 词典源晚于索引时，必须刷新 RAG 索引。
- 索引 `game_id` 或源清单与工作区不一致时必须重建。
- 当前 Mod 翻译批次应引用 `qa/<ModName>.external_glossary_matches.md` 或说明未生成原因。

## 完成标准

- 已优先查阅 `glossary/mod_terms.md` 和当前 Game Profile 对应的游戏术语种子。
- 已按当前游戏、词典源清单、索引版本和修改时间判断是否需要刷新 RAG 索引。
- 已为当前 Mod 生成或确认可复用的外部词库命中包。
- 新增术语有来源上下文和状态，未确认项未被硬翻。
- `qa/unresolved_terms.md` 已记录仍需人工确认的专有名词。
- 翻译批次可引用一致术语，不确定项保留待审状态。

## 失败处理

上下文不足时保留英文或暂定译名，并记录未决。
