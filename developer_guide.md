# 开发者指南

本文面向插件维护者，说明源码架构、状态合同、测试、扩展和发布维护。普通使用见 [用户指南](./USER_GUIDE.md)，本机工具、能力边界和报告判读见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。Fallout 4 的详细合同见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。

## 公开门面与内部入口

普通用户和顶层 Agent 的唯一公开控制入口是：

```powershell
python scripts\smt.py run <Mod路径> --game skyrim-se
```

本文列出的其他脚本均为**内部实现/诊断**接口。`smt.py` 在状态机外部做工作区/session、事务导入、状态驱动循环和公开结果投影；workflow policy、`next_actions` 与 `workflow_tasks` 不得引用这个外层 controller。底层初始化、队列、刷新、恢复、QA 和 adapter 入口继续由现有状态机与授权面管理，但不构成第二套用户 API。

## 仓库与工作区

插件仓库保存可复用能力：

- `skills/`：运行期 Skills 的唯一权威目录。
- `scripts/`：Python 主流程、QA、状态机和工具 wrapper。
- `adapters/`：受控 .NET adapter 及测试。
- `config/`：Game Profile、schema、workflow policy 和工具模板。
- `tests/`、`scripts/test_*.py`：合同、回归和集成测试。
- `samples/`：可提交的合成 fixture 与 effect-regression 快照。

具体 Mod 工作区只保存输入、译文、中间文件、报告和交付物。初始化不得复制仓库的 `scripts/`、`adapters/` 或运行期 `skills/`。

仓库脚本可以读取插件源码和当前工作区，不能访问真实游戏、MO2/Vortex、Steam、游戏/管理器的 AppData 配置或 `Documents/My Games`。唯一的 AppData 例外是项目受控脚本访问版本化 Windows Local AppData 共享托管工具存储；该存储不包含 Mod、译文或游戏配置。二进制由受控工具在工作区内生成副本；Python 入口只负责授权、调用、验证、复制和记录。

## Game Profile 与 GameContext

`config/game_profiles/` 描述游戏差异，包括插件格式、PEX 类别、Data 根、编码、风险路径、归档策略、词典来源和各项 capability。

`scripts/game_context.py` 将 Profile 解析为只读 `GameContext`。工作区 `.skyrim-chs-workspace.json` 是游戏身份来源，必须包含 `game_id`。显式参数与 marker 冲突时必须 fail closed，任何执行入口都不得以 Skyrim 作为静默回退。

一次工作流应尽早加载并向下游复用 GameContext。不要在循环或每个报告生成器中重复读取 Profile，也不要把外部 Agent 探测放进 Codex 默认翻译路径。

| Profile | 支持级别 | 定位 |
|---|---|---|
| `skyrim-se` | `stable` | Skyrim SE/AE 稳定支持 |
| `fallout4` | `experimental` | Fallout 4 实验支持 |

能力必须逐项判定，不能由上表的整体支持级别推导：

| 能力 | Skyrim SE/AE | Fallout 4 |
|---|---|---|
| 普通 `plugin_text` | `stable` | `experimental_write` |
| `plugin_text` + `light` trait | `experimental_write` | `experimental_write` |
| `pex` | `stable` | `experimental_write` |
| `string_tables` | `experimental_write` | `experimental_write` |
| `localized_delivery` | `experimental_write` | `experimental_write` |

`experimental_write` 允许显式授权的工作区写回和验证，但不能作为稳定 strict completion。当前插件为 Light 或实际写回目标属于 Light owner 时必须提供相应 master-style/FormKey 证据；仅引用 Light master 不触发整插件降级。localized 插件必须使用 composite receipt，不能借基础 `plugin_text` 或单独 `string_tables` 提权。

## 规模与风险评估

`scripts/audit_mod_scale.py` 在解包或复制前读取目录元数据、ZIP/7Z 中央目录和 Game Profile 资源分类，生成 `qa/<ModName>.scale_assessment.json`。规模 L0-L5 由预估展开体积、文件数、候选行数和归档数分别分级后取最高值；风险 R0-R4 根据插件、PEX、STRINGS、localized/light trait、实验能力和未知资源独立判断。阈值与建议保存在 `config/mod_scale_profiles.json`。

