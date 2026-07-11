# scripts 入口索引

本目录保存 Skyrim Mod CHS Translation 插件源仓库的 Python 入口脚本。初始化后的工作区不会复制本目录；工作流从插件源仓库运行这些脚本，并通过 `.skyrim-chs-workspace.json`、显式参数或当前工作目录定位目标工作区。

工作流、QA、工具适配器和 final_mod 组装都只使用 Python 入口。不要新增 shell 包装脚本。

仓库根目录提供 `pyproject.toml`，可以用 uv 运行脚本：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

uv 是可选入口；所有脚本仍支持 `python scripts\...` 直接运行。工作区自动工具准备检测到 uv 时，会优先用 `uv venv` 和 `uv pip install` 创建 `tools/python-venv/`，失败时回退到 `venv + pip`。

## 常用入口

| 任务 | 脚本 | 说明 |
|---|---|---|
| 创建工作区 | `init_workspace.py` | 主初始化入口。目标必须是插件仓库外的新路径或空目录。 |
| 旧初始化兼容入口 | `init_project.py` | 兼容包装，实际转到 `init_workspace.py`。 |
| 准备本地非 GUI 工具 | `setup_workspace_tools.py` | 处理 workspace-local 工具的 auto/manual/skip 准备模式。 |
| 准备队列中的 Mod 输入 | `run_translation_queue.py` | 批量准备工作区 `mod/` 下的压缩包或目录。 |
| 运行可重复非 GUI 主流程 | `run_non_gui_translation_workflow.py` | 单个 Mod 的主工作流入口。 |
| 恢复一个安全步骤 | `resume_workflow.py` | 依据 workflow policy/state 执行低风险恢复，并记录尝试。 |
| 运行严格完成门禁 | `run_non_gui_qa_gates.py` | 指定 Mod 的最终严格 QA 门禁。 |
| 刷新工作流状态 | `write_workflow_state.py` | 写入 `qa/workflow_state.*`、进度卡、时间线和阻断报告。 |
| 刷新就绪状态 | `audit_translation_readiness.py` | 汇总输入、输出、阻断项和建议下一步。 |
| 检查整体工作流健康 | `test_workflow_health.py` | 生成项目级健康报告，可选包装严格门禁。 |
| 运行效果回归 fixture | `run_effect_regression.py` | 运行 `samples/effect_regression/` 下的项目内回归快照，不调用真实游戏、GUI 或外部 API。 |
| 构建 final_mod 和交付包 | `build_final_mod.py` | 从项目内来源组装 `out/<ModName>/汉化产出/final_mod/`。 |

## 工作区与工具准备

| 脚本 | 用途 |
|---|---|
| `init_workspace.py` | 创建工作区骨架、本地配置、glossary 种子、QA 状态和进度文件。 |
| `init_project.py` | 旧命令兼容包装。 |
| `setup_workspace_tools.py` | 准备工作区本地非 GUI 工具，并写入安装/检查报告。 |
| `init_opencode.py` | 为工作区写入 opencode 配置和本地插件、刷新 handoff/context，并可一键启动 opencode。 |
| `validate_tools_config.py` | 校验 `config/tools.local.json` 的路径和工具配置。 |
| `validate_agent_capabilities.py` | 校验 Codex、opencode 和 Claude Code adapter 能力边界。 |
| `validate_claude_plugin_marketplace.py` | 校验 Claude Code marketplace 元数据和非 GUI Skill 暴露边界。 |
| `detect_decoder_tools.py` | 检测可用 decoder、CLI 和库工具。 |
| `check_project_dotnet_sdk.py` | 检查工作区本地 .NET SDK 要求。 |
| `audit_tool_prefs.py` | 审计工具偏好配置。 |

## 工作流状态、队列与进度

