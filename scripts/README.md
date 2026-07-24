# scripts 入口索引

本目录保存 Skyrim Mod CHS Translation 插件源仓库的 Python 入口脚本。初始化后的工作区不会复制本目录；工作流从插件源仓库运行这些脚本，并通过 `.skyrim-chs-workspace.json`、显式参数或当前工作目录定位目标工作区。

普通用户和顶层 Agent 的唯一公开控制入口是：

```powershell
python scripts\smt.py run <Mod路径> --game skyrim-se
```

除 `smt.py` 外，本索引中的脚本全部是**内部实现/诊断**接口，供公开 CLI 编排、workflow state/tasks、受控 adapter 和维护测试调用。普通用户和顶层 Agent 不应自行组合这些入口；workflow task 也不得指向外层 `smt.py` controller。

用户运行环境仅支持 Windows，命令从 PowerShell 执行。工作流、QA、工具适配器和 final_mod 组装都只使用 Python 入口；不要新增 Bash、WSL、Linux 命令或 shell 包装脚本。

仓库根目录提供 `pyproject.toml`，可以用 uv 运行脚本：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

uv 是可选入口；所有脚本仍支持 `python scripts\...` 直接运行。自动工具准备检测到 uv 时，会优先用 `uv venv` 和 `uv pip install --link-mode copy` 根据仓库提交的 hash-pinned runtime 导出创建机器共享不可变 Python 条目，失败时回退到标准 `venv + pip --require-hashes`；多个工作区只保存绑定并复用同一代条目。

## 内部实现/诊断入口索引

| 任务 | 脚本 | 说明 |
|---|---|---|
| 唯一公开入口 | `smt.py` | 五个公开子命令和 JSON 结果投影；不加入 workflow policy 授权面。 |
| 创建工作区 | `init_workspace.py` | 主初始化入口。目标必须是插件仓库外的新路径或空目录。 |
| 旧初始化兼容入口 | `init_project.py` | 兼容包装，实际转到 `init_workspace.py`。 |
| 准备受控非 GUI 工具 | `setup_workspace_tools.py` | 处理共享托管工具的 auto/manual/skip 准备与工作区绑定。 |
| 准备队列中的 Mod 输入 | `run_translation_queue.py` | 批量准备工作区 `mod/` 下的压缩包或目录。 |
| 运行可重复非 GUI 主流程 | `run_non_gui_translation_workflow.py` | 单个 Mod 的主工作流入口。 |
| 恢复一个安全步骤 | `resume_workflow.py` | 依据 workflow policy/state 执行低风险恢复，并记录尝试。 |
| 运行严格完成门禁 | `run_non_gui_qa_gates.py` | 指定 Mod 的最终严格 QA 门禁。 |
| 刷新工作流状态 | `write_workflow_state.py` | 写入 `qa/workflow_state.*`、进度卡、时间线和阻断报告。 |
| 刷新就绪状态 | `audit_translation_readiness.py` | 汇总输入、输出、阻断项和建议下一步。 |
| 检查整体工作流健康 | `test_workflow_health.py` | 生成项目级健康报告，可选包装严格门禁。 |
| 运行效果回归 fixture | `run_effect_regression.py` | 运行 `samples/effect_regression/` 下的项目内回归快照，不调用真实游戏、GUI 或外部 API。 |
| 验证全部 Skill 效果 | `test_skill_effects.py` | 覆盖受跟踪 runtime/meta Skill 的触发锚点、混淆目标、agent 可见性、正文入口、CLI probe 和 GUI blocked 证据。 |
| 构建 final_mod 和交付包 | `build_final_mod.py` | 按规模证据组装完整副本或翻译覆盖包。 |
| 聚合 L5 子项目 | `aggregate_translation_projects.py` | 校验普通文本子项目的依赖、冲突、coverage 和 provenance 后发布聚合覆盖包；二进制 lineage 当前阻断。 |

## 游戏能力与 Adapter

