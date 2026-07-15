---
name: skyrim-mod-translation-orchestrator
description: "用于入口完成分类且 workflow policy 已给出允许动作后，按当前 Game Profile 和 workflow_state 执行端到端运行期编排。中文触发：入口已完成分类、状态机推进、运行期编排、非 GUI workflow、授权脚本顺序、严格 QA 编排、final_mod 编排。Coordinates authorized script sequencing, profile-aware adapter routing, PEX/archive gates, Codex-only GUI handoff, progress/state refresh, QA, and delivery. Do not act as the natural-language entry, override capability gates, define string rules, operate GUI details, or assemble files directly."
---

# Skyrim Mod Translation Orchestrator

## 目标

只负责已进入运行期后的自动化汉化流水线编排策略：扫描输入、调用路由、安排对应 Skill、收集状态、触发 QA、安排 `final-mod-assembly` 组装 `out/<ModName>/汉化产出/final_mod/`、同步包含翻译文本词典的 `intermediate/`，并生成 `<ModName>_CHS.zip`。全局阶段策略和允许动作由 `workflow-policy-and-state` 决定；用户自然语言入口、总览和请求识别由 `skyrim-mod-chs-translation` 负责；本 Skill 不直接决定具体字符串是否可翻译，不描述 GUI 菜单细节，也不直接组装文件。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入路径和输出路径必须在当前工作区内。
- `mod/` 是唯一允许读取的 Mod 沙盒输入目录。
- 游戏身份和资源 capability 只读工作区 marker/Game Profile；不按 Mod 名猜游戏，也不以 `support_level` 代替单项能力判断。
- 不访问任何真实游戏目录或真实 MO2/Vortex 目录。
- 不直接修改 `mod/` 下原始 `.esp/.esm/.esl/.pex/.bsa/.ba2`。
- 插件或 PEX 二进制只能由受控 CLI/库写回器或 LexTranslator/xTranslator 对工作区内副本生成，Codex 不能直接改写。

## 职责边界

- `skyrim-mod-chs-translation`：对外入口、总说明、用户自然语言请求识别、workspace/tool setup 意图判断，以及下游 Skill 选择提示。
- `workflow-policy-and-state`：只读 workflow policy/state，判断当前阶段、允许动作、阻断项和下一条命令。
- `skyrim-mod-translation-orchestrator`：只做内部运行期阶段编排并服从 workflow policy/state；不要作为自然语言入口或第二套状态机。
- `translation-task-router`：负责文件类型、风险等级、工具优先级和下游 Skill 选择。
- `bsa-archive-audit`：只负责 BSA readonly inventory 和 BSA materialization。
- `ba2-archive-audit`：负责 BA2 readonly inventory、受控 materialization、独立 receipt/manifest/hash 验证和 loose override provenance；不重打包。它可以复用共享只读归档解析脚本，但 BA2 请求不转交给 BSA Skill。
- Decoder/CLI 阶段：负责无 GUI 解码、文本导出/导入、项目内工具输出。
- GUI Skill：Codex-only；只负责 decoder 不可用时的启动、打开、导入、导出、保存等兜底工具操作。opencode/Claude Code 遇到这类任务必须 blocked，并记录 `handoff_target=codex`。
- 文件类型 Skill：只负责可翻译范围、保护内容和文本规则。
- `qa-validation`：只负责校验和报告。
- `final-mod-assembly`：只负责完整 Mod 目录组装。

## 输入

- `mod/` 沙盒目录或项目内已解压工作副本。
- 工作区 `glossary/` 术语表；插件源仓库 `glossary/` 只作为初始化种子。
- `config/tools.local.json` 中的工具路径。
- `config/workflow_policy.json` 和 `qa/workflow_state.json`。

## 输出

