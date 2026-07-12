# 开发者指南

本文面向插件维护者，说明 Game Profile、adapter、证据 schema、状态机、并发边界、测试和发布维护。普通用户流程见 [USER_GUIDE.md](./USER_GUIDE.md)，工具与报告判读见 [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md)。Fallout 4 的精确能力合同见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。

## 仓库与工作区

插件仓库保存可复用能力：

- `skills/`：运行期 Skills 的唯一权威目录。
- `scripts/`：Python 主流程、QA、状态和工具 wrapper。
- `adapters/`：受控 .NET adapter 源码与测试。
- `config/`：Game Profile、schema、workflow policy 和工具模板。
- `tests/`、`scripts/test_*.py`：仓库单元、回归和集成测试。
- `samples/`：可提交的合成 fixture 与 effect-regression 快照。

具体 Mod 工作区只保存 `mod/`、`work/`、`source/`、`translated/`、`out/`、`qa/`、`.workflow/`、`traces/`、`glossary/` 和本机配置。初始化不得复制仓库的 `scripts/`、`adapters/` 或运行期 `skills/`。

仓库脚本可以读取插件源码和当前工作区，不能访问真实游戏、MO2/Vortex、Steam、AppData 或 `Documents/My Games`。二进制只能由受控工具生成工作区副本；Python 主流程负责调用、验证、复制和记录，不直接改写。

## Game Profile 与 GameContext

Game Profile 位于 `config/game_profiles/`，描述游戏差异：

- 插件格式和 Mutagen release。
- PEX category 与写回状态。
- Data 根目录、受保护目录和风险路径。
- localized plugin 与 string table 能力。
- Interface 运行时编码。
- 归档类型、materialization 和 repack 策略。
- glossary 种子和支持级别。

`scripts/game_context.py` 把 profile 解析为只读 `GameContext`。工作区 `.skyrim-chs-workspace.json` 是唯一游戏身份来源。旧 marker 缺少 `game_id` 时回退 `skyrim-se`；显式 CLI game 与 marker 冲突时 fail closed。不得通过 Mod 名、目录名或文件扩展名推断游戏。

一次工作流应尽早加载 GameContext，并向下游复用。不要在循环、每个报告生成器或 Codex 默认热路径中重复读取 profile，也不要把外部 agent 探测挂到 profile 加载过程。

对外只提供两种身份：

| Game Profile | 支持级别 | 定位 |
|---|---|---|
| `skyrim-se` | `stable` | Skyrim SE/AE 默认完整入口 |
| `fallout4` | `experimental` | Fallout 4 Experimental Support |

## adapter 架构

adapter 负责二进制格式读写，工作流负责授权和证据。当前插件 adapter 标识为 `skyrim-mutagen` 或 `fallout4-mutagen`，版本由 `scripts/game_context.py` 的合同常量传播。

### 插件 adapter

共享的 Mutagen 工具按 GameContext 选择 `SkyrimMod` 或 `Fallout4Mod`。写回只能处理白名单中的玩家可见字段，输出进入工作区 `tool_outputs`。验证阶段重新解析实际输出，而不是只相信写回报告。

Fallout 4 非 localized 插件至少验证 masters、FormID、record count 和原始二进制结构不变量，missing/unsupported 为 0。C# adapter 的规范快照按 record/subrecord occurrence 比较：目标 payload 必须与译表 source/target 精确对应；非目标 payload、record flags、subrecord 类型/顺序/索引和未列入允许项的 header bytes 必须逐字节一致。允许项只有目标 record data-size 与包含目标的祖先 GRUP size。localized flag 或 STRINGS 家族进入明确 blocker，不能回退到 Skyrim adapter。

### PEX adapter

PEX Export 和 Apply 都绑定 `pex_category`、输入相对路径和 SHA256。Skyrim category 为 `Skyrim`；Fallout 4 为 `Fallout4`。

Fallout 4 Apply 是 experimental。调用方必须显式 opt-in，输出仍要反读验证；但当前 strict gate 固定判定为不可放行。adapter 生成了输出文件不代表工作流已经获得交付授权，也没有可由用户补交的证据可以解除这道门禁。

### 归档 adapter

`bsa-archive-audit` 负责 BSA materialization 和 BSA/BA2 通用只读 inventory。`ba2-archive-audit` 独占 BA2 materialization。

BA2 wrapper 使用隔离 staging、受控协议和独立验证。adapter 返回后、原子发布前，wrapper 生成排序后的 `path/size/sha256` entry 清单、确定性 payload root，并将其与源 BA2 快照、adapter identity/protocol 和 limits 一起纳入 receipt binding。`new_ba2_archive_manifest.py` 只能复用该快照，当前 extracted payload 有任何增删改都必须失败，不能从当前目录重建证据。译文只作为 same-path loose override 交付；原归档不变，`archive_allow_repack=false`。Skyrim profile 的 BA2 为 inventory-only，Fallout 4 profile 才允许受控 materialization。

