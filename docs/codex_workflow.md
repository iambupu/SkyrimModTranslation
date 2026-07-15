# Codex 接手指南

本文件只给 Codex 接手项目时阅读。普通用户应先看根目录 `../README.md`；开发者扩展工作流时看 `../developer_guide.md`；opencode 和 Claude Code 使用 [Non-GUI Agent Workflow](./agent_workflow.md)。

这个仓库面向 Windows 下的 Bethesda Mod 简体中文汉化。Skyrim SE/AE 是稳定完整入口，Fallout 4 是 Experimental Support；实际能力只由工作区 marker 和当前 Game Profile 决定。Codex 是默认完整入口，优先读取 `qa/codex_handoff.json`，并且是唯一能处理 GUI、Computer Use、pywinauto/UI Automation 和 `gui:desktop` 锁的入口。接手时先判断当前目录是插件源仓库还是已初始化工作区：插件源仓库包含 `.codex-plugin/plugin.json`，用于维护插件；工作区包含 `.skyrim-chs-workspace.json`，用于处理具体 Mod 输入、QA 状态和输出。

如果用户要创建新工作区，只能从插件源仓库运行：

```powershell
python scripts/init_workspace.py <workspace> --game skyrim-se
python scripts/init_workspace.py <workspace> --game fallout4
```

用户没有说明游戏且目标还没有有效 marker 时，主控 agent 必须先用自然语言询问并等待确认，不得根据 Mod 名或目录名猜测。CLI 的交互选择只供人工直接运行脚本时使用。

目标必须是不存在的路径或插件仓库外部的空目录；不要在已有工作区、插件源仓库本身或插件仓库内部目录上重新初始化。初始化后的工作区不是插件源码副本，不包含 `.codex-plugin/`、`skills/`、`.codex/skills/`、`scripts/`、`adapters/` 或完整文档树；但会包含可编辑的 `glossary/` 种子目录，供用户维护 `mod_terms.md` 和新增词典。

后续文档中的 `python scripts/...` 是插件源仓库脚本的简写。若当前目录是初始化后的工作区，不要复制 `scripts/` 或 `adapters/` 到工作区；工作区不复制插件源码。应读取 `.skyrim-chs-workspace.json` 中记录的插件源路径，或直接执行 `qa/workflow_state.json`、`qa/codex_handoff.json` 输出的规范化绝对命令。

本文件不重复展开 final_mod、翻译规则或校对门禁细节，只说明 agent 读取状态、选择动作、记录恢复尝试和停止的方式。

## 与 Non-GUI Workflow 的关系

Codex Workflow 是 [Non-GUI Agent Workflow](./agent_workflow.md) 的完整增强版，不是另一套状态机。两者共享：

- `workflow_state.json`、`workflow_tasks.json` 和 `workflow_policy.json`；
- 非 GUI Python workflow；
- 主控派生子 agent、任务领取和完成回写协议；
- QA、锁、provenance、final_mod 和人工游戏测试边界。

Codex 只在入口和辅助能力上增加以下内容：

| 差异 | Non-GUI Agent | Codex |
|---|---|---|
| 首选 handoff | `qa/agent_handoff.json` | `qa/codex_handoff.json` |
| GUI | blocked，交回 Codex | 按路由进入受控 GUI 后备 |
| 桌面能力 | 无 | Computer Use，失败后才降级 pywinauto/UI Automation |
| 插件辅助 | 不作统一假设 | 可按场景使用已安装的 Codex 插件 |

除这些差异外，Codex 不得另建任务状态、跳过 QA、改变资源锁或绕过受控 adapter。

`workflow_state.json` 只声明动作所需的 agent capability，不探测桌面。`qa/codex_handoff.json` 可以确认 `gui:desktop`，但未知 capability 必须失败关闭；生成 opencode/Claude Code handoff 仍是显式操作，不加入 Codex 默认刷新链。

## 控制分层

