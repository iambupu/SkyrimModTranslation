---
name: mod-input-preparation
description: "用于按工作区 Game Profile 准备 Bethesda Mod 输入。中文触发：准备 mod、扫描 mod、评估 Mod 规模、解压 ZIP/7Z、导入 Mod、输入队列、生成清单、BSA、BA2。Use before translation to estimate scale and risk, scan mod/, safely extract supported containers, classify files, route BSA to bsa-archive-audit and BA2 to ba2-archive-audit, and write inventory reports. Do not translate, operate GUI tools, or edit binaries."
---

# Mod Input Preparation

## 目标

准备 Mod 输入阶段：按工作区 marker/Game Profile 只读扫描 `mod/`，不按 Mod 名猜游戏。`.zip` 和受支持的 `.7z` 解到 `work/extracted_mods/<ModName>/`；`.bsa` 交给 `bsa-archive-audit`，`.ba2` 交给 `ba2-archive-audit`。BSA/BA2 都先做 inventory；materialization 只能由各自受控 wrapper 执行。RAR 只生成提取建议。本 Skill 不翻译、不调用 GUI、不修改原始归档或插件。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 原始输入只读取当前工作区 `mod/`。
- 解压工作副本只允许位于 `work/extracted_mods/<ModName>/`。
- 不访问当前或任何真实游戏目录，也不访问真实 MO2/Vortex 目录。
- 不修改 `mod/` 下原始文件、归档或插件二进制。

## 输入

- `mod/` 沙盒目录。
- `mod/` 下的工作区内 `.zip` 和 `.7z`。
- 已解压的 `work/extracted_mods/<ModName>/`。

## 输出

- `work/extracted_mods/<ModName>/`
- `qa/<ModName>.scale_assessment.json`
- `qa/<ModName>.scale_execution.json`
- `qa/<ModName>.resource_inventory.json`
- `qa/<ModName>.extraction_plan.md`
- `work/shards/<ModName>/index.json`
- `work/shards/<ModName>/files.jsonl`
- `work/shards/<ModName>/events.jsonl`
- `qa/mod_inventory.md`
- `qa/archive_extraction_report.md`

## 推荐工具

- `scripts/prepare_mod_workspace.py`
- `scripts/audit_mod_scale.py`
- `scripts/mod_scale_policy.py`
- `scripts/mod_materialization.py`
- `scripts/run_translation_queue.py`
- `scripts/detect_mod_files.py`
- `scripts/detect_decoder_tools.py`
- `scripts/new_bsa_archive_manifest.py`
- `scripts/new_archive_audit_manifest.py`
- `scripts/audit_archive_coverage.py`

## 具体流程

1. 确认输入路径在当前工作区内。
2. 在 materialization 前运行 `python scripts/audit_mod_scale.py --mod-name <ModName> --source-path <mod/...>`，写入 `qa/<ModName>.scale_assessment.json`。L0-L5 规模与 R0-R4 风险相互独立；候选行数只是容量估算。
3. 由 `prepare_mod_workspace.py` 根据当前评估和 `config/mod_scale_profiles.json` 生成 `qa/<ModName>.scale_execution.json`，实际执行文件数、单文件大小、总大小、超时、磁盘余量、并发、checkpoint、materialization 和打包模式。用户覆盖值不得超过 `absolute_limits`；L5 固定要求拆分子项目后聚合，不能强制回单工作区。
4. 目录、`.zip` 和 `.7z` 使用有界 materialization 写入 `work/extracted_mods/<ModName>/`。L2 以上默认启用断点恢复并只 materialize 当前 Game Profile 中非受保护资源，同时写入 resource inventory、extraction plan 和 shard checkpoint。恢复时只复用源身份与输出 hash 都一致的文件，同时删除当前源或策略已不再选择的旧输出。
5. 使用 `--force` 时，不得假定旧工作区能被直接删除；准备脚本应优先保留/改名旧目录后重建。如果 Windows 文件锁导致旧目录不能移动，则复用现有工作区并在 `qa/archive_extraction_report.md` 记录 reuse/warning，而不是把半删除状态伪装成干净重建。
6. 如果输入是 `.7z`，优先使用 Python `py7zr`；没有 `py7zr` 时才尝试 `config/tools.local.json` 的 `DecoderTools.Archive7zPath`；两者都不可用时写阻断报告。
7. 如果需要批量准备多个输入，运行 `python scripts/run_translation_queue.py --mode prepare`，由 readiness 报告选择下一个未处理输入。
8. 如果发现 `.bsa`，交给 `bsa-archive-audit`；如果发现 `.ba2`，交给 `ba2-archive-audit`。归档 wrapper 必须复用 scale execution 的限制、超时和磁盘预检。本 Skill 不直接 materialize BSA/BA2。
9. 如果输入是 `.rar`，生成提取建议；未添加明确工作区安全 adapter 前不自动解包。
10. 如果 BSA 只需要内容审计证据，运行 `python scripts/new_bsa_archive_manifest.py`；如果归档已由工作区安全解包器展开到 `work/archive_extracts/`，再运行 `python scripts/new_archive_audit_manifest.py` 刷新 extraction-backed manifest。
11. 扫描工作副本，写入清单和后续路由建议，再将具体翻译路由交给 `translation-task-router`。