外部进程不是操作系统沙箱。wrapper 可以拒绝观察到的路径逃逸、链接和源文件变化，不能证明恶意 executable 没有写入任意系统位置。只有审查过且位于工作区或插件目录的 adapter 可以进入协议。

### GUI adapter

GUI、Computer Use、pywinauto/UI Automation 和 `gui:desktop` 锁属于 Codex。opencode 与 Claude Code 是非 GUI 顶层主控，遇到 GUI-only 任务必须 blocked 并 handoff 到 Codex。中性化 GUI 文案不能被解释为 Fallout 4 GUI 已认证。

## metadata 与 schema 传播

游戏上下文至少包含：

```text
game_id
game_profile_version
game_display_name
support_level
plugin_adapter
plugin_adapter_version
pex_category
pex_writeback_status
archive_delivery
archive_materialization_enabled
archive_allow_repack
```

这些 metadata 必须在 readiness、workflow state、workflow tasks、Codex/通用 handoff、`.workflow/workflow_state.json`、progress card JSON、strict QA summary、final_mod manifest、provenance 和 final binary review metadata 中保持一致。

传播原则：

- `workflow_state.json` 仍是状态权威，GameContext 只提供上下文和一致性门禁。
- schema 对新增字段给出明确类型；旧消费者需要兼容缺失字段时，只能使用规定的 Skyrim 回退。
- 下游证据声明不同 game/profile/adapter/PEX category 时标记 stale/mismatch。
- 不一致证据不能静默复用，也不能靠手工改 JSON 放行。
- manifest 和 provenance 同时验证游戏身份、直接来源、source hash 与 final hash。
- handoff 保持短摘要，不复制大型报告或 agent registry。

新增 metadata 时要同时检查生产者、schema、消费者、mismatch 逻辑、fixture 和旧格式兼容。只改 schema 或只改报告标题都不算完成。

## 状态机、任务与锁

`qa/workflow_state.json` 是权威状态。`qa/workflow_tasks.json` 从状态派生，只表达可执行任务、依赖和调度信息。`qa/codex_handoff.json` 与 `qa/agent_handoff.json` 是短接手摘要，不取代前两者。

脚本授权面来自 `config/workflow_policy.json` 的 entrypoint、stage、leaf 和 always-allowed 集合。`next_actions` 与兼容字段 `next_command` 不能指向未授权脚本。

锁分为两层：

| 锁 | 作用 |
|---|---|
| `work/.workflow.lock` | readiness、状态刷新、严格 QA、旧主流程等全局动作 |
| `mod:<ModName>` | 同一 Mod 的整体写入、final_mod 和严格 QA |
| `file:<ModName>:...` | 大型 Mod 的单文件 lane |
| `resource:<ModName>:...` | 独立资源 lane |
| `global:workflow-state` | 派生状态、任务、handoff 和进度卡 |
| `gui:desktop` | Codex 桌面操作 |

`resource_locks` 必须在领取前检查。`mod:` 锁与该 Mod 下所有 `file:`、`resource:` lane 冲突。GUI、全局状态刷新、strict QA、final_mod、共享 glossary/RAG 重建和同一资源写入保持串行。

### 主控与子智能体

主控读取状态、生成任务、划分 lane、限制并发、汇总结果，并在批次后串行刷新状态链。子智能体只能通过 `claim_workflow_task.py` 领取 `can_run_parallel=true` 且依赖、锁都满足的任务，只执行已领取的 `command`，完成后通过同一入口回写。

顶层 opencode/Claude Code 不是子智能体，不领取任务，也不直接编辑任务 JSON。它们和 Codex 使用同一状态机、Skills 和 QA 门禁；能力差异只影响 GUI handoff。

失败恢复由 `workflow-agent-orchestration` 在状态机授权范围内处理。AgentOps 可以辅助复核和归因，但不能替代 workflow policy、状态刷新或严格 QA。

## final_mod 与 QA

交付路径保持：

