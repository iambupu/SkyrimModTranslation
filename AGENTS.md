# Skyrim Mod CHS Translation 插件工程规则

## 1. 项目目标

- 本项目是 Windows 环境下的 Bethesda Mod 简体中文汉化 agent workflow 工程；Skyrim SE/AE 是默认完整入口，Fallout 4 仅提供 `Fallout 4 Experimental Support`。
- 插件名为 `skyrim-mod-chs-translation`。
- 插件源仓库提供规则、Skills、Python 脚本、受控适配器源码、配置模板、文档和 QA 门禁；具体 Mod 汉化任务应在初始化后的工作区中运行。
- Agent 是文本工程助手，不是插件编辑器。
- 项目配合 LexTranslator 和 xTranslator 使用。
- 项目目标是建立可维护、可回滚、可批量处理的汉化流程。
- 游戏身份由工作区 `.skyrim-chs-workspace.json` marker 和当前 Game Profile 决定。旧 marker 缺少 `game_id` 时按 `skyrim-se` 兼容；不得根据 Mod 名、目录名或文件名猜游戏。

## 2. 工作边界

- 具体 Mod 汉化任务中，agent 只能处理当前工作区目录。
- 具体 Mod 汉化任务中，agent 只能读取当前工作区目录下的 `mod/` 作为 Mod 输入。
- Agent 不能访问真实游戏目录、真实 MO2/Vortex 目录。
- Agent 不能直接修改 `.esp`、`.esm`、`.esl`、`.bsa`、`.ba2`、`.pex` 等文件。
- Agent 只能编辑文本类文件、插件脚本、插件文档、配置模板和工作区 QA/manifest 报告。
- Agent 可以在工作区内复制二进制文件，但只能原样复制，不能编辑、重写、反编译后回写或重新编译。
- Agent 不能绕过受控工具直接写入、保存或修改插件二进制文件。
- Agent 可以通过 Tool Adapter 调用受控工具，把工具生成的插件输出保存到工作区 `tool_outputs`；Computer Use 操作 LexTranslator 或 xTranslator 仅限 Codex adapter。
- 对需要解码的文件，优先使用工作区内配置的 CLI/库解码器生成文本中间文件；GUI/Computer Use 只作为解码器不可用或写回工具缺失时的兜底。

## 3. 运行环境与脚本入口

- 命令执行环境为 Windows；项目流程不引入任何 shell 作为脚本层，统一由 Python 入口承载。
- 工程主流程、工具包装、QA 门禁和可复用入口统一使用 Python 脚本。
- `uv` 可作为可选易用性增强，用于 `uv run` 启动插件源 Python 脚本，以及在 `--tool-setup auto` 中优先创建/安装工作区 `tools/python-venv/`；不得把 uv 变成硬依赖，缺失或失败时必须回退到标准 `python`、`venv` 和 `pip`。
- 禁止 Bash/WSL/Linux 命令。
- 禁止使用 `sed`、`awk`、`grep`、`rm`、`cp`、`mv`、`cat`、`touch`、`mkdir -p` 等 Unix 风格命令。
- 准备、扫描、路由、总控、QA 门禁和 final_mod 组装这类工程主流程优先且默认使用 Python；新增流程不得再引入 shell 包装层。

## Active Tool Usage

Codex 应主动判断是否需要使用 LexTranslator 或 xTranslator。

Codex 也应主动判断是否需要显式使用 AgentOps 插件能力，但 AgentOps 只能作为编排、复核和恢复辅助，不能替代本项目 `skills/`、`workflow_state.json` 状态机、Python 主入口或 QA 门禁。

Codex 可在需要向用户展示复杂 QA/队列/覆盖率状态时显式使用 Data Analytics 能力，但 Data Analytics 只能用于报告、表格、图表或仪表盘展示，不能替代 QA 脚本、状态机判定或人工游戏内测试。

使用原则：

- ESP/ESM/ESL：优先 CLI/库解码器导出/导入工作区内文本中间文件；没有可用解码器时再用 LexTranslator/xTranslator。
- MCM：优先 agent 结构化文本管线；必要时再用 LexTranslator。
- PEX：优先 `PexStringToolPath`/Mutagen PEX 适配器提取可见字符串和写回项目内 PEX 副本；LexTranslator/xTranslator PapyrusPex 只作为后备。
- Interface/translations：优先 agent 文本管线。
- JSON/XML/CSV/TXT：优先 agent 文本管线。
- BSA/BA2：先做工作区内只读 inventory。BSA materialization 由 `bsa-archive-audit` 通过受控 `DecoderTools.BsaFileExtractorPath` wrapper 执行；BA2 materialization 必须路由到 `ba2-archive-audit`，通过 BA2 protocol、receipt/manifest/hash 独立验证后生成同路径 loose override。两类归档都不直接修改；BA2 不重打包。
- 7Z：优先 Python `py7zr`；没有 `py7zr` 时才使用 `DecoderTools.Archive7zPath`；两者都不可用时写阻断报告。

主动使用工具的方式：

