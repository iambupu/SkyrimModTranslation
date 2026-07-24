# 高级用户指南

本文面向需要配置本机工具、判断能力边界、阅读报告和处理阻断的用户。日常使用见 [用户指南](./USER_GUIDE.md)，源码架构与测试维护见 [开发者指南](./developer_guide.md)。所有命令均在 Windows PowerShell 中运行。

## 入口层级

普通用户和顶层 Agent 的唯一公开入口是：

```powershell
python scripts\smt.py run <Mod路径> --game skyrim-se
```

后续只使用同一入口的 `status`、`resume`、`doctor` 和 `output`。本文后面出现的其他 Python 脚本全部标记为**内部实现/诊断**：它们供 CLI 内部编排、受控 adapter 排错或仓库维护使用，不是普通用户或顶层 Agent API，也不能替代公开结果投影和工作区/session 保护。

## 工作区与游戏身份

工作区的 `.skyrim-chs-workspace.json` 决定当前游戏，其中必须包含 `game_id`。命令指定的游戏与工作区不一致时，流程会停止，不会自动改走 Skyrim 或 Fallout 4 路径。

不要根据 Mod 名或目录结构修改游戏身份。需要切换游戏时，应新建工作区。

```text
mod/                         原始 Mod 副本
source/                      提取的源文本
translated/                  译文和同路径覆盖文件
work/                        解包结果和中间文件
qa/                          QA 与状态报告
out/<ModName>/汉化产出/       final_mod 和 _CHS.zip
glossary/                    当前工作区词典
config/                      本机工具配置
```

## 配置本机工具

工具路径写入工作区 `config/tools.local.json`，字段结构参考 `config/tools.example.json`。可以让 Agent 自动准备非 GUI 工具并检测缺项；不要把游戏目录、MO2 或 Vortex 目录配置成工具输入。

配置文件中的路径不全是外部程序。`MutagenCliPath`、`PexStringToolPath` 和 BSA 安全 wrapper 默认指向项目自带的 Python 入口，用户不需要下载同名程序。处理顺序始终是 CLI/库解码器、受控 wrapper、Codex GUI 后备。