- 主控 agent 负责编排：阅读状态、解释阻断、选择下一步、决定是否重试或停下。Codex 是默认主控入口，也是唯一 GUI 入口。
- 状态机负责边界和证据：记录当前阶段、最后成功阶段、允许动作、推荐动作、修复候选和停止条件。
- 脚本负责可复现动作：只执行插件提供的 Python 入口，在当前工作区生成可重跑、可审计的中间产物和报告。
- QA 负责是否允许推进：严格门禁、覆盖率、结构校验、模型审读和 final_mod 校验决定状态能否前进。

## Codex 默认接手顺序

1. 先读 `../AGENTS.md`，确认项目边界和禁止事项。
2. 如果用户只问进度，先读 `.workflow/progress_card.md`；必要时再读 `.workflow/workflow_state.json`，不要重新扫描全项目。
3. 再读 `qa/codex_handoff.md` 或 `qa/codex_handoff.json`，快速确认优先 Mod、当前阻断和下一条低风险动作。
4. 再读 `qa/workflow_state.md` 或 `qa/workflow_state.json`，确认每个 Mod 当前状态、最后成功阶段、阻断检查、`recommended_actions`、`repair_candidates`、`stop_conditions` 和下一条建议命令。
5. 再读 `qa/workflow_health.md` 或 `qa/workflow_health.json`，确认核心脚本、Skill、严格门禁和最终证据状态。
6. 再读 `qa/translation_readiness.md` 或 `qa/translation_readiness.json`，确认 `mod/` 输入、已知输出和项目级状态。
7. 如果 workflow state 给出推荐命令，优先执行推荐命令；不要手动拼接分步脚本。
8. 如果状态是 `qa_failed` 或 `blocked`，使用 `workflow-agent-orchestration`：先读阻断报告，再选择一个允许动作，执行前后写入 `qa/workflow_agent_runs.jsonl`。
9. 只有状态 blocked、证据缺失或用户明确要求局部处理时，才打开具体规则文档和对应 Skill。

`qa/workflow_state.json` 的 `allowed_scripts` 已合并 `workflow_policy.json` 中的常规状态刷新脚本、总控入口脚本、当前阶段脚本和 QA/adapter 分步脚本。主控 agent 可以灵活选择其中一个动作，但不能把未授权脚本当成推荐命令执行。

## 状态刷新入口

先读取现有状态。只有 `workflow_state`、`workflow_health` 或 `translation_readiness` 缺失、过期或互相矛盾时，才串行刷新状态、任务视图和最短摘要：

```powershell
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
python scripts/audit_project_completion.py
python scripts/new_manual_game_test_plan.py
python scripts/new_manual_game_test_results_template.py
python scripts/audit_translation_goal_compliance.py
```

普通接手、状态查询和报告刷新不得附加 `--run-strict-gate`。只有 `workflow_state.json` 推荐严格 QA，或用户明确要求运行严格 QA 时，才执行 `run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete` 或带 `--run-strict-gate` 的健康检查。

这些命令写入：

```text
qa/translation_readiness.json
qa/workflow_state.json
qa/workflow_tasks.json
qa/codex_handoff.json
.workflow/workflow_state.json
.workflow/progress_card.md
.workflow/progress_card.json
.workflow/progress_events.jsonl
qa/workflow_timeline.md
qa/blockers.md
```

`qa/codex_handoff.json` 只回答“现在卡在哪里、哪个 Mod 优先、下一条低风险安全动作是什么、必须先看哪些证据、执行后要刷新什么”。它不能取代 `qa/workflow_state.json`，也不会把 QA 标记为通过。

## 进度卡和 Trace

用户可见进度只来自 `.workflow/progress_card.md`，并只使用三类前缀：

```text
[SMT 进度]
[SMT 阻断]
[SMT 完成]
```