- `source/`
- `work/`
- `translated/`
- `out/<ModName>/tool_outputs/`
- `qa/`
- `qa/workflow_state.json`
- `qa/workflow_state.md`
- `.workflow/workflow_state.json`
- `.workflow/progress_card.md`
- `.workflow/progress_card.json`
- `.workflow/progress_events.jsonl`
- `qa/workflow_timeline.md`
- `qa/blockers.md`
- `traces/latest.jsonl`
- `traces/trace_summary.md`
- `out/<ModName>/汉化产出/final_mod/`，默认采用直接替换交付模式。
- `out/<ModName>/汉化产出/intermediate/`
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl`
- `out/<ModName>/汉化产出/<ModName>_CHS.zip`

## 推荐工具

- `scripts/run_non_gui_translation_workflow.py`
- `scripts/write_workflow_state.py`
- `scripts/workflow_progress.py`
- `scripts/workflow_trace.py`
- `scripts/prepare_mod_workspace.py`
- `scripts/detect_mod_files.py`
- `scripts/detect_decoder_tools.py`
- `scripts/route_translation_task.py`
- `scripts/extract_mcm_text.py`
- `scripts/export_esp_strings.py`
- `scripts/apply_plugin_translation_map.py`
- `scripts/run_plugin_translation_stage.py`
- `scripts/invoke_mutagen_plugin_text_tool.py`
- `scripts/write_translation_status.py`
- `scripts/new_model_review_packet.py`
- `scripts/proofread_translation.py`
- `scripts/validate_translation.py`
- `scripts/new_final_text_review_packet.py`
- `scripts/extract_non_gui_candidates.py`
- `scripts/audit_non_gui_coverage.py`
- `scripts/new_bsa_archive_manifest.py`
- `scripts/invoke_bsa_file_extractor_safe.py`
- `scripts/new_archive_audit_manifest.py`
- `scripts/audit_archive_coverage.py`
- `scripts/validate_final_text_structure.py`
- `scripts/new_final_binary_review_packet.py`
- `scripts/audit_final_review_quality.py`
- `scripts/run_non_gui_qa_gates.py`
- `scripts/run_translation_queue.py`
- `scripts/audit_translation_readiness.py`
- `scripts/test_workflow_health.py`
- `scripts/audit_project_completion.py`
- `scripts/new_manual_game_test_plan.py`
- `scripts/new_manual_game_test_results_template.py`
- `scripts/validate_manual_game_test_results.py`
- `scripts/audit_translation_goal_compliance.py`
- `scripts/invoke_mutagen_pex_string_tool.py`
- `scripts/verify_pex_output.py`
- `scripts/validate_final_mod.py`
- `scripts/validate_chs_package.py`

## 具体流程

0. 本 Skill 只在请求已被 `skyrim-mod-chs-translation` 识别为运行期汉化/推进任务后使用；如果用户意图、工作区位置或工具准备模式还不清楚，先回到入口 Skill。进入本 Skill 后，先运行插件源脚本 `python scripts/write_workflow_state.py` 或读取工作区 `qa/workflow_state.json`，确认当前 `state`、`last_success_stage`、`blocking_checks` 和结构化 `next_actions`；该脚本会同步生成 `.workflow/progress_card.*`、`.workflow/progress_events.jsonl`、`.workflow/workflow_state.json`、`qa/workflow_timeline.md` 和 `qa/blockers.md`。如果 workflow state 给出明确下一步，不要跳过状态机手动拼接后续命令。初始化后的工作区不包含 `scripts/`，命令中的 `scripts/` 指插件源仓库脚本。
1. 默认先运行 `python scripts/audit_translation_readiness.py` 查看 `mod/` 中未处理输入；需要批量准备多个输入时运行 `python scripts/run_translation_queue.py --mode prepare`。
2. 对单个 Mod 的完整非 GUI 流程，运行 `python scripts/run_non_gui_translation_workflow.py`。需要排错或局部重跑时再执行下面的分步脚本。
3. Python 总控、队列、严格门禁、状态刷新和健康检查会使用 `work/.workflow.lock`；同一项目不要并发运行这些入口。长流程的详细执行记录写入 `traces/latest.jsonl` 和 `traces/trace_summary.md`，用户可见进度只读 `.workflow/progress_card.md`。
   如果 `qa/workflow_tasks.json` 同时提供多个依赖已满足、锁不冲突且 `can_run_parallel=true` 的 Mod/file/resource lane，调用 `workflow-subagent-orchestration` 由当前主控分配子智能体；不要让顶层 adapter 自己领取任务，也不要并发运行全局刷新、严格 QA 或 final_mod 组装。
状态卡展示规则：每次运行总控、队列、严格门禁、状态刷新、健康检查或恢复动作后，Codex 必须再次读取 `.workflow/progress_card.md`，并把完整 Markdown 卡片作为正文直接输出到对话中，让界面渲染成标题和表格；禁止放进三反引号代码围栏、代码块、引用块或其他会显示 Markdown 源码的容器。不能只依赖命令 stdout 中的进度卡，也不能用摘要或自写状态代替，因为 Codex 桌面版会折叠命令输出。未执行该 read-and-paste 步骤视为本 Skill 执行违规。
4. 使用 `mod-input-preparation` 扫描 `mod/` 或项目内解压工作副本。
5. 先运行 `python scripts/detect_decoder_tools.py`，确认当前 Game Profile 所需 ESP/PEX/BSA/BA2/7Z adapter 是否可用。BSA materialization 只允许 `scripts/invoke_bsa_file_extractor_safe.py`；BA2 materialization 只允许 `ba2-archive-audit` 指定的受控 wrapper 和独立验证链。
6. 对每个候选文件先调用 `python scripts/route_translation_task.py` 或 `translation-task-router`，由路由层决定 Decoder/agent 文本管线/GUI fallback 优先级。
7. 对低风险文本调用对应文件类型 Skill 和 Agent Text Pipeline。
8. 对 ESP/ESM/ESL、PEX 和归档，按 profile capability 使用 decoder/CLI。FO4 localized plugin/STRINGS 必须 blocked；非 localized ESP/ESM 走 Fallout 4 adapter 与反解析不变量，`.esl` 只允许只读 inventory，受控写回固定 blocked。PEX Export 可按 profile 执行，Apply 只有在 capability 允许时执行。BSA 交给 `bsa-archive-audit`；BA2 交给 `ba2-archive-audit`。归档译文默认作为同路径 loose override，BA2 不重打包。
9. 只有 profile 允许 PEX Apply 时，才在 `build_final_mod.py` 前执行 Apply + `verify_pex_output.py`。Fallout 4 必须有 experimental opt-in；受控 Apply 可生成工作区实验副本并反读验证，但 strict completion 固定 blocked，必须人工游戏内测试，不得把通用“必须写回”规则或静态验证当作交付授权。
10. 只有 decoder/CLI 不可用、格式不支持或 QA 失败且确需工具写回工作区内副本时，Codex 才能调用 LexTranslator/xTranslator GUI Skill。
11. Codex GUI Skill 先尝试 Computer Use；只有 Computer Use 不可用或失败时才降级到 pywinauto/UI Automation，并记录降级原因。
12. opencode/Claude Code 不调用 GUI Skill；遇到 GUI-only 任务必须 blocked，并记录 `handoff_target=codex`。
13. 要求 decoder/GUI 的输入和输出都在项目内，并写入工具日志。
14. 要求文件类型 Skill 产出翻译规则、未决项和 QA 检查点。
15. 翻译由 agent 模型完成；脚本只做提取、分批、格式保护和机械校验，不能把字典替换或正则替换当成完整翻译。
16. 运行机械校验后，生成中间译文模型校对包，并由 agent 模型完成语义/风格/误翻风险校对。
17. 模型校对和机械校验都通过后，才允许进入 ESP/PEX 写回或 final_mod 交付阶段。
18. 所有进入 final_mod 的译文默认必须以原相对路径和原文件名直接替换原文件；旁挂语言文件只作为中间件，除非 QA 证明游戏会加载它。
19. 运行非 GUI 候选抽取、覆盖率和归档覆盖审计。BSA/BA2 都要有 readonly inventory；BA2 materialization 还必须有 `ba2-archive-audit` 的 receipt/manifest/hash 和 loose provenance。确认 `final_mod` 中所有应翻译候选都已直接替换、由工具输出验证覆盖，或按 capability 明确阻断。
20. 运行 `qa-validation`，其中必须包含 final_mod 文本结构校验、final_mod 交付态文本模型校对包、final_mod 交付态 ESP/PEX 二进制反读校对包、final_mod 反读项机械质量审计，以及模型校对合同校验；失败时停止后续交付阶段。
21. 调用 `final-mod-assembly` 生成完整 Mod 目录、中间产出汇总目录、必备 `translation_text_dictionary/` 翻译文本词典和 `_CHS.zip` 包。
22. 构建完成后必须立即运行 `python scripts/validate_chs_package.py`，刷新当前 `_CHS.zip` 哈希和逐文件一致性报告；不能让 readiness 继续引用旧包哈希。
23. 运行 final_mod 校验、final_mod 文本结构校验、final_mod 交付态文本模型校对和非 GUI QA 总门禁，确认 `out/<ModName>/汉化产出/final_mod/`、`out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl` 与 `<ModName>_CHS.zip` 都存在，且 `_CHS.zip` 与 `final_mod/` 逐文件一致；交付完成判定使用 `python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete`，然后更新状态报告。
24. 运行 `python scripts/audit_translation_readiness.py` 生成项目级接手/就绪报告，确认 `mod/` 中是否还有未处理输入，并给出下一条命令。
25. 运行 `python scripts/test_workflow_health.py --run-strict-gate` 生成项目级健康报告，集中列出核心脚本、Skill frontmatter、全量 Known Mod Outputs、Goal Boundary、final text/binary packet、模型校对、严格门禁、translation readiness 和 final_mod 证据；健康检查应在 readiness 干净后刷新当前 manual plan 和 result template。
26. 依次运行 `python scripts/audit_project_completion.py`、`python scripts/new_manual_game_test_plan.py`、`python scripts/new_manual_game_test_results_template.py` 与 `python scripts/audit_translation_goal_compliance.py`，把项目内完成性证据、当前 CHS 包绑定的人工测试清单和玩家实机外部验证边界分开记录。`audit_project_completion.py` 覆盖全部 Known Mod Outputs；manual plan/template 只覆盖当前 `ready_for_manual_test` 的 Mod。只要 readiness 更新过，就必须重建 manual plan 和 template；这些入口是依赖链，不得并行运行，否则目标合规审计必须把旧 plan/template 视为项目内阻断。
27. 如果玩家填写了 `qa/manual_game_test_results.json`，必须先运行 `python scripts/validate_manual_game_test_results.py`；只有 `qa/manual_game_test_results_validation.json` 验证当前 CHS 包哈希、final_mod manifest 哈希、当前测试计划路径、测试环境、加载顺序说明、全部玩家检查项通过，且每项都有具体观察证据和 `qa/manual_game_test_artifacts/<ModName>/` 下的项目内证据附件后，外部运行验证才能算通过。Codex 不得直接操作真实游戏或 Mod 管理器路径，只能验证玩家提供的项目内证据。验证报告还必须记录每个附件的路径、大小和 SHA256；目标合规审计必须拒绝验证报告早于结果、计划、模板或附件，以及附件哈希不再匹配的结果。没有该验证时，目标级状态仍可在项目内严格 QA 通过后标记 `complete`，但玩家实机验证必须显示为 `out_of_scope_for_proofreading_workflow`。

## QA 检查

- 每个文件先经过 `translation-task-router`。
- Decoder 检测必须有 `qa/decoder_tools_report.md`。
- PEX 写回必须有 Mutagen PEX 写回报告、`verify_pex_output.py` 报告，以及一次 PEX 反读导出报告。
- 写回前必须有 `scripts/proofread_translation.py` 机械校对报告和 agent 模型校对报告。
- Agent 模型校对报告新格式必须写明 `Reviewer: Agent model`，不能早于最新译文输入；旧 `Reviewer: Codex model` 仅作兼容。
- 非 GUI 覆盖率必须有 `out/<ModName>/qa/non_gui_translation_coverage.md`，并且 `Missing: 0`、`Unverified: 0`。
- 归档覆盖必须有 `qa/<ModName>.archive_coverage.md`；存在 BSA/BA2 时必须有 `out/<ModName>/archive_audits/<ArchiveName>/manifest.json` 作为内容审计证据。
- BSA readonly inventory 和 extraction-backed manifest 由 `bsa-archive-audit` 生成；BA2 readonly inventory 和 materialization evidence 由 `ba2-archive-audit` 生成，materialization evidence 还必须包含独立 receipt/manifest/hash 验证。
- BSA 内翻译结果默认必须以同路径 loose override 交付；原 BSA 应保持原样。只有人工测试证明 loose override 不加载或导致 Mod 问题时，才允许记录高风险 BSA 重打包需求，且不能在缺少受控 packer adapter 时宣称完成。
- final_mod 文本结构必须有 `qa/<ModName>.final_text_structure.md`，确认 JSON/XML/INI/CSV/Interface 结构未损坏，PSC 源码未被改写。
- final_mod 交付态文本模型校对必须有 `qa/<ModName>.final_text_review_packet.md` 和 `qa/<ModName>.final_text_review_items.jsonl`；`qa/<ModName>.model_review.md` 必须明确覆盖该 packet。
- final_mod 交付态 ESP/PEX 二进制模型校对必须有 `qa/<ModName>.final_binary_review_packet.md` 和 `qa/<ModName>.final_binary_review_items.jsonl`；`Protected review items: 0`，`Export failures: 0`，且 `qa/<ModName>.model_review.md` 必须明确覆盖该 packet。
- final_mod 反读项机械质量审计必须有 `qa/<ModName>.final_review_quality.md` 和 `.json`；空译、原文未变、占位符/受保护 token 丢失、可疑英文残留、protected-review 漂移和现代口语都必须为 0 阻断、0 警告。
- `qa/<ModName>.model_review.md` 必须点名全部 changed final_mod 文件，并明确写出 `No runtime-impacting issues remain`、`No required translation candidates remain untranslated`、`No semantic quality blockers remain`、`All changed final_mod files listed in the review packets were reviewed`、`Mechanical checks do not replace agent model semantic review`、`Final review quality audit has 0 blocking issues and 0 warnings`。
- `qa/<ModName>.model_review.md` 必须包含当前 final text/binary review packet 的 `Items SHA256`，防止 packet 更新后沿用旧校对结论。
- `qa/workflow_health.md` 的模型校对检查必须与目标审计同强度：优先复用当前干净且不早于证据的 strict gate；只有 strict gate 缺失、失败或过期时，才回退确认模型报告包含当前 packet hash、全部 changed final_mod 文件和 `final_review_quality` 报告名。`RowsChecked` 由 `final_review_quality.json` 提供，不要求模型报告正文重复该数字。
- 最终交付判定必须使用 `python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete`；缺失插件译表、缺失 PEX 译表、覆盖率候选为 0 或任何 warning 都不能算完成。严格 QA 尚未运行时，进度卡必须显示 `qa_pending_strict` 或“严格 QA 待运行”，不得提前显示 `qa_checked / ok`。
- 插件-only Mod 的独立文本覆盖率候选可以为 0，但必须由 final binary review packet 中的 ESP/PEX review items 覆盖；不能把文本覆盖率 0 误判为无翻译。
- Codex-only GUI fallback 阶段必须有 `qa/tool_invocation_log.md`。
- 批量翻译后必须运行 `qa-validation`。
- final_mod 交付前必须有 `qa/final_mod_validation.md`。
- final_mod manifest 必须记录 `DeliveryMode = direct-replacement-final-mod`。
- final_mod manifest 必须记录 `OutputLayout = mod-root/localization-output/final_mod-intermediate-package`、`PackagedModPath` 和 `PackagedModNameSuffix = CHS`。
- final_mod manifest 必须记录 `TranslationDictionaryEntryCount`，并且 `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/manifest.json` 的 `TranslatedEntryCount` 必须大于 0。
- final_mod 校验必须显示 `Language sidecar overlays: 0`。
- `qa/<ModName>.chs_package_validation.md` 必须显示 `_CHS.zip` 与 `final_mod/` 的路径、数量和 SHA256 完全一致。
- `qa/<ModName>.non_gui_qa_gates.md` 和 `qa/<ModName>.chs_package_validation.md` 必须不早于当前 `final_mod/`、翻译文本词典和 CHS 包内容；输出改变后必须重跑门禁。
- 项目级健康报告 `qa/workflow_health.md` 和 `qa/workflow_health.json` 必须显示 `Blocking issues: 0`，并从 `qa/translation_readiness.json` 汇总全量 Known Mod Outputs，同时显示 Goal Boundary，便于后续 agent 和脚本不再重复探索证据位置或误把玩家实机证据缺失当作校对工作流阻断。
- 机器状态报告 `qa/workflow_state.md` 和 `qa/workflow_state.json` 必须存在，并显示每个 Mod 的 `state`、`last_success_stage`、`blocking_checks` 和结构化 `next_actions`；agent 接手时优先读它，不重新扫描猜阶段。
- 用户进度卡 `.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`.workflow/workflow_state.json`、`qa/workflow_timeline.md` 和 `qa/blockers.md` 必须由 `qa/workflow_state.json` 派生；Codex 不能把脚本 stdout 或 trace 明细当作阶段完成证据。
- 本地 Trace `traces/latest.jsonl` 和 `traces/trace_summary.md` 只用于失败复盘和开发者排查，不替代 QA 门禁、workflow state 或 provenance。
- 项目级接手报告 `qa/translation_readiness.md` 和 `qa/translation_readiness.json` 必须存在；如果 `mod/` 还有未处理输入，不能把整个项目称为完成。
- 目标级合规报告 `qa/translation_goal_compliance.md` 和 `qa/translation_goal_compliance.json` 必须存在；项目内严格 QA 通过但未记录玩家操作的真实游戏测试时，校对工作流可以标记 `complete`，玩家实机验证必须显示为 `out_of_scope_for_proofreading_workflow`。
- 目标级合规报告必须确认 readiness、project completion、manual game test plan 和 manual result template 的 Mod 范围一致，且 manual plan/template 不早于当前 readiness；否则视为项目内证据阻断。
- 项目级完成性审计、人工计划、人工结果模板和目标级合规报告必须顺序刷新；不得为了节省时间并行运行这些依赖入口。
- 玩家操作的游戏测试结果必须先通过 `qa/manual_game_test_results_validation.md`/`.json`；不得直接信任手写的 `qa/manual_game_test_results.json`，也不得接受计划外 Mod、重复 Mod、空泛 evidence、缺少项目内证据附件、附件哈希不匹配、验证报告过期或缺少加载顺序说明的结果。Codex 只校验证据，不操作真实游戏。
- Workflow Policy 必须通过：项目脚本目录和工具下载残留中没有 shell 包装入口，且权威脚本、Skill、docs、README、AGENTS、config、tools/README 中没有旧 shell 命令入口引用。
- blocked 阶段不能进入最终完成状态。