```text
out/<ModName>/汉化产出/final_mod/
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

`final_mod/` 使用当前 Game Profile 的 Data 根。默认以同路径同名文件直接替换；旁挂语言文件不能冒充完整交付。原始二进制只能原样复制，受控工具输出经过验证后才能覆盖。

严格 QA 至少检查：

- GameContext metadata 在报告和交付物中一致。
- provenance 覆盖所有 final_mod 文件，source/final hash 匹配。
- 文本结构、编码、占位符、覆盖率和模型校对为最新版本。
- 插件与 PEX 实际输出可反读，不变量和保护项通过。
- 归档 inventory、materialization 和 loose override 证据符合当前 profile。
- unsupported required input 保持 blocked。

状态刷新和 strict gate 通过只允许进入人工游戏测试。不能据此写成真实游戏、GUI 保存或翻译质量已经认证。

## fixture 与测试

测试分为 3 层：

1. `tests/` 覆盖通用 Python 合同和文档职责。
2. `scripts/test_*.py` 覆盖 Game Profile、路由、adapter、PEX、BA2、状态传播和端到端合成工作区。
3. `samples/effect_regression/` 保存确定性的仓库能力快照和后续 fixture 用例。

Fallout 4 集成 fixture 使用 `Classic Holstered Weapons - v1.09-46101-1-09-1779912557` 作为真实用例名，但内容是合成目录。它验证 marker 权威、F4SE/Materials/MCM/Strings 路由、DLL 保护、metadata 传播和 mismatch；不证明真实 Mod 二进制或游戏内效果。

新增能力至少提供：

- 正常路径与 fail-closed 路径。
- 旧 Skyrim marker 回归。
- 新 Skyrim 与 Fallout 4 profile 分支。
- 显式 game 冲突和跨游戏 artifact 注入。
- metadata/schema 传播与篡改测试。
- 路径越界、hash 漂移和 stale evidence 测试。
- 合成 fixture 与真实认证差距说明。

effect-regression 的 `repo-contract` 快照记录运行期 Skill 数和 workflow policy script refs 等稳定仓库事实。只在行为变化已审查、实际输出已确认时更新 expected；不能用刷新快照掩盖命令失败。

## CI 与验证

CI 是仓库级、无外部工具的确定性检查。它不读取真实游戏目录，不启动 GUI，不调用翻译 API，也不证明真实 Mod 加载或中文质量。

文档、Game Profile 或集成语义改动完成后，在仓库根目录统一运行：

```powershell
python -m pytest -q scripts/test_game_profile_regressions.py scripts/test_fallout4_routing_regressions.py
python -m pytest -q scripts/test_fallout4_plugin_adapter_regressions.py scripts/test_fallout4_pex_adapter_regressions.py
python -m pytest -q scripts/test_ba2_extractor_regressions.py
python -m pytest -q scripts/test_fallout4_workflow_integration.py
python -m pytest -q
python scripts/ci_validate_repo.py --strict
python scripts/test_workflow_health.py --repo-only --strict
python scripts/run_effect_regression.py --all --ci
python -m compileall -q scripts
git diff --check
```

`pytest` 的默认 `testpaths` 是 `tests/`，所以 `scripts/test_*.py` 必须按影响范围显式运行。Windows CI 将 Game Profile/routing 快组放在 smoke job，将 plugin/PEX 与 BA2 adapter 回归放在 adapter job，并把端到端 Fallout 4 合成工作区放在独立 workflow job；三组可并行，避免重复串行运行重型 fixture。CI 和 effect regression 保持 repo-only；需要真实工具或游戏的验证另行记录。

## 扩展新游戏

新增游戏不能只加一个 CLI 选项。按以下顺序建立合同：

1. 新增版本化 Game Profile，定义 Data 根、风险路径、编码、插件、PEX、归档、glossary 和 support level。
2. 让 GameContext loader、marker 校验和显式 game 冲突 fail closed。
3. 为插件、PEX 和归档选择或新增 adapter，定义输入/输出不变量与版本。
4. 更新 router 和文件类型 Skills，明确支持、experimental、blocked 和 protected 输入。
5. 将 metadata 传播到状态、handoff、progress、QA、manifest 和 provenance。
6. 增加旧 marker、新 profile、跨游戏污染、篡改和路径安全 fixture。
7. 更新 README 短矩阵、用户选择入口、高级边界和独立能力合同。
8. 经过真实工具链与游戏内验证后，再按单项能力提升认证级别。

不要全局重命名插件、marker、`SKYRIM_CHS_*` 环境变量或 `[SMT ...]` 进度前缀。这些是兼容合同，不代表运行时只能支持 Skyrim。

## 版本与发布维护

版本变化分开处理：

- Game Profile schema 或能力含义变化时，提升 profile 版本并增加迁移/兼容测试。
- adapter 输入、输出或验证合同变化时，提升 `plugin_adapter_version` 或协议版本。
- report schema 变化时，同步 schema、所有生产者/消费者和旧报告 stale 判定。
- 用户可见能力、插件安装内容或兼容性变化时，再按项目版本策略调整插件版本。

发布前确认 Codex 与 Claude manifest 版本一致，运行完整验证，检查 effect snapshot 的变化原因，并从 Git 跟踪文件生成源码包。源码包不能包含真实 Mod、工具缓存、工作区产物或本机配置。

Experimental 升级为稳定支持需要逐项证据：合法可复现的真实样本、固定工具版本、adapter 不变量、严格 QA、人工游戏内测试和失败记录。不要因合成 fixture 或单个成功样本扩大支持声明。

## 相关文档

- [AGENTS.md](./AGENTS.md)
- [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)
- [Effect Regression Workflow](./docs/effect_regression_workflow.md)
- [Agent Compatibility](./docs/agent_compatibility.md)
- [Tool Adapter](./docs/tool_adapter.md)
- [Skill Architecture](./docs/skill_architecture.md)
- [Codex Workflow](./docs/codex_workflow.md)