`scripts/write_workflow_state.py` 会从 `qa/workflow_state.json` 派生 `.workflow/workflow_state.json`、`.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`qa/workflow_timeline.md` 和 `qa/blockers.md`。Codex 不能把脚本 stdout、自然语言说明或 trace 明细当成阶段完成证据。

Codex 桌面版会折叠命令输出。每次运行总控、队列、严格门禁、状态刷新、健康检查或恢复动作后，Codex 必须再次读取 `.workflow/progress_card.md`，并把 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]` Markdown 卡片作为正文直接输出到对话中，让界面渲染成标题和表格；禁止放进三反引号代码围栏、代码块、引用块或其他会显示 Markdown 源码的容器。命令 stdout 里的进度卡不算已经对普通用户可见，摘要或自写状态也不能代替。未执行“读取 progress_card -> Markdown 正文输出”视为执行违规。

严格 QA 尚未运行或尚未通过时，进度卡不得显示 `qa_checked / ok`；应显示 `qa_pending_strict` 或明确写“严格 QA 待运行”。`ready_for_manual_test` 只说明项目内静态 QA 与包一致性证据已通过。下一步是检查 `final_mod` / `_CHS.zip`，再按 `qa/manual_game_test_plan.md` 做游戏内测试；这不表示 Codex 已完成真实游戏、MO2 或 Vortex 验证。

`traces/latest.jsonl` 和 `traces/trace_summary.md` 是开发者排查用本地 trace。只有用户明确要求排查失败原因时才摘要 trace；普通进度回答不展示 trace 细节。

进度卡和 trace 中的 `artifacts` 只能记录当前工作区内的相对路径。外部绝对路径或 `..` 逃逸路径不得作为进度、trace 或 QA 证据；脚本应丢弃这类路径，而不是把真实游戏、MO2/Vortex 或 AppData 位置写入报告。

## Agent 可以做

- 只在当前工作区内分析 `mod/` 沙盒、`work/` 工作副本、`source/`、`translated/`、`out/`、`qa/`、`.workflow/` 和 `traces/` 证据。
- 执行插件提供的 Python 主流程、工具适配器、QA 门禁和 final_mod 组装，输出写入当前工作区。
- 在插件维护任务中维护插件源仓库的文档、脚本、受控适配器源码、配置模板、默认术语种子和 `skills/`；在具体汉化工作区中只维护运行状态、输入输出、本机工具配置和工作区 `glossary/`。
- 通过受控 Tool Adapter / Computer Use 操作 LexTranslator 或 xTranslator，但输入、输出和日志必须全部位于当前工作区内。

## Codex 不能做

- 不能访问真实游戏目录、真实 MO2/Vortex 目录、Steam 游戏目录、AppData 或 Documents/My Games 配置目录。
- 不能直接修改 `.esp`、`.esm`、`.esl`、`.bsa`、`.ba2`、`.pex`、`.dll`、`.exe` 等二进制文件。
- 不能直接修改 `.psc` 源码并重新编译。
- 不能覆盖 `mod/` 下原始输入。
- 不能自动复制 `final_mod/` 或 `_CHS.zip` 到 MO2/Vortex。
- 不能把 GUI 只打开、只检查或人工临时保存伪装成自动化完成。

## blocked / qa_failed 恢复循环

`blocked` 和 `qa_failed` 是安全暂停，不是普通进度阶段。恢复时按这个顺序处理：

1. 读取 `qa/workflow_state.json`，确认状态、阻断项、允许脚本和停止条件。
2. 阅读阻断报告，例如 `qa/<ModName>.non_gui_qa_gates.md`、`qa/final_mod_validation.md`、`qa/<ModName>.final_review_quality.md` 或 `qa/<ModName>.model_review.md`。
3. 只选择一个 `allowed_scripts` 中的工作区安全 Python 动作。
4. 执行动作前后写入 `qa/workflow_agent_runs.jsonl`。
5. 执行后刷新 readiness、workflow state、进度卡、timeline、blockers、workflow tasks 和 codex handoff。
6. 如果停止条件命中，向用户说明原因，不继续重试。

安全续跑一个低风险任务：

