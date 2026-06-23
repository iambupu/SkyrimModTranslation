---
name: qa-validation
description: "用于汉化后的 QA 校验和放行判断。中文触发：QA、校验、检查漏译、严格门禁、strict、占位符、保护 ID、残留英文、结构检查、hash、provenance、ready、能不能测试、验证 final_mod、PEX 覆盖。Use after translation batches, GUI tool_outputs, PEX writeback, package rebuilds, readiness refreshes, or final_mod assembly. Do not use for translation or GUI control."
---

# QA Validation

## 目标

对翻译输出、工具输出和 final_mod 执行自动 QA。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前工作区内。
- Mod 原始输入只允许来自当前工作区 `mod/` 沙盒。
- 不访问真实 Skyrim 游戏目录。
- 不访问真实 MO2/Vortex 目录。
- 不直接修改插件二进制。

## 触发条件

- 每次批量翻译后。
- 工具 GUI 自动化输出后。
- final_mod 组装后。

## 输入

- 源 JSONL。
- 译文 JSONL。
- 工具输出文件。
- `out/<ModName>/汉化产出/final_mod/`。
- `out/<ModName>/汉化产出/intermediate/`。
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl`。
- `out/<ModName>/汉化产出/<ModName>_CHS.zip`。

## 输出

- `qa/validation_errors.md`
- `qa/placeholder_report.md`
- `qa/translation_proofread.md`
- `qa/translation_proofread_issues.jsonl`
- `qa/review_notes.md`
- `qa/<ModName>.final_text_review_packet.md`
- `qa/<ModName>.final_text_review_items.jsonl`
- `qa/<ModName>.final_binary_review_packet.md`
- `qa/<ModName>.final_binary_review_items.jsonl`
- `qa/<ModName>.pex_delivery_pre_build.md`
- `qa/<ModName>.pex_delivery_post_build.md`
- `qa/final_mod_validation.md`
- `qa/<ModName>.chs_package_validation.md`
- `qa/<ModName>.chs_package_validation.json`
- `out/<ModName>/汉化产出/final_mod/meta/provenance.jsonl`
- `out/<ModName>/qa/non_gui_translation_coverage.md`
- `qa/<ModName>.non_gui_qa_gates.md`
- `qa/workflow_health.md`
- `qa/workflow_health.json`
- `qa/workflow_state.md`
- `qa/workflow_state.json`
- `qa/translation_readiness.md`
- `qa/translation_readiness.json`
- `qa/project_completion_audit.md`
- `qa/project_completion_audit.json`
- `qa/manual_game_test_plan.md`
- `qa/manual_game_test_plan.json`
- `qa/manual_game_test_results.template.json`
- `qa/manual_game_test_results_template.md`
- `qa/manual_game_test_results_validation.md`
- `qa/manual_game_test_results_validation.json`
- `qa/translation_goal_compliance.md`
- `qa/translation_goal_compliance.json`

## 推荐工具

- `scripts/validate_translation.py`
- `scripts/scan_placeholders.py`
- `scripts/normalize_export.py`
- `scripts/split_jsonl.py`
- `scripts/validate_interface_translation.py`
- `scripts/audit_final_interface_translations.py`
- `scripts/proofread_translation.py`
- `scripts/new_model_review_packet.py`
- `scripts/update_model_review_contract.py`
- `scripts/new_final_text_review_packet.py`
- `scripts/extract_non_gui_candidates.py`
- `scripts/audit_non_gui_coverage.py`
- `scripts/new_bsa_archive_manifest.py`
- `scripts/invoke_bsa_file_extractor_safe.py`
- `scripts/new_archive_audit_manifest.py`
- `scripts/audit_archive_coverage.py`
- `scripts/validate_final_text_structure.py`
- `scripts/run_plugin_translation_stage.py`
- `scripts/verify_plugin_output.py`
- `scripts/verify_pex_output.py`
- `scripts/audit_pex_delivery.py`
- `scripts/invoke_mutagen_pex_string_tool.py --mode Export`
- `scripts/new_final_binary_review_packet.py`
- `scripts/audit_final_review_quality.py`
- `scripts/run_non_gui_qa_gates.py --strict-complete`
- `scripts/run_translation_queue.py`
- `scripts/audit_translation_readiness.py`
- `scripts/write_workflow_state.py`
- `scripts/write_workflow_tasks.py`
- `scripts/write_codex_handoff.py`
- `scripts/test_workflow_health.py`
- `scripts/audit_project_completion.py`
- `scripts/audit_translation_goal_compliance.py`
- `scripts/new_manual_game_test_plan.py`
- `scripts/new_manual_game_test_results_template.py`
- `scripts/validate_manual_game_test_results.py`
- `scripts/run_non_gui_translation_workflow.py`
- `scripts/write_translation_status.py`
- `scripts/validate_final_mod.py`
- `scripts/validate_chs_package.py`

## 具体流程

1. 校验路径在项目内。
2. 校验 JSON/JSONL/XML/CSV 格式。
3. 检查行数、ID、key、占位符、换行符。
4. 运行 `python scripts/proofread_translation.py` 检查误翻 protected/key/path/filename/FormID、占位符/控制符丢失、空译、残留英文和现代口语。
5. 生成中间译文模型校对包，并要求 Codex 模型完成语义、风格、术语一致性和过度翻译风险校对。
6. 检查未翻译英文和术语一致性。
7. ESP/ESM/ESL 工具输出用 `python scripts/verify_plugin_output.py` 检查哈希变化、原文残留、译文出现和 protected token；同时提供 `--output-export-jsonl-path`，用结构化反读确认译文，不要只依赖字节搜索。
8. PEX 工具输出用 `python scripts/verify_pex_output.py` 检查哈希变化、原文残留和译文完整出现；验证脚本必须跳过 protected、空 target、source 等于 target、以及 `CMP_*` 比较指令中的 PEX 译表行，避免把逻辑字符串当作应写回译文；源文消失但完整 target 未出现、只发现中文片段时必须阻断。再用 Mutagen PEX `Export` 反读确认输出仍可解析。
9. 抽取非 GUI 翻译候选并审计 `final_mod` 覆盖率。
10. 审计归档覆盖证据；严格完成模式下，BSA/BA2 必须有 `bsa-archive-audit` / `bethesda-structs` 产生的项目内只读 manifest；BA2 解包必须有单独 adapter 证据或明确阻断，未审计归档不能放行。
11. 运行 `python scripts/audit_final_interface_translations.py --mod-name <ModName> --final-mod-dir out/<ModName>/汉化产出/final_mod`，确认最终交付的 `Interface/translations/*.txt` 是 Skyrim 可加载的 UTF-16 LE BOM，且每行保留 `$key<TAB>value`。
12. 校验 final_mod 中 Interface、JSON/JSONL、XML、INI、CSV、PSC 的结构保护；防止 key、tag、header、section、PSC 源码被误改。
13. 从最终 `final_mod` 和工作副本差异生成交付态文本模型校对包，要求 Codex 模型审查实际交付文本，而不只看中间译表。
14. 从最终 `final_mod` 中反读 ESP/ESM/ESL 与 PEX 二进制文本，生成交付态二进制模型校对包，确认实际写入的插件/脚本文本可读、无 protected 结构漂移。
15. 运行 `python scripts/audit_final_review_quality.py --mod-name <ModName>`，对最终交付反读项中的 `Final` 字段做机械质量审计，拒绝空译、原文未变、占位符/受保护 token 丢失、可疑英文残留、protected-review 漂移和现代口语；不能只校对中间译表。
16. 校验 final_mod 结构、meta、直接替换交付记录和 `meta/provenance.jsonl` 逐文件产物溯源；缺失溯源、final hash 不匹配或来源 hash 不匹配必须阻断。
17. 校验 Codex 模型校对报告满足严格合同：必须点名所有 changed final_mod 文件，并包含 `No runtime-impacting issues remain`、`No required translation candidates remain untranslated`、`No semantic quality blockers remain`、`All changed final_mod files listed in the review packets were reviewed`、`Mechanical checks do not replace Codex model semantic review`、`Final review quality audit has 0 blocking issues and 0 warnings` 六条通过声明。
18. 校验 `intermediate/` 存在，且 `translation_text_dictionary/translation_dictionary.jsonl` 存在并包含非空 `source -> target` 译文条目；词典必须按译文条目保留上下文，不因重复文本丢行。
19. 校验 CHS 包存在且文件名以 `_CHS.zip` 结尾。
20. 运行 `python scripts/validate_chs_package.py`，确认 `_CHS.zip` 与 `final_mod/` 的文件路径、文件数量和 SHA256 完全一致；每次重新构建 final_mod 或 CHS 包后都必须刷新该报告，避免 readiness 读取旧包哈希。
21. Python 总控、严格门禁、状态刷新和健康检查会使用 `work/.workflow.lock`；同一项目不要并发运行这些入口。
22. 运行 `python scripts/audit_translation_readiness.py` 汇总 `mod/` 输入、已知输出、final_mod 状态、CHS 包、QA 证据和下一步建议，避免后续 agent 重复探索。
23. 运行 `python scripts/write_workflow_state.py` 生成 `qa/workflow_state.md` 和 `qa/workflow_state.json`，把每个 Mod 固化为 `state`、`last_success_stage`、`blocking_checks`、结构化 `next_actions` 和兼容用 `next_command`。
24. 运行 `python scripts/test_workflow_health.py --run-strict-gate` 汇总核心脚本、Skill、模型校对、translation readiness、workflow state、全量 Known Mod Outputs、Goal Boundary 和 final_mod 证据，作为后续 agent 接手入口，并写出 `qa/workflow_health.json` 供脚本读取。健康检查在 readiness 干净时应刷新 manual plan、result template 和 workflow state，避免报告链新鲜度误阻断。
25. 运行 `python scripts/audit_project_completion.py` 对所有 known Mod outputs 做项目级完成性审计，逐项确认严格门禁、最终反读质量审计、模型校对合同、final_mod、CHS 包逐文件一致性、翻译文本词典和证据新鲜度。
26. 如果单独手动刷新报告链，必须按 `audit_translation_readiness.py` -> `write_workflow_state.py` -> `test_workflow_health.py --run-strict-gate` -> `write_workflow_tasks.py` -> `write_codex_handoff.py` -> `audit_project_completion.py` -> `audit_translation_goal_compliance.py` 顺序执行；不要把这些依赖报告并行跑。
27. 运行 `python scripts/new_manual_game_test_plan.py` 生成 `qa/manual_game_test_plan.md`，列出真实游戏/MO2/Vortex 玩家操作验证步骤。项目内自动化不得把玩家测试伪装成已完成，Codex 不得直接操作真实游戏或 Mod 管理器路径。
28. 运行 `python scripts/new_manual_game_test_results_template.py` 生成 `qa/manual_game_test_results.template.json`，把每个待测 Mod 绑定到当前 CHS 包 SHA256 和 final_mod manifest SHA256。只要 `qa/translation_readiness.json` 更新过，就必须重建 manual plan 和 template，避免人工测试绑定旧包或旧 final_mod。
29. `audit_project_completion.py`、`new_manual_game_test_plan.py`、`new_manual_game_test_results_template.py`、`audit_translation_goal_compliance.py` 是依赖链，必须按顺序运行，不得并行；否则目标合规审计必须把旧 template 或旧 plan 视为项目内阻断。
30. 如果玩家填写了 `qa/manual_game_test_results.json`，运行 `python scripts/validate_manual_game_test_results.py`；只有 `qa/manual_game_test_results_validation.json` 通过后，外部运行验证才可被认为有证据支持。验证器必须拒绝计划外 Mod、重复 Mod、缺失或不匹配的 `SourcePlanPath`、旧包哈希、旧 manifest 哈希、缺少加载顺序说明、没有具体日期时间的 `CheckedAt`，以及 `ok`/`passed`/`done`/`正常` 这类空泛证据。每个 RequiredCheck 还必须在 `CheckResults[].EvidenceArtifacts` 中列出至少一个项目内证据文件，路径必须位于 `qa/manual_game_test_artifacts/<ModName>/` 下；验证报告必须记录每个附件的路径、大小和 SHA256，目标合规审计必须拒绝附件哈希不匹配或验证报告早于附件的结果。
31. 运行 `python scripts/audit_translation_goal_compliance.py` 生成 `qa/translation_goal_compliance.md`，把“无运行影响问题、无漏汉化、无语义质量阻断”拆成项目内校对证据和玩家实机外部验证边界；玩家尚未提供真实游戏测试结果和证据时不得阻断校对工作流完成。目标合规审计还必须直接读取 `translation_text_dictionary/translation_dictionary.jsonl`，确认它存在、非空、JSONL 有效，并包含实际 `source -> target` 译文条目；不能只信任 manifest 里的数量字段。
32. 写入 QA 报告。

## 禁止事项

- 不跳过错误继续标记完成。
- 不把 QA 未通过产物纳入最终完成状态。

## QA 检查

- 格式。
- 行数。
- key/ID。
- 占位符。
- 未翻译英文。
- protected/key/path/filename/FormID 是否被误翻。
- 译文质量风险：空译、残留英文、现代网络口语。
- final_mod 结构。
- `meta/provenance.jsonl` 是否存在，是否覆盖每个 final_mod 文件，是否记录 source/source_sha256/file_sha256/transform/tool/status，且 hash 均与当前文件匹配。
- 工具日志。
- PEX 输出是否可反读。
- PEX 输出是否完整包含每条预期 target；只出现中文片段但完整 target 缺失时必须阻断。
- PEX 写回译表是否排除了 protected、空 target、source 等于 target、以及 `CMP_*` 比较指令中的行；这类行不得作为 PEX 写回或 target 完整性验证依据。
- `build_final_mod.py` 前后是否已运行 PEX 交付核对：译表行数、受控 tool_outputs PEX hash 变化、final_mod 同路径复制和 SHA256 一致都必须有证据。
- PEX 输出验证报告是否使用 `qa/<ModName>.<Script>.pex_output_verification.md` 标准命名；覆盖率不得依赖 `gate_`、`batch_` 等临时报告名。
- `out/<ModName>/qa/non_gui_translation_coverage.md` 是否存在，且 `Missing: 0`、`Unverified: 0`。
- `qa/<ModName>.archive_coverage.md` 是否存在；如果存在 BSA/BA2，是否有项目内归档内容审计 manifest。
- `qa/<ModName>.final_interface_runtime.md` 是否存在，且 `Interface translation files checked` 已记录，`Blocking issues: 0`、`Warnings: 0`；所有 `Interface/translations/*.txt` 必须是 UTF-16 LE BOM 且每行保留 `$key<TAB>value`。
- BSA manifest 是否来自 `scripts/new_bsa_archive_manifest.py` / `bethesda-structs` 只读审计，或来自 `scripts/invoke_bsa_file_extractor_safe.py` 输出的 `work/archive_extracts/` 目录；不得直接调用第三方 BSAFileExtractor 或使用项目外解包目录。
- `out/<ModName>/archive_audits/<ArchiveName>/manifest.json` 是否列出归档内可翻译、需 decoder、需人工审查的资源。
- BSA/BA2 manifest 中每个 `Risk=translatable` 项是否以归档内原始相对路径作为 loose override 进入 `final_mod/`；未进入时是否在 `qa/<ModName>.archive_loose_override_exemptions.jsonl` 中有有效豁免记录。原 `.bsa/.ba2` 是否保持未修改。默认不得要求或接受 BSA 重打包作为完成证据。
- `qa/<ModName>.final_text_structure.md` 是否存在，且 `Blocking issues: 0`、`Warnings: 0`。
- `qa/<ModName>.final_text_review_packet.md` 和 `qa/<ModName>.final_text_review_items.jsonl` 是否存在，且 protected review items 为 0 或已被模型明确处理。
- `qa/<ModName>.final_binary_review_packet.md` 和 `qa/<ModName>.final_binary_review_items.jsonl` 是否存在；`Protected review items: 0` 且 `Export failures: 0`。
- `qa/<ModName>.final_review_quality.md` 和 `qa/<ModName>.final_review_quality.json` 是否存在，且 `Blocking issues: 0`、`Warnings: 0`；该报告必须不早于当前 final text/binary review items。
- final_mod 中 JSON key、XML tag/attribute name、INI section/key、CSV header、Interface key/tab/行数是否保留。
- `Source/scripts/*.psc` 等只读脚本文本是否与工作副本保持一致。
- `qa/<ModName>.non_gui_qa_gates.md` 是否显示 `Strict complete mode: True`，且阻断和警告都为 0。
- 对插件-only Mod，`out/<ModName>/qa/non_gui_translation_coverage.md` 可能没有独立文本候选；此时必须由 `qa/<ModName>.final_binary_review_packet.md` 的 review items 覆盖实际译文候选。
- `qa/workflow_state.md` 和 `qa/workflow_state.json` 是否存在，并显示每个 Mod 的 `state`、`last_success_stage`、`blocking_checks`、结构化 `next_actions` 和兼容用 `next_command`；后续接手必须先读它，不能重新扫描猜阶段。
- `qa/workflow_health.md` 和 `qa/workflow_health.json` 是否存在，且 `Blocking issues: 0`、`Warnings: 0`。
- `qa/workflow_health.md` 和 `qa/workflow_health.json` 是否包含从 `qa/translation_readiness.json` 刷新的全量 Known Outputs 汇总；不能只展示最后一次运行的单个 Mod，导致后续接手重复探索。
- `qa/workflow_health.md` 和 `qa/workflow_health.json` 是否包含 Goal Boundary，明确区分项目内静态 QA、玩家操作的真实游戏/MO2/Vortex 外部验证和校对工作流目标；玩家实机证据缺失不能被误读成校对工作流未完成。
- `qa/workflow_health.md` 和 `qa/workflow_health.json` 的模型校对检查是否与目标审计同强度：模型报告必须包含当前 final text/binary packet hash、全部 changed final_mod 文件、`final_review_quality` 报告名和 `RowsChecked` 数值。
- `qa/translation_readiness.md` 和 `qa/translation_readiness.json` 是否存在；如果 `mod/` 下仍有未处理输入，项目级状态不能显示为 ready。
- 目标合规审计必须交叉检查 `qa/translation_readiness.json`、`qa/project_completion_audit.json`、`qa/manual_game_test_plan.json` 和 `qa/manual_game_test_results.template.json` 的范围、包路径、词典条目数和证据新鲜度；`project_completion_audit` 必须覆盖全部 Known Mod Outputs，manual plan/template 只要求覆盖当前 `ready_for_manual_test` 的 Mod。任一报告落后于当前 readiness 或范围不一致，不能显示项目内 QA 通过。
- 目标合规审计必须独立复核模型校对 current packet contract：`qa/<ModName>.model_review.md` 必须包含当前 final text/binary review packet 的 `Items SHA256`、全部 changed final_mod 文件、`qa/<ModName>.final_review_quality.md` 文件名，以及 `qa/<ModName>.final_review_quality.json` 的 `RowsChecked` 数值；不能只依赖固定通过声明或 project completion 的间接结论。
- 目标合规审计必须直接读取 `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl`，确认词典文件存在、非空、可解析，并且至少包含一条 `source` 和 `target` 均非空且不相同的译文条目。
- `qa/workflow_health.md` 的 Workflow Policy 是否显示项目脚本目录和工具下载残留中没有 shell 包装入口，且没有旧 shell 命令入口引用。
- `scripts/validate_interface_translation.py` 只写 Markdown 报告；`--report-output-path` 必须使用 `.md` 后缀，不得把该报告写成 `.json`。
- `qa/<ModName>.model_review.md` 是否存在并明确通过。
- `qa/<ModName>.model_review.md` 是否明确写有 `Reviewer: Codex model`，且不早于最新译文输入。
- `qa/<ModName>.model_review.md` 是否明确提到 `qa/<ModName>.final_text_review_packet.md`，证明模型校对覆盖了实际 final_mod 文本差异。
- `qa/<ModName>.model_review.md` 是否明确提到 `qa/<ModName>.final_binary_review_packet.md`，证明模型校对覆盖了实际 final_mod ESP/PEX 二进制文本差异。
- `qa/<ModName>.model_review.md` 是否包含当前 `qa/<ModName>.final_text_review_packet.md` 与 `qa/<ModName>.final_binary_review_packet.md` 的 `Items SHA256`，防止 packet 变更后旧模型校对继续放行。
- `qa/<ModName>.model_review.md` 是否点名 `qa/<ModName>.final_text_review_items.jsonl` 和 `qa/<ModName>.final_binary_review_items.jsonl` 中的全部 changed final_mod 文件。
- `qa/<ModName>.model_review.md` 是否包含严格通过声明：无运行影响问题、无需要汉化但未汉化问题、无语义质量阻断。
- `qa/<ModName>.model_review.md` 是否明确承认机械检查不能替代 Codex 模型语义校对，并确认 final review quality 审计 0 阻断、0 警告。
- `meta/manifest.json` 是否记录 `DeliveryMode = direct-replacement-final-mod`。
- `meta/manifest.json` 是否记录 `OutputLayout = mod-root/localization-output/final_mod-intermediate-package`、`IntermediateOutputDir`、`PackagedModPath` 和 `PackagedModNameSuffix = CHS`。
- `meta/manifest.json` 是否记录 `ProvenancePath` 和 `ProvenanceEntryCount`，且 `qa/final_mod_validation.md` 中 `Missing provenance rows`、`Final file SHA256 mismatches`、`Source SHA256 mismatches` 都为 0。
- `ReplacementFilesApplied` 是否记录同路径同名替换；旁挂语言文件是否没有成为唯一交付依据。
- `qa/final_mod_validation.md` 中 `Language sidecar overlays` 是否为 0。
- `out/<ModName>/汉化产出/intermediate/` 是否存在。
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/manifest.json` 是否存在，且 `TranslatedEntryCount` 大于 0。
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl` 是否存在并包含实际译文条目。
- `out/<ModName>/汉化产出/<ModName>_CHS.zip` 是否存在。
- `qa/<ModName>.chs_package_validation.md` 是否证明 `_CHS.zip` 与 `final_mod/` 完全一致。
- `qa/<ModName>.non_gui_qa_gates.md` 和 `qa/<ModName>.chs_package_validation.md` 是否不早于当前 `final_mod/`、翻译文本词典和 CHS 包内容。
- `qa/translation_readiness.json`、`qa/workflow_state.json`、`qa/workflow_tasks.json`、`qa/codex_handoff.json`、`qa/workflow_health.json`、`qa/project_completion_audit.json`、`qa/manual_game_test_plan.json`、`qa/manual_game_test_results.template.json` 和 `qa/translation_goal_compliance.json` 是否按依赖顺序刷新；旧 state、旧 tasks/handoff、旧 plan/template 或旧 package validation 不得放行。

## 完成标准

- 对本阶段相关输入运行了对应校验脚本。
- `qa/validation_errors.md`、`qa/placeholder_report.md`、`qa/translation_proofread.md`、`qa/review_notes.md` 或 `qa/final_mod_validation.md` 已更新。
- 已有 Codex 模型校对结论；机械脚本通过不能单独代表译文质量通过。
- Codex 模型校对报告必须明确说明机械检查不能替代模型语义审读；final review quality 只能作为辅助证据，不能替代模型逐项阅读最终交付文本。
- 如果译文输入在模型校对后又发生变化，当前阶段不能标记通过，必须重新由 Codex 模型校对。
- 非 GUI 覆盖率审计已经运行，并证明没有缺失或未验证的应翻译候选。
- BSA/BA2 归档覆盖审计已经运行；没有归档，或每个归档都有项目内内容审计证据。BSA/BA2 只读 manifest 证据必须符合 `bsa-archive-audit` 边界；BA2 解包或写回证据必须来自单独配置的 BA2 adapter。
- BSA/BA2 归档中 `Risk=translatable` 的条目已经通过 `scripts/audit_archive_coverage.py` 证明存在同路径 loose override，或存在有效豁免记录；严格完成模式下 `Archive loose overrides missing` 和 `Archive loose override exemption issues` 必须为 0。
- 如果人工测试记录显示 loose override 不加载或导致 Mod 问题，QA 必须标记 `bsa_repack_required_without_adapter`，直到存在受控 BSA packer adapter、manifest、hash 校验和新的人工测试证据。
- final_mod 文本结构校验已经运行；结构化文本未破坏 key/tag/header/section，PSC 未被 Codex 改写。
- final_mod 产物溯源校验已经运行；每个交付文件都有项目内来源、工具/transform 和 hash 证据。
- intermediate 汇总目录、翻译文本词典和 `_CHS.zip` 包已经生成。
- `_CHS.zip` 包一致性校验已经运行，并证明包内文件与 `final_mod/` 逐文件一致。
- final_mod 交付态文本校对包已经生成，并由 Codex 模型在 `qa/<ModName>.model_review.md` 中明确审过。
- final_mod 交付态二进制校对包已经生成，并由 Codex 模型在 `qa/<ModName>.model_review.md` 中明确审过；ESP master、FormID、EditorID、脚本逻辑 key 等 protected 内容没有发生未解释变化。
- final_mod 交付态文本和二进制反读项已经由 `qa/<ModName>.final_review_quality.md` 机械审计，空译、原文未变、占位符/受保护 token 丢失、可疑英文残留和现代口语均无阻断或警告。
- 严格完成性门禁已经运行；缺失插件译表、缺失 PEX 译表、未验证覆盖率和 warning 都没有被放行。
- 工作流状态机已经运行；后续 agent 可以从 `qa/workflow_state.md` 看到每个 Mod 的机器状态、最后成功阶段、阻断检查、结构化下一步动作和兼容命令。
- 工作流健康检查已经运行；后续 agent 可以从 `qa/workflow_health.md` 看到核心脚本、Workflow Policy、Skill、全量 Known Outputs、Goal Boundary 和最终证据状态。
- 接手/就绪审计已经运行；后续 agent 可以从 `qa/translation_readiness.md` 看到 `mod/` 输入、已知输出、当前状态和下一步建议。
- 项目级完成性审计已经运行；后续 agent 可以从 `qa/project_completion_audit.md` 看到所有 known Mod outputs 的严格完成证据。
- 项目级完成性审计已经确认报告没有过期：严格门禁不早于当前交付内容，模型校对包含当前 review packet 哈希。
- 目标级合规审计已经直接验证模型报告绑定当前 packet hash、changed files 和 final_review_quality RowsChecked，并直接验证中间产出词典 JSONL 中存在实际 source/target 译文条目。
- 玩家操作的游戏测试清单已经生成且不早于当前 `qa/translation_readiness.json`；玩家可以从 `qa/manual_game_test_plan.md` 逐个验证运行风险。
- 玩家操作的游戏测试结果模板已经生成且不早于当前测试清单；玩家必须从 `qa/manual_game_test_results.template.json` 填写 `qa/manual_game_test_results.json`，保留 `SourcePlanPath`、当前包 SHA256 和当前 final_mod manifest SHA256，填写具体日期时间、测试人、游戏版本、Mod 管理器、Profile、加载顺序说明、逐项观察证据和 `qa/manual_game_test_artifacts/<ModName>/` 下的项目内附件；Codex 只运行 `qa/manual_game_test_results_validation.md` 验证玩家证据。验证报告必须记录附件 SHA256，后续目标审计只信任未过期且附件哈希仍匹配的验证结果。
- 目标级合规审计已经生成；后续 agent 可以从 `qa/translation_goal_compliance.md` 直接看到项目内 QA 是否已通过、每个 Mod 的翻译文本词典条目数、final review quality 状态，以及玩家操作的真实游戏测试是否属于外部后续证据。
- Workflow Policy 已通过，防止旧 shell 包装层、旧命令名或过期入口再次成为接手路径。
- 阻断错误未被标记为完成，后续交付阶段已停止。
- 非阻断问题已写入 review notes，便于人工抽查和游戏内测试。

## 失败处理

阻断错误必须修复后重跑；非阻断问题记录到 review notes。
