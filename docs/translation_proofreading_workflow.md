# Translation Proofreading Workflow

## 目标

在写回 ESP/PEX 或组装 `final_mod` 前，检查译文是否存在两类风险：

- 误翻：把 FormID、EditorID、路径、文件名、插件名、脚本 key、占位符或控制符翻掉，导致插件或脚本行为损坏。
- 低质：空译、残留英文、现代网络口语、明显不适合 Skyrim 游戏本地化的表达。

脚本校对只是机械门禁。翻译生成、语义校对、语气修正、术语一致性判断和“是否误翻了不该翻的内容”必须由 Codex 模型完成，并写入 `qa/<ModName>.model_review.md`。

模型校对报告必须明确：

- `Reviewer: Codex model`
- `Verdict` 中有明确通过或不通过结论
- 不含 TODO 占位
- 报告时间不早于最新译文输入文件；如果译表被修改，必须重新由 Codex 模型校对
- 报告必须明确覆盖 `qa/<ModName>.final_text_review_packet.md`
- 如果 final_mod 中包含 ESP/ESM/ESL 或 PEX，报告还必须明确覆盖 `qa/<ModName>.final_binary_review_packet.md`
- 报告必须写入当前 final text/binary review packet 的 `Items SHA256`，不能只提文件名。
- 报告必须点名 `qa/<ModName>.final_text_review_items.jsonl` 和 `qa/<ModName>.final_binary_review_items.jsonl` 中的全部 changed final_mod 文件。
- 报告通过时必须包含以下精确声明：
  - `No runtime-impacting issues remain`
  - `No required translation candidates remain untranslated`
  - `No semantic quality blockers remain`
  - `All changed final_mod files listed in the review packets were reviewed`
  - `Mechanical checks do not replace Codex model semantic review`
  - `Final review quality audit has 0 blocking issues and 0 warnings`

## 命令

```console
python .\scripts\proofread_translation.py --input-path "translated\plugin_exports\<ModName>\<Plugin>.esp_strings.zh.jsonl" --input-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --report-output-path "qa\<ModName>.translation_proofread.md" --issues-jsonl-path "qa\<ModName>.translation_proofread_issues.jsonl"
```

临时查看误报时可以加 `-WarnOnly`，但进入写回或 final_mod 前不应依赖 `-WarnOnly` 放行阻断问题。

## 检查内容

- JSON/JSONL 格式是否可解析。
- candidate 译文是否为空。
- `%s`、`%d`、`{0}`、`{name}`、`<Alias=...>`、`<font ...>`、`<color ...>`、`$变量`、`\n`、`\r\n` 是否保留。
- `.esp/.esm/.esl/.pex/.psc/.dll/.exe/.json/.xml/.txt/.ini` 文件名是否保留。
- `Data/`、`Scripts/`、`Interface/`、`MCM/`、`SKSE/` 等路径是否保留。
- `FormID`、key-like 字符串、`StorageUtil`/`JsonUtil` 风格 key 是否被误翻。
- 译文中是否残留非 allowlist 英文单词。
- 译文中是否出现现代口语或网络表达。

## 结果解释

- `Blocking issues` 大于 0：停止写回和 final_mod 交付，修正译表后重跑。
- `Warnings` 大于 0：需要人工校对，确认是否是名称、缩写、技术名或允许保留的英文。
- 0 阻断、0 警告：允许进入工具写回和后续二进制验证，但不替代游戏内测试。

## 直接替换交付检查

`final_mod` 默认以直接替换原文件为目标。校对时还要确认：

- 文本 overlay 使用原始相对路径和原文件名。
- `Interface/translations/*_chinese.txt` 这类旁挂语言文件不能作为唯一交付，除非 QA 记录已经证明目标环境会加载它。
- ESP/PEX 的译文输出必须覆盖 `final_mod` 中原插件或原脚本的同名副本。
- `meta/manifest.json` 中应记录 `DeliveryMode = direct-replacement-final-mod` 和 `ReplacementFilesApplied`。
- `qa/final_mod_validation.md` 中 `Language sidecar overlays` 应为 0。
- `out/<ModName>/qa/non_gui_translation_coverage.md` 中 `Missing` 和 `Unverified` 应为 0。
- `qa/<ModName>.final_binary_review_packet.md` 应存在，且 `Protected review items: 0`、`Export failures: 0`。
- `qa/<ModName>.final_review_quality.md` 应存在，且 `Blocking issues: 0`、`Warnings: 0`。它直接检查 final text/binary review items 的 `Final` 字段，防止实际交付反读项出现空译、原文未变、占位符/受保护 token 丢失、可疑英文残留或现代口语。
- `qa/<ModName>.model_review.md` 应逐文件覆盖所有 changed final_mod 文件，并明确没有运行风险、漏汉化或语义质量阻断。

最终交付前运行总门禁：

```console
python .\scripts\run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete
python .\scripts\validate_chs_package.py --mod-name <ModName>
```

这个门禁不会翻译文本，也不会写插件或 PEX；它只重跑候选抽取、覆盖率审计、机械校对、模型校对时效检查、ESP/PEX 输出验证、final_mod 文本/二进制反读校对包生成、final review quality 审计和 final_mod 校验。严格模式会把缺失插件译表、缺失 PEX 译表、覆盖率未验证项、二进制 protected-review/export failure、final review quality warning 和任何其他 warning 都视为阻断。`validate_chs_package.py` 负责额外确认 `_CHS.zip` 与 `final_mod/` 逐文件一致，避免测试包和通过 QA 的目录不是同一份内容。

## 项目级目标审计

批量交付完成后，按这个顺序刷新项目级证据：

