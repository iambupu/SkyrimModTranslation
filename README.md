# Skyrim SE/AE Mod 汉化工程助手

这个项目帮你把 Skyrim SE/AE 的 Mod 汉化流程放进一个安全的项目目录里完成。

你不需要懂插件结构，也不需要自己判断哪些文件能改、哪些文件不能改。通常只要把待汉化的 Mod 放进 `mod/`，然后在 Codex 里说“翻译 mod”。Codex 会在项目内扫描、分类、翻译、组装和检查，并把结果放到 `out/`。

这个项目不是游戏 Mod 管理器，也不是一键发布工具。它不会自动安装 Mod，不会碰你的真实 Skyrim、MO2 或 Vortex 目录。最后能不能在你的游戏里正常工作，仍然需要你自己进游戏测试。

## 最短使用流程

1. 把要汉化的 Mod 压缩包或文件夹放进：

```text
mod/
```

2. 打开 Codex，在这个项目里输入：

```text
翻译 mod
```

如果 `mod/` 里有多个 Mod，也可以说：

```text
翻译 <ModName> 这个 mod
```

3. 等 Codex 完成。它会告诉你结果是否可以测试，或者卡在哪一步。

4. 到这里查看输出：

```text
out/<ModName>/汉化产出/
```

5. 用里面的 `final_mod/` 或 `<ModName>_CHS.zip` 去你自己的 MO2/Vortex 里手动测试。

## 你会看到哪些结果

每个 Mod 的输出都集中在：

```text
out/<ModName>/汉化产出/
```

里面最重要的是：

```text
final_mod/              完整汉化 Mod 目录，适合人工检查
<ModName>_CHS.zip       打包好的汉化包，适合导入 MO2/Vortex 测试
intermediate/           中间文件，一般不用看
package_report.md       打包记录
```

简单理解：

- 想看文件结构，用 `final_mod/`。
- 想拿去本地测试，用 `<ModName>_CHS.zip`。
- 想知道 Codex 到底做了什么，看 `qa/` 和 `final_mod/meta/` 里的报告。

## Codex 会替你做什么

Codex 会按安全流程处理项目内的 Mod：

- 扫描 `mod/` 里的输入。
- 判断哪些是文本、菜单、插件、脚本、压缩包或归档。
- 优先处理玩家能看到的文本，例如菜单、说明、对话、提示、MCM 选项。
- 保护不能乱改的内容，例如 FormID、EditorID、脚本名、变量名、路径、文件名和占位符。
- 需要工具写回时，只让工具把结果保存到项目内目录。
- 如果需要处理插件或脚本二进制，调用项目配置的受控工具，在项目内生成可追溯的输出副本，再把这个副本放进 `final_mod/`。
- 组装 `final_mod/`，生成 `_CHS.zip`。
- 运行检查，确认结构、占位符、文件来源和最终输出尽量可靠。
- 遇到不能自动完成的步骤时，写清楚 blocked 原因。

## Codex 不会做什么

为了避免破坏你的游戏环境，Codex 不会：

