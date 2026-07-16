# 开发者指南

本文面向插件维护者，说明源码架构、状态合同、测试、扩展和发布维护。普通使用见 [用户指南](./USER_GUIDE.md)，本机工具、能力边界和报告判读见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。Fallout 4 的详细合同见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。

## 仓库与工作区

插件仓库保存可复用能力：

- `skills/`：运行期 Skills 的唯一权威目录。
- `scripts/`：Python 主流程、QA、状态机和工具 wrapper。
- `adapters/`：受控 .NET adapter 及测试。
- `config/`：Game Profile、schema、workflow policy 和工具模板。
- `tests/`、`scripts/test_*.py`：合同、回归和集成测试。
- `samples/`：可提交的合成 fixture 与 effect-regression 快照。

具体 Mod 工作区只保存输入、译文、中间文件、报告和交付物。初始化不得复制仓库的 `scripts/`、`adapters/` 或运行期 `skills/`。

仓库脚本可以读取插件源码和当前工作区，不能访问真实游戏、MO2/Vortex、Steam、AppData 或 `Documents/My Games`。二进制由受控工具在工作区内生成副本；Python 入口只负责授权、调用、验证、复制和记录。

## Game Profile 与 GameContext

`config/game_profiles/` 描述游戏差异，包括插件格式、PEX 类别、Data 根、编码、风险路径、归档策略、词典来源和各项 capability。

`scripts/game_context.py` 将 Profile 解析为只读 `GameContext`。工作区 `.skyrim-chs-workspace.json` 是游戏身份来源，必须包含 `game_id`。显式参数与 marker 冲突时必须 fail closed，任何执行入口都不得以 Skyrim 作为静默回退。

一次工作流应尽早加载并向下游复用 GameContext。不要在循环或每个报告生成器中重复读取 Profile，也不要把外部 Agent 探测放进 Codex 默认翻译路径。

| Profile | 支持级别 | 定位 |
|---|---|---|
| `skyrim-se` | `stable` | Skyrim SE/AE 稳定支持 |
| `fallout4` | `experimental` | Fallout 4 实验支持 |

## Adapter 架构

工作流决定能否执行，适配器（adapter）负责格式读写，QA 验证实际输出。插件文本共用 `mutagen-bethesda-plugin`，游戏差异通过 Game Profile options 传入。

### 插件

共享 Mutagen 工具按 GameContext 选择 `SkyrimMod` 或 `Fallout4Mod`。导出、写回和验证必须读取同一字段合同；每条写回记录使用 schema v2 的稳定记录身份、字段路径和 occurrence，不能根据过滤后的译文重新编号。

写回后要重新解析输出并验证 masters、FormID、记录数量，以及解析后的结构与逻辑内容。校验覆盖 record flags、subrecord 类型/顺序/索引和非目标逻辑 payload，但不声称原始文件只有目标字节发生变化；压缩记录、`XXXX` 长度包装和重序列化都可能改变等价的二进制表示。

localized flag、STRINGS 家族以及尚未支持的 light FormID 必须形成明确 blocker，不能改走另一游戏 adapter。

### PEX

PEX Export 与 Apply 绑定 `pex_category`、输入相对路径和 SHA256。Skyrim 使用 `Skyrim`，Fallout 4 使用 `Fallout4`。

Fallout 4 Apply 需要显式 opt-in，并在输出后反读验证；当前 strict gate 固定判定为不可放行。adapter 生成文件不等于工作流拥有交付权限。

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

`final_mod/` 保持当前 Game Profile 的 Data 根。默认交付同路径同名替换文件；旁挂语言文件不能冒充完整交付。原始二进制只能原样复制，受控工具输出验证通过后才能覆盖。

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
| `windows-smoke` | Windows 文档、Game Profile、路由和工作流语义 |
| `windows-fallout4-adapters` | .NET 插件、PEX、BSA capability evidence、BA2 adapter |
| `windows-fallout4-workflow` | Fallout 4 合成工作区集成 |
| `effect-regression` | 仓库效果快照 |

本地提交前至少运行：

```powershell
python scripts/ci_validate_repo.py --strict
python -m pytest -q tests/test_task6b2_documentation.py scripts/test_skill_effects.py
python scripts/test_workflow_health.py --repo-only --strict
python scripts/run_effect_regression.py --all --ci
python -m compileall -q scripts
git diff --check
```

按改动范围补充以下架构回归：

```powershell
python -m pytest -q scripts/test_capability_resolver.py scripts/test_adapter_registry.py scripts/test_plugin_capability_adapter.py
python -m pytest -q scripts/test_archive_capabilities.py scripts/test_bsa_loose_override.py scripts/test_used_capabilities.py
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

Experimental 升级为稳定支持需要合法可复现的真实样本、固定工具版本、adapter 合同、严格 QA、人工游戏内测试和失败记录。单个成功样本或合成 fixture 不足以扩大支持声明。

## 相关文档

- [AGENTS.md](./AGENTS.md)
- [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)
- [Effect Regression Workflow](./docs/effect_regression_workflow.md)
- [Agent 入口索引](./docs/agent_adapters.md)
- [Agent Compatibility](./docs/agent_compatibility.md)
- [Non-GUI Agent Workflow](./docs/agent_workflow.md)
- [Tool Adapter](./docs/tool_adapter.md)
- [Skill Architecture](./docs/skill_architecture.md)
- [Codex 接手指南](./docs/codex_workflow.md)