```console
python .\scripts\audit_translation_readiness.py
python .\scripts\test_workflow_health.py --mod-name <ModName> --workspace-path "work\extracted_mods\<ModName>" --final-mod-dir "out\<ModName>\汉化产出\final_mod" --run-strict-gate
python .\scripts\audit_project_completion.py
python .\scripts\new_manual_game_test_plan.py
python .\scripts\new_manual_game_test_results_template.py
python .\scripts\audit_translation_goal_compliance.py
```

这些命令必须顺序运行，不得并行。`audit_project_completion.py` 依赖当前 readiness，`new_manual_game_test_results_template.py` 依赖当前 manual plan，`audit_translation_goal_compliance.py` 又会检查 plan/template 是否不早于当前证据。并行运行会产生旧 template 或旧 plan，目标合规审计应当把它视为项目内阻断。

`qa/project_completion_audit.md` 只证明项目内静态交付证据完整，例如严格门禁、final review quality 审计、模型校对、final_mod、CHS 包和翻译文本词典。final review quality 是硬门禁辅助，不替代 Codex 模型语义校对；模型报告必须明确承认这一点。`qa/translation_goal_compliance.md` 才按用户目标拆分为：

- 严格校对是否完成。
- 全部可翻译文件是否已被 final text/binary review packets 覆盖。
- 是否还有需要汉化但未汉化的候选。
- 是否还有语义质量阻断。
- 是否已生成玩家操作的真实游戏测试清单和结果模板；玩家实机结果属于外部验证，不属于校对工作流完成条件。

`qa/workflow_health.md` 是接手入口，不只展示当前单个 Mod 的详细健康检查；它还必须从 `qa/translation_readiness.json` 汇总全部 Known Mod Outputs，包括 final_mod、CHS 包、词典条目数、严格门禁、覆盖率、final review quality、模型校对状态和下一步动作。它还必须包含 Goal Boundary，明确区分项目内静态 QA、玩家操作的真实游戏/MO2/Vortex 外部验证和校对工作流目标。这样后续 Codex 不需要重新探索哪些包已经产出，也不会把玩家实机证据缺失误读成校对工作流未完成。

`qa/workflow_health.md` 的模型校对检查必须和 `qa/translation_goal_compliance.md` 使用同强度合同：优先复用当前干净且不早于证据的 strict gate；只有 strict gate 缺失、失败或过期时，才回退检查模型报告是否包含当前 final text/binary packet 的 `Items SHA256`、全部 changed final_mod 文件和 `final_review_quality` 报告名。`RowsChecked` 从 `qa/<ModName>.final_review_quality.json` 读取为结构化证据，不要求模型报告正文重复该数字，避免因为报告措辞格式导致重复阻断。

项目完成性审计还会确认关键证据没有过期：严格门禁必须不早于当前 `final_mod/` 和翻译文本词典，final review quality 审计必须不早于当前 final text/binary review items，CHS 包一致性报告必须不早于当前 `final_mod/` 和 `_CHS.zip`，模型校对必须包含当前 review packet 的 `Items SHA256`。

目标合规审计会继续交叉检查 `translation_readiness`、`project_completion_audit`、`manual_game_test_plan` 和 `manual_game_test_results.template`：`project_completion_audit` 必须覆盖全部 Known Mod Outputs；`manual_game_test_plan` 和 `manual_game_test_results.template` 只要求覆盖当前 `ready_for_manual_test` 的 Mod，blocked/qa_failed 的 Mod 不进入人工计划。包路径和词典条目数必须一致，manual plan 必须不早于当前 readiness，manual template 必须不早于当前 manual plan。只要 readiness 更新过，就先重建人工计划和模板，再运行目标合规审计。

目标合规审计还会直接检查两类不能只靠声明证明的内容：模型校对必须由当前干净 strict gate 证明，或由模型报告兜底证明其包含当前 final text/binary review packet 的 `Items SHA256`、全部 changed final_mod 文件和 `final_review_quality` 报告名；中间产出词典必须有 `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl`，且 JSONL 中存在实际 `source -> target` 译文条目。固定通过声明或 manifest 数量字段都不能单独放行。

如果 `qa/manual_game_test_results.json` 尚未记录所有 CHS 包由玩家在真实游戏测试通过，`qa/translation_goal_compliance.md` 必须把玩家实机验证标为 `out_of_scope_for_proofreading_workflow`，同时允许项目内校对工作流在严格 QA 通过后显示 `complete`。不得把玩家尚未提交外部证据写成项目内校对阻断。

玩家完成测试后，不直接信任手写结果。玩家先从 `qa/manual_game_test_results.template.json` 填写 `qa/manual_game_test_results.json` 并放入项目内证据附件；Codex 只运行证据验证，不直接操作真实游戏或 Mod 管理器路径：

```console
python .\scripts\validate_manual_game_test_results.py
python .\scripts\audit_translation_goal_compliance.py
```

验证器会检查每个 Mod 的 CHS 包 SHA256、final_mod manifest SHA256、当前测试计划路径、测试人、具体日期时间、游戏版本、Mod 管理器、Profile、加载顺序说明、全部 Required Manual Checks 和 `RuntimeIssues = none`。每条 `CheckResults[].Evidence` 必须是具体观察证据，不能只写 `ok`、`passed`、`done`、`正常`；每条 `CheckResults[].EvidenceArtifacts` 还必须列出至少一个项目内证据文件，路径必须在 `qa/manual_game_test_artifacts/<ModName>/` 下。验证报告会记录附件路径、大小和 SHA256；目标合规审计会拒绝附件哈希不匹配，或 `qa/manual_game_test_results_validation.json` 早于测试结果、测试计划、模板、证据附件的结果。只有该验证通过且保持当前有效，目标合规审计才会接受外部运行验证，但它不改变校对工作流的完成边界。