| 脚本 | 用途 |
|---|---|
| `audit_translation_readiness.py` | 生成项目就绪状态和已知输出汇总。 |
| `write_workflow_state.py` | 刷新权威 workflow state 和用户进度卡。 |
| `write_workflow_tasks.py` | 从 workflow state 派生可并行任务队列。 |
| `claim_workflow_task.py` | 供主控分派的子 agent 领取和回写单个 workflow task。 |
| `run_workflow_tasks.py` | 在锁保护下执行队列任务。 |
| `run_translation_queue.py` | 准备或处理队列中的 Mod 输入。 |
| `run_non_gui_translation_workflow.py` | 非 GUI 主工作流驱动入口。 |
| `resume_workflow.py` | blocked/qa_failed 状态下的单步安全恢复入口。 |
| `refresh_project_handoff_reports.py` | 一次刷新 readiness、state、tasks 和 handoff 报告。 |
| `write_agent_handoff.py` | 写入 agent-neutral 接手报告；不挂到现有 Codex 热路径。 |
| `write_codex_handoff.py` | 写入简短 Codex 兼容接手报告。 |
| `list_agent_skills.py` | 输出指定 adapter 可用的 portable runtime Skill 摘要。 |
| `export_agent_context.py` | 为指定 adapter 显式导出有界上下文包。 |
| `init_opencode.py` | opencode 辅助入口；生成工作区 `.opencode/` 配置、本地插件和 `latest.opencode.context.md` 后启动 opencode。 |
| `write_translation_status.py` | 写入单个 Mod 的翻译状态报告。 |
| `log_workflow_agent_run.py` | 追加恢复尝试记录。 |
| `workflow_lock.py` | 共享工作流锁辅助模块。 |
| `workflow_progress.py` | 共享进度卡写入模块。 |
| `workflow_trace.py` | 共享本地 trace 写入模块。 |

## 输入发现与路由

| 脚本 | 用途 |
|---|---|
| `prepare_mod_workspace.py` | 解包或暂存沙盒 Mod 输入到工作区工作目录。 |
| `detect_mod_files.py` | 扫描 Mod 文件并汇总候选类型。 |
| `route_translation_task.py` | 将单个项目内文件路由到正确 Skill 和工具路径。 |
| `new_translation_task.py` | 生成聚焦的翻译任务包。 |
| `project_paths.py` | 共享路径解析和工作区边界辅助模块。 |

## 术语与翻译审阅包

| 脚本 | 用途 |
|---|---|
| `build_external_glossary_matches.py` | 生成指定 Mod 的外部术语命中报告。 |
| `build_lextranslator_dictionary_rag_index.py` | 构建或刷新 LexTranslator 风格动态词典索引。 |
| `new_model_review_packet.py` | 为中间译文生成模型校对包。 |
| `update_model_review_contract.py` | 刷新模型校对报告所需合同文本。 |
| `proofread_translation.py` | 对译文行做机械校对。 |
| `validate_translation.py` | 校验译表结构和必填字段。 |
| `scan_placeholders.py` | 检查占位符和控制 token。 |
| `split_jsonl.py` | 将 JSONL 文件拆成较小批次。 |
| `normalize_export.py` | 规范化导出的翻译行。 |

## 文本、MCM 与 Interface 文件

| 脚本 | 用途 |
|---|---|
| `extract_mcm_text.py` | 从受支持的文本结构中提取 MCM 文本候选。 |
| `validate_interface_translation.py` | 校验 Skyrim Interface 翻译文件。 |
| `audit_final_interface_translations.py` | 审计 final_mod 中 Interface 翻译交付状态。 |

## ESP、ESM、ESL 与插件文本

| 脚本 | 用途 |
|---|---|
| `export_esp_strings.py` | 将插件字符串导出为项目内文本中间文件。 |
| `apply_plugin_translation_map.py` | 通过受控工具应用插件译表。 |
| `run_plugin_translation_stage.py` | 串联插件文本导出、译表、写回和验证阶段。 |
| `invoke_mutagen_plugin_text_tool.py` | 调用受控 Mutagen 插件文本适配器。 |
| `verify_plugin_output.py` | 验证生成的插件输出。 |
| `invoke_ssedump_safe.py` | 通过安全包装调用受支持的插件 dump 工具。 |

## PEX 与 Papyrus 可见字符串

| 脚本 | 用途 |
|---|---|
| `invoke_mutagen_pex_string_tool.py` | 通过受控 Mutagen PEX 适配器导出或应用可见字符串。 |
| `prepare_pex_tool_output.py` | 为 final_mod 组装准备受控 PEX tool_output。 |
| `verify_pex_output.py` | 验证生成的 PEX 输出和可解析性。 |
| `audit_pex_delivery.py` | 检查 PEX 写回与 final_mod 交付一致性。 |
| `pex_translation_safety.py` | 共享受保护行过滤和规范化逻辑。 |

