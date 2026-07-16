---
name: qa-validation
description: "用于按当前 Game Profile 执行汉化 QA、game/profile/adapter 证据一致性检查和严格放行判断。中文触发：QA、严格门禁、漏译、结构、hash、provenance、ready、验证 final_mod、PEX/归档覆盖。Use after translations, tool_outputs, package rebuilds, or final_mod assembly; Fallout 4 Experimental is allowed only when required capabilities have valid evidence. Do not translate, control GUI, recover workflow, or schedule tasks."
---

# QA Validation

## Goal

Windows 运行环境；所有可复用动作使用插件源 Python 入口。不得引入 Bash、WSL、Linux 命令或 shell 包装层。

Validate translated text, controlled tool outputs, final_mod contents, and release evidence. QA decides whether the workflow may advance; it does not translate, schedule tasks, operate GUI tools, or refresh workflow state on its own.

## Read Strategy

Use this file for routine batch QA and gate selection.

Read [references/strict-qa-contract.md](references/strict-qa-contract.md) completely when any of these apply:

- running `--strict-complete`
- validating final_mod or `_CHS.zip`
- checking ESP/PEX writeback evidence
- preparing release or manual game testing
- diagnosing a failed strict gate

## Hard Boundaries

- Work only inside the initialized workspace.
- Read Mod input only from workspace `mod/`.
- Never access any real game, MO2, or Vortex directories.
- Never directly edit `.esp`, `.esm`, `.esl`, `.bsa`, `.ba2`, `.pex`, `.dll`, or `.exe` files.
- Do not mark a stage complete when a required report is missing, stale, or failed.
- Keep player-run game testing separate from project-local automated QA.

## Routine Batch QA

1. Confirm the input and output paths are inside the workspace.
2. Validate JSON, JSONL, XML, CSV, or Interface translation structure.
3. Check row counts, stable IDs/keys, placeholders, protected tokens, line breaks, and non-empty targets.
4. Check for untranslated English, source-equals-target rows, modern web slang, and terminology drift; two-letter visible labels such as `On` and `OK` are included rather than ignored as noise.
5. For PEX exports, reject protected rows, logic keys, and `CMP_*` comparison strings from writeback candidates.
6. Refresh the model review whenever a translated input changes. New reports must say `Reviewer: Agent model`.
7. Write QA findings under `qa/`; do not modify source Mod files to make a check pass.

Useful focused entrypoints:

```powershell
python scripts/validate_translation.py
python scripts/scan_placeholders.py
python scripts/validate_interface_translation.py
python scripts/proofread_translation.py
python scripts/validate_final_text_structure.py
python scripts/verify_plugin_output.py
python scripts/verify_pex_output.py
```

Use the entrypoint appropriate to the routed file type; do not run every leaf script by default.

## Strict Gate

Use the consolidated strict gate after translation and controlled writeback evidence are current:

```powershell
python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete
```

全量门禁只剩模型校对问题时，补完 `qa/<ModName>.model_review.md` 后在命令末尾加 `--reuse-mechanical-evidence`。复用条件不满足时脚本会自动回退到全量检查。

The strict gate must cover final text/binary review packets, model review freshness, PEX delivery where applicable, archive coverage, final_mod validation, provenance, package consistency, and translation goal compliance. Plugin verification must first run the production exporter on the final plugin, then require the translation JSONL, identity-based output export, hash-bound Mutagen writeback report, game/profile metadata, successful reparse, parsed structural and logical payload invariant, and `--require-translation-evidence`; strict mode must not pass `--warn-only`. Inspect the named failure report before rebuilding or retrying anything.

## Final Output Checks

Require all of the following before project-local completion:

- `out/<ModName>/汉化产出/final_mod/` has the correct Data-root layout.
- The final output contains direct replacements, not unsupported language sidecars.
- Every final_mod file has current provenance and matching source/final SHA256 evidence.
- Controlled ESP/PEX outputs come from workspace `tool_outputs`; untouched binaries are byte-identical copies from `mod/`.
- `<ModName>_CHS.zip` matches final_mod file-for-file.
- Final text and binary review packets are current and covered by agent model review.
- Strict QA reports zero blockers and zero unresolved warnings required by policy.

## Fallout 4 资源与交付检查

- `final_mod` 是完整 Mod，必须保留原 Mod 的 Data 根结构。插件及其受控输出必须保持原相对路径和原文件名。
- Materials、Meshes、Textures、Sound、Music、Video、Vis、Seq，以及 SWF、GFX、DLL、EXE 等受保护资源只能从 `mod/` 原样复制。source SHA256 与 final SHA256 必须相同，provenance transform 必须是 `original-copy`。
- `tool_outputs` 只允许当前 Game Profile 明确开放写回的插件或 PEX。宽泛的 Tool Adapter 二进制说明不能放开材质、网格、纹理、声音、视频或界面二进制。
- Skyrim/Fallout 4 `.esl` 与带 light trait 的插件不得作为受控写回进入 `final_mod`。Fallout 4 localized 插件及 STRINGS、DLSTRINGS、ILSTRINGS 继续作为 blocker。
- MCM 文本按实际 JSON、INI、TOML、TXT、Interface、插件或 PEX 来源检查。F4SE 配置只允许玩家可见 value 变化；key、路径、协议值和内部标识必须不变。

## State Boundary

QA scripts write validation evidence. The controller/orchestrator owns the serialized report refresh chain defined in [strict-qa-contract.md](./references/strict-qa-contract.md); this Skill does not maintain a shortened copy. Run `write_agent_handoff.py --agent <opencode|claude-code>` only when explicitly preparing that adapter's handoff. After workflow/state/health commands, the controller must re-read `.workflow/progress_card.md` and present its complete rendered Markdown card.

`translation_readiness` owns complete issue descriptions and evidence paths. `workflow_state` consumes readiness error issue codes and carries only stable `issue_id` and short `code` references for blocking decisions; it must not reimplement package, coverage, final-review, or model-review checks. `workflow_health` aggregates those references by `issue_id`, shows one root cause with impact scope and evidence, and records every producer in `reported_by`.

## Completion Rule

Project-local QA may finish at `ready_for_manual_test`. That means the package and static evidence for the current Game Profile are ready for the player; it does not mean real game or Mod manager testing happened. Fallout 4 Experimental is not a permanent blocker, but localized plugin/STRINGS, missing BA2 verification, or any unsupported required capability must block strict completion. Any Fallout 4 PEX Apply remains ineligible for strict completion even when it was explicitly opted in and its Apply/Verify evidence passes; it can only produce an experimental workspace-local copy for manual game testing. Readiness, state, handoff, manifest, provenance, adapter and profile metadata must agree; cross-game or stale evidence fails closed. Validate player-supplied results only through the manual-test contract in the strict QA reference.

Strict completion evaluates the capabilities actually used by final_mod. The used-capability summary must reconcile scan inventory, AdapterResult lineage and provenance before a stable capability can pass; `support_level` does not grant completion, and removed top-level capability fields are rejected.
