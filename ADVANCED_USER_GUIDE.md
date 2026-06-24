# 高级用户指南

这份指南面向愿意配置工具、查看 QA 报告、理解暂停原因并判断输出是否可测试的用户。它不覆盖插件开发、脚本维护或状态机改造。

## 工作区结构

初始化后的工作区通常包含：

```text
mod/                         待汉化 Mod 输入
work/                        解包、锁、临时工作区和中间缓存
source/                      提取出的源文本
translated/                  翻译后的中间文本和 overlay
out/<ModName>/汉化产出/       final_mod、intermediate 和 _CHS.zip
qa/                          检查报告、状态报告、问题记录
glossary/                    工作区术语表和动态词典
config/                      本机工具路径配置
```

高级用户通常只需要关注工作区目录。插件源码目录不属于当前 Mod 汉化工作区。

## 工具配置

本机工具路径写在工作区：

```text
config/tools.local.json
```

参考模板：

```text
config/tools.example.json
```

如果不确定配置是否可用，可以让 Codex 检查：

```text
帮我检查这个工作区的工具配置
```

优先从工具作者主页、官方页面或可信项目页下载；不要从不明镜像站下载可执行文件。

常见工具：

| 工具 | 主页 | 本项目主要用途 |
|---|---|---|
| LexTranslator / Lexicon AI Translator | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/143056) / [GitHub](https://github.com/YD525/YDSkyrimToolR) | GUI 后备；插件、PEX、MCM 或翻译字典 |
| xTranslator | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/134) / [GitHub](https://github.com/MGuffin/xTranslator) | GUI 后备；精修、查漏、复杂导入或 PapyrusPex 后备 |
| Mutagen | [GitHub](https://github.com/Mutagen-Modding/Mutagen) | ESP/ESM/ESL 文本导出、写回和验证；PEX 可见字符串适配器 |
| .NET SDK | [Microsoft .NET 下载页](https://dotnet.microsoft.com/en-us/download) | 运行或构建 Mutagen 相关适配器 |
| SSEEdit / xEdit | [xEdit 主页](https://tes5edit.github.io/) / [GitHub](https://github.com/tes5edit/tes5edit) / [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/164) | 插件文本辅助导出、对照检查和安全 dump 包装器 |
| Champollion | [GitHub](https://github.com/Orvid/Champollion) | PEX/PSC 只读分析或后备解码 |
| bethesda-structs | [PyPI](https://pypi.org/project/bethesda-structs/) / [文档](https://bethesda-structs.readthedocs.io/) | BSA/BA2 只读归档目录读取和 manifest 证据 |
| BSAFileExtractor | [GitHub](https://github.com/Sw4T/BSAFileExtractor) | 通过项目安全包装器把 BSA 内容物化到 `work/archive_extracts/` |
| B.A.E. - Bethesda Archive Extractor | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/974) | BA2/BSA 人工提取参考；默认不作为本项目自动解包入口 |
| 7-Zip | [官方主页](https://www.7-zip.org/) | `.7z` 解包后备；首选 Python `py7zr` |
| py7zr | [PyPI](https://pypi.org/project/py7zr/) / [文档](https://py7zr.readthedocs.io/) | Python 内部 `.7z` 解包 |

`--tool-setup auto` 只自动准备安全的非 GUI 依赖和工具。LexTranslator、xTranslator、SSEEdit/xEdit、B.A.E. 和 7-Zip 这类 GUI 或系统级外部程序不会静默安装，需要用户自己安装并确认路径。

## 自定义词典配置

工作区的 `glossary/` 是可编辑术语区，不是插件源码目录。初始化工作区时会复制一份默认术语种子，之后应优先在工作区里维护具体 Mod 的术语和用户词典。

常用文件和目录：

| 路径 | 用途 |
|---|---|
| `glossary/mod_terms.md` | 当前工作区和具体 Mod 的人工确认术语、译名和未决名词 |
| `glossary/skyrim_cn_glossary.md` | Skyrim 常用中文术语参考 |
| `glossary/lex_dictionary_notes.md` | LexTranslator 风格词典维护说明 |
| `glossary/lextranslator_dynamic_dictionaries/` | 放用户新增的 LexTranslator 风格 `.txt`、`.csv` 或 `.dict` 词典 |

新增自定义词典后，可以让 Codex 刷新索引和命中包：

```text
刷新这个工作区的动态词典索引
```

```text
为 <ModName> 生成外部词典命中包
```

相关输出：

| 路径 | 用途 |
|---|---|
| `work/glossary_rag/lextranslator_dynamic.sqlite` | 动态词典本地索引 |
| `qa/lextranslator_dictionary_rag_index.md` | 索引刷新报告 |
| `qa/<ModName>.external_glossary_matches.md` | 当前 Mod 的词典命中摘要 |
| `work/glossary_matches/<ModName>/` | 当前 Mod 的详细命中包 |

优先级通常是：`glossary/mod_terms.md` 高于 `glossary/skyrim_cn_glossary.md`，再高于动态词典命中。动态词典只提供术语提示，不是自动替换规则；FormID、EditorID、脚本名、变量名、路径、文件名、JSON/XML key、占位符和运行时逻辑 key 仍然必须保护。

更细的动态词典规则见 [docs/lextranslator_dictionary_rag.md](./docs/lextranslator_dictionary_rag.md)。

## 暂停和 blocked

`blocked` 是安全暂停，不是失败。它表示当前步骤缺少足够证据，继续自动推进可能损坏输出或伪造完成状态。

常见原因：

- 缺少本机工具路径。
- 压缩包或归档没有可用安全解包器。
- GUI 工具无法自动保存到工作区内目录。
- 插件或 PEX 文本风险较高，需要人工确认。
- QA 发现漏译、占位符损坏、结构错误、来源缺失或输出不一致。
- 已经到达需要人工游戏测试的阶段。

常用处理方式：

```text
说明现在卡在哪里
```

```text
继续处理 blocked 的问题
```

```text
检查工具配置有没有问题
```

## QA 报告入口

高级用户优先看这些报告：

```text
.workflow/progress_card.md
qa/blockers.md
qa/workflow_timeline.md
qa/translation_readiness.md
qa/workflow_state.md
qa/workflow_health.md
traces/trace_summary.md
```

`.workflow/progress_card.md` 是对话中展示进度的来源；`traces/trace_summary.md` 只用于排查失败原因，不作为 QA 放行依据。

如果只想判断某个 Mod 能否测试，可以问 Codex：

```text
说明 <ModName> 能不能进游戏测试
```

常见专项报告：

| 报告 | 用途 |
|---|---|
| `qa/decoder_tools_report.md` | 解码器和 CLI 工具检测 |
| `qa/tools_config_validation.md` | 工具路径配置检查 |
| `qa/translation_goal_compliance.md` | 翻译目标完成度 |
| `qa/<ModName>.model_review.md` | Codex 模型校对结果 |
| `qa/validation_errors.md` | 当前检查错误汇总 |

## final_mod 和 _CHS.zip

最终输出位于：

```text
out/<ModName>/汉化产出/
```

其中：

- `final_mod/` 是完整 Mod Data 根结构，适合人工检查和本地测试。
- `<ModName>_CHS.zip` 是项目打包产物，适合手动导入 MO2/Vortex 测试。
- `intermediate/` 保存工具输出、overlay、patch、审计等中间产物。

通常应优先测试 `<ModName>_CHS.zip`，必要时再检查 `final_mod/` 的文件结构是否符合 Skyrim Mod 的 Data 根目录结构。

## 人工游戏测试边界

项目内 QA 通过只表示可以进入人工游戏测试。它不表示已经在真实游戏中通过。

公开发布或长期使用前，应至少确认：

- Mod 作者是否允许翻译和再发布。
- 真实加载顺序下是否有冲突。
- MCM 菜单是否出现，页面是否可打开。
- 对话、任务、菜单、通知和脚本文本是否正常显示。
- `_CHS.zip` 是否对应最新 `final_mod/` 和 QA 报告。

可以让 Codex 生成测试计划：

```text
帮我给 <ModName> 生成进游戏测试计划
```

也可以生成记录模板：

```text
帮我给 <ModName> 生成测试结果记录模板
```

## 超出高级使用范围的内容

如果需要修改插件源码、脚本入口、Skills、适配器、状态机或 QA 门禁，请转到 [developer_guide.md](./developer_guide.md)。这些内容属于开发者用户范围，不属于高级用户日常使用范围。
