# 高级用户指南

这份指南写给需要配置工具、判断实验性能力、阅读报告或处理 blocked 状态的用户。插件架构、测试和发布维护见 [开发者指南](./developer_guide.md)。

## 工作区与 Game Profile

工作区的 `.skyrim-chs-workspace.json` 决定当前游戏。旧 marker 没有 `game_id` 时按 `skyrim-se` 兼容；显式命令中的游戏与 marker 冲突时，流程会 fail closed。

不要根据 Mod 名或目录结构修改游戏身份。要切换游戏，应创建新的工作区。

主要目录：

```text
mod/                         原始输入沙盒
work/                        解包、锁和中间缓存
source/                      提取的源文本
translated/                  译文和同路径 overlay
out/<ModName>/汉化产出/       final_mod、intermediate 和 _CHS.zip
qa/                          QA、状态和 handoff 报告
glossary/                    当前工作区术语表
config/                      本机工具配置
```

## 工具配置

本机路径写入工作区 `config/tools.local.json`。字段结构参考 `config/tools.example.json`。可以让 Codex 运行工具检测并解释缺项，不要把真实游戏目录填成工具输入。

常用字段：

| 字段 | 用途 |
|---|---|
| `DecoderTools.MutagenCliPath` | 插件文本导出、写回和反解析验证 |
| `DecoderTools.PexStringToolPath` | PEX 可见字符串 Export/Apply |
| `DecoderTools.BsaFileExtractorPath` | 受控 BSA 解包 wrapper |
| `DecoderTools.Ba2ExtractorPath` | 实现受控 BA2 协议且经过审查的 adapter |
| `DecoderTools.Archive7zPath` | `py7zr` 不可用时的 7Z 后备 |
| `GuiTools.LexTranslatorPath` | Codex GUI 后备 |
| `GuiTools.XTranslatorPath` | Codex GUI 精修或后备 |

`--tool-setup auto` 只准备受控的非 GUI 依赖。GUI 程序不会静默安装。

工具优先级是：CLI/库解码器、受控 wrapper、Codex GUI 后备。只有 `DecoderTools.Ba2ExtractorPath` 指向符合协议的受控 adapter，并且 receipt、manifest、路径和 hash 独立验证都通过时，BA2 才能安全物化。直接运行外部 extractor 不算有效证据。

## 非 GUI agent 边界

Codex 是完整入口。opencode 和 Claude Code 是非 GUI 顶层主控，可以读取当前 Game Profile、状态机和 QA 报告，处理已经授权的非 GUI 步骤。

它们不是子智能体 worker，不直接领取 `qa/workflow_tasks.json` 中的任务，也不能绕过 `workflow_state.json`、资源锁或严格 QA。遇到以下任务时必须 blocked，并把 `handoff_target` 指向 Codex：

- LexTranslator 或 xTranslator 窗口操作。
- Computer Use、pywinauto 或 UI Automation。
- `gui:desktop` 锁。
- 只能通过 GUI 保存的插件或 PEX 输出。

Claude Code marketplace 只暴露非 GUI Skills。安装该入口不会获得 Codex 的桌面能力。

## Fallout 4 Experimental 能力边界

精确合同和审计字段见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。这里保留用户需要作决定的部分。

### 插件与 STRINGS

非 localized ESP/ESM/ESL 只处理 profile 白名单中的玩家可见字段。写回后必须由 `Fallout4Mod` 反解析，并验证 masters、FormID、record count 和非目标字段不变。

Fallout 4 localized plugin 以及 `.strings`、`.dlstrings`、`.ilstrings` 当前不支持。它们会被检测并 blocked。这不是普通工具漏配，不能改走 Skyrim 路径后宣称完成。

### PEX Export 与 Apply

Fallout 4 PEX Export 可用，类别必须是 `Fallout4`。PEX Apply 可以在显式 opt-in 后生成并验证工作区副本，但当前 `strict completion` 固定阻断。现阶段没有可由用户补交的证据能够解除这道门禁。

Codex 不直接修改 `.pex`。Apply 输出必须来自受控工具目录，并经过反读、hash 和交付一致性检查。