```powershell
python scripts/resume_workflow.py --mod-name "<ModName>" --mode safe
```

批量任务视图：

```powershell
python scripts/write_workflow_tasks.py
```

`qa/workflow_tasks.json` 是从 `qa/workflow_state.json` 派生出来的调度视图。它会暴露：

| 字段 | 用途 |
|---|---|
| `mod_lanes` | 按 Mod 汇总的可分派 lane |
| `resource_lanes` | 大型 Mod 内按文件或资源拆出的可分派 lane |
| `counts.pending_executable` | 可自动执行的待办数 |
| `counts.pending_manual` | 需要人工或模型判断的待办数 |
| `parallel_policy` | 当前调度器使用的并发/串行规则 |

调度可并行的低风险任务：

```powershell
python scripts/run_workflow_tasks.py --max-workers 2
```

并发判断矩阵：

| 场景 | 是否可并发 | 条件 |
|---|---|---|
| 不同 Mod lane | 是 | `can_run_parallel=true`，依赖已完成，`resource_locks` 不冲突 |
| 同一大型 Mod 的不同文件 lane | 是 | 锁为 `file:<ModName>:<RelativePathOrHash>`，文件不同，依赖已完成 |
| 同一大型 Mod 的不同资源 lane | 是 | 锁为 `resource:<ModName>:<Name>`，资源不同，依赖已完成 |
| 同一文件或同一资源 lane | 否 | 必须串行，避免译表、报告或输出互相覆盖 |
| `mod:<ModName>` 任务 | 否 | 会和该 Mod 下所有 `file:` / `resource:` lane 冲突 |
| `global:workflow-state` 或 `gui:desktop` | 否 | 全局状态和 GUI 桌面自动化必须串行 |

大型单 Mod 可以继续拆成文件/资源 lane。不同文件/资源 lane 可以并发做解析、候选抽取、只读审计、译文分片生成和模型校对分片。GUI 工具操作、全局状态刷新、共享索引重建、旧总控入口、final_mod 组装、严格 QA、同一文件/资源 lane 内多个写入任务和 Mod 级写入任务仍然必须串行。

效率预期只对并行段成立：如果有 `P` 个独立 lane 且配置 `--max-workers N`，理想吞吐上限接近 `min(P, N)`，但模型调用排队、文件 IO、任务领取/完成回写、主控汇总和后续串行 QA 会降低端到端收益。大型文本 Mod 的文件级解析、翻译分片和校对分片通常收益最高；GUI、final_mod 和严格 QA 通常不提速。

多子智能体并发编排使用 `workflow-subagent-orchestration`。主控智能体先刷新状态并生成任务视图，读取 `qa/workflow_tasks.json` 中的 `mod_lanes` 和 `resource_lanes`，然后按 Mod 或按大型 Mod 内文件/资源 lane，把 `dependencies=[]` 或依赖已完成、资源锁不冲突的独立 lane 分给多个子智能体。子智能体不得直接编辑 `qa/workflow_tasks.json`，必须通过领取协议抢占任务。

领取一个 Mod lane 内的下一个可并行任务：

```powershell
python scripts/claim_workflow_task.py --mod-name <ModName> --owner <AgentId> --parallel-only
```

领取大型 Mod 内一个文件/资源 lane 的下一个可并行任务：

```powershell
python scripts/claim_workflow_task.py --mod-name <ModName> --resource-lock <ResourceLock> --owner <AgentId> --parallel-only
```

领取成功后只执行返回 JSON 中的 `command`。执行结束后回写任务状态；如果该 Mod 或资源 lane 仍有待处理任务，同一个子智能体可以继续用相同 `--mod-name`、`--resource-lock` 和 `--owner` 领取下一条任务：

```powershell
python scripts/claim_workflow_task.py --task-id <TaskId> --owner <AgentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
python scripts/claim_workflow_task.py --task-id <TaskId> --owner <AgentId> --complete --complete-status failed --exit-code 1 --output-tail "<short error>"
```