1. 读取 `config/tools.local.json`。
2. 先运行或参考 `python scripts/detect_decoder_tools.py`，检查 CLI/库解码器是否可用。
3. 检查输入路径是否位于当前工作区目录。
4. 如果 decoder/CLI/Python 解码器路径可用，先用非 GUI 路径生成 `source/`、`work/`、`translated/` 下的文本中间文件和 QA 报告。
5. 只有 decoder/CLI 不可用、导出格式不支持或必须由 GUI 工具写回工作区内副本时，才进入 LexTranslator/xTranslator。
6. 进入 GUI 工具时，Windows 桌面操作优先使用 Computer Use；只有 Computer Use 在当前会话不可用、无法识别目标窗口或操作失败时，才降级到插件提供的 pywinauto/UI Automation 适配器。
7. Computer Use 可以基于当前窗口截图使用窗口相对坐标，但必须先截图确认目标控件；pywinauto/UI Automation 降级方案不得默认使用固定屏幕坐标。
8. 记录 decoder/GUI 工具调用日志。
9. 如果 decoder/CLI、Computer Use 和降级 GUI 自动化都无法自动完成，标记该工具步骤为 blocked，并说明需要补充 CLI/自动化适配器；不得把人工操作伪装成已完成的自动化。

AgentOps 使用原则：