## 归档与 BSA 覆盖

| 脚本 | 用途 |
|---|---|
| `new_bsa_archive_manifest.py` | 生成 BSA 内容审计 manifest。 |
| `new_archive_audit_manifest.py` | 从解包或审计内容生成归档审计 manifest。 |
| `audit_archive_coverage.py` | 检查归档 loose override 覆盖和豁免记录。 |
| `invoke_bsa_file_extractor_safe.py` | 通过项目安全包装调用已配置的 BSA 解包工具。 |

## GUI 工具兜底

| 脚本 | 用途 |
|---|---|
| `invoke_lextranslator.py` | LexTranslator 适配器入口。 |
| `invoke_lextranslator_gui.py` | LexTranslator GUI 包装入口。 |
| `automate-lextranslator-gui.py` | 旧 LexTranslator GUI 自动化辅助脚本。 |
| `invoke_xtranslator.py` | xTranslator 适配器入口。 |
| `convert_xtranslator_xml_to_lextranslator_jsonl.py` | 将 xTranslator XML 输出转换为 LexTranslator 风格 JSONL。 |

## 覆盖率、最终审阅与 QA 门禁

| 脚本 | 用途 |
|---|---|
| `extract_non_gui_candidates.py` | 从项目内内容提取非 GUI 翻译候选。 |
| `audit_non_gui_coverage.py` | 审计候选在 final_mod 中的覆盖状态。 |
| `new_final_text_review_packet.py` | 生成 final_mod 文本审阅包。 |
| `new_final_binary_review_packet.py` | 生成 final_mod ESP/PEX 二进制反读审阅包。 |
| `audit_final_review_quality.py` | 审计最终审阅项中的阻断和警告。 |
| `validate_final_text_structure.py` | 校验最终 JSON/XML/INI/CSV/Interface 结构。 |
| `run_non_gui_qa_gates.py` | 运行最终非 GUI QA 门禁，包括 strict-complete 模式。 |
| `test_workflow_health.py` | 检查项目级工作流健康状态。 |

## final_mod、交付包与人工测试证据

| 脚本 | 用途 |
|---|---|
| `build_final_mod.py` | 组装 final_mod、中间产出、provenance 和打包输入。 |
| `clean_final_mod.py` | 清理生成的 final_mod 输出，以便重建。 |
| `recover_final_mod_overlays.py` | 在安全条件下恢复预期 final_mod overlay 文件。 |
| `validate_final_mod.py` | 校验 final_mod provenance、hash 和旁挂文件策略。 |
| `validate_chs_package.py` | 校验 `_CHS.zip` 与 final_mod 是否一致。 |
| `audit_project_completion.py` | 审计项目级完成证据。 |
| `audit_translation_goal_compliance.py` | 审计目标级合规状态。 |
| `new_manual_game_test_plan.py` | 为 ready 输出生成人工计划测试清单。 |
| `new_manual_game_test_results_template.py` | 生成人工计划测试结果模板。 |
| `validate_manual_game_test_results.py` | 校验用户提供的人工计划测试证据。 |

## 仓库维护与发布

| 脚本 | 用途 |
|---|---|
| `ci_validate_repo.py` | GitHub Actions 和本地维护使用的 repo-only 结构校验入口，不读取真实游戏或外部工具目录。 |
| `validate_claude_plugin_marketplace.py` | 维护 `.claude-plugin/marketplace.json` 和 `.claude-plugin/plugin.json` 时的 Claude Code marketplace 校验。 |
| `run_effect_regression.py` | 运行或更新项目内效果回归 fixture 快照；`--ci` 模式只比对，不改写 expected。 |
| `install_codex_plugin.py` | 从当前源树安装或刷新本地 Codex 插件。 |
| `package_project_release.py` | 将已跟踪源文件打包为项目源码发布 zip 和 manifest。 |
| `dotnet_adapter_cache.py` | adapter 构建和缓存 manifest 的共享辅助模块。 |