### BA2

BA2 materialization 只对 Fallout 4 profile 开启。流程先做只读 inventory，再由受控 wrapper 解包到工作区，校验 source hash、receipt、manifest、entry path、size 和 SHA256。

译文以归档内原路径生成 same-path loose override。原 BA2 不修改，`archive_allow_repack=false`，不重打包。Skyrim profile 对 BA2 只做 inventory，不允许物化。

### 受保护资源

SWF、GFX、DLL、EXE 只能只读审计或从 `mod/` 原样复制。工作流不修改这些文件，也不把改名、重新压缩或旁挂文件当成翻译完成。

## 报告怎么读

状态和交付报告应显示同一组 GameContext metadata：

- `game_id`
- `game_profile_version`
- `game_display_name`
- `support_level`
- `plugin_adapter`
- `plugin_adapter_version`
- `pex_category`
- `pex_writeback_status`
- `archive_delivery`
- `archive_materialization_enabled`
- `archive_allow_repack`

重点入口：

| 文件 | 看什么 |
|---|---|
| `qa/translation_readiness.json` | 当前输入和必需证据是否齐全 |
| `qa/workflow_state.json` | 权威阶段、阻断原因和下一步 |
| `qa/workflow_tasks.json` | 从状态派生的任务和锁 |
| `qa/codex_handoff.json` / `qa/agent_handoff.json` | 短接手摘要和目标 agent |
| `.workflow/progress_card.md` | 用户可见进度 |
| `qa/final_mod_validation.md` | provenance、hash、路径和旁挂文件问题 |
| `out/<ModName>/汉化产出/final_mod/meta/provenance.jsonl` | 每个交付文件的直接来源 |

出现 `stale` 或 `mismatch` 时，先比较 marker 与报告中的游戏、profile version、adapter、PEX category 和归档策略。Skyrim 报告不能复用到 Fallout 4 工作区，反向也一样。不要手工改 JSON 消掉错误。

常见判读：

- `support_level=experimental` 本身不是永久阻断。
- 必需输入命中 profile 不支持的能力时，必须阻断。
- `Missing provenance rows`、`Final file SHA256 mismatches`、`Source SHA256 mismatches` 必须为 0。
- PEX 或插件输出的 adapter、输入 hash、输出 hash 与当前报告不一致时，旧证据失效。
- BA2 只有 inventory 而没有受控 extraction evidence 时，不能生成 BA2 来源的 loose override 结论。

## 恢复 blocked 状态

先让 Codex 解释 `qa/workflow_state.json` 和 `.workflow/progress_card.md`。恢复时按阻断类型处理：

| 阻断 | 处理方式 |
|---|---|
| 缺少工具 | 补充工作区工具路径，再运行检测 |
| stale / mismatch | 重新生成当前 Game Profile 的报告和受控输出 |
| localized / STRINGS | 保持 blocked，等待能力实现 |
| Fallout 4 PEX Apply | 可保留已验证的实验性工作区副本；strict completion 当前固定 blocked，等待能力升级 |
| BA2 adapter 或证据缺失 | 配置受控 adapter，重新执行安全解包和独立验证 |
| GUI 保存失败 | 交回 Codex；无法自动保存时记录人工接手，不冒充完成 |
| 模型校对过期 | 基于最新 final text/binary packet 重新校对 |
| 人工游戏测试待完成 | 在对应游戏的隔离测试环境中执行 |

低风险恢复可使用项目提供的 safe resume，但它只执行状态机授权的工作区内 Python 动作。严格 QA、final_mod、GUI 和人工测试不会因重试而自动放行。

## 交付与人工测试

`final_mod/` 必须保持当前游戏的 Data 根结构。插件、PEX 和归档来源文件只有通过受控工具与 QA 证据后，才能覆盖同路径原始副本。

项目报告、合成 fixture、反解析和 effect regression 都不能证明真实游戏加载成功。进入人工测试后，仍需检查加载顺序、MCM、任务与对话、脚本触发、菜单显示、冲突和中文质量。