| 模块 | 用途 |
|---|---|
| `game_context.py` | 加载必含 `game_id` 的工作区 marker 和 schema v2 Game Profile。 |
| `capability_resolver.py` | 按资源能力和操作级别判断 inventory/read/write/strict-complete。 |
| `adapter_registry.py` | 把 Profile 中的 adapter id 映射到仓库内受控入口。 |
| `used_capabilities.py` | 从扫描、AdapterResult 和 provenance 汇总本次交付实际使用的能力。 |

核心流程不得新增 `game_id == "..."` 这类游戏分支。新增游戏应先声明格式族、资源能力和 adapter，再由 resolver 路由；工具报告可记录本次调用证据，但不能把旧 Profile 字段重新引入执行判断。

## 工作区与工具准备

| 脚本 | 用途 |
|---|---|
| `init_workspace.py` | 创建工作区骨架、本地配置、glossary 种子、QA 状态和进度文件。 |
| `init_project.py` | 旧命令兼容包装。 |
| `setup_workspace_tools.py` | 发布或复用机器共享非 GUI 工具、写工作区绑定与检查报告；manual/skip 不发布。 |
| `init_opencode.py` | 为工作区写入 opencode 配置和本地插件、刷新 handoff/context，并可一键启动 opencode。 |
| `validate_tools_config.py` | 校验 `config/tools.local.json` 的路径和工具配置。 |
| `validate_agent_capabilities.py` | 校验 Codex、opencode 和 Claude Code adapter 能力边界。 |
| `validate_claude_plugin_marketplace.py` | 校验 Claude Code marketplace 元数据和非 GUI Skill 暴露边界。 |
| `detect_decoder_tools.py` | 检测可用 decoder、CLI 和库工具。 |
| `check_project_dotnet_sdk.py` | 在 lease 下检查用户外部或共享绑定的 .NET SDK。 |
| `verify_python_runtime_lock.py` | 验证 hash-pinned runtime requirements 导出仍与 `uv.lock` 一致。 |
| `managed_tool_store.py` | 共享 payload/control 根、不可变条目、manifest、binding、catalog 与锁基础。 |
| `managed_tool_provisioning.py` | 确定性发布/复用 Python、.NET SDK、decoder 和 adapter。 |
| `managed_tool_resolver.py` | 按字段区分外部配置、受控 wrapper、legacy 与 leased managed binding。 |
| `managed_tool_migration.py` | 对可证明的旧工作区工具执行只复制迁移。 |
| `manage_managed_tool_cache.py` | 显式缓存维护内部入口；只接受 inspect/plan/apply，不属于翻译 CLI。 |
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
| `refresh_project_handoff_reports.py` | 刷新项目级状态与接手报告；默认不运行严格门禁，发布前显式传 `--run-strict-gate`。 |
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
| `audit_mod_scale.py` | 在 materialization 前按 Game Profile 估算 L0-L5 规模和 R0-R4 风险，写入评估报告。 |
| `mod_scale_policy.py` | 将规模评估解析为有绝对上限、磁盘预检和超时的实际执行策略。 |
| `mod_materialization.py` | 对目录/ZIP/7Z 执行有界、可恢复、可选择的 materialization，并写 shard checkpoint。 |
| `prepare_mod_workspace.py` | 评估规模并按执行策略解包或暂存沙盒 Mod 输入。 |
| `benchmark_mod_scale.py` | 运行不解包真实 Mod 的规模分类与元数据基准。 |
| `detect_mod_files.py` | 扫描 Mod 文件并汇总候选类型。 |
| `route_translation_task.py` | 将单个项目内文件路由到正确 Skill 和工具路径。 |
| `new_translation_task.py` | 生成聚焦的翻译任务包。 |
| `project_paths.py` | 共享路径解析和工作区边界辅助模块。 |

## 术语与翻译审阅包