## QA 检查

- 输入只来自 `mod/` 或工作区内工作副本。
- 规模评估必须明确 `candidate_rows_are_estimated=true`；实际采用的策略只认当前 `scale_execution.json`，不能把评估建议冒充已执行限制。
- scale execution 必须绑定当前 assessment/config，磁盘预检通过，所有覆盖值未超过绝对上限。
- L2-L4 的 checkpoint 必须绑定源身份和输出 hash；选择性 materialization 不得遗留上一次运行的已删除或新近受保护文件。
- `.zip/.7z` 只解压到 `work/extracted_mods/<ModName>/`。
- force-prepare 遇到锁定文件时必须在 archive report 中记录复用或保留旧工作区的状态；不能只显示成功提取而不说明 reuse。
- `.bsa` 必须路由给 `bsa-archive-audit`，`.ba2` 交给 `ba2-archive-audit`；本 Skill 只记录发现和移交。
- `.ba2` 只有在 profile 允许且受控 adapter、receipt、manifest 和 hash 验证齐备时才能安全解包；`.rar` 仍默认不解包。
- 严格完成模式下，BSA/BA2 必须有工作区内归档内容审计 manifest，否则 final_mod 不能标记完整。
- 归档内容审计 manifest 必须来自工作区安全只读审计器，或基于工作区 `work/` 下的已解包目录生成，不能读取真实游戏目录。
- 清单列出文件类型、风险、建议 Skill 和推荐工具。

## 完成标准

- `work/extracted_mods/<ModName>/` 已存在，或已明确记录无需解压。
- `qa/<ModName>.scale_assessment.json` 和 `qa/<ModName>.scale_execution.json` 已生成；如果本次评估失败，必须写 blocked execution/workflow report 并停止，不能沿用旧报告或退回无界解包。
- L2-L4 已生成 resource inventory、extraction plan 和 shard checkpoint；L5 已明确阻断单工作区 materialization 并转入子项目聚合。
- `qa/mod_inventory.md` 已生成并包含路由建议。
- `qa/archive_extraction_report.md` 已记录归档处理状态。
- 如果复用了已有工作区，`qa/archive_extraction_report.md` 已明确记录 `Reused existing workspace` 和 warning 原因。
- `qa/<ModName>.archive_coverage.md` 已记录 BSA/BA2 归档覆盖状态。
- 如果存在 BSA/BA2，`out/<ModName>/archive_audits/<ArchiveName>/manifest.json` 已生成，或阻断状态已记录。
- 未修改 `mod/` 原始文件、归档或插件二进制。
- 下游处理已交给 `translation-task-router`。

## 禁止事项

- 不直接修改 BSA/BA2/ZIP/RAR/7Z。
- 不覆盖原归档。
- 不调用 LexTranslator/xTranslator。
- 不打开或修改插件二进制。
- 不把解压工作副本写回 `mod/`。

## 失败处理

路径不安全、归档内容无法确认或解压目标冲突时停止，并写入 QA 报告。Windows 文件锁导致无法清理旧工作区时，不要继续写入半清理目录；只能安全复用并记录 warning，或阻断。