如果子智能体只做只读审计，也必须把结论写回项目内 QA 报告或人工摘要，并由主控智能体统一刷新 `translation_readiness`、`workflow_state`、`workflow_tasks`、`codex_handoff` 和进度卡。并发批次完成后不要让每个子智能体各自运行全局刷新；由主控智能体串行刷新一次，避免进度卡、状态和 blockers 互相覆盖。

Codex 是顶层主控，不是子任务执行器。需要并发时，由 Codex 派生子 agent，再让子 agent 使用上面的 `claim_workflow_task.py` 协议领取和回写任务。Codex 自己负责汇总结果、串行刷新全局状态，并在路由要求时进入受控 GUI 后备。

其他顶层入口的非 GUI 接手与 GUI handoff 规则统一见 `docs/agent_workflow.md`，不在本文件重复维护。

## Codex 原生能力与插件辅助

Computer Use 是 GUI fallback 的首选桌面操作能力。只有 decoder/CLI 不可用、导出格式不支持或必须由 GUI 工具写回工作区内副本时，才进入 LexTranslator/xTranslator GUI；进入 GUI 前必须确认目标窗口和控件，输出必须保存到工作区 `tool_outputs`。

Browser / Chrome 可用于查看工具主页、官方文档、下载页和排查资料。下载或执行外部工具前仍必须遵守 `config/tools.local.json`、项目路径边界和工具 adapter 规则。

Codex 插件只提供辅助能力，项目 `skills/`、状态机、Python 主入口和 QA 门禁始终是权威流程。不要在每次翻译启动时枚举插件，也不要因为某个可选插件缺失而阻断基础 workflow。

| 能力层 | 适用场景 | 边界 |
|---|---|---|
| Computer Use | LexTranslator/xTranslator 桌面后备 | 必须由路由授权，输出只能进入工作区 |
| Browser / Chrome | 官方文档、工具主页、下载和兼容性排查 | 不得绕过工具配置、hash 和路径检查 |
| AgentOps | blocked 恢复、复核、并行审计、失败复盘 | 不直接翻译，不替代状态机或 QA |
| Data Analytics | QA、队列、覆盖率和 blocker 可视化 | 只作展示和解释，不负责放行 |
| Superpowers / Aegis | 修改插件源码时的计划、调试、测试和复核 | 只用于工程维护，不进入 Mod 翻译运行时 |
| Codex Reviewer / Brooks Lint | 插件脚本和 Skill 的静态审查 | 结论必须由项目测试和严格校验验证 |

AgentOps 可作为恢复、复核和并行审计辅助，适合 `qa_failed`、`blocked`、多次重试失败、严格 QA 前复核、发布前复核或批量队列诊断。它不能替代 `skills/`、状态机、Python 主入口或 QA 门禁。

| 场景 | 建议 AgentOps 能力 | 必须遵守 |
|---|---|---|
| `qa_failed` / `blocked` 恢复循环 | `agentops:recover`、`agentops:validate`、`agentops:reality-check` | 先读 `workflow_state.json`，只选择授权动作 |
| 严格 QA 或发布前复核 | `agentops:review`、`agentops:validate`、`agentops:standards` | 不能替代 `run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete` |
| 多报告、多 manifest 并行审计 | `agentops:swarm`、`agentops:converge`、`agentops:review` | 结论必须回写项目 QA 报告或人工摘要 |
| 自动化脚本或流程设计改动 | `agentops:pre-mortem`、`agentops:review`、`agentops:test` | 不扩大到无关重构，不改变二进制边界 |
| 失败复盘和后续接手 | `agentops:post-mortem`、`agentops:handoff` | 以 `workflow_health` 和 `workflow_state` 为接手入口 |

如果 AgentOps 不可用，继续使用插件内 `workflow-agent-orchestration` Skill 和 Python 入口完成同等边界内的恢复或复核，不得把插件不可用视为流程完成。