## 完成标准

- 每个候选文件都经过 `translation-task-router`，未绕过路由直接处理。
- 已调用对应文件类型 Skill、decoder/CLI 或必要且由 Codex 执行的 GUI fallback、`qa-validation` 和 `final-mod-assembly`。
- 非 GUI QA 总门禁以 `Strict complete mode: True` 通过，候选覆盖率报告显示没有缺失或未验证的应翻译候选。
- final_mod 文本结构校验通过，没有 key/tag/header/section 破坏或只读 PSC 改动。
- final_mod 反读项机械质量审计通过，证明实际交付 `Final` 字段没有空译、漏译、占位符/受保护 token 丢失、可疑英文残留或现代口语。
- Agent 模型已逐文件审查 final_mod 实际文本差异和 ESP/PEX 实际二进制反读差异，而不是只审查中间译文文件；模型报告必须证明无运行风险、无漏汉化、无语义质量阻断，并明确说明机械检查不能替代 agent 模型语义校对。
- `qa/workflow_health.md` 和 `qa/workflow_health.json` 已生成并通过，证明核心脚本、Workflow Policy、`skills/`、全量 Known Outputs、Goal Boundary、最终证据和状态报告在当前工作树中一致。
- `qa/workflow_state.md` 和 `qa/workflow_state.json` 已生成，且当前下一步命令与 readiness/health 不矛盾。
- `.workflow/progress_card.md`、`.workflow/progress_card.json`、`.workflow/progress_events.jsonl`、`.workflow/workflow_state.json`、`qa/workflow_timeline.md` 和 `qa/blockers.md` 已生成，用户可见进度与当前 workflow state 一致；`traces/trace_summary.md` 在长流程运行后可用于开发者排查。
- `qa/translation_readiness.md` 已生成；项目级状态必须区分单个 Mod ready 与 `mod/` 目录仍有未处理输入。
- `qa/translation_goal_compliance.md` 已生成；每个 Mod 行必须显示翻译文本词典条目数和 final review quality 状态。玩家真实游戏测试结果属于外部验证，不属于校对工作流完成条件；该报告还必须证明项目完成性审计、玩家测试清单和玩家结果模板与当前 readiness 输出范围一致。
- `qa/manual_game_test_results_validation.json` 用于验证玩家外部测试结果是否匹配当前包哈希和 final_mod manifest 哈希，并且每个 RequiredCheck 都有具体观察证据、项目内附件、附件大小和附件 SHA256；缺失该报告时不得宣称玩家实机验证已完成，但不得阻断校对工作流完成。
- `qa/<ModName>.chs_package_validation.md` 已生成；否则不能证明用户安装测试的 CHS 包与通过 QA 的 `final_mod/` 是同一份内容。
- Workflow Policy 已证明当前入口是 Python 主流程，没有旧 shell 包装入口或旧命令引用。
- 每个阶段的状态已写入 `qa/`，blocked 阶段未被伪装成完成。
- 最终交付只认 `out/<ModName>/汉化产出/final_mod/`、`out/<ModName>/汉化产出/intermediate/translation_text_dictionary/` 和 `out/<ModName>/汉化产出/<ModName>_CHS.zip`，并有 final_mod 校验报告。

## 失败处理

路径不安全、decoder 不可用、GUI 自动化失败、输出缺失或 QA 失败时，写入 `qa/` 报告并标记阶段未完成。不得把人工操作或失败工具步骤伪装成自动化完成。