候选行数是基于资源类别和字节数的容量估算，不是翻译候选真值。评估报告本身仍标记 `candidate_rows_are_estimated=true` 和 `recommendations_status=advisory-not-enforced`；实际行为由随后生成的 `qa/<ModName>.scale_execution.json` 决定。该报告绑定 profile/config hash，记录默认值、覆盖值、实际限额、超时、磁盘预检、解包和打包模式。覆盖值不能超过 `absolute_limits`。

L2 以上使用 `work/shards/<ModName>/index.json` 和 append-only `events.jsonl` 保存源身份、输出 hash 与 checkpoint。目录、ZIP 和 7Z 只重做变化项；L2-L4 默认排除 Profile 标记的受保护资源。BSA/BA2 wrapper 复用同一限制、超时和磁盘策略，并分别写 `qa/<ModName>.<Archive>.archive_execution.json`。BA2 内置 adapter 只选择性提取 GNRL；DX10 纹理保持 inventory-only。

L3/L4 的 `package_mode=translation-overlay` 不复制原 Mod 的受保护资源。L5 固定 `multi-project/aggregate-only`，不能通过参数强制回单工作区；`aggregate_translation_projects.py` 只读取 `work/aggregate_inputs/` 下有 manifest、coverage、provenance、dictionary 和 final_overlay 的子项目，按 manifest `order` 处理并验证 `dependencies`/`overrides`，冲突未解决时不发布。当前聚合器只接受 capability 为 `loose_text` 的子项目 provenance；插件、PEX、字符串表等二进制 lineage 在具备 adapter evidence 迁移合同前 fail closed。

## Adapter 架构

工作流决定能否执行，适配器（adapter）负责格式读写，QA 验证实际输出。插件文本共用 `mutagen-bethesda-plugin`，游戏差异通过 Game Profile options 传入。

### 插件

共享 Mutagen 工具按 GameContext 选择 `SkyrimMod` 或 `Fallout4Mod`。导出、写回和验证必须读取同一字段合同；每条写回记录使用 schema v2 的稳定记录身份、字段路径和 occurrence，不能根据过滤后的译文重新编号。

写回后要重新解析输出并验证 masters、FormID、记录数量，以及解析后的结构与逻辑内容。TES4 header 清点、payload 保留和二进制不变量检查共用同一个 subrecord reader，统一处理 `XXXX` 扩展长度。校验覆盖 record flags、subrecord 类型/顺序/索引和非目标逻辑 payload，但不声称原始文件只有目标字节发生变化；压缩记录、`XXXX` 长度包装和重序列化都可能改变等价的二进制表示。

插件 Apply/Verify 必须确认当前插件类型，并只为实际写回目标 owner 解析所需的 master style。Skyrim SE 与 Fallout 4 使用 MAST 顺序进行插件内 FormID 到 FormKey 的映射；无关 master 的 full/light 分类不构成前置条件。当前插件以自身 TES4 header 为准；`.esl` owner 可由扩展名识别为 light；`config/plugin_master_styles.json` 中的官方 Full master 由嵌入适配器的同一版本策略确认，不读取用户游戏文件；只有实际目标 owner 仍未知时，其他 `.esp/.esm` 才需要工作区副本或 schema v2 manifest 的相对路径、SHA256 与 Small flag 证据。无法确认、hash 过期或证据冲突分别返回 `master_style_unknown`、`master_style_evidence_stale`、`master_style_conflict`。STRINGS 家族只能进入 `bethesda-string-tables`，localized 插件只能由 `localized_delivery` 联合插件锚点、引用覆盖和字符串表组件；任何路径都不能改走另一游戏 adapter 或普通文本流程。