- 访问真实 Skyrim 游戏目录。
- 访问真实 MO2/Vortex 目录。
- 自动安装或启用 Mod。
- 直接修改原始 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`。
- 直接改 Papyrus 源码再重新编译。
- 把“只打开了工具”说成“已经翻译并保存完成”。

你不需要手动修改这些二进制文件；你可能需要做的是提供工具路径、确认工具窗口操作，或者在最后进游戏测试。

## 第一次使用

先安装基础 Python 依赖：

```console
python -m pip install -r requirements.txt
```

当前基础依赖主要用于：

- 处理 `.7z` 压缩包。
- 只读检查 BSA/BA2 归档内容。

有些 Mod 只靠基础依赖就能完成，例如纯文本、Interface 翻译或 MCM 文本。更复杂的 Mod 可能还需要本机工具，例如 LexTranslator、xTranslator、Mutagen、.NET SDK、SSEEdit/xEdit、Champollion、BSAFileExtractor 或 7-Zip。

## 工具主页速查

优先从工具作者主页、官方页面或可信项目页下载；不要从不明镜像站下载可执行文件。

| 工具 | 主页 | 本项目主要用途 |
|---|---|---|
| LexTranslator / Lexicon AI Translator | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/143056) / [GitHub](https://github.com/YD525/YDSkyrimToolR) | GUI fallback，处理插件、PEX、MCM 或翻译字典 |
| xTranslator | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/134) / [GitHub](https://github.com/MGuffin/xTranslator) | GUI fallback，精修、查漏、复杂导入或 PapyrusPex 后备 |
| Mutagen | [GitHub](https://github.com/Mutagen-Modding/Mutagen) | ESP/ESM/ESL 文本导出、写回和验证；PEX 可见字符串适配器 |
| .NET SDK | [Microsoft .NET 下载页](https://dotnet.microsoft.com/en-us/download) | 运行或构建 Mutagen 相关适配器 |
| SSEEdit / xEdit | [xEdit 主页](https://tes5edit.github.io/) / [GitHub](https://github.com/tes5edit/tes5edit) / [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/164) | 插件文本辅助导出、对照检查和安全 dump 包装器 |
| Champollion | [GitHub](https://github.com/Orvid/Champollion) | PEX/PSC 只读分析或后备解码 |
| bethesda-structs | [PyPI](https://pypi.org/project/bethesda-structs/) / [文档](https://bethesda-structs.readthedocs.io/) | BSA/BA2 只读归档目录读取和 manifest 证据 |
| BSAFileExtractor | [GitHub](https://github.com/Sw4T/BSAFileExtractor) | 通过项目安全包装器把 BSA 内容物化到 `work/archive_extracts/` |
| B.A.E. - Bethesda Archive Extractor | [Nexus Mods](https://www.nexusmods.com/skyrimspecialedition/mods/974) | BA2/BSA 人工提取参考；默认不作为本项目自动解包入口 |
| 7-Zip | [官方主页](https://www.7-zip.org/) | `.7z` 解包后备；首选 Python `py7zr` |
| py7zr | [PyPI](https://pypi.org/project/py7zr/) / [文档](https://py7zr.readthedocs.io/) | Python 内部 `.7z` 解包 |

如果 Codex 需要这些工具，它会先检查：

```text
config/tools.local.json
```

你可以把自己电脑上的工具路径写到这个文件里。示例文件是：

```text
config/tools.example.json
```

示例文件只用来参考，Codex 不会把示例路径当成真实工具路径。

## 不懂工具也没关系

你可以直接让 Codex 判断下一步：

```text
现在这个项目应该怎么继续？
```

```text
检查工具配置有没有问题
```

```text
继续处理 blocked 的问题
```

```text
说明这个汉化包能不能测试了
```

Codex 会读取项目状态和报告，不需要你自己去翻所有中间文件。

如果某个 Mod 处于 `qa_failed` 或 `blocked`，Codex 会先读阻断报告，再按 `qa/workflow_state.json` 里的推荐动作、修复候选和停止条件决定是否自动续跑。低风险的派生产物可以自动修复；语义质量、插件/脚本二进制和游戏内测试问题会安全停下。每次恢复尝试会记录到 `qa/workflow_agent_runs.jsonl`。

这个项目的控制分层是：

- Codex 负责准确和灵活的编排。
- 状态机负责边界和证据。
- 脚本负责可复现动作。
- QA 负责判断是否允许推进。

## 如果 Codex 说 blocked

`blocked` 不是失败，而是安全暂停。

它的意思是：当前步骤没有足够证据自动完成，继续装作成功会有风险。

常见原因：

- 缺少本机工具。
- 某种压缩包暂时不能自动解包。
- GUI 工具打开了，但无法自动保存到项目内目录。
- 插件或 PEX 文本风险较高，需要人工确认。
- QA 发现漏译、占位符损坏、结构错误或输出不一致。

遇到 blocked，可以直接问：

```text
说明现在卡在哪里
```

或：

```text
继续处理 blocked 的问题
```

通常要看的报告是：

```text
qa/workflow_state.md
qa/translation_readiness.md
qa/workflow_health.md
qa/translation_issue_log.md
```

## 怎么判断能不能测试

优先看：

```text
qa/translation_goal_compliance.md
qa/workflow_state.md
qa/translation_readiness.md
```

如果报告显示项目内 QA 已完成，意思是：

```text
项目内的翻译、组装和静态检查已经通过，可以进入人工游戏测试。
```

这不等于已经在游戏里通过。

Skyrim 的真实加载顺序、脚本触发、Mod 冲突和游戏内显示效果，仍然必须由你在自己的游戏环境中测试。

## 目录速查

```text
mod/                         放待汉化 Mod
work/                        解包和临时工作区
source/                      提取出的源文本
translated/                  翻译后的中间文本
out/<ModName>/汉化产出/       最终输出
qa/                          检查报告、状态报告、问题记录
config/                      工具路径和流程配置
tools/                       项目内工具依赖
docs/                        维护说明
scripts/                     自动化流程脚本
```

普通用户主要关心 `mod/`、`out/` 和 `qa/`。

## 常用对话

```text
翻译 mod
```

```text
翻译 mod，如果遇到问题就记下来
```

```text
检查现在有哪些 mod 已经 ready
```

```text
重新跑 <ModName> 的 QA
```

```text
说明 <ModName> 能不能进游戏测试
```

```text
生成人工测试计划
```

## 给想手动运行的人

普通用户可以跳过这一节，直接让 Codex 执行。

准备 `mod/` 里的输入队列：

```console
python scripts/run_translation_queue.py --mode prepare
```

运行某个 Mod 的非 GUI 主流程：

```console
python scripts/run_non_gui_translation_workflow.py --mod-name "<ModName>"
```

运行严格 QA 门禁：

```console
python scripts/run_non_gui_qa_gates.py --mod-name "<ModName>" --strict-complete
```

同一时间不要并行跑多个主流程。项目会使用 `work/.workflow.lock` 避免报告被同时改写。

## 给维护者看的说明

维护流程、改规则或扩展工具时再看这些文件：

- `AGENTS.md`：Codex 的项目边界和硬规则。
- `.codex/skills/`：Codex 执行汉化时使用的 Skill。
- `docs/`：工具适配、流程设计和补充说明。
- `scripts/`：Python 主流程、工具适配器和 QA 门禁。
- `config/workflow_policy.json`：流程状态机。

项目主流程统一使用 Python 脚本。不要新增 Bash、WSL、Linux shell 包装层。

## 给开发者：状态机扩展契约

状态机不是重型编排器，它是后续开发者和 Codex 共同依赖的工作流契约。扩展新文件类型、新工具 adapter、新 QA 门禁或新阶段时，必须保持四层分工：

| 层 | 负责 | 不负责 |
|---|---|---|
| Codex | 准确、灵活地读取状态、解释阻断、选择下一步 | 绕过证据或伪造完成 |
| 状态机 | 记录阶段、边界、证据、允许动作、推荐动作和停止条件 | 执行具体翻译或工具操作 |
| 脚本 | 执行可复现的项目内 Python 动作 | 做语义判断或直接改二进制 |
| QA | 决定是否允许状态推进 | 替代人工游戏内测试 |

当前标准流转是：

```text
discovered
-> extracted
-> routed
-> candidates_extracted
-> translated
-> tool_outputs_generated
-> final_mod_built
-> qa_passed
-> ready_for_manual_test
-> manual_tested
```

失败状态是显式状态，不是进度阶段：

```text
needs_input
blocked
qa_failed
```

### 扩展状态机时必须同步

| 改动 | 必须同步的位置 |
|---|---|
| 新增阶段或调整阶段顺序 | `config/workflow_policy.json` 的 `state_order`、`states`，以及 `scripts/write_workflow_state.py` 的阶段推断 |
| 新增可执行入口 | `allowed_entrypoint_scripts` 或对应阶段的 `allowed_scripts` |
| 新增 QA/adapter 分步脚本 | `allowed_leaf_scripts`，以及对应 Skill/文档 |
| 新增 ready 前必须满足的证据 | `scripts/audit_translation_readiness.py`、`scripts/write_workflow_state.py`、`scripts/run_non_gui_qa_gates.py` |
| 新增状态字段 | `config/workflow_state.schema.json`、`scripts/write_workflow_state.py`、相关 Skill |
| 新增文件类型或工具优先级 | `translation-task-router`、对应文件类型 Skill、`docs/tool_adapter.md` |
| 新增 final_mod 交付证据 | `final-mod-assembly`、`validate_final_mod.py`、`qa-validation` |

### 状态机不变量

- `qa/workflow_state.json` 的 `next_command` 不得指向未授权脚本。
- `allowed_scripts` 是 `always_allowed_scripts`、`allowed_entrypoint_scripts`、当前阶段 `allowed_scripts` 和 `allowed_leaf_scripts` 的合并结果。
- 缺少 `final_mod/meta/provenance.jsonl`、严格 QA 未过、包校验不一致、覆盖率缺失或模型审读未通过时，不能进入 `ready_for_manual_test`。
- `ready_for_manual_test` 只表示项目内静态证据允许人工测试，不表示游戏内实测通过。
- GUI fallback 只能在 CLI/库 adapter 不可用、格式不支持或受控写回必须 GUI 时进入；只打开工具不算完成。
- BSA/BA2 默认走只读审计和同路径 loose override；重打包是后续高风险受控流程，不是默认路径。
- Codex 不能访问真实 Skyrim、MO2、Vortex、AppData 或真实游戏配置目录。

### 修改后最小验证

改状态机、Skill 或 QA 规则后，至少运行：

```console
python -m json.tool config/workflow_policy.json
python -m json.tool config/workflow_state.schema.json
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py
```

如果改了 Python 脚本，还要运行对应语法检查：

```console
python -m py_compile scripts/write_workflow_state.py scripts/audit_translation_readiness.py scripts/run_non_gui_qa_gates.py
```

如果改了 Skill，运行 Skill 校验：

```console
python C:\Users\bupuy\.codex\skills\.system\skill-creator\scripts\quick_validate.py .\.codex\skills\<SkillName>
```

更完整的扩展说明见 `docs/codex_workflow.md`、`docs/skill_architecture.md`、`.codex/skills/workflow-policy-and-state/SKILL.md` 和 `.codex/skills/workflow-agent-orchestration/SKILL.md`。

## 发布前提醒

Codex 只能证明项目内文件、报告和工具输出一致。公开发布或长期使用前，你还需要自己确认：

- Mod 作者是否允许翻译和再发布。
- Nexus 或其他平台的发布规则。
- 你的真实游戏环境是否有冲突。
- MCM、脚本、任务、对话和菜单是否在游戏内正常显示。

没有人工游戏测试，不建议公开发布。