| 脚本 | 用途 |
|---|---|
| `build_external_glossary_matches.py` | 生成指定 Mod 的外部术语命中报告。 |
| `build_lextranslator_dictionary_rag_index.py` | 按当前 Game Profile 构建或刷新 Markdown/TXT/SST/EET 词典索引。 |
| `new_model_review_packet.py` | 根据当前游戏和去重候选生成 Mod 摘要模板及中间译文模型校对包。 |
| `translation_context.py` | 生成 Game Profile 绑定的 Mod 摘要证据、上下文安全的校对组和冲突/高风险摘要。 |
| `workflow_issues.py` | 生成稳定 issue_id，并在 readiness、workflow state 和 health 之间投影、聚合问题。 |
| `update_model_review_contract.py` | 刷新模型校对报告中的摘要、final text/binary packet 哈希合同；证据变化时撤销旧 PASS。 |
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

## 归档与 BSA/BA2 覆盖

| 脚本 | 用途 |
|---|---|
| `new_bsa_archive_manifest.py` | 生成 BSA 内容审计 manifest。 |
| `new_archive_audit_manifest.py` | 从解包或审计内容生成归档审计 manifest。 |
| `audit_archive_coverage.py` | 检查归档 loose override 覆盖和豁免记录。 |
| `archive_execution_policy.py` | 为 BSA/BA2 统一解析限额、超时、磁盘预检和 scale evidence。 |
| `bethesda_archive_adapter.py` | 项目内受控 BSA 读取器和 Fallout 4 BA2 GNRL 流式读取器；DX10 只做 inventory。 |
| `invoke_bsa_file_extractor_safe.py` | 执行受限 BSA materialization；选择性模式使用项目内读取器。 |
| `invoke_ba2_extractor_safe.py` | 执行事务式 BA2 materialization；选择性 GNRL 可内置处理，完整模式可使用受控外部 adapter。 |
| `verify_ba2_extraction.py` | 独立验证 BA2 receipt、manifest、hash、路径和 limit 证据。 |

## GUI 工具兜底

| 脚本 | 用途 |
|---|---|
| `invoke_lextranslator.py` | LexTranslator 适配器入口。 |
| `invoke_lextranslator_gui.py` | LexTranslator GUI 包装入口。 |
| `automate-lextranslator-gui.py` | LexTranslator pywinauto/UI Automation 实现，由安全包装入口调用。 |
| `invoke_xtranslator.py` | xTranslator 适配器入口。 |
| `convert_xtranslator_xml_to_lextranslator_jsonl.py` | 将 xTranslator XML 输出转换为 LexTranslator 风格 JSONL。 |

## 覆盖率、最终审阅与 QA 门禁

| 脚本 | 用途 |
|---|---|
| `extract_non_gui_candidates.py` | 从项目内内容提取非 GUI 翻译候选。 |
| `translation_candidate_shards.py` | 按规模策略把完整候选证据切成有界模型上下文分片，并保留可验证的增量状态。 |
| `audit_non_gui_coverage.py` | 审计候选在 final_mod 中的覆盖状态。 |
| `new_final_text_review_packet.py` | 生成 final_mod 文本审阅包。 |
| `new_final_binary_review_packet.py` | 生成 final_mod ESP/PEX 二进制反读审阅包。 |
| `audit_final_review_quality.py` | 审计最终审阅项中的阻断和警告。 |
| `validate_final_text_structure.py` | 校验最终 JSON/XML/INI/CSV/Interface 结构。 |
| `run_non_gui_qa_gates.py` | 运行最终非 GUI QA 门禁，包括 strict-complete 模式。 |
| `strict_qa_reuse.py` | 校验并复用内容未变化的严格 QA 机械检查证据。 |
| `test_workflow_health.py` | 检查项目级工作流健康状态。 |

## final_mod、交付包与人工测试证据

| 脚本 | 用途 |
|---|---|
| `build_final_mod.py` | 按 scale execution 组装完整副本或翻译覆盖 final_mod、中间产出、provenance 和包。 |
| `aggregate_translation_projects.py` | 聚合 QA 通过且 provenance 为 `loose_text` 的 L5 子项目覆盖层；冲突或二进制 lineage 未迁移时停止发布。 |
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