主流程先只读导出并记录每个候选的 canonical owner。只有候选 owner 为未知第三方 `.esp/.esm` 时，才依次查找插件同目录及 `work/master_context/<game_id>/`、`work/master_context/<ModName>/`、`work/master_context/` 下的只读副本，并自动生成 `work/plugin_context/<ModName>/<ArtifactKey>.master-styles.json` 后重跑导出。`ArtifactKey` 绑定插件相对路径，同名嵌套插件不会共享证据。无关 master 不查找、不哈希；已有 manifest 中的无关条目只检查结构和身份，不读取或哈希其指向的文件，并会裁剪为当前目标集合。实际目标缺证据时以 `master_style_unknown` 和 `master_style_preflight_blocked` 阻断。官方已知 Full master 直接使用 `config/plugin_master_styles.json`，不查找本地游戏文件。完整插件阶段只接受 `work/extracted_mods/<ModName>/` 下由 `prepare_mod_workspace.py` 准备的工作区，不直接从 `mod/` 执行写回流程。

```json
{
  "schema_version": 2,
  "game_id": "fallout4",
  "plugin": "Patch.esp",
  "masters": [{
    "mod_key": "SomeMaster.esp",
    "master_style": "light",
    "inspected_path": "work/master_context/fallout4/SomeMaster.esp",
    "inspected_sha256": "<64-character SHA256>",
    "small_flag": true
  }]
}
```

### PEX

PEX Export 与 Apply 绑定 `pex_category`、输入相对路径和 SHA256。Skyrim 使用 `Skyrim`，Fallout 4 使用 `Fallout4`。

Fallout 4 Apply 需要显式 opt-in，并在输出后反读验证；当前只有 fixture 证明过的 `Debug.Notification` 和 `Debug.MessageBox` 直接字面量可自动写回，其他 API 或动态参数保持人工复核。当前 strict gate 固定判定为不可放行。adapter 生成文件不等于工作流拥有交付权限。

### 归档

归档职责按格式拆分：

- `bsa-archive-audit` 负责 BSA inventory、受控 materialization、manifest 和 loose override 路由。
- `ba2-archive-audit` 负责 BA2 inventory、受控 materialization、receipt/manifest/hash 验证和 loose override 路由。

BA2 可以复用共享的只读解析代码，但不能把 BA2 请求转交给 BSA Skill。解包使用临时 staging；新 payload 和 evidence 全部验证通过后才原子替换旧结果，失败时保留上一份已验证内容。原归档不修改、不重打包。

外部进程不是系统沙箱。wrapper 只能验证工作区路径、链接、源文件和输出证据，不能证明任意 executable 没有修改系统其他位置。因此 adapter 必须经过审查，并受路径和协议约束。

### GUI

GUI、Computer Use、pywinauto/UI Automation 和 `gui:desktop` 锁属于 Codex。opencode 与 Claude Code 是非 GUI 顶层主控；GUI-only 任务必须 blocked 并 handoff 到 Codex。

## Metadata 与 Schema

公共上下文至少包含：

```text
game_id
game_profile_version
game_display_name
support_level
interface_translation_encoding
```

这些 metadata 要在 readiness、workflow state、workflow tasks、handoff、progress card、strict QA、final_mod manifest、provenance 和 final binary review 中保持一致。

新增或修改字段时必须同时处理：

1. GameContext 生产者和 schema。
2. 所有报告生产者与消费者。
3. stale/mismatch 判定。
4. fixture 和篡改测试。
5. manifest 与 provenance 校验。

`workflow_state.json` 仍是状态权威。GameContext 只提供上下文和一致性门禁；工具证据中的 adapter、operation、options 和 hash 按当前 capability 另行验证。

## 状态机、任务与锁

`qa/workflow_state.json` 是权威状态。`qa/workflow_tasks.json` 从状态派生，只保存可执行任务、依赖和调度信息。`qa/codex_handoff.json` 与 `qa/agent_handoff.json` 只是短摘要。

脚本授权来自 `config/workflow_policy.json`。`next_actions` 不能引用授权面之外的脚本。