非 GUI 路径使用的 Python、.NET、Mutagen、归档读取和 PEX 分析依赖，以及对应的上游项目链接，统一列在 [README 的“翻译依赖与引用”](./README.md#翻译依赖与引用)。自动准备会把需要的组件按完整版本、架构、来源、锁摘要或 adapter 源码摘要发布到 Windows Local AppData 下分离的机器共享 payload/control 根，并在 `.workflow/managed-tools.json` 记录工作区绑定；不会把缓存绝对路径写入 `tools.local.json`，也不会把工具写入真实游戏目录。

共享条目发布后不可修改，运行时由进程级共享租约保护；每个运行时租约先持有 store lifecycle shared guard，再按稳定条目顺序取锁，完整卸载只有取得 lifecycle exclusive 后才会进入删除，因此不会与“共享 Python 启动 adapter/SDK”形成交叉锁等待。修复、清理和卸载需要排他锁。`doctor` 只读报告绑定代、引用的持久 `pending/active/stale` 状态与当前只读 `valid/stale` 观察分类、条目身份/hash、损坏条目、可回收代及 staging/trash，不执行修复，也不会用观察结果自动重写 catalog。缓存维护必须通过 `managed-tool-cache-maintenance` Skill 的 inspect → plan → 用户确认 → apply → inspect 流程；只有明确确认的新计划才能释放观察为 stale 的引用，不要直接删除 Local AppData 目录或编辑 catalog。

### 手动安装的 GUI 与后备工具

Nexus Mods 下载通常需要登录。建议使用工具作者页面的当前版本，不要从转载站获取安装包。

| 工具 | 下载地址 | 工具说明 | 配置字段与使用边界 |
|---|---|---|---|
| LexTranslator | [Lexicon AI Translator](https://www.nexusmods.com/skyrimspecialedition/mods/143056) | 面向 Skyrim Mod 的 GUI 翻译工具，可用于插件、PEX 和词典辅助处理 | 填入 `LexTranslatorPath`；仅由 Codex 在非 GUI 路径不可用时操作 |
| xTranslator | [xTranslator](https://www.nexusmods.com/skyrimspecialedition/mods/134) | 支持 Skyrim 和 Fallout 4 的 ESP/ESM、Papyrus PEX、词典和字符串精修 | 填入 `XTranslatorPath`；用于查漏、精修或受控写回后备 |
| ESP-ESM Translator（EET4） | [ESP-ESM Translator](https://www.nexusmods.com/skyrimspecialedition/mods/921) | 可查看和维护 EET 工程、数据库，也能辅助检查插件和 PEX | 填入 `EspEsmTranslatorPath`；当前只是可选 GUI 工具，EET 词典的 RAG 只读检索不依赖它 |
| xEdit / SSEEdit / FO4Edit | [xEdit GitHub Releases](https://github.com/TES5Edit/TES5Edit/releases) | 查看插件结构、记录冲突和错误，作为 Mutagen 输出的交叉验证工具 | 填入 `DecoderTools.XEditPath`；默认只做审计，不作为自动翻译写回器 |

GUI 工具必须由用户自行下载安装。Codex 只把工作区内文件交给 GUI，并要求输出回到工作区 `tool_outputs`；opencode 和 Claude Code 不执行这些桌面步骤。

### 配置字段速查

| 字段 | 应填写的内容 |
|---|---|
| `DecoderTools.MutagenCliPath` | 项目自带的 `scripts/invoke_mutagen_plugin_text_tool.py`，通常无需修改 |
| `DecoderTools.PexStringToolPath` | 项目自带的 `scripts/invoke_mutagen_pex_string_tool.py`，通常无需修改 |
| `DecoderTools.PexDecompilerPath` | 已构建的 `Champollion.exe`；没有可执行文件时可以只保留源码目录 |
| `DecoderTools.BsaFileExtractorPath` | 项目自带的 BSA 安全 wrapper，不直接填写第三方脚本 |
| `DecoderTools.Ba2ExtractorPath` | 可选的外部完整 BA2 adapter；选择性 GNRL 解包可使用项目内置受控 adapter |
| `LexTranslatorPath` | `LexTranslator.exe` 路径 |
| `XTranslatorPath` | xTranslator 主程序路径，文件名可能随游戏工作区版本不同 |
| `EspEsmTranslatorPath` | 可选的 `EET4.exe` 路径 |

项目内置 adapter 可以流式盘点 Fallout 4 BA2，并对 GNRL 归档选择性提取有翻译价值的文件；DX10 纹理归档默认只盘点。需要完整解包时，`Ba2ExtractorPath` 仍必须实现 `skyrim-mod-chs.ba2-extractor.v1` 协议，并通过 staging、receipt、manifest、路径和 hash 验证。BSA Browser、Archive2 或其他普通 BA2 解包器不能直接填入该字段。

## 规模策略与恢复

`config/mod_scale_profiles.json` 定义 L0-L5 默认值和绝对安全上限。`prepare_mod_workspace.py` 会依次生成：

```text
qa/<ModName>.scale_assessment.json
qa/<ModName>.scale_execution.json
qa/<ModName>.resource_inventory.json
qa/<ModName>.extraction_plan.md
work/shards/<ModName>/index.json
work/shards/<ModName>/events.jsonl
```

L2 以上再次运行时会比较源身份和输出 hash，只重做变化的 shard。可用 `--max-files`、`--max-file-bytes`、`--max-total-bytes`、`--timeout-seconds`、`--extract-mode` 和 `--package-mode` 覆盖 profile 默认值；覆盖值会写入执行证据，超过绝对上限时直接拒绝。

任务调度器会读取各 Mod 的规模执行报告，自动采用较保守的文本、二进制和归档并发数。显式 `--max-workers`、`--max-binary-workers`、`--max-archive-workers` 和 `--timeout-seconds` 同样受绝对上限约束。

L5 聚合只读取当前聚合工作区的 `work/aggregate_inputs/<Project>/`。每个子项目必须提供 `manifest.json`、`coverage.json`、`provenance.jsonl`、`translation_dictionary.jsonl` 和 `final_overlay/`。manifest 至少声明与目录同名的 `project_name`、当前 `game_id`、`status=passed`、非负 `order`、`dependencies` 和 `overrides`。依赖项目必须存在且具有更小的 order。运行：

```powershell
python .\scripts\aggregate_translation_projects.py --mod-name <ModName> --force
```

相同路径不同内容、同一原文对应不同译文会写入 `out/<ModName>/aggregate/conflict_report.md` 并阻断发布。项目严格按 `order` 再按名称处理；后置模块只有在 manifest 的 `overrides` 中明确声明前置项目名时，才能覆盖同路径文件。

当前聚合合同只接受 provenance 已证明为 `loose_text` 的普通文本覆盖项。插件、PEX 和字符串表需要把子项目的 adapter result、验证报告及能力元数据一起迁移到聚合工作区，这条证据转移尚未实现，因此会在发布前阻断。

## Agent 能力差异

Codex、opencode 和 Claude Code 都能作为主控 Agent 使用同一工作流。opencode 和 Claude Code 属于非 GUI 顶层主控，可以处理扫描、翻译、报告、状态推进和其他已授权的非 GUI 步骤。

它们不是子智能体 worker，也不直接领取并行任务。遇到 LexTranslator、xTranslator、Computer Use 或其他桌面操作时必须 blocked，并把任务交给 Codex。Claude Code marketplace 提供的是非 GUI Skills，不会因此获得 Codex 的桌面能力。

STRINGS/DLSTRINGS/ILSTRINGS 已由专用 `BethesdaStringTableTool` 提供可验证的清点、导出、写回和复核。Skyrim SE/AE 与 Fallout 4 当前都为 `experimental_write`，在真实 Mod、xEdit 和游戏内验收完成前不能作为稳定交付。字符串表只是 localized 交付的一半；插件与语言表必须再形成 `localized_delivery` 复合证据。两个游戏的联合交付目前也都是 `experimental_write`，因此只能生成供人工测试的工作区产物。xTranslator GUI 产物本身不能替代 adapter receipt 和最终文件 provenance。

## Fallout 4 Experimental 边界

Fallout 4 的精确审计合同见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。这里仅说明会影响用户决策的能力。

| 资源 | 当前处理方式 |
|---|---|
| 文本直接保存在插件中的 ESP/ESM | 可处理已验证白名单字段；写回后必须重新解析并校验 |
| `.esl`、带 light 标记的 ESP/ESM、实际目标属于 Light master | 实验性受控写回；只对实际目标 owner 要求 master-style 与 canonical FormKey 证据；仅引用 Light master 不会使整个 full 插件降级，官方 Full master 由内置版本化策略识别，无需复制游戏文件 |
| STRINGS/DLSTRINGS/ILSTRINGS | 专用 adapter 实验性清点、导出、写回和复核 |
| 文字由外部字符串表保存的插件（localized） | 实验性插件/字符串表联合交付；组件缺失或 hash 不一致时整体阻断 |
| PEX Export（导出） | 使用 Fallout 4 类别提取可见字符串 |
| PEX Apply（写回） | 可在明确启用后生成并验证工作区副本，但不能正式交付 |
| BA2 | 可受控解包，译文使用同路径松散覆盖（loose override）；不修改、不重打包原 BA2 |
| SWF、GFX、DLL、EXE | 只读审计或原样复制 |

插件校验比较解析后的记录结构和逻辑内容，包括 masters、FormID、记录数量、字段位置及非目标内容。压缩记录和扩展长度会经过重新解析，因此不承诺输入输出之间只有目标原始字节变化。

Fallout 4 PEX Apply 即使生成并验证了工作区副本，`strict completion` 仍固定阻断正式完成。目前没有可由用户补交的证据可以解除这项限制，所以该副本不能作为正式汉化交付。

报告中的 `capabilities.archive.ba2.level` 描述 BA2 能力级别；当前 `read_only` 允许受控读取和物化，不代表允许写回或重打包。Skyrim 工作区中的 BA2 只做清单检查。

## 报告怎么读

通常只需按下面的顺序查看：

| 文件 | 用途 |
|---|---|
| `.workflow/progress_card.md` | 当前进度或最直接的阻断说明 |
| `qa/translation_readiness.json` | 缺少什么输入、工具或证据，以及详细检查结果 |
| `qa/workflow_state.json` | 当前阶段和允许执行的下一步 |
| `qa/final_mod_validation.md` | 最终目录的路径、来源、hash 和覆盖问题 |
| `out/<ModName>/汉化产出/final_mod/meta/provenance.jsonl` | 每个交付文件来自哪里、如何生成 |

出现 `stale` 或 `mismatch` 时，说明报告与当前工作区、输入文件或工具输出不再一致。应重新生成对应报告或受控输出，不要手工修改 JSON 消除错误。

以下结果必须优先处理：

- `Missing provenance rows`、`Final file SHA256 mismatches` 和 `Source SHA256 mismatches` 必须为 0。
- 插件或 PEX 的输入、输出 hash 与当前报告不一致时，旧证据失效。
- BA2 只有文件清单（inventory）、没有受控解包证据时，不能据此交付归档内资源的 loose override。
- `support_level=experimental` 只是总提示；是否能继续取决于本次实际使用的资源能力。

## 恢复阻断

先让当前 Agent 调用公开
`python scripts\smt.py --format json status`，读取返回的 `progress_card`
与 `diagnostics`；继续推进时调用公开 `resume`。下面的内部 QA 文件只用于
理解诊断证据，不用于让顶层 Agent自行拼接底层命令：

| 阻断 | 处理方式 |
|---|---|
| 工具缺失 | 补充 `tools.local.json`，重新运行工具检测 |
| `stale` / `mismatch` | 用当前输入和 Game Profile 重建报告或工具输出 |
| 单独 STRINGS adapter 证据缺失 | 重新执行专用 adapter；不能改走普通文本或 GUI 提权路径 |
| localized 联合证据缺失 | 重新生成插件锚点、引用覆盖和组件 receipt；任一半缺失都保持阻断 |
| Fallout 4 PEX Apply | 可保留验证副本，但不能纳入正式交付 |
| BA2 证据缺失 | 配置受控 adapter，重新执行解包和独立验证 |
| GUI 步骤 | 交给 Codex；无法自动保存时记录人工接手 |
| 模型校对过期 | 根据最新译文重新生成校对包并校对 |
| 游戏内测试待完成 | 在隔离的目标游戏环境中人工验证 |

Agent 可以重试低风险、已授权的工作区动作，但严格 QA、final_mod、GUI 和人工测试不会因为重试自动放行。

## 交付与人工测试

`final_mod/` 必须保持当前游戏的 Data 根结构。插件、PEX 或归档中的资源只有经过受控工具和 QA 验证后，才能作为同路径替换文件进入交付目录。

项目内报告和反解析只能证明流程证据成立，不能证明 Mod 已在游戏中正常加载。最终仍要人工检查加载顺序、菜单、MCM、任务与对话、脚本触发、冲突和中文质量。
