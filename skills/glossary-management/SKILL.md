---
name: glossary-management
description: "用于按当前 Game Profile 维护汉化术语、游戏词典种子和译名一致性。中文触发：术语表、译名统一、未决术语、glossary、mod_terms、动态词典、角色名怎么翻。Use for profile-specific glossary seeds, workspace Mod terms, user dictionaries, and unresolved proper nouns; never apply the Skyrim seed to Fallout 4 automatically. Do not route files, operate GUI, edit binaries, or assemble final_mod."
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

- 当前 Game Profile 的游戏术语种子：Skyrim 使用 `glossary/skyrim_cn_glossary.md`，Fallout 4 使用 `glossary/fallout4_cn_glossary.md`
- 工作区 `glossary/mod_terms.md`
- 工作区 `glossary/lextranslator_dynamic_dictionaries/` 下的 LexTranslator 风格动态词表。
- 用户新增的工作区词典文件或子目录。
- 翻译任务文本。
- QA 未决术语。

## 输出

- 更新后的工作区 glossary。
- `work/glossary_rag/lextranslator_dynamic.sqlite` 动态词表 RAG 索引。
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
2. 再查当前 profile 的游戏术语种子；不得在 Fallout 4 工作区自动回退到 `skyrim_cn_glossary.md`。
3. 对 `glossary/lextranslator_dynamic_dictionaries/` 下的动态词表使用 RAG 索引检索；用户可以在该目录新增 LexTranslator 风格词典文件，不把全量词表直接并入人工术语表。
4. 构建/刷新索引前，先比较动态词典目录及其文件的最新修改时间与 `work/glossary_rag/lextranslator_dynamic.sqlite` 的修改时间；只有词典目录更新、索引缺失、索引版本变化或用户要求 `--force` 时才重建索引。
5. 翻译前为当前 Mod 生成命中词表；命中项作为高优先级术语提示，不作为自动替换规则。
6. 结合当前任务上下文。
7. 使用 agent 模型判断术语是否应翻译、保留英文、音译或意译。
8. 不确定项写入 `qa/unresolved_terms.md`。
9. 用户确认或上下文充分后再进入 `mod_terms.md`。

具体 Mod 的术语改动只写入工作区 `glossary/`。插件源仓库的 `glossary/` 只维护默认种子，除非任务明确是维护插件默认词库。

## 禁止事项

- 不硬翻不确定专有名词。
- 不把 FormID、EditorID、脚本名、路径、文件名当术语翻译。
- 不把动态词表全量内容直接复制进 `skyrim_cn_glossary.md` 或 `mod_terms.md`。
- 不把 RAG 命中项当成已确认人工术语；上下文冲突时必须记录未决项。

## QA 检查

- 术语一致性。
- 未决术语有上下文。
- 暂定和确认状态分开。
- 动态词典目录修改时间晚于索引修改时间时，必须刷新 RAG 索引。
- 当前 Mod 翻译批次应引用 `qa/<ModName>.external_glossary_matches.md` 或说明未生成原因。

## 完成标准

- 已优先查阅 `glossary/mod_terms.md` 和当前 Game Profile 对应的游戏术语种子。
- 已按动态词典目录和索引修改时间判断是否需要刷新 RAG 索引。
- 已为当前 Mod 生成或确认可复用的外部词库命中包。
- 新增术语有来源上下文和状态，未确认项未被硬翻。
- `qa/unresolved_terms.md` 已记录仍需人工确认的专有名词。
- 翻译批次可引用一致术语，不确定项保留待审状态。

## 失败处理

上下文不足时保留英文或暂定译名，并记录未决。