- 当任务进入 `qa_failed`、`blocked`、多次重试失败、严格 QA 前复核、发布前复核、跨多个 Mod 的批量队列诊断，或需要并行审计多个报告/manifest 时，Codex 应在行动前显式说明将使用 AgentOps。
- AgentOps 可用于任务拆分、并行检查、失败归因、恢复建议、复核清单和尝试记录；不得用于直接翻译、直接修改二进制、绕过路由 Skill、绕过 QA 门禁或覆盖 `workflow_policy.json` 授权面。
- 使用 AgentOps 后，仍必须按项目规则刷新 `qa/translation_readiness.json`、`qa/workflow_state.json`、`.workflow/workflow_state.json`、`.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`qa/workflow_timeline.md`、`qa/blockers.md`、`qa/workflow_tasks.json` 和 `qa/codex_handoff.json`，并在 `qa/workflow_agent_runs.jsonl` 记录恢复尝试。
- 如果 AgentOps 不可用，Codex 应继续使用项目内 `workflow-agent-orchestration` Skill 和 Python 入口完成同等边界内的恢复或复核，不得把插件不可用视为流程完成。

AgentOps 触发建议：

| 场景 | 建议 AgentOps 能力 | 必须遵守 |
|---|---|---|
| `qa_failed`/`blocked` 恢复循环 | `agentops:recover`、`agentops:validation`、`agentops:trace` | 先读 `workflow_state.json`，只选择授权动作 |
| 严格 QA 或发布前复核 | `agentops:review`、`agentops:validation`、`agentops:standards` | 不能替代 `run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete` |
| 多报告、多 manifest 并行审计 | `agentops:swarm`、`agentops:harvest`、`agentops:trace` | 结论必须回写项目 QA 报告或人工摘要 |
| 自动化脚本或流程设计改动 | `agentops:pre-mortem`、`agentops:review`、`agentops:test` | 不扩大到无关重构，不改变二进制边界 |
| 失败复盘和后续接手 | `agentops:post-mortem`、`agentops:handoff` | 以 `workflow_health` 和 `workflow_state` 为接手入口 |

扩展到其他主控 adapter 和子智能体编排的难易程度：

- 总体难度：中等偏高。项目已有 `workflow_state.json`、`workflow_tasks.json`、`codex_handoff.json`、进度卡、锁文件和 QA 报告，适合把只读审计、报告汇总、失败归因、候选任务拆分和低风险恢复建议扩展到非 GUI 顶层 adapter 与主控派生的子智能体编排。
- 低难度扩展：只读读取 `qa/`、`.workflow/`、manifest、coverage、provenance 和 trace 摘要，生成复核清单、blocked 原因分类、队列状态摘要或后续接手建议。
- 中等难度扩展：让 opencode 和 Claude Code 作为完整非 GUI 顶层 adapter 使用同一套 workflow core、Skills、状态文件和 QA 门禁；`qa/workflow_tasks.json` 中的可并行子任务由主控分派的子智能体领取，不由顶层 adapter 直接领取。
- 高难度扩展：GUI 自动化、ESP/PEX 写回、BSA 解包、final_mod 组装、严格 QA 前恢复和发布前复核。这些任务必须保留项目状态机、受控适配器、锁和 QA 门禁，不得由任何顶层 adapter 或子智能体自行推断放行。
- 禁止扩展：直接修改插件二进制、直接访问真实游戏目录或 MO2/Vortex 目录、绕过 `workflow_policy.json` 授权面、绕过 `translation-task-router`、跳过严格 QA、把人工操作伪装成自动完成。
- 接入前提：opencode 和 Claude Code 必须先读当前工作区的 `qa/agent_handoff.json`（缺失时读 `qa/codex_handoff.json`）、`qa/workflow_state.json`、`qa/workflow_tasks.json`，以及插件源 `config/workflow_policy.json`，只处理当前工作区内文件，使用项目锁机制，并在恢复、重试或 blocked handoff 时把尝试记录写入 `qa/workflow_agent_runs.jsonl`。
- opencode 和 Claude Code 是非 GUI 顶层 adapter，不是子任务执行器。子任务领取只属于主控分派的子智能体，入口是 `python scripts/claim_workflow_task.py --owner <SubagentId> --parallel-only` 及其 `--complete` 回写协议。不得让顶层 adapter 直接编辑 `qa/workflow_tasks.json`、绕过状态机选择下一步、执行全局状态/final_mod/严格 QA 任务，或修改任何二进制文件。
- Claude Code 支持 `.claude-plugin/marketplace.json` 和 `/plugin marketplace add`，但该 marketplace 只暴露非 GUI Skills；不得把 Claude marketplace 安装解释为 Claude 获得 Codex GUI、Computer Use 或 Codex 插件调用能力。
- GUI、Computer Use、pywinauto/UI Automation、LexTranslator/xTranslator 桌面操作和 `gui:desktop` 锁是 Codex 专属能力，不适配给 opencode 或 Claude Code。非 Codex adapter 遇到 GUI-only 任务必须 blocked，并记录 `handoff_target=codex`。
- 新增 opencode/Claude Code 支持不得损害现有 Codex 插件性能：不得把 adapter capability 探测、大上下文导出、agent skill registry 或 `write_agent_handoff.py` 挂到 Codex 默认翻译热路径；这些只能在显式命令或 CI 中运行。

多子智能体并发编排规则：

- 当 `qa/workflow_tasks.json` 中存在多个 Mod lane，且每个 lane 内有 `status=pending`、`executable=true`、`can_run_parallel=true`、`dependencies=[]` 或依赖已完成、且 `resource_locks` 不冲突的任务时，当前主控 agent 应优先按 Mod 拆分给多个子智能体并发处理，而不是串行等待。
- 对大型单 Mod，如果文本量很大且任务已拆成 `file:<ModName>:<RelativePathOrHash>` 或 `resource:<ModName>:<Name>` 资源 lane，则可在同一 Mod 内把不同文件/资源 lane 分给不同子智能体并发解析、候选抽取、只读审计、译文分片生成和模型校对分片；同一文件/资源 lane 内仍串行。
- 主控智能体负责刷新 `qa/workflow_state.json`、生成 `qa/workflow_tasks.json`、读取 `mod_lanes` 和 `resource_lanes`、按 Mod 或资源 lane 分配子智能体、限制并发数、汇总结果、刷新进度卡和决定是否进入严格 QA；子智能体可以绑定一个 Mod lane，串行处理该 Mod 的已领取任务，绑定一个大型 Mod 内的文件/资源 lane，或处理一个单独只读审计范围。
- 子智能体领取 Mod lane 任务必须通过 `python scripts/claim_workflow_task.py --mod-name <ModName> --owner <AgentId> --parallel-only`；领取大型 Mod 内资源 lane 任务必须加上 `--resource-lock <ResourceLock>`；也可以交给 `run_workflow_tasks.py` 的资源锁调度。不得手动编辑 `qa/workflow_tasks.json` 抢任务。
- 子智能体只能执行领取到的 `command`，且必须确认命令仍位于插件源 `scripts/` 下、输出仍位于当前工作区内、资源锁未冲突、依赖未失效。
- 子智能体完成后必须用 `claim_workflow_task.py --complete --task-id <TaskId> --owner <AgentId> --complete-status done|failed|blocked --exit-code <N>` 回写任务状态，并在需要时记录 `qa/workflow_agent_runs.jsonl`。
- 并发批次结束后必须由主控智能体串行运行状态刷新链：`audit_translation_readiness.py` -> `write_workflow_state.py` -> `write_workflow_tasks.py` -> `write_codex_handoff.py`，然后重新读取 `.workflow/progress_card.md` 输出用户可见进度；只有在显式准备 opencode/Claude Code 顶层 adapter 接手时，才额外运行 `write_agent_handoff.py`。
- 不能并发的工作包括 GUI 自动化、全局状态刷新、严格 QA、final_mod 组装、共享 glossary/RAG 索引重建、旧总控入口、同一 Mod lane 内多个 Mod 级写入任务、同一文件/资源 lane 内多个写入任务，以及任何 `can_run_parallel=false` 或含 `global:workflow-state` / `gui:desktop` 锁的任务；`mod:<ModName>` 锁会和该 Mod 下所有 `file:` / `resource:` lane 冲突，用于阻止 Mod 级写入和文件级任务并行。

Data Analytics 使用原则：

- 当用户需要查看批量 Mod 队列、QA 通过/失败分布、覆盖率趋势、归档 loose override 缺口、provenance 覆盖情况、blocked 原因分类或发布前状态汇总时，Codex 可在行动前显式说明将使用 Data Analytics。
- Data Analytics 可读取项目内 `qa/*.json`、manifest、coverage、workflow 状态和经过脱敏/裁剪的汇总数据，生成表格、图表、报告或 dashboard；不得读取真实游戏目录、真实 MO2/Vortex 目录或项目外隐私数据。
- Data Analytics 的输出只作为可视化和解释层；最终是否可推进仍以 `workflow_state.json`、`translation_readiness.json`、严格 QA 门禁和人工测试结论为准。
- 如果 Data Analytics 不可用，Codex 应退回 Markdown 表格、项目内 QA 报告和简短人工摘要，不得把可视化不可用视为流程阻断。

Data Analytics 触发建议：

| 场景 | 建议 Data Analytics 能力 | 必须遵守 |
|---|---|---|
| 批量 Mod 队列状态展示 | `data-analytics:build-dashboard`、`data-analytics:visualize-data` | 只展示项目内队列和 QA 状态 |
| QA 失败/blocked 原因分类 | `data-analytics:build-report`、`data-analytics:metric-diagnostics` | 不把图表结论当成 QA 放行 |
| 覆盖率、provenance、archive loose override 汇总 | `data-analytics:visualize-data`、`data-analytics:kpi-reporting` | 指标口径必须来自项目 QA 脚本输出 |
| 发布前状态说明 | `data-analytics:build-report`、`data-analytics:design-kpis` | 必须同时引用严格 QA 和 workflow 状态 |

## 进度反馈与 Trace 规则

本项目使用“进度卡 + 本地 Trace”机制，不接入 OTel 或外部观测平台。

Codex 必须区分三类输出：

1. 普通工作说明：说明即将执行或正在执行的动作，不代表阶段完成。
2. 用户进度反馈：必须来自 `.workflow/progress_card.md` 或 `.workflow/progress_card.json`，且只使用 `[SMT 进度]`、`[SMT 阻断]`、`[SMT 完成]` 三类前缀。
3. 工程追踪信息：写入 `traces/latest.jsonl`、`traces/trace_summary.md` 和 `qa/` 摘要，默认不直接刷到对话中。

只有当 `qa/workflow_state.json` 已刷新，且 `.workflow/progress_card.md` 已生成后，Codex 才能汇报阶段进展。用户问“现在进度到哪了”时，先读 `.workflow/progress_card.md`，必要时再读 `.workflow/workflow_state.json`；不要重新扫描全项目，也不要把脚本 stdout 当作进度事实。

在 Codex 桌面版中，命令输出可能会被折叠。每次运行总控、队列、严格门禁、状态刷新、健康检查或自动恢复后，只要 `.workflow/progress_card.md` 存在，Codex 必须再次读取该文件，并把其中的 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]` Markdown 卡片作为正文直接输出到对话中，让界面渲染成标题和表格；禁止放进三反引号代码围栏、代码块、引用块或其他会显示 Markdown 源码的容器。不得只依赖命令 stdout 中的进度卡，也不得用摘要、自写状态或 trace 代替。未执行“重新读取 progress_card -> Markdown 正文用户可见输出”的回合视为执行违规。

禁止：

- 把“准备执行”说成“已经完成”。
- 把 trace 细节当作用户进度输出。
- 在未通过严格 QA 时输出 `[SMT 完成]`。
- 在没有更新状态文件时汇报阶段完成。
- 把 `blocked`、`qa_failed` 或 `needs_input` 模糊描述成“基本完成”。

进度与追踪文件：

```text
.workflow/workflow_state.json
.workflow/progress_card.md
.workflow/progress_card.json
.workflow/progress_events.jsonl
traces/latest.jsonl
traces/trace_summary.md
qa/workflow_timeline.md
qa/blockers.md
```

## Workspace 初始化

- 插件仓库提供可复用规则、Skills、脚本、文档和配置模板；每个工作区保存 `mod/`、`work/`、`qa/`、`out/`、`source/`、`translated/`、`glossary/`、`.workflow/`、`traces/`、`.skyrim-chs-workspace.json` 和本机工具配置。
- 工作区不得作为插件源码副本；初始化不得复制 `.codex-plugin/`、`skills/`、`.codex/skills/`、`scripts/`、`adapters/` 或完整文档树。
- 初始化可以把插件源仓库的 `glossary/` 复制为工作区可编辑种子。`glossary/mod_terms.md` 和用户新增词典属于工作区状态，应随工作区走。
- 新工作区初始化由 Skill 指引并由 `python scripts/init_workspace.py <workspace>` 执行；`scripts/init_project.py` 只是兼容包装入口。
- 初始化目标必须是不存在的路径或插件仓库外部的空目录。
- 初始化脚本必须拒绝插件仓库本身、插件仓库内部目录、已有文件和非空目录；`--force` 不得绕过非空目录限制。
- `.codex-plugin/` 只属于插件源仓库，不复制进初始化后的工作区。
- 初始化后的工作区不包含 `scripts/` 或 `adapters/`；流程命令应运行插件源仓库中的 Python 脚本和受控适配器，并让脚本通过 `.skyrim-chs-workspace.json` 或环境变量把输出写回工作区。

## Skills

翻译能力必须按根目录 `skills/` 下的插件运行 Skill 拆分执行。
`skills/` 是本项目与 Codex 插件共同使用的唯一权威运行 Skill 目录；不得在 `.codex/skills/` 维护第二套运行 Skill 镜像。
`.codex/skills/` 只允许保存仓库维护用 meta Skill，例如插件安装、使用指南和维护流程；这些 meta Skill 不参与 Mod 文件路由、翻译、QA 或 final_mod 组装。

这些 Skill 主要给 Codex 检索和执行使用。新增或修改 Skill 时，优先优化 `SKILL.md` 的 `description`，因为 Codex 选择 Skill 前主要依赖 `name`、`description` 和路径；正文只在 Skill 被选中后才加载。

处理任何文件前，先使用 `translation-task-router` 判断：

- 文件类型
- 风险等级
- 推荐工具
- 推荐输出目录
- 是否允许 agent 直接处理
- 是否必须人工处理

职责边界：

- 主控 agent 负责准确和灵活的编排。
- 状态机负责边界和证据。
- 脚本负责可复现动作。
- QA 负责判断是否允许推进。
- `workflow_policy.json` 的授权面由 `always_allowed_scripts`、`allowed_entrypoint_scripts`、阶段 `allowed_scripts` 和 `allowed_leaf_scripts` 共同组成；`workflow_state.json` 的 `next_command` 不得指向未授权脚本。
- 并行任务调度由 `qa/workflow_tasks.json` 表示；它从 `workflow_state.json` 派生任务，不取代 `workflow_state.json` 的权威状态。
- Agent 接手优先读取 `qa/agent_handoff.json`；Codex 兼容入口可继续读取 `qa/codex_handoff.json`。handoff 文件只做短摘要，不取代 `workflow_state.json`、`workflow_tasks.json` 或 QA 报告。
- `workflow_state.json` 应提供结构化 `next_actions`；旧的 `next_command` 只作为兼容显示和兜底。
- 单次安全恢复入口为 `python scripts/resume_workflow.py --mod-name <ModName> --mode safe`；它只能执行低风险、已授权、工作区内 Python 任务，并必须记录尝试后刷新 readiness/state/tasks/handoff。
- 调度入口为 `python scripts/run_workflow_tasks.py --max-workers <N>`；任务生成入口为 `python scripts/write_workflow_tasks.py`，单任务领取入口为 `python scripts/claim_workflow_task.py`。
- 锁分为两层：Mod/资源级锁位于 `work/locks/*.lock`，用于防止同一 Mod 级任务或同一文件/资源 lane 并行写入；全局工作流锁仍为 `work/.workflow.lock`，用于串行化全局 readiness/state/health 刷新和旧总控入口。
- 可并行任务仅限不同 Mod lane，或同一大型 Mod 内不同 `file:<ModName>:...` / `resource:<ModName>:...` lane，且必须是资源锁不冲突、依赖已完成、`can_run_parallel=true` 的工作区内 Python 任务；GUI 自动化、全局状态刷新、共享 glossary/RAG 索引重建、旧总控入口、`mod:<ModName>` 级任务和同一文件/资源 lane 多任务必须串行。
- `skyrim-mod-chs-translation` 只作为对外入口和总说明，负责用户自然语言请求识别、workspace/tool setup 与显式游戏意图判断、状态/进度问题和下游 Skill 选择提示。
- `skyrim-mod-translation-orchestrator` 只作为内部运行期编排策略，负责已识别端到端汉化任务后的状态机推进、脚本顺序和下游 Skill 串联；不得作为第二个自然语言总入口。
- Agent 编排 Skill 只负责 Codex 在 `qa_failed`/`blocked` 时的恢复循环、允许动作选择、尝试日志和安全停止。
- 子智能体编排 Skill 只负责正常流程中由主控分配的并发 lane、领取/完成协议、结果汇总和串行刷新边界；顶层 adapter 不得把自己当作子任务执行器领取任务。
- AgentOps 插件只作为 Agent 编排 Skill 的外部辅助；启用时必须显式说明用途和边界，且不能越过本项目 Skill、状态机和 QA 证据。
- Data Analytics 只作为 QA/队列/覆盖率状态的展示和分析辅助；启用时必须显式说明数据来源和口径，且不能替代 QA 判定。
- 路由 Skill 只负责工具优先级和下游 Skill。
- GUI Skill 只负责 LexTranslator/xTranslator 工具操作。
- 文件类型 Skill 只负责可翻译范围和保护规则。
- BSA Skill 负责 BSA materialization、BSA/BA2 通用只读 inventory、manifest 证据和 loose override 路由建议；BA2 materialization 只属于 `ba2-archive-audit`。两者都不翻译、不直接修改归档，BA2 不重打包。
- Final Skill 只负责组装完整 Mod 目录。

Agent 查找索引：

| 任务或文件 | 首选 Skill | 不要误用 |
|---|---|---|
| 用户自然语言入口、总览、请求识别、初始化/状态/进度/测试问题 | `skyrim-mod-chs-translation` | 不直接承担运行期脚本排序、状态机推进、QA 放行或 final_mod 组装 |
| 判断当前阶段、允许动作、下一条命令 | `workflow-policy-and-state` | 不翻译、不路由单文件、不操作 GUI、不组装 final_mod |
| QA 失败后的 workflow agent 恢复、重试、回退继续、记录尝试 | `workflow-agent-orchestration` | 不直接翻译、不绕过 QA、不直接改二进制 |
| 正常流程中的主控/子智能体并发分派、lane 领取与完成回写 | `workflow-subagent-orchestration` | 不处理失败恢复、不让顶层 adapter 领取任务、不并发全局刷新/严格 QA/final_mod |
| 已识别端到端汉化任务后的运行期编排、完整流程、状态门禁推进 | `skyrim-mod-translation-orchestrator` | 不作为用户自然语言入口，不做具体字符串规则、GUI 细节或文件组装 |
| 任意文件处理前的分流 | `translation-task-router` | 不翻译、不操作 GUI、不组装 final_mod |
| 扫描 `mod/`、解压项目内 ZIP、生成清单 | `mod-input-preparation` | 不翻译、不调用 LexTranslator/xTranslator |
| `.bsa` 只读 inventory、BSAFileExtractor 安全解包、归档 manifest | `bsa-archive-audit` | 不翻译、不处理 RAR、不 materialize BA2、不直接修改或重打包归档 |
| `.ba2` 只读 inventory、受控安全解包、receipt/manifest/hash、loose override | `ba2-archive-audit` | 不翻译、不直接调用 extractor、不重打包 BA2 |
| `Interface/translations/*.txt`、JSON、JSONL、XML、CSV、TXT、MD | `text-resource-translation` | 不处理 ESP/PEX 二进制写回 |
| MCM 菜单、选项、帮助文本 | `mcm-translation` | 不翻译脚本逻辑 key |
| `.esp/.esm/.esl` 插件导出文本规则 | `esp-esm-esl-translation` | 不操作 GUI、不直接保存插件 |
| `.pex` 可见字符串、PSC 只读提取 | `pex-visible-strings-translation` | 不直接改 PEX、不回写或编译 PSC |
| LexTranslator GUI 工具操作 | `lextranslator-gui-automation` | 不判断字符串是否可翻译 |
| xTranslator GUI 精修、查漏、后备、PapyrusPex | `xtranslator-gui-automation` | 不作为主工具，除非路由指定 |
| 术语、未决名词、一致性 | `glossary-management` | 不做文件路由或工具操作 |
| 翻译、工具输出、final_mod 校验 | `qa-validation` | 不翻译、不控制 GUI |
| 生成 `out/<ModName>/汉化产出/final_mod/`、`intermediate/` 和 `<ModName>_CHS.zip` | `final-mod-assembly` | 不翻译、不修改二进制、不自动安装 |

## 4. mod/ 沙盒规则

- `mod/` 是项目内沙盒 Mod 副本。
- `mod/` 不是游戏实际加载目录。
- 所有导出、分析、翻译、校验都只能围绕工作区 `mod/` 和工作区内目录进行。
- 翻译、构建、QA、进度和 trace 产物只能进入 `source/`、`work/`、`translated/`、`qa/`、`out/`、`.workflow/`、`traces/`；具体 Mod 术语和用户词典进入工作区 `glossary/`。
- 插件维护可以写入插件源仓库的 `docs/`、`scripts/`、`adapters/`、`glossary/`、`config/`、`tools/`、`skills/`；具体 Mod 术语应优先写入工作区 `glossary/`。
- 不覆盖 `mod/` 下的原始文件，除非该文件是明确的文本导出文件，并且已经先创建备份。

## Final Mod Output

- `out/` 必须按 Mod 聚合，第一层为 Mod 名：`out/<ModName>/`。
- 第二层必须是汉化产出目录：`out/<ModName>/汉化产出/`。
- 最终完整 Mod 输出目录为 `out/<ModName>/汉化产出/final_mod/`。
- 中间产出汇总目录为 `out/<ModName>/汉化产出/intermediate/`，用于汇总工具输出、overlay、patch、审计等项目内中间产物。
- 汉化后打包好的 Mod 必须位于 `out/<ModName>/汉化产出/<ModName>_CHS.zip`，文件名必须使用 `_CHS` 后缀。
- `final_mod/` 必须保持当前 Game Profile 的 Data 根结构，方便人工检查和 Mod 管理器本地安装测试；项目内交付包仍由 `<ModName>_CHS.zip` 承载。
- 默认交付模式是直接替换：翻译结果必须以原始相对路径和原始文件名覆盖 `final_mod` 中的对应文件，而不是依赖旁挂语言补丁文件。
- `Interface/translations/*_chinese.txt`、外部 XML/JSONL 对照表、词典和 patch-only 产物默认只作为中间件；除非路由和 QA 明确证明游戏会加载该文件，否则不得把它当成最终交付。
- `final_mod/meta/provenance.jsonl` 必须记录每个 `final_mod` 文件的直接来源、来源 SHA256、最终 SHA256、transform、tool、生成器和 QA 证据入口；缺失溯源、hash 不匹配或来源丢失不得宣称完整交付。
- `python scripts/validate_final_mod.py` 中 `Language sidecar overlays` 必须为 0；新增旁挂语言文件不能被当成完整汉化交付。
- Agent 可以从项目内 `mod/` 沙盒目录复制文件到 `out/<ModName>/汉化产出/final_mod/`。
- Agent 可以只读解包项目内 `mod/` 沙盒中的 `.zip/.7z` 到 `work/extracted_mods/<ModName>/`，但不得修改压缩包本身。
- `.rar` 默认只生成提取建议，除非后续添加明确的项目内解包工具流程。
- `.bsa/.ba2` 默认先生成只读 inventory 证据；`.bsa` 只有通过项目安全 wrapper 调用 `BSAFileExtractorPath` 时才允许解到 `work/archive_extracts/<ModName>/<ArchiveName>/`；`.ba2` 只能由 `ba2-archive-audit` 的受控 wrapper 解到 profile 允许的工作区目录并独立验证。
- BSA 内容汉化后默认不重新组合打包；翻译结果必须按归档内原始相对路径生成同路径 loose override，例如写入 `translated/final_mod/<ModName>/Interface/...` 后由 final_mod 组装覆盖。只有人工测试证明 loose override 不加载或导致 Mod 问题时，才允许把 BSA 重打包列为高风险受控工具流程；未配置 BSA packer adapter 时必须 blocked。
- Codex 不允许从真实游戏目录、真实 MO2/Vortex 目录复制文件。
- Codex 不允许修改 `.esp`、`.esm`、`.esl`、`.bsa`、`.ba2`、`.pex`、`.dll`、`.exe` 等二进制文件。
- `.swf`、`.dll`、`.exe` 在 Skyrim SE/AE 和 Fallout 4 工作区都只能只读审计或原样复制，不修改。
- 如果 `final_mod` 中需要这些二进制文件，只允许从 `mod/` 沙盒目录原样复制。
- 翻译后的文本文件、Interface 翻译文件、DSD Patch、LexTranslator/xTranslator 导出的结果，可以写入 `final_mod`。
- 如果需要替换插件文件，必须由受控工具适配器在项目内自动生成到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`，然后 Codex 才能把该文件原样复制进 `final_mod`。
- 如果需要替换 PEX 文件，优先由受控 PEX CLI 适配器生成项目内 `out/<ModName>/tool_outputs/Scripts/*.pex` 或 `translated/tool_outputs/<ModName>/Scripts/*.pex`；LexTranslator/xTranslator PapyrusPex 只作为后备。Codex 不能直接修改 `.pex`。
- 如果当前 GUI 工具无法自动保存到项目内 `tool_outputs`，该步骤必须标记为 blocked；人工保存只能作为外部临时处置记录，不能算作全流程自动化完成。
- Codex 不得直接保存或修改插件。
- Codex 不得自动把 `final_mod` 复制到 MO2/Vortex。
- Codex 默认只在项目内生成 `<ModName>_CHS.zip` 交付包；不得自动安装、不得自动复制到真实 MO2/Vortex，公开发布前仍需人工确认权限。

## 5. 翻译规则

- 目标语言为简体中文。
- 风格为自然游戏本地化。
- 翻译和校对必须使用 agent 的模型能力；脚本只能做提取、分批、格式转换、机械检查和报告，不能把字典替换或正则替换当作完整翻译。
- `qa/<ModName>.model_review.md` 新报告必须明确 `Reviewer: Agent model`，旧报告中的 `Reviewer: Codex model` 仅作兼容；报告不能早于最新译文输入文件，译表变更后必须重新由 agent 模型校对。
- 保留占位符和格式。
- 不翻译 FormID、EditorID、脚本名、变量名、路径、文件名、插件名。
- 不确定术语进入 `qa/unresolved_terms.md`。
- LexTranslator 风格动态词典放在当前工作区 `glossary/lextranslator_dynamic_dictionaries/`，通过 `work/glossary_rag/lextranslator_dynamic.sqlite` 做本地 RAG 检索索引；用户可以按来源新增词典文件或子目录。主流程应先比较动态词典目录及词表文件修改时间与索引修改时间，只有词典更新、索引缺失、索引版本变化或显式 `--force` 时才重建索引。
- 翻译前可由插件源脚本 `python scripts/build_external_glossary_matches.py --mod-name "<ModName>"` 生成 `qa/<ModName>.external_glossary_matches.md`；该命中包只作为术语提示，不是自动替换规则，也不能覆盖禁翻项和运行时 key。

## 6. Papyrus 脚本可见文本规则

- 本项目允许按当前 Game Profile 处理 Papyrus 脚本中的玩家可见文本，但不允许修改脚本逻辑。
- 允许分析 `mod/` 目录下的 `Interface/translations/*.txt`。
- 允许分析 `mod/` 目录下导出的 MCM 文本。
- 允许分析 LexTranslator 或 xTranslator 从 `.pex` 中导出的可翻译字符串。
- 允许翻译玩家可见的通知、菜单、说明、MessageBox、MCM 文本。
- 脚本翻译结果只能输出到 `translated/` 或 `out/`。
- 禁止 Codex 直接修改 `.pex` 文件。
- 禁止 Codex 直接修改 `.psc` 源码并重新编译。
- 禁止翻译函数名、变量名、属性名、状态名、事件名。
- 禁止翻译脚本内部 key、page id、state id、StorageUtil key、JsonUtil key。
- 禁止翻译任何可能参与 if 判断、switch 判断、数组索引、字典 key 的字符串。
- 禁止翻译 PEX 导出行中 `opcode` 为 `CMP_*` 的字符串；MCM `OnPageReset(Page)` 这类页面标题比较字符串必须按 page id 保护，否则会出现左侧菜单存在但右侧 MCM 页面为空。
- 禁止覆盖 `mod/` 下原始脚本文件。
- 如果存在 `Interface/translations/*.txt`，优先翻译这些文件，不碰 `.pex`。
- 如果没有独立翻译文件，优先用 `PexStringToolPath` / Mutagen PEX 适配器提取 `.pex` 中的可见字符串。
- Fallout 4 PEX Export 可用；Apply 仅在明确 experimental opt-in 且 strict gate 通过时可进入受控写回。缺少认证证据时必须 blocked，不能因已有译表就宣称完成。
- `.psc` 不属于可编辑翻译文件；如果必须处理 `.psc`，只允许只读提取字符串字面量到 `work/psc_strings/` 供人工确认，不自动回写源码，不自动编译。
- 所有脚本翻译结果必须经过人工抽查和游戏内测试。

## 7. QA 要求

- 批量翻译后必须运行校验脚本。
- 必须检查行数、JSON 格式、ID 不变、占位符不丢失、target 不为空。
- 必须运行非 GUI 候选抽取和覆盖率审计，确认 `final_mod` 已覆盖所有应翻译候选；`Missing` 和 `Unverified` 必须为 0。
- PEX 写回必须在 `build_final_mod` 前后都运行 `python scripts/audit_pex_delivery.py`：前置检查译表行数、受控 tool_outputs PEX 是否存在且 hash 已变化；后置检查 `out/<ModName>/tool_outputs/Scripts/*.pex` 或 `translated/tool_outputs/<ModName>/Scripts/*.pex` 是否已同路径复制进 `final_mod` 且 SHA256 一致。
- PEX 输出验证报告必须统一命名为 `qa/<ModName>.<Script>.pex_output_verification.md`，覆盖率脚本以该标准报告作为已验证写回证据；不得只生成 `gate_`、`batch_` 等临时命名报告。
- PEX 覆盖率判断必须优先使用 PEX 导出身份和标准验证报告；对 `Chain/Sent to pit if all 0%` 这类受保护调用参数，不能只因原文字节子串命中就要求翻译整条受保护参数。
- PEX 写回和严格 QA 必须过滤 protected、空 target、source 等于 target、以及 `CMP_*` 比较指令中的行；这些行不得进入 `work/normalized/<ModName>/pex_apply/*.translation.jsonl` 或 `work/gates/<ModName>/*.translation.jsonl` 的可写回候选。
- 如果工作副本或 final_mod 中存在 BSA/BA2，必须运行归档覆盖审计；没有项目内内容审计证据时不能宣称完整汉化。
- BSA 内文本完成汉化后，默认 QA 目标是证明 `final_mod/` 中存在同路径 loose override 且原 BSA 未被修改；不得把“需要重打包 BSA”当作默认完成路径。
- BSA/BA2 manifest 中每个 `Risk=translatable` 项必须在 `final_mod/` 中存在同路径 loose override，或在 `qa/<ModName>.archive_loose_override_exemptions.jsonl` 中有明确豁免记录；严格完成模式下缺失 loose override 和无效豁免都必须阻断。
- Fallout 4 localized plugin 和 STRINGS 家族必须检测后 blocked；非 localized 插件只能处理 profile 白名单字段，并由 `Fallout4Mod` 反解析验证 masters、FormID、record count 和非目标字段不变。
- Fallout 4 Experimental 本身不是永久阻断，但任何 profile 声明不支持的必需输入都必须 fail closed。所有 QA、handoff、manifest 与 provenance 的 game/profile/adapter metadata 必须一致，跨游戏旧证据视为 stale/mismatch。
- 必须运行 `python scripts/validate_final_text_structure.py`，确认 final_mod 的 JSON key、XML tag/attribute name、INI section/key、CSV header、Interface key/tab/行数未被翻译破坏，PSC 源码未被改写。
- 必须由 `python scripts/validate_final_mod.py` 校验 `final_mod/meta/provenance.jsonl` 覆盖所有 final_mod 文件；`Missing provenance rows`、`Final file SHA256 mismatches` 和 `Source SHA256 mismatches` 必须为 0。
- 必须运行 `python scripts/new_final_text_review_packet.py`，并由 agent 模型在 `qa/<ModName>.model_review.md` 中明确校对 final_mod 实际文本差异；不能只校对中间译文文件。
- 必须运行 `python scripts/new_final_binary_review_packet.py`，反读 final_mod 中实际交付的 ESP/PEX 文本；`Protected review items` 和 `Export failures` 必须为 0，且模型校对报告必须明确覆盖该 packet。
- 重建 final_mod 或重写 PEX 后，固定顺序为：`build_final_mod` -> final text/binary review packet -> final review quality -> agent 模型校对 -> `run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete`；旧模型校对不得在 packet/hash 变化后继续放行。
- 大型 PEX Mod 可在完整 strict gate 前先跑候选抽取和覆盖率快检，先确认基础写回/覆盖为 0 缺口，再进入完整 final binary 反读和 strict gate。
- 常规重跑优先使用 `python scripts/run_non_gui_translation_workflow.py`，让准备、构建、严格门禁、状态刷新和健康报告形成一个可重复入口。
- 批量输入准备优先使用 `python scripts/run_translation_queue.py --mode prepare`，让 `mod/` 下多个压缩包逐个解包、扫描并写入队列报告。
- 最终交付完成判定必须运行 `python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete`，不能用带 warning 的普通门禁结果宣称完整汉化。
- Python 主入口会使用项目内 `work/.workflow.lock` 防止总控、严格门禁、状态刷新和健康检查并发写报告；不要为同一个项目并行运行这些入口。
- 必须生成 `qa/translation_readiness.md` 和 `qa/translation_readiness.json`，汇总 `mod/` 输入、已知输出、final_mod 状态、QA 证据和下一条建议命令；如果 `mod/` 下仍有未处理输入，项目级状态不能显示为 ready。
- 必须生成 `qa/workflow_health.md` 和 `qa/workflow_health.json`，作为后续 agent 接手的人工/机器双入口。
- 必须生成 `qa/workflow_state.md` 和 `qa/workflow_state.json`，按 `config/workflow_policy.json` 的状态机记录每个 Mod 的 `state`、`last_success_stage`、`blocking_checks`、结构化 `next_actions` 和兼容用 `next_command`；后续 agent 接手必须优先读取该机器状态，不靠重新扫描猜阶段。
- 必须生成 `.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`.workflow/workflow_state.json`、`qa/workflow_timeline.md` 和 `qa/blockers.md`；Codex 只能从进度卡转述用户进度，不能把 stdout 或 trace 当作进度事实。严格 QA 未运行或未完成时，进度卡不得显示 `qa_checked / ok`，必须显示 `qa_pending_strict` 或明确写出“严格 QA 待运行”。
- 长流程运行后必须生成 `traces/latest.jsonl` 和 `traces/trace_summary.md` 供开发者排查；trace 不替代 QA 门禁、状态机或用户进度卡。
- `qa_failed` 或 `blocked` 的 workflow agent 恢复尝试必须记录到 `qa/workflow_agent_runs.jsonl`；每次自动修复或重试后必须刷新 `qa/translation_readiness.json`、`qa/workflow_state.json`、`.workflow/workflow_state.json`、`.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`qa/workflow_timeline.md`、`qa/blockers.md`、`qa/workflow_tasks.json` 和 `qa/codex_handoff.json`；agent-neutral cross-adapter handoff 由显式 `write_agent_handoff.py` 生成。
- 必须运行译文校对脚本，检查误翻 protected/key/path/filename/FormID、占位符/控制符丢失、残留英文、现代口语和空译。
- PEX/ESP 工具输出必须分别运行 `python scripts/verify_pex_output.py` 和 `python scripts/verify_plugin_output.py`；PEX 还必须反读确认输出仍可解析。
- 必须记录错误。
- 校验错误默认写入 `qa/validation_errors.md`。

## 8. Git 建议

- 每处理一个 Mod 或一个 batch 提交一次。
- 不提交真实插件二进制。
- 不提交压缩包。
- 不提交真实游戏目录、真实 MO2/Vortex 目录或 AppData 配置目录内容。

## 9. 必须保护的内容

- FormID
- Plugin name
- Record type
- EditorID
- Script name
- Variable name
- File path
- File name
- JSON key
- XML tag
- HTML-like tag
- `%s`、`%d`、`%f`
- `{0}`、`{1}`、`{name}`
- `<Alias=...>`
- `<font ...>`
- `<color ...>`
- `$变量`
- `\n`
- `\r\n`