Data Analytics 可用于展示 QA 分布、队列状态、覆盖率、blocked 原因和发布前状态。它只能作为报告和可视化层，不能替代 QA 脚本、状态机判定或人工游戏内测试。

| 场景 | 建议 Data Analytics 能力 | 必须遵守 |
|---|---|---|
| 批量 Mod 队列状态展示 | `data-analytics:build-dashboard`、`data-analytics:visualize-data` | 只展示项目内队列和 QA 状态 |
| QA 失败或 blocked 原因分类 | `data-analytics:build-report`、`data-analytics:metric-diagnostics` | 不把图表结论当成 QA 放行 |
| 覆盖率、provenance、archive loose override 汇总 | `data-analytics:visualize-data`、`data-analytics:kpi-reporting` | 指标口径必须来自项目 QA 脚本输出 |
| 发布前状态说明 | `data-analytics:build-report`、`data-analytics:design-kpis` | 必须同时引用严格 QA 和 workflow 状态 |

使用这些能力后，仍必须刷新：

```text
qa/translation_readiness.json
qa/workflow_state.json
.workflow/workflow_state.json
.workflow/progress_card.md
.workflow/progress_card.json
.workflow/progress_events.jsonl
qa/workflow_timeline.md
qa/blockers.md
qa/workflow_tasks.json
qa/codex_handoff.json
```

恢复尝试还要写入：

```text
qa/workflow_agent_runs.jsonl
```

## 常规入口

```powershell
python .\scripts\audit_translation_readiness.py
python .\scripts\write_workflow_state.py
python .\scripts\run_translation_queue.py --mode prepare --limit 1
python .\scripts\run_non_gui_translation_workflow.py --mod-name <ModName> --source-path ".\mod\<ModArchive>.zip" --force-prepare
python .\scripts\test_workflow_health.py --mod-name <ModName> --run-strict-gate
```

同一项目不要并行运行总控、严格门禁、状态刷新和健康检查入口；这些入口会使用项目内 workflow lock，避免报告和 final_mod 校验互相覆盖。

## 详细规则索引

| 主题 | 权威文档 |
|---|---|
| 翻译风格、禁翻项、占位符、Papyrus 可见文本 | `docs/translation_rules.md` |
| 当前游戏 Markdown/TXT/SST/EET 词典、RAG 索引和刷新规则 | `docs/lextranslator_dictionary_rag.md` |
| decoder-first、非 GUI 主流程、ESP/PEX 工具优先级 | `docs/decoder_first_workflow.md` |
| BSA 只读审计、安全解包、归档 manifest 和 loose override 交付边界 | `skills/bsa-archive-audit/SKILL.md` |
| LexTranslator GUI fallback | `docs/lextranslator_workflow.md` |
| xTranslator GUI fallback | `docs/xtranslator_workflow.md` |
| GUI / Computer Use 操作边界 | `docs/gui_automation_rules.md` |
| Tool Adapter 和本地工具配置 | `docs/tool_adapter.md` |
| PEX 可见字符串写回 | `docs/pex_visible_strings_writeback.md` |
| Skill 路由、职责边界、防重复探索 | `docs/skill_architecture.md` |
| 状态机、允许动作和下一步命令 | `config/workflow_policy.json` / `qa/workflow_state.json` |
| 正常主控/子智能体并发 | `skills/workflow-subagent-orchestration/SKILL.md` / `qa/workflow_tasks.json` |
| 失败恢复尝试和重试日志 | `skills/workflow-agent-orchestration/SKILL.md` / `qa/workflow_agent_runs.jsonl` |
| final_mod、intermediate、`_CHS.zip` 输出结构 | `docs/final_mod_output.md` |
| 模型校对、严格门禁、目标合规和玩家实机边界 | `docs/translation_proofreading_workflow.md` |

## 完成边界

项目内静态校对完成不等于玩家实机验证完成。真实游戏测试由玩家操作；玩家尚未提供真实游戏测试结果和证据时，应在目标合规报告中标记为校对工作流范围外，而不是当作项目内校对阻断。