| 锁 | 作用 |
|---|---|
| `work/.workflow.lock` | 全局状态刷新、严格 QA 和兼容主流程 |
| `mod:<ModName>` | Mod 级写入、final_mod 和严格 QA |
| `file:<ModName>:...` | 单文件 lane |
| `resource:<ModName>:...` | 独立资源 lane |
| `global:workflow-state` | 状态、任务、handoff 和进度卡 |
| `gui:desktop` | Codex 桌面操作 |

领取任务前必须检查 `resource_locks`。`mod:` 与该 Mod 下全部 `file:`、`resource:` lane 冲突。GUI、全局刷新、strict QA、final_mod、共享 glossary/RAG 重建和同资源写入保持串行。

### 主控与子智能体

主控负责读取状态、生成任务、划分 lane、限制并发、汇总结果，并在并行批次后串行刷新状态。子智能体只能通过 `claim_workflow_task.py` 领取已授权且可并行的任务，只执行领取到的命令，并通过同一入口回写结果。

顶层 opencode 和 Claude Code 不是子智能体，不领取任务或直接编辑任务 JSON。失败恢复仍受状态机和 workflow policy 限制；AgentOps 只能辅助复核、归因和恢复建议。

## Final Mod 与 QA

```text
out/<ModName>/汉化产出/final_mod/
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

`final_mod/` 保持当前 Game Profile 的 Data 根。`direct-replacement-final-mod` 是完整副本；`translation-overlay-package` 只包含已验证替换项并明确 `RequiresOriginalMod=true`。两种模式都必须同路径同名替换，旁挂语言文件不能冒充交付。原始二进制只能原样复制，受控工具输出验证通过后才能覆盖。

严格 QA 至少检查：

- GameContext metadata 在报告和交付物中一致。
- provenance 覆盖全部 final_mod 文件，source/final hash 匹配。
- 文本结构、编码、占位符、覆盖率和模型校对为最新版本。
- 插件与 PEX 输出可反读，结构合同和保护项通过。
- BSA/BA2 inventory、materialization 和 loose override 证据符合当前 Profile。
- 必需输入使用不支持的能力时保持 blocked。

项目门禁通过只允许进入人工游戏测试，不代表真实游戏加载、GUI 保存或翻译质量已经认证。

## Fixture 与测试

测试分为三层：

1. `tests/`：通用 Python 合同与文档职责。
2. `scripts/test_*.py`：Game Profile、路由、adapter、状态传播和合成工作区回归。
3. `samples/effect_regression/`：确定性的能力快照与 fixture。

新增能力至少覆盖正常路径、fail-closed、游戏身份冲突、跨游戏证据、路径越界、hash 漂移、stale evidence 和 metadata 篡改。合成 fixture 只能证明仓库合同，不能替代真实工具和游戏内认证。

effect-regression 的 `repo-contract` 只记录稳定仓库事实。只有行为变化已经审查且实际输出正确时才更新 expected，不能用刷新快照掩盖失败。

## CI 与验证

GitHub Actions 在 `master`、`main`、`codex/**` push、目标为主分支的 PR 和手动触发时运行，并取消同一 ref 上的旧任务。五个 job 的职责是：

| Job | 主要检查 |
|---|---|
| `static` | 仓库结构、Skills、capability/Registry、状态合同和跟踪回归 |
| `windows-smoke` | Windows 入口合同、Game Profile、路由和工作流语义 |
| `windows-fallout4-adapters` | .NET 插件、PEX、BSA capability evidence、BA2 adapter |
| `windows-fallout4-workflow` | Fallout 4 合成工作区集成 |
| `effect-regression` | 仓库效果快照 |

本地提交前至少运行：

```powershell
python scripts/ci_validate_repo.py --strict
python scripts/test_skill_effects.py
python scripts/test_workflow_health.py --repo-only --strict
python scripts/run_effect_regression.py --all --ci
python -m compileall -q scripts
git diff --check
```

按改动范围补充以下架构回归：

```powershell
python -m pytest -q scripts/test_capability_resolver.py scripts/test_adapter_registry.py scripts/test_plugin_capability_adapter.py
python -m pytest -q scripts/test_archive_capabilities.py scripts/test_bsa_loose_override.py scripts/test_used_capabilities.py
python -m pytest -q scripts/test_mod_scale_execution.py scripts/test_bethesda_archive_adapter.py scripts/test_translation_overlay_delivery.py scripts/test_aggregate_translation_projects.py
python -m pytest -q scripts/test_agent_handoff_checkpoint_regressions.py
dotnet test adapters/SkyrimPluginTextTool.Tests/SkyrimPluginTextTool.Tests.csproj -c Release --nologo
python -m pytest -q scripts/test_fallout4_plugin_adapter_regressions.py scripts/test_fallout4_pex_adapter_regressions.py scripts/test_ba2_extractor_regressions.py
python -m pytest -q scripts/test_fallout4_workflow_integration.py
```

`pytest` 默认只发现 `tests/`，所以 `scripts/test_*.py` 必须显式运行。CI 不读取真实游戏目录、不启动 GUI、不调用翻译 API，也不证明真实 Mod 可以加载。

## 扩展新游戏

新增游戏按能力逐项接入，不复制现有游戏流程：

1. 新增 schema v2 Game Profile，声明 Data 根、编码、风险路径、glossary 和逐项 capability。
2. 接入 GameContext 与 marker 校验，确认所有执行入口都 fail closed。
3. 为插件、string table、PEX 和各归档格式选择 adapter；未实现项明确阻断。
4. 更新资源分类和对应 Skills。
5. 将 metadata 传播到状态、QA、handoff、manifest 和 provenance。
6. 增加身份冲突、跨游戏污染、篡改、路径安全和 adapter fixture。
7. 更新用户支持矩阵、高级边界和独立能力合同。
8. 完成真实工具与游戏内验证后，再提升单项能力级别。

不使用 PEX 的游戏将 `capabilities.pex.level` 设为 `unsupported`。归档按 `archive.<ext>` 分别声明。不要全局重命名插件、marker、`SKYRIM_CHS_*` 环境变量或 `[SMT ...]` 前缀，它们是兼容合同，不是单游戏限制。

## 版本与发布

- Profile schema 或能力含义变化：提升 Profile 版本并增加合同测试。
- Adapter 输入、输出或验证合同变化：提升 `adapter_contract_version` 或协议版本。
- 报告 schema 变化：同步生产者、消费者和 stale 判定。
- 用户可见能力或安装内容变化：按项目版本策略提升插件版本。

发布前确认 Codex 与 Claude manifest 版本一致，运行完整 CI 和 effect regression，并只从 Git 跟踪文件生成源码包。不得包含真实 Mod、本机工具配置、缓存、编译文件或工作区产出。

`master` 分支保护、独立 Agent 审查、hotfix 和堆叠 PR 的维护要求见 [仓库合并与审查规则](./docs/repository_governance.md)。紧急修复也必须经过 PR、完整 CI、审查对话解决和 squash merge，不能先合入再验证。

`config/capability_wiring_contracts.json` 只检查 Profile、adapter 入口、fixture 源文件和 routing/provenance/QA 消费面是否接线一致，不是能力认证或晋级门禁。fixture 执行结果由测试负责，真实 Mod、xEdit 和游戏内证据必须另外记录。Experimental 升级为稳定支持仍需要合法可复现的真实样本、固定工具版本、adapter 合同、严格 QA、人工游戏内测试和失败记录；单个成功样本或合成 fixture 不足以扩大支持声明。

## 相关文档

- [AGENTS.md](./AGENTS.md)
- [仓库合并与审查规则](./docs/repository_governance.md)
- [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)
- [Effect Regression Workflow](./docs/effect_regression_workflow.md)
- [Agent 入口索引](./docs/agent_adapters.md)
- [Agent Compatibility](./docs/agent_compatibility.md)
- [Non-GUI Agent Workflow](./docs/agent_workflow.md)
- [Tool Adapter](./docs/tool_adapter.md)
- [Skill Architecture](./docs/skill_architecture.md)
- [Codex 接手指南](./docs/codex_workflow.md)
